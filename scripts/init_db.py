"""Initialize the local or configured Turso database."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.db import init_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        help="SQLite database path (defaults to instance/planmytravel.sqlite3).",
    )
    parser.add_argument(
        "--database-url",
        help="SQLite or Turso URL; Turso also requires --auth-token.",
    )
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

    init_db(app)
    print("Database schema initialized.")


if __name__ == "__main__":
    main()
