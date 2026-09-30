"""Register files under images/ in the media_assets table."""

from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path
import sys
from urllib.parse import quote

from flask import Flask, current_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.db import get_db, init_db


SUPPORTED_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


def _image_dir() -> Path:
    configured_path = Path(current_app.config.get("IMAGES_DIR", PROJECT_ROOT / "images"))
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return configured_path


def sync_images() -> int:
    """Upsert supported project images as media assets and return the file count."""
    image_dir = _image_dir()
    if not image_dir.is_dir():
        return 0

    connection = get_db()
    images = sorted(
        path for path in image_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    for image_path in images:
        relative_path = image_path.relative_to(image_dir).as_posix()
        mime_type, _encoding = mimetypes.guess_type(image_path.name)
        connection.execute(
            """
            INSERT INTO media_assets
                (relative_path, path, url, filename, original_filename, mime_type,
                 size_bytes, file_size, folder)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                url = excluded.url,
                path = excluded.path,
                filename = excluded.filename,
                original_filename = excluded.original_filename,
                mime_type = excluded.mime_type,
                size_bytes = excluded.size_bytes,
                file_size = excluded.file_size,
                folder = excluded.folder,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                relative_path,
                relative_path,
                f"/images/{quote(relative_path)}",
                image_path.name,
                image_path.name,
                mime_type,
                image_path.stat().st_size,
                image_path.stat().st_size,
                image_path.parent.relative_to(image_dir).as_posix()
                if image_path.parent != image_dir
                else "",
            ),
        )
    connection.commit()
    return len(images)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="SQLite database path.")
    parser.add_argument("--database-url", help="SQLite or Turso database URL.")
    parser.add_argument("--auth-token", help="Turso auth token.")
    args = parser.parse_args()

    app = Flask(__name__)
    app.config.update(load_config())
    if args.database:
        app.config["DATABASE_PATH"] = args.database
        app.config["DATABASE_URL"] = ""
    if args.database_url:
        app.config["DATABASE_URL"] = args.database_url
    if args.auth_token:
        app.config["TURSO_AUTH_TOKEN"] = args.auth_token

    with app.app_context():
        init_db()
        count = sync_images()
        image_dir = _image_dir()
    print(f"Registered {count} image(s) from {image_dir}.")


if __name__ == "__main__":
    main()
