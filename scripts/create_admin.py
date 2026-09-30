from __future__ import annotations

import sys

from werkzeug.security import generate_password_hash

from app import create_app
from app.config import load_config
from app.db import get_db


def main() -> int:
    config = load_config()
    email = str(config.get("ADMIN_BOOTSTRAP_EMAIL", "")).strip().lower()
    password = str(config.get("ADMIN_BOOTSTRAP_PASSWORD", ""))
    if not email or not password:
        print("Set ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD in .env.", file=sys.stderr)
        return 2
    if len(password) < 12:
        print("Bootstrap password must be at least 12 characters.", file=sys.stderr)
        return 2

    app = create_app()
    with app.app_context():
        connection = get_db()
        existing = connection.execute(
            "SELECT id FROM admin_users WHERE lower(email) = ?",
            (email,),
        ).fetchone()
        if existing:
            print("An admin account with that email already exists; no changes made.")
            return 0
        connection.execute(
            """INSERT INTO admin_users
               (email, password_hash, name, role, is_active, failed_attempts, created_at)
               VALUES (?, ?, ?, 'super_admin', 1, 0, CURRENT_TIMESTAMP)""",
            (email, generate_password_hash(password), email.split("@", 1)[0]),
        )
        connection.commit()
    print("Initial super_admin created. Clear ADMIN_BOOTSTRAP_PASSWORD from .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
