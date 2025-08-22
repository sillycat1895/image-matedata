from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
import base64
from io import BytesIO

from PIL import Image, PngImagePlugin, ExifTags
import piexif
import uvicorn

app = FastAPI(title="Image Metadata Service", version="1.0.0")


class ReadRequest(BaseModel):
    image_base64: str


class ReadResponse(BaseModel):
    format: str
    width: int
    height: int
    exif: Optional[Dict[str, Any]] = None
    png_text: Optional[Dict[str, str]] = None


class SetRequest(BaseModel):
    image_base64: str
    set: Dict[str, Any]
    format: Optional[str] = None  # keep original if not provided


class SetResponse(BaseModel):
    image_base64: str
    format: str
    updated: Dict[str, Any]


# ---- Helpers ----

def _strip_data_url_prefix(b64_string: str) -> str:
    if b64_string.startswith("data:"):
        comma_index = b64_string.find(",")
        if comma_index != -1:
            return b64_string[comma_index + 1 :]
    return b64_string


def _decode_base64_to_image(b64_string: str) -> Image.Image:
    cleaned = _strip_data_url_prefix(b64_string).strip()
    try:
        image_bytes = base64.b64decode(cleaned, validate=True)
    except Exception:
        # Fallback: remove whitespace/newlines and try again
        compact = "".join(cleaned.split())
        try:
            image_bytes = base64.b64decode(compact, validate=False)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 image: {exc}")
    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
        return image
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to decode image: {exc}")


def _decode_user_comment(user_comment_bytes: bytes) -> str:
    try:
        # EXIF UserComment may start with an 8-byte charset prefix like b"ASCII\x00\x00\x00"
        if len(user_comment_bytes) >= 8:
            prefix = user_comment_bytes[:8]
            rest = user_comment_bytes[8:]
            if prefix.startswith(b"ASCII"):
                return rest.decode("ascii", errors="replace").rstrip("\x00")
            if prefix.startswith(b"UNICODE"):
                return rest.decode("utf-16", errors="replace").rstrip("\x00")
            if prefix.startswith(b"JIS"):
                return rest.decode("shift_jis", errors="replace").rstrip("\x00")
        # Fallback
        return user_comment_bytes.decode("utf-8", errors="replace").rstrip("\x00")
    except Exception:
        return ""


def _decode_exif_value(tag_id: int, value: Any) -> Any:
    # XP* tags are UTF-16LE byte arrays of uint8
    xp_tag_ids = {40091, 40092, 40093, 40094, 40095}
    if isinstance(value, bytes):
        if tag_id == 37510:  # UserComment
            return _decode_user_comment(value)
        if tag_id in xp_tag_ids:
            try:
                return value.decode("utf-16le", errors="replace").rstrip("\x00")
            except Exception:
                return value.decode("utf-8", errors="replace")
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    return value


def _read_exif(image: Image.Image) -> Dict[str, Any]:
    exif_data: Dict[str, Any] = {}
    try:
        exif = image.getexif()
        if not exif:
            return exif_data
        for tag_id, v in exif.items():
            tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
            exif_data[tag_name] = _decode_exif_value(tag_id, v)
    except Exception:
        pass
    return exif_data


def _read_png_text(image: Image.Image) -> Dict[str, str]:
    text_data: Dict[str, str] = {}
    if (image.format or "").upper() != "PNG":
        return text_data
    try:
        for k, v in image.info.items():
            if isinstance(v, str):
                text_data[str(k)] = v
    except Exception:
        pass
    return text_data


def _ensure_ascii_bytes(text: str) -> bytes:
    return text.encode("ascii", errors="ignore")


def _build_user_comment_bytes(text: str) -> bytes:
    return b"ASCII\x00\x00\x00" + _ensure_ascii_bytes(text)


