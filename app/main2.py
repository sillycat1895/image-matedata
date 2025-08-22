from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
import base64
from io import BytesIO

from PIL import Image, PngImagePlugin, ExifTags
import piexif
import datetime
import xml.etree.ElementTree as ET

app = FastAPI(title="Image Metadata Service", version="1.0.0")


class ReadRequest(BaseModel):
    image_base64: str


class ReadResponse(BaseModel):
    format: str
    width: int
    height: int
    exif: Optional[Dict[str, Any]] = None
    png_text: Optional[Dict[str, str]] = None
    xmp: Optional[Dict[str, Any]] = None


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


def _extract_xmp_bytes_from_jpeg(jpeg_bytes: bytes) -> Optional[bytes]:
    # Find APP1 segment with XMP header
    data = jpeg_bytes
    if not (len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8):
        return None
    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA or marker == 0xD9:
            break
        if 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > len(data):
            break
        seg_length = int.from_bytes(data[i + 2:i + 4], "big")
        seg_end = i + 2 + seg_length
        if marker == 0xE1 and data[i + 4:i + 4 + len(xmp_header)] == xmp_header:
            return data[i + 4 + len(xmp_header):seg_end]
        i = seg_end
    return None


def _extract_xmp_text_from_png(image: Image.Image) -> Optional[str]:
    if (image.format or "").upper() != "PNG":
        return None
    # Standard key used when writing XMP as iTXt
    xmp_txt = image.info.get("XML:com.adobe.xmp")
    return xmp_txt


def _parse_xmp_to_dict(xmp_xml: bytes | str) -> Dict[str, Any]:
    try:
        xml_str = xmp_xml.decode("utf-8") if isinstance(xmp_xml, (bytes, bytearray)) else str(xmp_xml)
        # Basic cleanup
        xml_str = xml_str.strip()
        # Parse
        ns = {
            "x": "adobe:ns:meta/",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "dc": "http://purl.org/dc/elements/1.1/",
            "xmp": "http://ns.adobe.com/xap/1.0/",
            "ims": "https://example.com/image-metadata-service/1.0/",
        }
        root = ET.fromstring(xml_str)
        result: Dict[str, Any] = {}

        # Helper to get local tag name without namespace
        def local(tag: str) -> str:
            if "}" in tag:
                return tag.split("}", 1)[1]
            if ":" in tag:
                return tag.split(":", 1)[1]
            return tag

        # Extract known properties
        # dc:description as x-default
        for li in root.findall(".//dc:description/rdf:Alt/rdf:li", ns):
            lang = li.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", "")
            if lang == "x-default" or "description" not in result:
                result["description"] = (li.text or "").strip()

        # dc:creator first item
        first_creator = root.find(".//dc:creator/rdf:Seq/rdf:li", ns)
        if first_creator is not None and (first_creator.text or "").strip():
            result["artist"] = first_creator.text.strip()

        # xmp:CreatorTool, xmp:ModifyDate, dc:rights/x-default, xmp:Label
        node = root.find(".//xmp:CreatorTool", ns)
        if node is not None and (node.text or "").strip():
            result["software"] = node.text.strip()
        node = root.find(".//xmp:ModifyDate", ns)
        if node is not None and (node.text or "").strip():
            result["datetime"] = node.text.strip()
        rights = root.find(".//dc:rights/rdf:Alt/rdf:li", ns)
        if rights is not None and (rights.text or "").strip():
            result["copyright"] = rights.text.strip()
        label = root.find(".//xmp:Label", ns)
        if label is not None and (label.text or "").strip():
            result["user_comment"] = label.text.strip()

        # Extract all custom ims:* elements under rdf:Description (flat, leaf values)
        for desc in root.findall(".//rdf:Description", ns):
            for child in list(desc):
                tag = child.tag
                # Skip known namespaces we already handled
                if any(tag.startswith("{" + url + "}") for url in [ns["dc"], ns["xmp"], ns["rdf"]]):
                    continue
                key = local(tag)
                text_value = (child.text or "").strip()
                if text_value:
                    result[key] = text_value

        return result
    except Exception:
        return {}


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


