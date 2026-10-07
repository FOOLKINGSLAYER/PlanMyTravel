"""Database connections and schema initialization for the Flask application."""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from flask import Flask, current_app, g, has_app_context


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_DIR = _PROJECT_ROOT / "schema"
_DEMO_IMAGES = (
    "disney imagination garden 1.png",
    "disney imagination garden 2.png",
    "disney imagination garden 3.png",
    "disney discovery reef.png",
    "toy story place.png",
)


def _database_settings(app: Flask) -> tuple[str, str | None, str | None]:
    config: Any = app.config
    url = (
        config.get("TURSO_DATABASE_URL")
        or config.get("TURSO_AUTH_URL")
        or config.get("TURSO AUTH URL")
        or config.get("DATABASE_URL")
    )
    token = (
        config.get("TURSO_AUTH_TOKEN")
        or config.get("TURSO AUTH TOKEN")
        or config.get("DATABASE_AUTH_TOKEN")
    )

    if url:
        url = str(url).strip()
        if url.startswith(("libsql://", "https://", "http://")):
            if token:
                return "turso", url, str(token)
            url = None
    if url:
        if url.startswith("sqlite:///"):
            path = unquote(url[len("sqlite:///"):])
            return "sqlite", path, None
        if url.startswith("sqlite://"):
            path = unquote(url[len("sqlite://"):])
            return "sqlite", path, None
        return "sqlite", url, None

    configured_path = (
        config.get("DATABASE_PATH")
        or config.get("DATABASE")
        or config.get("DB_PATH")
    )
    path = str(configured_path or (_PROJECT_ROOT / "instance" / "planmytravel.sqlite3"))
    return "sqlite", path, None


def _connect(app: Flask | None = None) -> Any:
    backend, location, token = _database_settings(app or current_app)
    if backend == "turso":
        import libsql  # type: ignore[import-not-found]

        assert location is not None and token is not None
        connection = libsql.connect(database=location, auth_token=token)
        return connection

    assert location is not None
    if location == ":memory:":
        flask_app = app or current_app._get_current_object()
        memory_uri = flask_app.extensions.get("planmytravel_memory_db_uri")
        if memory_uri is None:
            memory_uri = f"file:planmytravel_{id(flask_app)}?mode=memory&cache=shared"
            flask_app.extensions["planmytravel_memory_db_uri"] = memory_uri
            flask_app.extensions["planmytravel_memory_db_anchor"] = sqlite3.connect(
                memory_uri, uri=True, check_same_thread=False
            )
        connection = sqlite3.connect(
            memory_uri,
            detect_types=sqlite3.PARSE_DECLTYPES,
            uri=True,
            check_same_thread=False,
        )
    elif location.startswith("file:"):
        connection = sqlite3.connect(
            location, detect_types=sqlite3.PARSE_DECLTYPES, uri=location.startswith("file:")
        )
    else:
        database_path = Path(location).expanduser()
        if not database_path.is_absolute():
            database_path = _PROJECT_ROOT / database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(database_path), detect_types=sqlite3.PARSE_DECLTYPES
        )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def close_db(_error: BaseException | None = None) -> None:
    """Close the connection associated with the current Flask application context."""
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def get_db(app: Flask | None = None) -> Any:
    """Return this application context's database connection.

    With no ``app``, requires an active Flask application context and caches the
    connection on that context until teardown. If ``app`` is passed while its
    application context is active, the same scoped behavior applies. If passed
    outside a context, returns a standalone connection that the caller must
    close. Both backends accept ``?`` parameters and expose DB-API ``execute``,
    ``commit``, ``rollback``, and cursor methods.
    """
    if app is not None and not has_app_context():
        return _connect(app)
    if not has_app_context():
        raise RuntimeError("get_db() requires an active Flask application context.")
    if app is not None and current_app._get_current_object() is not app:
        return _connect(app)
    if "db" not in g:
        app = current_app._get_current_object()
        if not app.extensions.get("planmytravel_db_teardown_registered"):
            app.teardown_appcontext(close_db)
            app.extensions["planmytravel_db_teardown_registered"] = True
        g.db = _connect()
    return g.db


