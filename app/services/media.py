from __future__ import annotations

import io
import re
from pathlib import Path, PurePosixPath
from uuid import uuid4

from flask import current_app, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_SIDE = 2400
ALLOWED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class InvalidImageError(ValueError):
    pass


def resolve_image(path: str | None) -> str:
    placeholder = "placeholders/default.jpg"
    candidate = _safe_relative_path(path or placeholder)
    root = Path(current_app.config["IMAGES_DIR"]).resolve()
    if not (root / candidate).is_file():
        candidate = placeholder
    return url_for("images", filename=candidate.as_posix())


def store_image(upload: FileStorage, folder: str) -> dict[str, object]:
    if not upload or not upload.filename:
        raise InvalidImageError("Choose an image to upload.")
    safe_folder = _safe_relative_path(folder)
    if upload.content_length and upload.content_length > MAX_UPLOAD_BYTES:
        raise InvalidImageError("Each image must be 8 MB or smaller.")

    raw = upload.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise InvalidImageError("Each image must be 8 MB or smaller.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            detected_format = image.format
            if detected_format not in ALLOWED_FORMATS:
                raise InvalidImageError("Use a JPEG, PNG, or WebP image.")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
            if detected_format == "JPEG":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format=detected_format, optimize=True)
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImageError("The uploaded file is not a valid image.") from exc

    extension = ALLOWED_FORMATS[detected_format][1]
    original = secure_filename(upload.filename)
    stem = Path(original).stem or "image"
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:50] or "image"
    relative_path = PurePosixPath(safe_folder, f"{stem}-{uuid4().hex[:10]}{extension}")
    destination = (Path(current_app.config["IMAGES_DIR"]) / relative_path).resolve()
    root = Path(current_app.config["IMAGES_DIR"]).resolve()
    if root not in destination.parents:
        raise InvalidImageError("Invalid media folder.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output.getvalue())
    return {
        "relative_path": relative_path.as_posix(),
        "original_filename": upload.filename,
        "mime_type": ALLOWED_FORMATS[detected_format][0],
        "width": width,
        "height": height,
        "size_bytes": destination.stat().st_size,
    }


def _safe_relative_path(path: str) -> PurePosixPath:
    normalized = path.replace("\\", "/").strip("/")
    parsed = PurePosixPath(normalized)
    if not normalized or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise InvalidImageError("Invalid image path.")
    return parsed