# ---- XMP helpers ----

def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_xmp_packet(set_map: Dict[str, Any]) -> bytes:
    # Map common fields to standard XMP properties; others go to a custom namespace
    description = set_map.get("description")
    artist = set_map.get("artist")
    software = set_map.get("software")
    copyright_txt = set_map.get("copyright")
    user_comment = set_map.get("user_comment")
    dt = set_map.get("datetime")

    # XMP requires ISO 8601; attempt a best-effort normalization
    iso_dt: Optional[str] = None
    if dt:
        try:
            # Try common formats; fall back to now if parsing fails
            iso_dt = datetime.datetime.fromisoformat(str(dt)).isoformat()
        except Exception:
            try:
                iso_dt = datetime.datetime.strptime(str(dt), "%Y:%m:%d %H:%M:%S").isoformat()
            except Exception:
                iso_dt = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    else:
        iso_dt = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # Collect custom keys not mapped above
    mapped_keys = {"description", "artist", "software", "copyright", "user_comment", "datetime"}
    custom_items = {k: v for k, v in set_map.items() if k not in mapped_keys and v is not None}

    # Build minimal XMP packet
    # Note: For multi-valued properties like dc:creator we use Bag with one item
    parts = []
    parts.append('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>')
    parts.append(
        "<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
        "  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'\n"
        "           xmlns:dc='http://purl.org/dc/elements/1.1/'\n"
        "           xmlns:xmp='http://ns.adobe.com/xap/1.0/'\n"
        "           xmlns:ims='https://example.com/image-metadata-service/1.0/'>\n"
        "    <rdf:Description rdf:about=''>"
    )

    if description:
        parts.append(
            "      <dc:description>\n        <rdf:Alt>\n          <rdf:li xml:lang='x-default'>" + _xml_escape(description) + "</rdf:li>\n        </rdf:Alt>\n      </dc:description>"
        )
    if artist:
        parts.append(
            "      <dc:creator>\n        <rdf:Seq>\n          <rdf:li>" + _xml_escape(artist) + "</rdf:li>\n        </rdf:Seq>\n      </dc:creator>"
        )
    if software:
        parts.append("      <xmp:CreatorTool>" + _xml_escape(software) + "</xmp:CreatorTool>")
    if copyright_txt:
        parts.append("      <dc:rights>\n        <rdf:Alt>\n          <rdf:li xml:lang='x-default'>" + _xml_escape(copyright_txt) + "</rdf:li>\n        </rdf:Alt>\n      </dc:rights>")
    if user_comment:
        parts.append("      <xmp:Label>" + _xml_escape(user_comment) + "</xmp:Label>")
    if iso_dt:
        parts.append("      <xmp:ModifyDate>" + _xml_escape(iso_dt) + "</xmp:ModifyDate>")

    # Custom items under ims:*
    for k, v in custom_items.items():
        parts.append(f"      <ims:{_xml_escape(k)}>" + _xml_escape(v) + f"</ims:{_xml_escape(k)}>")

    parts.append("    </rdf:Description>\n  </rdf:RDF>\n</x:xmpmeta>")
    parts.append("<?xpacket end='w'?>")

    xml = "\n".join(parts).encode("utf-8")
    return xml


