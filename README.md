# Image Metadata Service

A FastAPI service to read and set image metadata. Images are provided as base64 strings (optionally data: URLs).

## Features
- Read EXIF for JPEG/TIFF/WebP when available
- Read PNG text chunks
- Set EXIF fields for JPEG/TIFF (description, artist, copyright, software, datetime, user_comment)
- Set PNG text key/value pairs

## Setup

```bash
# (First-time) install venv support if missing
sudo apt-get update && sudo apt-get install -y python3.13-venv

# Create and activate virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Run

```bash
python3 app/main.py
```

Open docs at `http://localhost:8000/docs`.

## API

- POST `/metadata/read`
  - body:
    ```json
    { "image_base64": "<base64 or data URL>" }
    ```
  - response:
    ```json
    {
      "format": "JPEG|PNG|TIFF|...",
      "width": 1000,
      "height": 800,
      "exif": {"Artist": "...", "UserComment": "..."},
      "png_text": {"Description": "..."}
    }
    ```

- POST `/metadata/set`
  - JPEG/TIFF EXIF fields supported: `description, artist, copyright, software, datetime, user_comment`
  - PNG supports arbitrary text key/values
  - body:
    ```json
    {
      "image_base64": "<base64 or data URL>",
      "set": {"description": "example"}
    }
    ```
  - response:
    ```json
    {
      "image_base64": "<updated image base64>",
      "format": "JPEG",
      "updated": {"description": "example"}
    }
    ```

## Notes
- WebP EXIF reading may be limited depending on Pillow version.
- When writing EXIF, the image is re-encoded. PNG text updates preserve existing text entries when possible.
