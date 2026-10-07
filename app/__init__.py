from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session
from markupsafe import Markup, escape

from app.config import ROOT, load_config
from app.security import authenticated_user, csrf_token, validate_csrf


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    app = Flask(
        __name__,
        root_path=str(ROOT),
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
        instance_path=str(ROOT / "instance"),
    )
    app.config.update(load_config())
    if test_config:
        app.config.update(test_config)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "FLASK_SECRET_KEY is required. Add it to .env before starting the app."
        )
    has_database_url = bool(app.config.get("DATABASE_URL"))
    has_database_token = bool(app.config.get("DATABASE_AUTH_TOKEN"))
    if has_database_url != has_database_token:
        raise RuntimeError(
            "Configure both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or omit both to use local SQLite."
        )

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["IMAGES_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOADS_DIR"]).mkdir(parents=True, exist_ok=True)
    app.permanent_session_lifetime = timedelta(
        minutes=int(app.config["USER_SESSION_MINUTES"])
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(app.config["SESSION_COOKIE_SECURE"]),
    )

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

    from app.db import init_db

    init_db(app)
    with app.app_context():
        setting = app.extensions.get("planmytravel_settings")
        if setting is None:
            from app.db_utils import query_one

            setting = query_one(
                "SELECT setting_value FROM site_settings WHERE setting_key = 'app_name'"
            )
            app.extensions["planmytravel_settings"] = setting
        if setting and setting.get("setting_value"):
            app.config["APP_NAME"] = setting["setting_value"]

    @app.before_request
    def protect_state_changes() -> None:
        authenticated_user()
        validate_csrf()

    @app.context_processor
    def inject_shared_context() -> dict[str, object]:
        from app.db_utils import query_all

        current_user = authenticated_user()
        user_id = current_user["id"] if current_user else None
        site_settings = {
            row["setting_key"]: row["setting_value"]
            for row in query_all("SELECT setting_key, setting_value FROM site_settings")
        }

        def csrf_input() -> Markup:
            token = escape(csrf_token())
            return Markup(
                f'<input type="hidden" name="csrf_token" value="{token}">'
            )

        return {
            "app_name": app.config["APP_NAME"],
            "csrf_token": csrf_input,
            "csrf_value": csrf_token,
            "current_user_id": user_id,
            "current_user": current_user,
            "site_settings": site_settings,
            "current_year": datetime.now(timezone.utc).year,
        }

    @app.after_request
    def add_security_headers(response):
        if request.path.startswith("/admin"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.get("/images/<path:filename>", endpoint="images")
    def serve_image(filename: str):
        from app.services.media import cloudinary_image_url

        image_url = cloudinary_image_url(f"/images/{filename}")
        if image_url:
            return redirect(image_url, code=302)
        return send_from_directory(app.config["IMAGES_DIR"], filename)

    @app.get("/uploads/<path:filename>", endpoint="uploads")
    def serve_upload(filename: str):
        from app.services.media import cloudinary_image_url

        image_url = cloudinary_image_url(f"/uploads/{filename}")
        if image_url:
            return redirect(image_url, code=302)
        return send_from_directory(app.config["UPLOADS_DIR"], filename)

    @app.get("/robots.txt")
    def robots_txt():
        return "User-agent: *\nDisallow: /admin\n", 200, {"Content-Type": "text/plain"}

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(500)
    def handle_http_error(error):
        status = getattr(error, "code", 500)
        message = getattr(error, "description", "Something went wrong.")
        if request.path.startswith("/api/"):
            return jsonify({"error": {"message": message, "status": status}}), status
        return render_template("404.html" if status == 404 else "error.html", message=message), status

    from app.blueprints.admin import admin_bp
    from app.blueprints.admin_api import admin_api_bp
    from app.blueprints.api import api_bp
    from app.blueprints.public import public_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(admin_api_bp)

    return app
