## Image Metadata Service (main2)

A FastAPI service to read and set image metadata using XMP for custom keys. Images are provided as base64 strings (optionally data: URLs).

### Features
- **XMP read/write for JPEG/PNG**: embeds/parses XMP, supports arbitrary custom keys (e.g., `AIGC`).
- **EXIF read**: extracts available EXIF for JPEG/TIFF.
- **PNG text read**: reads existing PNG text chunks.
- **TIFF write fallback**: writes EXIF for TIFF (XMP not embedded in TIFF in this version).

### Setup

```bash
# (First-time) install venv support if missing
sudo apt-get update && sudo apt-get install -y python3.13-venv

# Create and activate virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Run (main2)

```bash
python -m uvicorn app.main2:app --host 0.0.0.0 --port 8000
```

Open docs at `http://localhost:8000/docs`.

### API

- **POST** `/metadata/read`
  - **body**:
    ```json
    { "image_base64": "<base64 or data URL>" }
    ```
  - **response** (fields returned when available):
    ```json
    {
      "format": "JPEG|PNG|TIFF|...",
      "width": 1000,
      "height": 800,
      "exif": { "Artist": "...", "UserComment": "..." },
      "png_text": { "Description": "..." },
      "xmp": { "description": "...", "AIGC": "true", "software": "..." }
    }
    ```

- **POST** `/metadata/set`
  - **JPEG/PNG**: writes XMP, allowing arbitrary custom keys, e.g. `AIGC`.
  - **TIFF**: writes EXIF as a fallback (same input keys accepted; stored in EXIF where applicable).
  - **body**:
    ```json
    {
      "image_base64": "<base64 or data URL>",
      "set": { "AIGC": "true", "description": "example", "software": "IMS" }
    }
    ```
  - **response**:
    ```json
    {
      "image_base64": "<updated image base64>",
      "format": "JPEG|PNG|TIFF",
      "updated": { "AIGC": "true", "description": "example", "software": "IMS" }
    }
    ```

### Notes
- **Custom keys**: Any key in `set` is embedded into XMP for JPEG/PNG and returned under `xmp` when reading.
- **DateTime**: When provided, attempts to normalize to ISO-8601 (`xmp:ModifyDate`).
- **Re-encoding**: Writing metadata re-encodes the image. PNG updates preserve existing text entries where possible.
- **Limitations**: TIFF XMP embedding is not implemented in this version.
