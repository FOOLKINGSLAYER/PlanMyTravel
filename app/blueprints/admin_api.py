from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.db import get_db
from app.security import admin_required
from app.services.media import InvalidImageError, MediaStorageError, store_image

admin_api_bp = Blueprint("admin_api", __name__, url_prefix="/admin/api")


@admin_api_bp.post("/media/upload")
@admin_required()
def upload_media():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded."}), 400
    saved = []
    for upload in files:
        if not upload or not upload.filename:
            continue
        try:
            result = store_image(
                upload,
                "admin-upload",
            )
        except (InvalidImageError, MediaStorageError) as exc:
            return jsonify({"error": str(exc)}), 400
        connection = get_db()
        connection.execute(
            """
            INSERT INTO media_assets
            (owner_user_id, folder, relative_path, path, url, filename,
             original_filename, mime_type, size_bytes, file_size, width, height,
             alt_text, metadata_json, storage_provider, cloudinary_public_id,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                None,
                "admin-upload",
                f"uploads/{result['relative_path']}",
                f"uploads/{result['relative_path']}",
                result["url"],
                result["relative_path"].split("/")[-1],
                result["original_filename"],
                result["mime_type"],
                result["size_bytes"],
                result["size_bytes"],
                result["width"],
                result["height"],
                result["original_filename"],
                result["storage_provider"],
                result["cloudinary_public_id"],
            ),
        )
        connection.commit()
        saved.append({
            "relative_path": result["relative_path"],
            "url": result["url"],
            "filename": result["original_filename"],
        })
    return jsonify({"files": saved}), 200
