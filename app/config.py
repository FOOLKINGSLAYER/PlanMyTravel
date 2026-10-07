from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return values

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        values[key.strip().strip('"\'')] = value.strip().strip('"\'')
    return values


def _value(file_values: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name) or file_values.get(name)
        if value and value.strip():
            return value.strip()
    return default


def load_config() -> dict[str, object]:
    file_values = _read_env_file()
    flask_env = _value(file_values, "FLASK_ENV", default="development")
    is_vercel = bool(os.environ.get("VERCEL"))
    writable_root = (
        Path(os.environ.get("TMPDIR") or "/tmp") / "planmytravel"
        if is_vercel
        else ROOT
    )
    instance_path = Path(
        _value(
            file_values,
            "INSTANCE_PATH",
            default=str(writable_root / "instance"),
        )
    )
    if not instance_path.is_absolute():
        instance_path = ROOT / instance_path
    secure_cookie = _value(
        file_values,
        "SESSION_COOKIE_SECURE",
        default="true" if flask_env.lower() == "production" else "false",
    ).lower()
    image_dir = Path(_value(file_values, "IMAGES_DIR", default=str(ROOT / "images")))
    if not image_dir.is_absolute():
        image_dir = ROOT / image_dir
    uploads_dir = Path(
        _value(
            file_values,
            "UPLOADS_DIR",
            default=str(writable_root / "uploads"),
        )
    )
    if not uploads_dir.is_absolute():
        uploads_dir = ROOT / uploads_dir

    database_url = _value(
        file_values,
        "TURSO_DATABASE_URL",
        "TURSO_AUTH_URL",
        "TURSO AUTH URL",
    )
    database_token = _value(
        file_values,
        "TURSO_AUTH_TOKEN",
        "TURSO AUTH TOKEN",
    )
    gemini_keys = [
        value
        for value in (
            _value(file_values, "GEMINI_API_KEY"),
            _value(file_values, "GEMINI_API_KEY_1", "GEMINI API KEY 1"),
            _value(file_values, "GEMINI_API_KEY_2", "GEMINI API KEY 2"),
        )
        if value
    ]
    return {
        "APP_NAME": "PlanMyTravel",
        "FLASK_ENV": flask_env,
        "INSTANCE_PATH": str(instance_path),
        "SECRET_KEY": _value(
            file_values, "FLASK_SECRET_KEY", "SECRET_KEY", default=""
        ),
        "DATABASE_URL": database_url,
        "DATABASE_AUTH_TOKEN": database_token,
        "DATABASE_PATH": _value(
            file_values,
            "DATABASE_PATH",
            default=str(instance_path / "planmytravel.sqlite3"),
        ),
        "GEMINI_API_KEYS": list(dict.fromkeys(gemini_keys)),
        "GEMINI_MODEL": _value(
            file_values, "GEMINI_MODEL", default="gemini-2.5-flash"
        ),
        "HOTELBEDS_API_KEY": _value(file_values, "HOTELBEDS_API_KEY"),
        "HOTELBEDS_API_SECRET": _value(file_values, "HOTELBEDS_API_SECRET"),
        "HOTELBEDS_BASE_URL": _value(
            file_values,
            "HOTELBEDS_BASE_URL",
            default="https://api.test.hotelbeds.com",
        ),
        "SERPAPI_API_KEY": _value(file_values, "SERPAPI_API_KEY"),
        "CLOUDINARY_CLOUD_NAME": _value(file_values, "CLOUDINARY_CLOUD_NAME"),
        "CLOUDINARY_API_KEY": _value(file_values, "CLOUDINARY_API_KEY"),
        "CLOUDINARY_API_SECRET": _value(file_values, "CLOUDINARY_API_SECRET"),
        "TURSO_API_TOKEN": _value(file_values, "TURSO_API_TOKEN"),
        "TURSO_ORG": _value(file_values, "TURSO_ORG"),
        "ADMIN_BOOTSTRAP_EMAIL": _value(file_values, "ADMIN_BOOTSTRAP_EMAIL"),
        "ADMIN_BOOTSTRAP_PASSWORD": _value(file_values, "ADMIN_BOOTSTRAP_PASSWORD"),
        "IMAGES_DIR": str(image_dir),
        "UPLOADS_DIR": str(uploads_dir),
        "USER_SESSION_MINUTES": int(
            _value(file_values, "USER_SESSION_MINUTES", default="10080")
        ),
        "ADMIN_SESSION_MINUTES": int(
            _value(file_values, "ADMIN_SESSION_MINUTES", default="60")
        ),
        "SESSION_COOKIE_SECURE": secure_cookie in {"true", "1", "yes"},
        "MAX_CONTENT_LENGTH": 80 * 1024 * 1024,
    }
