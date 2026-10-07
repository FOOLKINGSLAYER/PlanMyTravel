"""Upload local site media to Cloudinary and update media-library records."""

from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path
import sys

from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.db import get_db, init_db
from app.db_utils import query_one
from app.services.media import migrated_public_id, upload_bytes_to_cloudinary


SUPPORTED_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


def _media_files(app: Flask) -> list[tuple[Path, str, str]]:
    files = []
    for source_name, config_key, prefix in (
        ("images", "IMAGES_DIR", ""),
        ("uploads", "UPLOADS_DIR", "uploads/"),
    ):
        root = Path(app.config[config_key]).resolve()
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                local_path = path.relative_to(root).as_posix()
                logical_path = f"{prefix}{local_path}"
                files.append((path, logical_path, source_name))
    return files


def migrate_media(app: Flask, *, dry_run: bool = False) -> tuple[int, int]:
    files = _media_files(app)
    if dry_run:
        return len(files), 0

    migrated = 0
    with app.app_context():
        init_db()
        connection = get_db()
        for path, relative_path, source in files:
            existing = query_one(
                """SELECT storage_provider, cloudinary_public_id, url
                   FROM media_assets WHERE relative_path = ?""",
                (relative_path,),
            )
            if (
                existing
                and existing["storage_provider"] == "cloudinary"
                and existing["cloudinary_public_id"]
                and str(existing["url"] or "").startswith(
                    "https://res.cloudinary.com/"
                )
            ):
                continue

            cloudinary_public_id = migrated_public_id(relative_path, source)
            result = upload_bytes_to_cloudinary(
                path.read_bytes(), cloudinary_public_id, overwrite=True
            )
            mime_type, _encoding = mimetypes.guess_type(path.name)
            size = path.stat().st_size
            storage_path = relative_path.removeprefix("uploads/")
            folder = str(Path(storage_path).parent)
            if folder == ".":
                folder = ""
            connection.execute(
                """INSERT INTO media_assets
                   (folder, relative_path, path, url, filename, original_filename,
                    mime_type, size_bytes, file_size, alt_text, storage_provider,
                    cloudinary_public_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cloudinary', ?)
                   ON CONFLICT(relative_path) DO UPDATE SET
                     path = excluded.path,
                     url = excluded.url,
                     filename = excluded.filename,
                     original_filename = excluded.original_filename,
                     mime_type = excluded.mime_type,
                     size_bytes = excluded.size_bytes,
                     file_size = excluded.file_size,
                     storage_provider = 'cloudinary',
                     cloudinary_public_id = excluded.cloudinary_public_id,
                     updated_at = CURRENT_TIMESTAMP""",
                (
                    folder,
                    relative_path,
                    relative_path,
                    result["secure_url"],
                    path.name,
                    path.name,
                    mime_type,
                    size,
                    size,
                    path.name,
                    result["public_id"],
                ),
            )
            connection.commit()
            migrated += 1
    return len(files), migrated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload files and update the media library. Local originals are kept.",
    )
    args = parser.parse_args()

    app = Flask(__name__)
    app.config.update(load_config())
    total, migrated = migrate_media(app, dry_run=not args.apply)
    if args.apply:
        print(f"Cloudinary migration complete: {migrated} uploaded, {total - migrated} already migrated.")
    else:
        print(f"Cloudinary migration preview: {total} local media file(s) found. Run with --apply to upload.")


if __name__ == "__main__":
    main()
