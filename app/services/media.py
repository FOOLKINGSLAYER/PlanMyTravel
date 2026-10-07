from __future__ import annotations

import io
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from flask import current_app, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.db_utils import query_one


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_SIDE = 2400
ALLOWED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class InvalidImageError(ValueError):
    pass


class MediaStorageError(RuntimeError):
    pass


def resolve_image(path: str | None) -> str:
    if path:
        parsed_url = urlsplit(path)
        if parsed_url.scheme == "https" and parsed_url.netloc:
            return path

    cloudinary_url = cloudinary_image_url(path)
    if cloudinary_url:
        return cloudinary_url

    placeholder = PurePosixPath("placeholders/default.jpg")
    raw_path = unquote(urlsplit(path or "").path)
    is_upload = raw_path.startswith("/uploads/") or raw_path.startswith("uploads/")
    if raw_path.startswith("/images/"):
        raw_path = raw_path[len("/images/"):]
    elif raw_path.startswith("/uploads/"):
        raw_path = raw_path[len("/uploads/"):]
    elif raw_path.startswith("uploads/"):
        raw_path = raw_path[len("uploads/"):]
    candidate = _safe_relative_path(raw_path or placeholder.as_posix())
    root = Path(
        current_app.config["UPLOADS_DIR"] if is_upload else current_app.config["IMAGES_DIR"]
    ).resolve()
    if not (root / candidate).is_file():
        candidate = placeholder
        is_upload = False
    if is_upload:
        return url_for("uploads", filename=candidate.as_posix())
    return url_for("images", filename=candidate.as_posix())


def cloudinary_image_url(path: str | None) -> str | None:
    if not path:
        return None
    raw_path = unquote(urlsplit(path or "").path)
    is_upload = raw_path.startswith("/uploads/") or raw_path.startswith("uploads/")
    if raw_path.startswith("/images/"):
        raw_path = raw_path[len("/images/"):]
    elif raw_path.startswith("/uploads/"):
        raw_path = raw_path[len("/uploads/"):]
    elif raw_path.startswith("uploads/"):
        raw_path = raw_path[len("uploads/"):]
    candidate = _safe_relative_path(raw_path)
    relative_path = (
        PurePosixPath("uploads", candidate).as_posix()
        if is_upload
        else candidate.as_posix()
    )
    asset = query_one(
        """SELECT url FROM media_assets
           WHERE relative_path = ? AND storage_provider = 'cloudinary'""",
        (relative_path,),
    )
    image_url = asset["url"] if asset else None
    return (
        image_url
        if isinstance(image_url, str) and image_url.startswith("https://")
        else None
    )


def store_image(
    upload: FileStorage, folder: str
) -> dict[str, object]:
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
    filename = f"{stem}-{uuid4().hex[:10]}{extension}"
    relative_path = PurePosixPath(safe_folder, filename)
    cloudinary_public_id = PurePosixPath(
        "planmytravel", "uploads", safe_folder, Path(filename).stem
    ).as_posix()
    cloudinary_result = upload_bytes_to_cloudinary(
        output.getvalue(), cloudinary_public_id
    )
    return {
        "relative_path": relative_path.as_posix(),
        "url": cloudinary_result["secure_url"],
        "storage_provider": "cloudinary",
        "cloudinary_public_id": cloudinary_result["public_id"],
        "original_filename": upload.filename,
        "mime_type": ALLOWED_FORMATS[detected_format][0],
        "width": width,
        "height": height,
        "size_bytes": len(output.getvalue()),
    }


def upload_bytes_to_cloudinary(
    content: bytes, public_id: str, *, overwrite: bool = False
) -> dict[str, str]:
    cloud_name = str(current_app.config.get("CLOUDINARY_CLOUD_NAME", "")).strip()
    api_key = str(current_app.config.get("CLOUDINARY_API_KEY", "")).strip()
    api_secret = str(current_app.config.get("CLOUDINARY_API_SECRET", "")).strip()
    if not all((cloud_name, api_key, api_secret)):
        raise MediaStorageError(
            "Cloudinary storage is not configured. Add CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET to the server environment."
        )

    import cloudinary
    from cloudinary import uploader
    from cloudinary.exceptions import Error as CloudinaryError

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    try:
        result = uploader.upload(
            io.BytesIO(content),
            public_id=public_id,
            overwrite=overwrite,
            unique_filename=False,
            resource_type="image",
        )
    except CloudinaryError as exc:
        current_app.logger.exception("Cloudinary image upload failed.")
        raise MediaStorageError(
            "Cloudinary could not store the image. Check the Cloudinary account and retry."
        ) from exc

    secure_url = result.get("secure_url") if isinstance(result, dict) else None
    actual_public_id = result.get("public_id") if isinstance(result, dict) else None
    if (
        not isinstance(secure_url, str)
        or not secure_url.startswith("https://")
        or not isinstance(actual_public_id, str)
        or not actual_public_id
    ):
        raise MediaStorageError("Cloudinary returned an incomplete image upload result.")
    return {"secure_url": secure_url, "public_id": actual_public_id}


def migrated_public_id(relative_path: str, source: str) -> str:
    safe_path = _safe_relative_path(relative_path)
    safe_source = _safe_relative_path(source)
    components = [
        re.sub(r"[^A-Za-z0-9_-]+", "-", part).strip("-_") or "image"
        for part in (*safe_source.parts, *safe_path.with_suffix("").parts)
    ]
    return PurePosixPath("planmytravel", "migrated", *components).as_posix()


def delete_cloudinary_image(public_id: str) -> None:
    cloud_name = str(current_app.config.get("CLOUDINARY_CLOUD_NAME", "")).strip()
    api_key = str(current_app.config.get("CLOUDINARY_API_KEY", "")).strip()
    api_secret = str(current_app.config.get("CLOUDINARY_API_SECRET", "")).strip()
    if not all((cloud_name, api_key, api_secret)):
        raise MediaStorageError("Cloudinary storage is not configured.")

    import cloudinary
    from cloudinary import uploader
    from cloudinary.exceptions import Error as CloudinaryError

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    try:
        result = uploader.destroy(public_id, resource_type="image", invalidate=True)
    except CloudinaryError as exc:
        current_app.logger.exception("Cloudinary image deletion failed.")
        raise MediaStorageError(
            "Cloudinary could not delete the image. The media record was not removed."
        ) from exc
    if not isinstance(result, dict) or result.get("result") not in {"ok", "not found"}:
        raise MediaStorageError(
            "Cloudinary did not confirm image deletion. The media record was not removed."
        )


def _safe_relative_path(path: str) -> PurePosixPath:
    normalized = path.replace("\\", "/").strip("/")
    parsed = PurePosixPath(normalized)
    if not normalized or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise InvalidImageError("Invalid image path.")
    return parsed