def _inject_xmp_into_jpeg(original_bytes: bytes, xmp_packet: bytes) -> bytes:
    # JPEG markers: SOI (FFD8), APP1 (FFE1), SOS (FFDA)
    data = original_bytes
    if not (len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8):
        raise HTTPException(status_code=400, detail="Invalid JPEG data for XMP injection")

    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    xmp_app1_data = xmp_header + xmp_packet
    xmp_app1_length = len(xmp_app1_data) + 2  # includes the length field itself
    if xmp_app1_length > 0xFFFF:
        raise HTTPException(status_code=400, detail="XMP packet too large for APP1 segment")
    xmp_segment = b"\xFF\xE1" + xmp_app1_length.to_bytes(2, "big") + xmp_app1_data

    # Walk segments; replace existing XMP APP1 if present; otherwise insert after SOI (and after any APP0/APP1 without XMP)
    i = 2  # after SOI
    insert_pos = 2
    out_parts = [data[:2]]
    replaced = False

    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xD9:  # EOI
            break
        if marker == 0xDA:  # SOS - start of scan; image data follows
            insert_pos = i
            break
        if 0xD0 <= marker <= 0xD7:  # RSTn have no length
            out_parts.append(data[i:i + 2])
            i += 2
            continue
        # regular segment with 2-byte length
        if i + 4 > len(data):
            break
        seg_length = int.from_bytes(data[i + 2:i + 4], "big")
        seg_end = i + 2 + seg_length
        segment = data[i:seg_end]

        # Track insertion after initial APP0/APP1/JFIF segments
        if marker in (0xE0, 0xE1):
            insert_pos = seg_end

        # Check if this APP1 is XMP
        if marker == 0xE1 and segment[4:4 + len(xmp_header)] == xmp_header:
            # Replace with our XMP
            out_parts.append(xmp_segment)
            replaced = True
        else:
            out_parts.append(segment)
        i = seg_end

    if not replaced:
        # Insert at computed position
        out = data[:insert_pos] + xmp_segment + data[insert_pos:]
        return out

    # If replaced, append the rest
    out = b"".join(out_parts) + data[i:]
    return out


def _apply_xmp_png(image: Image.Image, set_map: Dict[str, Any]) -> bytes:
    if (image.format or "").upper() != "PNG":
        raise HTTPException(status_code=400, detail="XMP writing for PNG requires PNG format")
    xmp = _build_xmp_packet(set_map).decode("utf-8")
    pnginfo = PngImagePlugin.PngInfo()
    # Preserve existing text chunks
    for k, v in image.info.items():
        if isinstance(v, str):
            try:
                pnginfo.add_text(str(k), v)
            except Exception:
                pass
    # Add XMP as iTXt chunk with the standard key
    try:
        pnginfo.add_itxt("XML:com.adobe.xmp", xmp)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to embed XMP in PNG: {exc}")

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

    xmp_data: Optional[Dict[str, Any]] = None
    try:
        if fmt in {"JPEG", "JPG"}:
            cleaned = _strip_data_url_prefix(req.image_base64).strip()
            try:
                raw = base64.b64decode(cleaned, validate=True)
            except Exception:
                raw = base64.b64decode("".join(cleaned.split()), validate=False)
            xmp_bytes = _extract_xmp_bytes_from_jpeg(raw)
            if xmp_bytes:
                parsed = _parse_xmp_to_dict(xmp_bytes)
                if parsed:
                    xmp_data = parsed
        elif fmt == "PNG":
            xmp_txt = _extract_xmp_text_from_png(image)
            if xmp_txt:
                parsed = _parse_xmp_to_dict(xmp_txt)
                if parsed:
                    xmp_data = parsed
    except Exception:
        xmp_data = None

    return ReadResponse(
        format=fmt,
        width=width,
        height=height,
        exif=exif or None,
        png_text=png_text or None,
        xmp=xmp_data or None,
    )


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

    if fmt in {"JPEG", "JPG"}:
        try:
            xmp_packet = _build_xmp_packet(req.set)
            updated_bytes = _inject_xmp_into_jpeg(img_bytes, xmp_packet)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write XMP to JPEG: {exc}")
        updated_fields = dict(req.set)
    elif fmt == "PNG":
        with Image.open(BytesIO(img_bytes)) as img_png:
            updated_bytes = _apply_xmp_png(img_png, req.set)
        updated_fields = dict(req.set)
    elif fmt == "TIFF":
        # Fallback to EXIF for TIFF; XMP in TIFF requires a full TIFF writer
        updated_bytes = _apply_exif_updates(img_bytes, req.set, fmt)
        updated_fields = dict(req.set)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format for writing: {fmt}")

    b64_out = base64.b64encode(updated_bytes).decode("ascii")
    return SetResponse(image_base64=b64_out, format=fmt, updated=updated_fields)


@app.get("/")
def root():
    return {"service": "image-metadata", "version": "1.0.0", "endpoints": ["/metadata/read", "/metadata/set"]}