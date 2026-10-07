from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.datastructures import FileStorage

from app import create_app
from app.config import ROOT
from app.db import get_db, seed_demo_data
from app.db_utils import query_one
from app.security import make_admin_cookie
from app.blueprints import public
from app.services import travel_inventory
from app.services.media import MediaStorageError, store_image
from app.services.wallet import add_funds, charge


def test_vercel_uses_writable_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE_URL": "",
            "DATABASE_AUTH_TOKEN": "",
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }
    )

    writable_root = tmp_path / "planmytravel"
    assert app.instance_path == str(writable_root / "instance")
    assert app.config["DATABASE_PATH"] == str(
        writable_root / "instance" / "planmytravel.sqlite3"
    )
    assert app.config["UPLOADS_DIR"] == str(writable_root / "uploads")
    assert app.config["IMAGES_DIR"] == str(ROOT / "images")
    favicon = app.test_client().get("/favicon.ico")
    assert favicon.status_code == 302
    assert favicon.headers["Location"] == "/static/images/favicon.svg"
    assert Path(app.instance_path).is_dir()
    assert Path(app.config["UPLOADS_DIR"]).is_dir()


@pytest.fixture
def app(tmp_path, monkeypatch):
    def fake_cloudinary_upload(file, **options):
        file.read()
        public_id = options["public_id"]
        return {
            "public_id": public_id,
            "secure_url": (
                "https://res.cloudinary.com/test-cloud/image/upload/"
                f"{public_id}.png"
            ),
        }

    from cloudinary import uploader

    monkeypatch.setattr(uploader, "upload", fake_cloudinary_upload)
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE_PATH": str(tmp_path / "accounts.sqlite3"),
            "DATABASE_URL": "",
            "DATABASE_AUTH_TOKEN": "",
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_URL": "",
            "TURSO_AUTH_TOKEN": "",
            "IMAGES_DIR": str(tmp_path / "images"),
            "UPLOADS_DIR": str(tmp_path / "uploads"),
            "CLOUDINARY_CLOUD_NAME": "test-cloud",
            "CLOUDINARY_API_KEY": "test-key",
            "CLOUDINARY_API_SECRET": "test-secret",
            "SESSION_COOKIE_SECURE": False,
        }
    )