_MIGRATION_COLUMNS = {
    "users": {
        "name": "TEXT NOT NULL DEFAULT ''",
        "full_name": "TEXT NOT NULL DEFAULT ''",
        "phone": "TEXT",
        "date_of_birth": "TEXT", "country": "TEXT",
        "preferred_currency": "TEXT NOT NULL DEFAULT 'USD'",
        "avatar_url": "TEXT",
        "profile_image_url": "TEXT",
        "bio": "TEXT",
        "preferences_json": "TEXT NOT NULL DEFAULT '{}'",
        "emergency_contact_name": "TEXT", "emergency_contact_phone": "TEXT",
        "role": "TEXT NOT NULL DEFAULT 'traveler'",
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "session_version": "INTEGER NOT NULL DEFAULT 1",
        "email_verified_at": "TEXT",
        "updated_at": "TEXT",
    },
    "destinations": {
        "country": "TEXT", "region": "TEXT", "city": "TEXT", "description": "TEXT",
        "short_description": "TEXT", "hero_image_url": "TEXT", "image_url": "TEXT",
        "latitude": "REAL", "longitude": "REAL",
        "tagline": "TEXT", "category": "TEXT", "travel_styles_json": "TEXT NOT NULL DEFAULT '[]'",
        "attractions_json": "TEXT NOT NULL DEFAULT '[]'", "activities_json": "TEXT NOT NULL DEFAULT '[]'",
        "estimated_cost_min": "REAL", "estimated_cost_max": "REAL", "best_season": "TEXT",
        "cost_currency": "TEXT NOT NULL DEFAULT 'SGD'",
        "weather_info": "TEXT", "ideal_stay": "TEXT", "rating": "REAL NOT NULL DEFAULT 0",
        "is_featured": "INTEGER NOT NULL DEFAULT 0",
        "is_published": "INTEGER NOT NULL DEFAULT 1", "updated_at": "TEXT",
    },
    "trips": {
        "title": "TEXT NOT NULL DEFAULT ''",
        "destination_id": "INTEGER", "start_date": "TEXT", "end_date": "TEXT",
        "travelers": "INTEGER NOT NULL DEFAULT 1",
        "travelers_count": "INTEGER NOT NULL DEFAULT 1",
        "traveler_count": "INTEGER NOT NULL DEFAULT 1",
        "total_budget": "REAL", "selected_tier": "TEXT",
        "total_budget": "REAL", "budget": "REAL", "budget_amount": "REAL",
        "currency": "TEXT NOT NULL DEFAULT 'USD'", "selected_tier": "TEXT",
        "travel_style": "TEXT", "style": "TEXT", "status": "TEXT NOT NULL DEFAULT 'planning'",
        "origin": "TEXT", "interests_json": "TEXT NOT NULL DEFAULT '[]'",
        "notes": "TEXT", "preferences_json": "TEXT NOT NULL DEFAULT '{}'", "updated_at": "TEXT",
    },
    "trip_inventory": {
        "flight_status": "TEXT NOT NULL DEFAULT 'unavailable'",
        "hotel_status": "TEXT NOT NULL DEFAULT 'unavailable'",
        "flight_notice": "TEXT",
        "hotel_notice": "TEXT",
    },
    "itinerary_plans": {
        "title": "TEXT", "description": "TEXT", "tier": "TEXT NOT NULL DEFAULT 'standard'",
        "summary": "TEXT", "estimated_total": "REAL",
        "json_payload": "TEXT NOT NULL DEFAULT '{}'", "payload_json": "TEXT NOT NULL DEFAULT '{}'",
        "total_estimated_cost": "REAL", "currency": "TEXT NOT NULL DEFAULT 'USD'", "updated_at": "TEXT",
    },
    "itinerary_days": {
        "plan_id": "TEXT", "date": "TEXT", "start_date": "TEXT", "end_date": "TEXT",
        "title": "TEXT", "summary": "TEXT", "notes": "TEXT", "weather_json": "TEXT",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "itinerary_items": {
        "description": "TEXT", "location": "TEXT", "start_time": "TEXT", "end_time": "TEXT",
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
        "item_type": "TEXT NOT NULL DEFAULT 'activity'", "category": "TEXT",
        "duration_min": "INTEGER", "estimated_cost": "REAL", "lat": "REAL", "lng": "REAL",
        "route_from_prev_json": "TEXT",
        "currency": "TEXT NOT NULL DEFAULT 'USD'", "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "updated_at": "TEXT",
    },
    "guides": {
        "photo_url": "TEXT",
        "languages_json": "TEXT NOT NULL DEFAULT '[]'",
        "specialties_json": "TEXT NOT NULL DEFAULT '[]'",
        "rating": "REAL NOT NULL DEFAULT 0", "is_verified": "INTEGER NOT NULL DEFAULT 0",
        "is_active": "INTEGER NOT NULL DEFAULT 1", "updated_at": "TEXT",
    },
    "guide_messages": {
        "sender_user_id": "INTEGER", "recipient_user_id": "INTEGER",
        "body": "TEXT NOT NULL DEFAULT ''", "read_at": "TEXT",
    },
    "packages": {
        "destination_id": "INTEGER", "title": "TEXT NOT NULL DEFAULT ''",
        "name": "TEXT", "slug": "TEXT", "category": "TEXT NOT NULL DEFAULT 'tour'",
        "short_description": "TEXT", "description": "TEXT", "duration_days": "INTEGER",
        "duration_nights": "INTEGER", "group_size_min": "INTEGER", "group_size_max": "INTEGER",
        "base_price": "REAL NOT NULL DEFAULT 0", "price": "REAL NOT NULL DEFAULT 0",
        "currency": "TEXT NOT NULL DEFAULT 'SGD'", "pricing_json": "TEXT NOT NULL DEFAULT '{}'",
        "itinerary_json": "TEXT NOT NULL DEFAULT '[]'", "inclusions_json": "TEXT NOT NULL DEFAULT '[]'",
        "exclusions_json": "TEXT NOT NULL DEFAULT '[]'", "highlights_json": "TEXT NOT NULL DEFAULT '[]'",
        "difficulty": "TEXT", "rating": "REAL NOT NULL DEFAULT 0",
        "review_count": "INTEGER NOT NULL DEFAULT 0", "cover_image_url": "TEXT",
        "is_featured": "INTEGER NOT NULL DEFAULT 0",
        "is_published": "INTEGER NOT NULL DEFAULT 1", "updated_at": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'draft'", "deleted_at": "TEXT",
        "travel_style": "TEXT",
    },
    "departures": {
        "ends_at": "TEXT", "capacity": "INTEGER", "seats_available": "INTEGER",
        "price": "REAL", "currency": "TEXT NOT NULL DEFAULT 'SGD'",
        "status": "TEXT NOT NULL DEFAULT 'available'", "updated_at": "TEXT",
    },
    "package_departures": {
        "ends_at": "TEXT", "capacity": "INTEGER", "seats_available": "INTEGER",
        "price": "REAL", "currency": "TEXT NOT NULL DEFAULT 'SGD'",
        "status": "TEXT NOT NULL DEFAULT 'available'", "updated_at": "TEXT",
    },
    "package_images": {
        "media_asset_id": "INTEGER", "alt_text": "TEXT", "caption": "TEXT",
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
    },
    "package_itinerary_days": {
        "description": "TEXT", "activities_json": "TEXT NOT NULL DEFAULT '[]'",
        "meals_json": "TEXT NOT NULL DEFAULT '[]'", "accommodation": "TEXT",
    },
    "bookings": {
        "booking_reference": "TEXT", "trip_id": "INTEGER", "package_id": "INTEGER",
        "departure_id": "INTEGER", "status": "TEXT NOT NULL DEFAULT 'pending'",
        "travelers_count": "INTEGER NOT NULL DEFAULT 1", "total_amount": "REAL NOT NULL DEFAULT 0",
        "currency": "TEXT NOT NULL DEFAULT 'SGD'", "contact_name": "TEXT",
        "contact_email": "TEXT", "contact_phone": "TEXT",
        "payment_status": "TEXT NOT NULL DEFAULT 'unpaid'", "booked_at": "TEXT",
        "updated_at": "TEXT",
    },
    "wallets": {
        "balance": "REAL NOT NULL DEFAULT 0", "currency": "TEXT NOT NULL DEFAULT 'INR'",
        "updated_at": "TEXT",
    },
    "wallet_transactions": {
        "trip_id": "INTEGER", "booking_id": "INTEGER", "type": "TEXT NOT NULL DEFAULT 'adjustment'",
        "transaction_type": "TEXT", "status": "TEXT NOT NULL DEFAULT 'completed'",
        "currency": "TEXT NOT NULL DEFAULT 'INR'", "description": "TEXT", "reference": "TEXT",
    },
    "expenses": {
        "trip_id": "INTEGER", "booking_id": "INTEGER", "currency": "TEXT NOT NULL DEFAULT 'SGD'",
        "category": "TEXT", "spent_at": "TEXT", "notes": "TEXT", "updated_at": "TEXT",
    },
    "media_assets": {
        "relative_path": "TEXT", "url": "TEXT", "filename": "TEXT NOT NULL DEFAULT ''",
        "original_filename": "TEXT NOT NULL DEFAULT ''", "mime_type": "TEXT",
        "file_size": "INTEGER", "size_bytes": "INTEGER", "width": "INTEGER", "height": "INTEGER",
        "alt_text": "TEXT", "folder": "TEXT", "uploaded_by": "INTEGER",
        "storage_provider": "TEXT NOT NULL DEFAULT 'local'",
        "cloudinary_public_id": "TEXT",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'", "updated_at": "TEXT",
    },
    "reviews": {
        "destination_id": "INTEGER", "package_id": "INTEGER", "trip_id": "INTEGER",
        "rating": "INTEGER NOT NULL DEFAULT 5", "title": "TEXT", "body": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'published'", "is_verified": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "TEXT",
    },
    "review_photos": {
        "media_asset_id": "INTEGER", "caption": "TEXT", "sort_order": "INTEGER NOT NULL DEFAULT 0",
    },
    "admin_users": {
        "user_id": "INTEGER", "name": "TEXT NOT NULL DEFAULT ''",
        "role": "TEXT NOT NULL DEFAULT 'admin'", "is_active": "INTEGER NOT NULL DEFAULT 1",
        "failed_attempts": "INTEGER NOT NULL DEFAULT 0", "locked_until": "TEXT",
        "last_login_at": "TEXT", "updated_at": "TEXT",
    },
    "admin_audit_logs": {
        "admin_id": "INTEGER", "admin_user_id": "INTEGER",
        "action": "TEXT NOT NULL DEFAULT 'legacy'", "entity_type": "TEXT", "entity_id": "TEXT",
        "diff_json": "TEXT", "details_json": "TEXT NOT NULL DEFAULT '{}'",
        "ip": "TEXT", "ip_address": "TEXT", "user_agent": "TEXT",
    },
    "featured_placements": {
        "target_type": "TEXT NOT NULL DEFAULT 'package'", "target_id": "TEXT NOT NULL DEFAULT ''",
        "placement": "TEXT NOT NULL DEFAULT 'home'", "starts_at": "TEXT", "ends_at": "TEXT",
        "sort_order": "INTEGER NOT NULL DEFAULT 0", "is_active": "INTEGER NOT NULL DEFAULT 1",
        "updated_at": "TEXT",
    },
    "cache": {
        "value": "TEXT NOT NULL DEFAULT ''", "expires_at": "TEXT", "updated_at": "TEXT",
    },
}


def _apply_schema(connection: Any) -> None:
    if not _SCHEMA_DIR.is_dir():
        raise RuntimeError(f"Database schema directory not found: {_SCHEMA_DIR}")

    schema_files = sorted(_SCHEMA_DIR.glob("*.sql"))
    if not schema_files:
        raise RuntimeError(f"No SQL schema files found in {_SCHEMA_DIR}")

    tables: list[str] = []
    other_statements: list[str] = []
    for schema_file in schema_files:
        sql = schema_file.read_text(encoding="utf-8")
        for statement in sql.split(";"):
            statement = statement.strip()
            if not statement:
                continue
            if statement.upper().startswith("CREATE TABLE IF NOT EXISTS"):
                tables.append(statement)
            else:
                other_statements.append(statement)

    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "audit_logs" in existing_tables and "admin_audit_logs" not in existing_tables:
        connection.execute("ALTER TABLE audit_logs RENAME TO admin_audit_logs")

    for statement in tables:
        connection.execute(statement)

    for table_name, columns in _MIGRATION_COLUMNS.items():
        present = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if not present:
            continue
        newly_added = set(columns) - present
        for column_name, column_type in columns.items():
            if column_name not in present:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )
        if table_name == "media_assets":
            connection.execute(
                "UPDATE media_assets SET relative_path = path "
                "WHERE (relative_path IS NULL OR relative_path = '') AND path IS NOT NULL"
            )
        if table_name == "packages":
            connection.execute(
                "UPDATE packages SET slug = 'legacy-package-' || id "
                "WHERE slug IS NULL OR slug = ''"
            )
            connection.execute(
                "UPDATE packages SET title = name "
                "WHERE (title IS NULL OR title = '') AND name IS NOT NULL"
            )
            if "status" in newly_added:
                connection.execute(
                    """UPDATE packages
                       SET status = CASE WHEN is_published = 1
                                         THEN 'published' ELSE 'draft' END"""
                )
        elif table_name == "trips":
            if "travelers" in newly_added:
                connection.execute(
                    """UPDATE trips SET travelers = COALESCE(
                           NULLIF(travelers_count, 1),
                           NULLIF(traveler_count, 1), travelers)"""
                )
            if "total_budget" in newly_added:
                connection.execute(
                    "UPDATE trips SET total_budget = COALESCE(budget, budget_amount)"
                )
        elif table_name == "media_assets":
            connection.execute(
                "UPDATE media_assets SET original_filename = filename "
                "WHERE original_filename IS NULL OR original_filename = ''"
            )
            connection.execute(
                "UPDATE media_assets SET size_bytes = file_size WHERE size_bytes IS NULL"
            )
            connection.execute(
                """UPDATE media_assets
                   SET folder = CASE WHEN instr(relative_path, '/') > 0
                                     THEN substr(relative_path, 1, instr(relative_path, '/') - 1)
                                     ELSE '' END
                   WHERE folder IS NULL"""
            )
        elif table_name == "admin_audit_logs":
            connection.execute(
                "UPDATE admin_audit_logs SET admin_id = admin_user_id WHERE admin_id IS NULL"
            )
            connection.execute(
                "UPDATE admin_audit_logs SET diff_json = details_json "
                "WHERE (diff_json IS NULL OR diff_json = '{}') AND details_json IS NOT NULL"
            )
            connection.execute(
                "UPDATE admin_audit_logs SET ip = ip_address WHERE ip IS NULL"
            )

    _migrate_text_primary_key(connection, tables, "trips")
    _migrate_text_primary_key(connection, tables, "itinerary_plans")
    _migrate_text_primary_key(connection, tables, "admin_audit_logs")

    legacy_departures = connection.execute(
        "SELECT type FROM sqlite_master WHERE name = 'departures'"
    ).fetchone()
    if legacy_departures and legacy_departures[0] == "table":
        legacy_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(departures)").fetchall()
        }
        required_columns = {
            "id", "package_id", "starts_at", "ends_at", "capacity",
            "seats_available", "price", "currency", "status", "created_at", "updated_at",
        }
        if required_columns <= legacy_columns:
            connection.execute(
                """
                INSERT OR IGNORE INTO package_departures
                    (id, package_id, starts_at, ends_at, capacity, seats_available,
                     price, currency, status, created_at, updated_at)
                SELECT id, package_id, starts_at, ends_at, capacity, seats_available,
                       price, currency, status, created_at, updated_at
                FROM departures
                """
            )

    for statement in other_statements:
        connection.execute(statement)

    for table_name in _MIGRATION_COLUMNS:
        columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        id_column = next((column for column in columns if column[1] == "id"), None)
        if id_column is None or id_column[5] != 1 or str(id_column[2]).upper() == "INTEGER":
            continue
        trigger_name = f"tr_{table_name}_assign_legacy_id"
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_name}
            AFTER INSERT ON {table_name}
            WHEN NEW.id IS NULL
            BEGIN
                UPDATE {table_name}
                SET id = 'legacy-' || NEW.rowid
                WHERE rowid = NEW.rowid;
            END
            """
        )


def _migrate_text_primary_key(
    connection: Any, table_statements: list[str], table_name: str
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    id_column = next((column for column in columns if column[1] == "id"), None)
    if id_column is None or str(id_column[2]).upper() == "TEXT":
        return

    create_statement = next(
        (
            statement for statement in table_statements
            if statement.upper().startswith(
                f"CREATE TABLE IF NOT EXISTS {table_name.upper()} "
            )
        ),
        None,
    )
    if create_statement is None:
        raise RuntimeError(f"No canonical schema found for {table_name}.")

    replacement = f"{table_name}__text_migration"
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute(f"DROP TABLE IF EXISTS {replacement}")
        create_new = create_statement.replace(
            f"CREATE TABLE IF NOT EXISTS {table_name}",
            f"CREATE TABLE {replacement}",
            1,
        )
        connection.execute(create_new)
        source_columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        target_columns = [
            row[1] for row in connection.execute(f"PRAGMA table_info({replacement})")
            if row[1] in source_columns
        ]
        quoted_columns = ", ".join(f'"{column}"' for column in target_columns)
        selections = ", ".join(
            f'CAST("{column}" AS TEXT)' if column == "id" else f'"{column}"'
            for column in target_columns
        )
        connection.execute(
            f"INSERT INTO {replacement} ({quoted_columns}) "
            f"SELECT {selections} FROM {table_name}"
        )
        connection.execute(f"DROP TABLE {table_name}")
        connection.execute(f"ALTER TABLE {replacement} RENAME TO {table_name}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def seed_demo_data(app: Flask | None = None) -> None:
    """Seed the Singapore demo destination, package, and available local gallery."""
    if app is not None:
        with app.app_context():
            seed_demo_data()
        return
    if not has_app_context():
        raise RuntimeError("seed_demo_data() requires an app or active app context.")

    connection = get_db()
    image_root = Path(current_app.config.get("IMAGES_DIR", _PROJECT_ROOT / "images"))
    if not image_root.is_absolute():
        image_root = _PROJECT_ROOT / image_root

    try:
        connection.execute(
            """
            INSERT INTO destinations
                (name, slug, country, city, description, short_description,
                 tagline, category, travel_styles_json, attractions_json,
                 activities_json, estimated_cost_min, estimated_cost_max,
                 cost_currency, best_season, ideal_stay, hero_image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO NOTHING
            """,
            (
                "Singapore",
                "singapore",
                "Singapore",
                "Singapore",
                "A vibrant city destination with gardens, waterfront sights, and family attractions.",
                "A family-friendly city break in Singapore.",
                "A city of gardens, culture, and family adventures.",
                "city",
                '["Family","Culture","Food","City break"]',
                '["Gardens by the Bay","Marina Bay waterfront","Sentosa Island"]',
                '["Explore gardens","Discover local food","Enjoy family attractions"]',
                150.0,
                500.0,
                "SGD",
                "February to April",
                "3–5 days",
                "/images/disney%20imagination%20garden%201.png",
            ),
        )
        destination = connection.execute(
            "SELECT id FROM destinations WHERE slug = ?", ("singapore",)
        ).fetchone()
        destination_id = destination[0]
        images = [
            name for name in _DEMO_IMAGES if (image_root / name).is_file()
        ]
        itinerary = [
            {
                "day": 1,
                "title": "Gardens and the waterfront",
                "activities": [
                    "Explore Gardens by the Bay",
                    "Enjoy the Marina Bay waterfront",
                ],
            },
            {
                "day": 2,
                "title": "Family attractions",
                "activities": [
                    "Visit a family-friendly Singapore attraction",
                    "Explore the resort and waterfront precinct",
                ],
            },
        ]
        package_data = {
            "destination_id": destination_id,
            "title": "Singapore Family Theme Park Escape",
            "name": "Singapore Family Theme Park Escape",
            "slug": "singapore-family-theme-park-escape",
            "category": "family",
            "short_description": "A flexible Singapore family getaway with a Disney Adventure-inspired gallery.",
            "description": (
                "A two-day Singapore city escape for families, pairing garden and "
                "waterfront highlights with time for resort attractions."
            ),
            "duration_days": 2,
            "duration_nights": 1,
            "group_size_min": 1,
            "group_size_max": 20,
            "base_price": 399.0,
            "price": 399.0,
            "currency": "SGD",
            "pricing_json": '{"per_person":399,"currency":"SGD"}',
            "itinerary_json": json.dumps(itinerary),
            "inclusions_json": '["Two-day suggested itinerary","Singapore destination guide"]',
            "exclusions_json": '["Flights","Accommodation","Attraction admission tickets"]',
            "highlights_json": '["Gardens by the Bay","Marina Bay","Family attractions"]',
            "difficulty": "moderate",
            "rating": 0.0,
            "review_count": 0,
            "cover_image_url": (
                "/images/" + quote(images[0]) if images else None
            ),
            "is_featured": 1,
            "is_published": 1,
            "status": "published",
            "deleted_at": None,
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
        }
        connection.execute(
            """
            INSERT INTO packages
                (destination_id, title, name, slug, category, travel_style, short_description, description,
                 duration_days, duration_nights, base_price, price, currency,
                 pricing_json, itinerary_json, inclusions_json, exclusions_json,
                 highlights_json, cover_image_url, is_featured, is_published, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO NOTHING
            """,
            (
                package_data["destination_id"],
                package_data["title"],
                package_data["name"],
                package_data["slug"],
                package_data["category"],
                "family",
                package_data["short_description"],
                package_data["description"],
                package_data["duration_days"],
                package_data["duration_nights"],
                package_data["base_price"],
                package_data["price"],
                package_data["currency"],
                package_data["pricing_json"],
                package_data["itinerary_json"],
                package_data["inclusions_json"],
                package_data["exclusions_json"],
                package_data["highlights_json"],
                package_data["cover_image_url"],
                package_data["is_featured"],
                package_data["is_published"],
                package_data["status"],
            ),
        )
        package = connection.execute(
            "SELECT id FROM packages WHERE slug = ?",
            ("singapore-family-theme-park-escape",),
        ).fetchone()
        package_id = package[0]

        for day in itinerary:
            connection.execute(
                """
                INSERT INTO package_itinerary_days
                    (package_id, day_number, title, description, activities_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(package_id, day_number) DO NOTHING
                """,
                (
                    package_id,
                    day["day"],
                    day["title"],
                    "Suggested highlights for this Singapore itinerary day.",
                    json.dumps(day["activities"]),
                ),
            )

        for sort_order, image_name in enumerate(images):
            relative_path = image_name
            image_url = "/images/" + quote(relative_path)
            mime_type = "image/png" if image_name.lower().endswith(".png") else None
            connection.execute(
                """
                INSERT INTO media_assets
                    (relative_path, path, url, filename, original_filename, mime_type,
                     size_bytes, file_size, folder)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    path = excluded.path,
                    url = CASE
                        WHEN media_assets.storage_provider = 'cloudinary'
                        THEN media_assets.url
                        ELSE excluded.url
                    END,
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
                    image_url,
                    image_name,
                    image_name,
                    mime_type,
                    (image_root / image_name).stat().st_size,
                    (image_root / image_name).stat().st_size,
                    "",
                ),
            )
            asset = connection.execute(
                "SELECT id FROM media_assets WHERE relative_path = ?",
                (relative_path,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO package_images (package_id, media_asset_id, image_url, alt_text, sort_order)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(package_id, image_url) DO NOTHING
                """,
                (package_id, asset[0], image_url, image_name.rsplit(".", 1)[0], sort_order),
            )
            connection.execute(
                """INSERT INTO destination_images
                   (destination_id, media_asset_id, image_url, alt_text, sort_order)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (destination_id, asset[0], image_url, image_name.rsplit(".", 1)[0], sort_order),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def init_db(app: Flask | None = None, *, seed: bool = True) -> None:
    """Create all tables and indexes; safe to call repeatedly.

    When ``app`` is supplied, initialization runs in its application context.
    Otherwise an active Flask application context is required.
    """
    if app is not None:
        with app.app_context():
            init_db(seed=seed)
        return
    if not has_app_context():
        raise RuntimeError("init_db() requires an app argument or active app context.")

    connection = get_db()
    try:
        _apply_schema(connection)
        connection.commit()
        if seed:
            seed_demo_data()
    except Exception:
        connection.rollback()
        raise


__all__ = ["close_db", "get_db", "init_db", "seed_demo_data"]