def _apply_exif_updates(original_bytes: bytes, set_map: Dict[str, Any], fmt: str) -> bytes:
    if fmt not in {"JPEG", "JPG", "TIFF"}:
        raise HTTPException(status_code=400, detail=f"EXIF writing supported only for JPEG/TIFF, not {fmt}")

    try:
        exif_dict = piexif.load(original_bytes)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    mapping = {
        "description": ("0th", piexif.ImageIFD.ImageDescription, _ensure_ascii_bytes),
        "artist": ("0th", piexif.ImageIFD.Artist, _ensure_ascii_bytes),
        "copyright": ("0th", piexif.ImageIFD.Copyright, _ensure_ascii_bytes),
        "software": ("0th", piexif.ImageIFD.Software, _ensure_ascii_bytes),
        "datetime": ("0th", piexif.ImageIFD.DateTime, _ensure_ascii_bytes),
        "user_comment": ("Exif", piexif.ExifIFD.UserComment, _build_user_comment_bytes),
    }

    for key, value in set_map.items():
        if key not in mapping or value is None:
            continue
        ifd_name, tag_id, encoder = mapping[key]
        try:
            exif_dict[ifd_name][tag_id] = encoder(str(value))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to set EXIF field '{key}': {exc}")

    exif_bytes = piexif.dump(exif_dict)

    # Re-encode the image with updated EXIF
    try:
        with Image.open(BytesIO(original_bytes)) as img:
            out = BytesIO()
            save_fmt = "JPEG" if fmt in {"JPEG", "JPG"} else "TIFF"
            img.save(out, format=save_fmt, exif=exif_bytes)
            return out.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write EXIF: {exc}")


def _apply_png_text_updates(image: Image.Image, set_map: Dict[str, Any]) -> bytes:
    if (image.format or "").upper() != "PNG":
        raise HTTPException(status_code=400, detail="PNG text writing supported only for PNG format")

    pnginfo = PngImagePlugin.PngInfo()
    # Preserve existing text
    for k, v in image.info.items():
        if isinstance(v, str):
            try:
                pnginfo.add_text(str(k), v)
            except Exception:
                pass
    # Apply updates
    for k, v in set_map.items():
        if v is None:
            continue
        try:
            pnginfo.add_text(str(k), str(v))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to set PNG text '{k}': {exc}")

    out = BytesIO()
    image.save(out, format="PNG", pnginfo=pnginfo)
    return out.getvalue()


# ---- Endpoints ----

@app.post("/metadata/read", response_model=ReadResponse)
def read_metadata(req: ReadRequest):
    image = _decode_base64_to_image(req.image_base64)
    fmt = (image.format or "").upper() or "UNKNOWN"
    width, height = image.size
    exif = _read_exif(image)
    png_text = _read_png_text(image)
    return ReadResponse(format=fmt, width=width, height=height, exif=exif or None, png_text=png_text or None)


@app.post("/metadata/set", response_model=SetResponse)
def set_metadata(req: SetRequest):
    # Decode again to raw bytes to preserve original headers when possible
    cleaned = _strip_data_url_prefix(req.image_base64).strip()
    try:
        img_bytes = base64.b64decode(cleaned, validate=True)
    except Exception:
        img_bytes = base64.b64decode("".join(cleaned.split()), validate=False)

    with Image.open(BytesIO(img_bytes)) as img:
        fmt = (req.format or img.format or "").upper() or "UNKNOWN"

    updated_bytes: Optional[bytes] = None
    updated_fields: Dict[str, Any] = {}

    if fmt in {"JPEG", "JPG", "TIFF"}:
        updated_bytes = _apply_exif_updates(img_bytes, req.set, fmt)
        updated_fields = {k: v for k, v in req.set.items() if k in {"description", "artist", "copyright", "software", "datetime", "user_comment"}}
    elif fmt == "PNG":
        with Image.open(BytesIO(img_bytes)) as img_png:
            updated_bytes = _apply_png_text_updates(img_png, req.set)
        updated_fields = dict(req.set)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format for writing: {fmt}")

    b64_out = base64.b64encode(updated_bytes).decode("ascii")
    return SetResponse(image_base64=b64_out, format=fmt, updated=updated_fields)


@app.get("/")
def root():
    return {"service": "image-metadata", "version": "1.0.0", "endpoints": ["/metadata/read", "/metadata/set"]}

if __name__ == "__main__":
    import os
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port)