def _csrf_token(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    token = re.search(
        r'name="csrf_token" value="([^"]+)"',
        response.get_data(as_text=True),
    )
    assert token
    return token.group(1)


def _signup(client, email: str = "traveler@example.com") -> None:
    token = _csrf_token(client, "/signup")
    response = client.post(
        "/signup",
        data={
            "csrf_token": token,
            "name": "Taylor Traveler",
            "email": email,
            "password": "correct horse battery staple",
            "terms": "on",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/trips")


def test_signup_and_public_browsing(app):
    client = app.test_client()
    home_response = client.get("/")
    assert home_response.status_code == 200
    assert b'href="/itinerary"' in home_response.data
    assert b"Plan my trip with AI" in home_response.data
    assert b'href="/search?q=Disney+Adventure"' in home_response.data
    assert b"Show more categories" in home_response.data
    assert b"css/features.css" in home_response.data
    assert b"js/app.js" in home_response.data
    assert client.get("/static/css/style.css").status_code == 200
    assert client.get("/static/css/features.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200

    public_response = client.get("/search")
    assert public_response.status_code == 200
    assert b'href="/admin/login"' in public_response.data
    assert b"Admin login" in public_response.data
    planner_response = client.get("/itinerary")
    assert planner_response.status_code == 200
    assert b'<select name="destination" required' in planner_response.data
    assert b'Choose a destination' in planner_response.data
    assert b'id="flight-details"' in planner_response.data
    assert b'Departure airport code (optional)' in planner_response.data
    assert b'name="origin"' in planner_response.data
    assert b'name="total_budget"' in planner_response.data

    _signup(client)
    account_response = client.get("/account")
    assert account_response.status_code == 200
    with app.app_context():
        user = query_one(
            "SELECT email, password_hash, is_active FROM users WHERE email = ?",
            ("traveler@example.com",),
        )
    assert user["is_active"] == 1
    assert check_password_hash(user["password_hash"], "correct horse battery staple")


def test_database_seed_keeps_migrated_cloudinary_urls(app, monkeypatch):
    import app.db as db_module

    cloudinary_url = (
        "https://res.cloudinary.com/test-cloud/image/upload/seed-image.png"
    )
    image_name = "disney imagination garden 1.png"
    Path(app.config["IMAGES_DIR"], image_name).write_bytes(b"test image")
    monkeypatch.setattr(db_module, "_DEMO_IMAGES", (image_name,))
    with app.app_context():
        connection = get_db()
        connection.execute(
            """INSERT INTO media_assets
               (relative_path, path, url, filename, original_filename,
                storage_provider, cloudinary_public_id)
               VALUES (?, ?, ?, ?, ?, 'cloudinary', 'seed-image')""",
            (image_name, image_name, cloudinary_url, image_name, image_name),
        )
        connection.commit()
        seed_demo_data()
        asset = query_one(
            """SELECT url, storage_provider, cloudinary_public_id
               FROM media_assets WHERE relative_path = ?""",
            (image_name,),
        )
    assert asset == {
        "url": cloudinary_url,
        "storage_provider": "cloudinary",
        "cloudinary_public_id": "seed-image",
    }


def test_media_upload_does_not_fall_back_to_local_storage(app):
    app.config["CLOUDINARY_API_KEY"] = ""
    image = io.BytesIO()
    Image.new("RGB", (12, 12), color="purple").save(image, format="PNG")
    upload = FileStorage(stream=io.BytesIO(image.getvalue()), filename="image.png")
    with app.app_context(), pytest.raises(MediaStorageError, match="Cloudinary storage is not configured"):
        store_image(upload, "library")
    assert not list(Path(app.config["UPLOADS_DIR"]).rglob("*"))
    assert not list(Path(app.config["IMAGES_DIR"]).rglob("*"))


def test_remembered_login_and_profile_preferences(app):
    _signup(app.test_client())

    client = app.test_client()
    token = _csrf_token(client, "/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "traveler@example.com",
            "password": "correct horse battery staple",
            "remember": "on",
        },
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["_permanent"] is True

    profile_token = _csrf_token(client, "/account")
    image = io.BytesIO()
    Image.new("RGB", (12, 12), color="purple").save(image, format="PNG")
    image.seek(0)
    profile_response = client.post(
        "/account",
        data={
            "csrf_token": profile_token,
            "first_name": "Taylor",
            "last_name": "Traveler",
            "email": "taylor@example.com",
            "phone": "+1 555 0100",
            "date_of_birth": "1990-01-02",
            "country": "Canada",
            "preferred_currency": "USD",
            "bio": "I love quiet coastlines.",
            "profile_picture": (io.BytesIO(b"not an image"), "profile.png"),
        },
    )
    assert profile_response.status_code == 400
    assert b"not a valid image" in profile_response.data

    profile_token = _csrf_token(client, "/account")
    profile_response = client.post(
        "/account",
        data={
            "csrf_token": profile_token,
            "first_name": "Taylor",
            "last_name": "Traveler",
            "email": "taylor@example.com",
            "phone": "+1 555 0100",
            "date_of_birth": "1990-01-02",
            "country": "Canada",
            "preferred_currency": "EUR",
            "bio": "I love quiet coastlines.",
            "profile_picture": (image, "profile.png"),
        },
    )
    assert profile_response.status_code == 200
    assert b"Your profile has been updated." in profile_response.data
    with app.app_context():
        saved_profile_image = query_one(
            "SELECT profile_image_url FROM users WHERE lower(email) = ?",
            ("taylor@example.com",),
        )["profile_image_url"]
        assert saved_profile_image.startswith("https://res.cloudinary.com/")

    preferences_token = _csrf_token(client, "/account/preferences")
    preferences_response = client.post(
        "/account/preferences",
        data={
            "csrf_token": preferences_token,
            "preferred_language": "fr",
            "travel_pace": "Relaxed",
            "travel_interests": ["Nature", "Culture"],
        },
    )
    assert preferences_response.status_code == 200
    with app.app_context():
        saved = query_one(
            """SELECT email, phone, date_of_birth, country, preferred_currency,
                      bio, preferences_json
               FROM users WHERE email = ?""",
            ("taylor@example.com",),
        )
    assert saved["preferred_currency"] == "EUR"
    assert saved["email"] == "taylor@example.com"
    assert json.loads(saved["preferences_json"]) == {
        "preferred_language": "fr",
        "travel_pace": "Relaxed",
        "travel_interests": ["Nature", "Culture"],
    }
    with app.app_context():
        saved_picture = query_one(
            "SELECT profile_image_url FROM users WHERE email = ?",
            ("taylor@example.com",),
        )
    assert saved_picture["profile_image_url"].startswith(
        "https://res.cloudinary.com/"
    )
    assert not list(Path(app.config["IMAGES_DIR"]).rglob("*"))
    assert not list(Path(app.config["UPLOADS_DIR"]).rglob("*"))

    logout_token = _csrf_token(client, "/account")
    assert client.post("/logout", data={"csrf_token": logout_token}).status_code == 302
    assert client.get("/account").status_code == 302


def test_password_change_invalidates_other_sessions(app):
    first_client = app.test_client()
    _signup(first_client)

    other_client = app.test_client()
    token = _csrf_token(other_client, "/login")
    assert other_client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "traveler@example.com",
            "password": "correct horse battery staple",
            "remember": "on",
        },
    ).status_code == 302

    token = _csrf_token(first_client, "/account/password")
    response = first_client.post(
        "/account/password",
        data={
            "csrf_token": token,
            "current_password": "correct horse battery staple",
            "new_password": "a better password 123",
            "confirm_password": "a better password 123",
        },
    )
    assert response.status_code == 200
    assert other_client.get("/account").status_code == 302
    assert first_client.get("/account").status_code == 200

    login_client = app.test_client()
    login_token = _csrf_token(login_client, "/login")
    assert login_client.post(
        "/login",
        data={
            "csrf_token": login_token,
            "email": "traveler@example.com",
            "password": "a better password 123",
        },
    ).status_code == 302


def test_deactivation_requires_password_and_prevents_future_login(app):
    client = app.test_client()
    _signup(client)
    token = _csrf_token(client, "/account")
    response = client.post(
        "/account/deactivate",
        data={
            "csrf_token": token,
            "password": "correct horse battery staple",
            "confirmation": "deactivate",
        },
    )
    assert response.status_code == 302
    assert client.get("/account").status_code == 302

    another_client = app.test_client()
    token = _csrf_token(another_client, "/login")
    response = another_client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "traveler@example.com",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 401


def test_admin_dashboard_route_and_multiple_media_uploads(app):
    with app.app_context():
        connection = get_db()
        cursor = connection.execute(
            """INSERT INTO admin_users (email, password_hash, name, role)
               VALUES (?, ?, ?, ?)""",
            (
                "admin@example.com",
                generate_password_hash("test administrator password"),
                "Test Admin",
                "super_admin",
            ),
        )
        connection.commit()
        admin_cookie = make_admin_cookie(cursor.lastrowid, "super_admin")

    client = app.test_client()
    client.set_cookie("admin_session", admin_cookie)
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert b'href="/admin/dashboard"' in dashboard.data

    token = _csrf_token(client, "/admin/media")
    first_image = io.BytesIO()
    Image.new("RGB", (12, 12), color="purple").save(first_image, format="PNG")
    first_image.seek(0)
    second_image = io.BytesIO()
    Image.new("RGB", (12, 12), color="orange").save(second_image, format="PNG")
    second_image.seek(0)
    response = client.post(
        "/admin/media/upload",
        data={
            "csrf_token": token,
            "files": [
                (first_image, "first.png"),
                (second_image, "second.png"),
            ],
        },
    )
    assert response.status_code == 302

    media_page = client.get("/admin/media")
    assert b"2 images uploaded" in media_page.data
    with app.app_context():
        assert query_one("SELECT COUNT(*) AS count FROM media_assets")["count"] == 2
        cloud_assets = get_db().execute(
            """SELECT url, storage_provider, cloudinary_public_id
               FROM media_assets ORDER BY id"""
        ).fetchall()
        assert all(asset["storage_provider"] == "cloudinary" for asset in cloud_assets)
        assert all(asset["url"].startswith("https://res.cloudinary.com/") for asset in cloud_assets)
        assert all(asset["cloudinary_public_id"] for asset in cloud_assets)
        assert not list(Path(app.config["UPLOADS_DIR"]).rglob("*"))
        inventory_columns = {
            row[1] for row in get_db().execute("PRAGMA table_info(trip_inventory)")
        }
    assert {"flight_status", "hotel_status", "flight_notice", "hotel_notice"} <= inventory_columns


def test_super_admin_can_create_admin_with_hashed_password(app):
    with app.app_context():
        connection = get_db()
        cursor = connection.execute(
            """INSERT INTO admin_users (email, password_hash, name, role)
               VALUES (?, ?, ?, ?)""",
            (
                "owner@example.com",
                generate_password_hash("test super administrator password"),
                "Owner",
                "super_admin",
            ),
        )
        connection.commit()
        admin_cookie = make_admin_cookie(cursor.lastrowid, "super_admin")

    client = app.test_client()
    client.set_cookie("admin_session", admin_cookie)
    token = _csrf_token(client, "/admin/admin-users")
    response = client.post(
        "/admin/admin-users",
        data={
            "csrf_token": token,
            "name": "New Admin",
            "email": "new-admin@example.com",
            "password": "temporary secure password",
            "role": "editor",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        created = query_one(
            "SELECT id, password_hash, role FROM admin_users WHERE lower(email) = ?",
            ("new-admin@example.com",),
        )
        assert created
        assert created["id"] > 0
        assert created["role"] == "editor"
        assert created["password_hash"] != "temporary secure password"
        assert check_password_hash(created["password_hash"], "temporary secure password")


def test_non_super_admin_cannot_view_create_or_change_admin_accounts(app):
    with app.app_context():
        connection = get_db()
        cursor = connection.execute(
            """INSERT INTO admin_users (email, password_hash, name, role)
               VALUES (?, ?, ?, ?)""",
            (
                "editor@example.com",
                generate_password_hash("test editor account password"),
                "Editor",
                "editor",
            ),
        )
        target = connection.execute(
            """INSERT INTO admin_users (email, password_hash, name, role)
               VALUES (?, ?, ?, ?)""",
            (
                "target@example.com",
                generate_password_hash("test target account password"),
                "Target",
                "admin",
            ),
        )
        connection.commit()
        editor_cookie = make_admin_cookie(cursor.lastrowid, "editor")
        target_id = target.lastrowid

    client = app.test_client()
    client.set_cookie("admin_session", editor_cookie)
    token = _csrf_token(client, "/login")

    assert client.get("/admin/admin-users").status_code == 403
    assert client.post(
        "/admin/admin-users",
        data={
            "csrf_token": token,
            "name": "Unauthorized",
            "email": "unauthorized@example.com",
            "password": "temporary secure password",
            "role": "admin",
        },
    ).status_code == 403
    assert client.post(
        f"/admin/admin-users/{target_id}/status",
        data={"csrf_token": token},
    ).status_code == 403

    with app.app_context():
        assert query_one(
            "SELECT id FROM admin_users WHERE lower(email) = ?",
            ("unauthorized@example.com",),
        ) is None
        target_admin = query_one(
            "SELECT is_active FROM admin_users WHERE id = ?",
            (target_id,),
        )
        assert target_admin["is_active"] == 1


def test_trip_planner_preserves_guest_form_and_generates_ai_and_inventory_results(
    app, monkeypatch
):
    client = app.test_client()
    _signup(client)
    logout_token = _csrf_token(client, "/account")
    assert client.post("/logout", data={"csrf_token": logout_token}).status_code == 302

    planner_page = client.get("/itinerary")
    assert b'href="/itinerary"' in planner_page.data
    assert b"AI TRIP PLANNER" in planner_page.data
    assert b"Generate with AI" in planner_page.data
    assert b'href="#flight-details"' in planner_page.data
    assert b"planner-ideas" in planner_page.data
    token = re.search(
        r'name="csrf_token" value="([^"]+)"',
        planner_page.get_data(as_text=True),
    ).group(1)
    trip_form = {
        "csrf_token": token,
        "destination": "Phuket",
        "origin": "BOM",
        "flight_destination": "HKT",
        "start_date": "2027-01-01",
        "end_date": "2027-01-02",
        "travelers": "2",
        "total_budget": "100000",
        "pace": "Relaxed",
        "interests": ["Nature", "Food & drink"],
        "notes": "Prefer quiet beaches",
    }
    login_redirect = client.post("/itinerary/results", data=trip_form)
    assert login_redirect.status_code == 302
    assert login_redirect.headers["Location"].startswith("/login?next=")
    login_page = client.get(login_redirect.headers["Location"])
    login_token = re.search(
        r'name="csrf_token" value="([^"]+)"',
        login_page.get_data(as_text=True),
    ).group(1)
    assert b'name="next" value="/itinerary"' in login_page.data

    login_response = client.post(
        "/login",
        data={
            "csrf_token": login_token,
            "next": "/itinerary",
            "email": "traveler@example.com",
            "password": "correct horse battery staple",
        },
    )
    assert login_response.status_code == 302
    assert login_response.headers["Location"] == "/itinerary"
    resumed_form = client.get("/itinerary")
    assert b'value="Phuket"' in resumed_form.data
    assert b'value="BOM"' in resumed_form.data
    assert b"Prefer quiet beaches" in resumed_form.data

    with app.app_context():
        connection = get_db()
        connection.execute(
            """INSERT INTO destinations (name, slug, city, country, is_published)
               VALUES (?, ?, ?, ?, 1)""",
            ("Phuket", "phuket", "Phuket", "Thailand"),
        )
        connection.commit()
    app.config["GEMINI_API_KEYS"] = ["test-gemini-key"]
    generated_data = {}

    def fake_inventory(*args, **kwargs):
        return {
            "status": "available",
            "provider": "SerpApi + Hotelbeds",
            "origin_code": "BOM",
            "destination_code": "HKT",
            "flight_status": "available",
            "hotel_status": "available",
            "flight_notice": "",
            "hotel_notice": "",
            "notice": "",
            "flights": [{
                "price": 25000,
                "currency": "INR",
                "stops": 0,
                "legs": [{
                    "airline": "Test Airways",
                    "from": "BOM",
                    "to": "HKT",
                    "departure": "2027-01-01 08:00",
                    "arrival": "2027-01-01 14:00",
                    "duration": "6 hours",
                }],
            }],
            "hotels": [{
                "name": "Test Hotel",
                "category": "4 stars",
                "room": "Standard room",
                "board": "Breakfast",
                "price": 12000,
                "currency": "INR",
                "check_in": "2027-01-01",
                "check_out": "2027-01-02",
                "image_url": "https://photos.hotelbeds.com/giata/bigger/test-hotel.jpg",
            }],
        }

    def fake_generate(data):
        generated_data.update(data)
        return [{
            "tier": tier,
            "summary": f"{tier.title()} Phuket plan",
            "estimated_total": 50000,
            "days": [{
                "title": "Beach and local food",
                "items": [{
                    "title": "Explore Phuket",
                    "category": "Nature",
                    "description": "Visit a quiet beach.",
                    "start_time": "09:00",
                    "duration_min": 60,
                    "est_cost": 1000,
                    "lat": 7.88,
                    "lng": 98.39,
                }],
            }],
        } for tier in ("luxury", "comfort", "budget")]

    monkeypatch.setattr(travel_inventory, "inventory_result", fake_inventory)
    monkeypatch.setattr(public, "generate_itineraries", fake_generate)
    token = _csrf_token(client, "/itinerary")
    trip_form["csrf_token"] = token
    response = client.post("/itinerary/results", data=trip_form)
    assert response.status_code == 302
    results = client.get(response.headers["Location"])
    assert results.status_code == 200
    assert b"Test Airways" in results.data
    assert b"Test Hotel" in results.data
    assert b"https://photos.hotelbeds.com/giata/bigger/test-hotel.jpg" in results.data
    assert b"Recommended for your trip" in results.data
    assert b"data-share-trip" in results.data
    assert generated_data["personal_notes"] == "Prefer quiet beaches"
    assert generated_data["travel_inventory"]["flights"][0]["price"] == 25000
    with app.app_context():
        saved_inventory = query_one(
            "SELECT flight_status, hotel_status FROM trip_inventory"
        )
    assert saved_inventory == {"flight_status": "available", "hotel_status": "available"}


def test_wallet_displays_balance_and_ledger_with_schema_generated_ids(
    app, monkeypatch
):
    monkeypatch.setattr(public, "enforce_rate_limit", lambda *args, **kwargs: None)
    client = app.test_client()
    _signup(client)
    with client.session_transaction() as user_session:
        user_id = user_session["user_id"]

    with app.app_context():
        connection = get_db()
        assert add_funds(connection, user_id, "125.50") == 125.50
        assert charge(connection, user_id, "25.50") == 100.00

    response = client.get("/wallet")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "₹100.00" in page
    assert "Wallet credit" in page
    assert "Booking payment" in page
    assert "INR 125.50" in page
    assert "−INR 25.50" in page
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
    assert client.post(
        "/wallet", data={"amount": "100", "csrf_token": token}
    ).status_code == 405
