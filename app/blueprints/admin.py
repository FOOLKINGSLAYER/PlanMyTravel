from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db
from app.db_utils import query_all, query_one
from app.security import admin_required, make_admin_cookie
from app.services.audit import record_admin_action
from app.services.media import (
    InvalidImageError,
    MediaStorageError,
    delete_cloudinary_image,
    resolve_image,
    store_image,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _current_admin():
    admin_id = getattr(g, "admin_id", None)
    if not admin_id:
        return None
    return query_one(
        "SELECT id, email, name, role FROM admin_users WHERE id = ?",
        (admin_id,),
    )


def _admin_render(template_name: str, **kwargs):
    kwargs.setdefault("admin", _current_admin())
    kwargs.setdefault("active_page", "dashboard")
    kwargs.setdefault("breadcrumb", "Workspace")
    kwargs.setdefault(
        "current_date",
        datetime.now(timezone.utc).strftime("%B %d, %Y").upper(),
    )
    kwargs.setdefault("stats", {
        "bookings": query_one("SELECT COUNT(*) AS count FROM bookings")["count"],
        "users": query_one("SELECT COUNT(*) AS count FROM users WHERE is_active = 1")["count"],
        "packages": query_one(
            "SELECT COUNT(*) AS count FROM packages WHERE status = 'published' AND deleted_at IS NULL"
        )["count"],
        "rating": query_one(
            "SELECT COALESCE(ROUND(AVG(rating), 1), 0) AS rating FROM reviews WHERE status = 'published'"
        )["rating"],
        "reviews": query_one("SELECT COUNT(*) AS count FROM reviews WHERE status = 'published'")["count"],
    })
    kwargs.setdefault("pending_bookings", query_one("SELECT COUNT(*) AS count FROM bookings WHERE status = 'pending'")["count"])
    return render_template(f"admin/{template_name}", **kwargs)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:120].rstrip("-")


def _unique_slug(table: str, value: str, fallback: str, exclude_id: int | None = None) -> str:
    if table not in {"destinations", "packages", "experiences"}:
        raise ValueError("Unsupported catalog table.")
    slug = _slugify(value) or _slugify(fallback)
    existing = query_one(
        f"SELECT id FROM {table} WHERE slug = ? AND (? IS NULL OR id != ?)",
        (slug, exclude_id, exclude_id),
    )
    if existing:
        raise ValueError("That URL slug is already in use.")
    return slug


def _list_field(value: str) -> list[str]:
    return [item.strip() for item in value.splitlines() if item.strip()]


def _json_list(value: str) -> str:
    return json.dumps(_list_field(value), ensure_ascii=False)


def _number_field(name: str, default: float | None = None) -> float | None:
    raw_value = request.form.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"Enter a valid number for {name.replace('_', ' ')}.") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Enter a valid non-negative number for {name.replace('_', ' ')}.")
    return value


def _admin_upload(upload, folder: str) -> dict | None:
    if not upload or not upload.filename:
        return None
    stored = store_image(upload, folder)
    relative_path = f"uploads/{stored['relative_path']}"
    connection = get_db()
    connection.execute(
        """INSERT INTO media_assets
           (folder, relative_path, path, url, filename, original_filename,
            mime_type, size_bytes, file_size, width, height, alt_text,
            storage_provider, cloudinary_public_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            folder,
            relative_path,
            relative_path,
            stored["url"],
            stored["relative_path"].split("/")[-1],
            stored["original_filename"],
            stored["mime_type"],
            stored["size_bytes"],
            stored["size_bytes"],
            stored["width"],
            stored["height"],
            stored["original_filename"],
            stored["storage_provider"],
            stored["cloudinary_public_id"],
        ),
    )
    asset = query_one("SELECT id FROM media_assets WHERE relative_path = ?", (relative_path,))
    if not asset:
        raise RuntimeError("The uploaded image could not be registered in the media library.")
    return {"id": asset["id"], "path": relative_path, "url": stored["url"]}


def _media_library(search: str = "") -> list[dict]:
    assets = query_all(
        """SELECT * FROM media_assets
           WHERE (? = '' OR original_filename LIKE ? OR filename LIKE ? OR folder LIKE ?)
           ORDER BY created_at DESC""",
        (search, f"%{search}%", f"%{search}%", f"%{search}%"),
    )
    for asset in assets:
        asset["url"] = resolve_image(asset.get("relative_path"))
        asset["name"] = asset.get("original_filename") or asset.get("filename")
        asset["size"] = (
            f"{(asset.get('size_bytes') or 0) / 1024:.0f} KB · "
            f"{asset.get('mime_type') or 'Image'}"
        )
    return assets


def _media_library_count() -> int:
    total_media = query_one("SELECT COUNT(*) AS count FROM media_assets")
    return int(total_media["count"]) if total_media else 0


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        admin = query_one(
            "SELECT id, email, password_hash, name, role, is_active FROM admin_users WHERE lower(email) = ? AND is_active = 1",
            (email,),
        )
        if admin and check_password_hash(admin["password_hash"], password):
            response = redirect(url_for("admin.dashboard"))
            response.set_cookie(
                "admin_session",
                make_admin_cookie(admin["id"], admin["role"]),
                httponly=True,
                secure=current_app.config.get("SESSION_COOKIE_SECURE", False),
                samesite="Lax",
            )
            return response
        return render_template("admin/login.html", error="Invalid email or password."), 401
    return render_template("admin/login.html")


@admin_bp.post("/logout")
@admin_required()
def logout():
    response = redirect(url_for("admin.login"))
    response.delete_cookie("admin_session", path="/")
    return response


@admin_bp.get("")
@admin_bp.get("/")
@admin_bp.get("/dashboard")
@admin_required()
def dashboard():
    recent = query_all(
        """SELECT COALESCE(b.contact_name, u.full_name, u.name, b.contact_email, 'Traveler') AS traveler,
                  COALESCE(p.title, d.name, 'Trip') AS trip,
                  COALESCE(b.booked_at, b.created_at) AS booking_date,
                  b.status, b.id
           FROM bookings b LEFT JOIN users u ON u.id = b.user_id
           LEFT JOIN packages p ON p.id = b.package_id
           LEFT JOIN trips t ON t.id = b.trip_id
           LEFT JOIN destinations d ON d.id = t.destination_id
           ORDER BY b.created_at DESC LIMIT 8"""
    )
    return _admin_render(
        "dashboard.html", active_page="dashboard", breadcrumb="Overview",
        rows=[{
            "cells": [row["traveler"], row["trip"], row["booking_date"], row["status"].title()],
            "url": url_for("admin.booking_update", booking_id=row["id"]),
            "editable": False,
        } for row in recent],
    )


@admin_bp.get("/packages")
@admin_required()
def packages():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = """SELECT packages.*, destinations.name AS destination_name
             FROM packages LEFT JOIN destinations
               ON destinations.id = packages.destination_id
             WHERE packages.deleted_at IS NULL"""
    params = []
    if search:
        sql += " AND (packages.title LIKE ? OR packages.description LIKE ? OR packages.slug LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    if status in {"draft", "published", "archived"}:
        sql += " AND packages.status = ?"
        params.append(status)
    sql += " ORDER BY packages.updated_at DESC, packages.created_at DESC"
    records = query_all(sql, params)
    rows = [{
        "cells": [
            package["title"],
            package.get("destination_name") or "—",
            f"{package['currency']} {float(package['price'] or 0):,.2f}",
            package["status"].title(),
            package["updated_at"],
        ],
        "url": url_for("admin.package_edit", package_id=package["id"]),
        "delete_url": url_for("admin.package_delete", package_id=package["id"]),
    } for package in records]
    return _admin_render(
        "packages.html",
        active_page="packages",
        breadcrumb="Workspace / Packages",
        packages=records,
        rows=rows,
        request=request,
    )


def _package_editor_context(package: dict | None, action_url: str, errors: dict | None = None):
    is_editing = bool(package and package.get("id"))
    if is_editing:
        package = {
            **package,
            "duration": package.get("duration") or package.get("duration_days") or "",
            "pricing": package.get("pricing") or package.get("price") or package.get("base_price") or "",
            "destination": package.get("destination") or package.get("destination_id") or "",
            "images": [
                {
                    **image,
                    "url": resolve_image(image["image_url"]),
                    "filename": image["image_url"],
                    "alt": image.get("alt_text") or package["title"],
                }
                for image in query_all(
                    "SELECT id, image_url, alt_text FROM package_images WHERE package_id = ? ORDER BY sort_order, id",
                    (package["id"],),
                )
            ],
        }
    return _admin_render(
        "package_editor.html",
        active_page="packages",
        breadcrumb="Workspace / Packages",
        package=package,
        destinations=query_all("SELECT id, name, slug FROM destinations ORDER BY name"),
        errors=errors,
        is_editing=is_editing,
        action_url=action_url,
    )


@admin_bp.route("/packages/new", methods=["GET", "POST"])
@admin_required()
def package_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        try:
            if not title or not description:
                raise ValueError("Enter a title and description.")
            slug = _unique_slug("packages", request.form.get("slug", "").strip(), title)
            duration = int(request.form.get("duration", "1"))
            if not 1 <= duration <= 365:
                raise ValueError("Duration must be between 1 and 365 days.")
            price = _number_field("pricing", 0) or 0
            destination_id = request.form.get("destination", type=int)
            status = request.form.get("status", "draft")
            if status not in {"draft", "published", "archived"}:
                raise ValueError("Choose a valid publishing status.")
            if not destination_id or not query_one("SELECT id FROM destinations WHERE id = ?", (destination_id,)):
                raise ValueError("Choose a valid destination.")
            uploads = [
                _admin_upload(image, "packages")
                for image in request.files.getlist("images")
                if image and image.filename
            ]
        except (ValueError, InvalidImageError, MediaStorageError) as exc:
            return _package_editor_context(
                {**request.form, "title": title, "description": description},
                url_for("admin.package_new"),
                {"form": str(exc)},
            ), 400
        connection = get_db()
        connection.execute(
            """
            INSERT INTO packages
            (destination_id, title, name, slug, category, travel_style, description,
             short_description, duration_days, base_price, price, currency,
             cover_image_url, status, is_published, is_featured)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                destination_id,
                title,
                title,
                slug,
                request.form.get("category", "tour").strip() or "tour",
                request.form.get("travel_style", "").strip() or None,
                description,
                description[:160],
                duration,
                price,
                price,
                request.form.get("currency", "SGD").strip().upper() or "SGD",
                uploads[0]["path"] if uploads else None,
                status,
                int(status == "published"),
                int(request.form.get("featured") == "on"),
            ),
        )
        package = query_one("SELECT id FROM packages WHERE slug = ?", (slug,))
        for index, upload in enumerate(uploads):
            connection.execute(
                """INSERT INTO package_images
                   (package_id, media_asset_id, image_url, alt_text, sort_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (package["id"], upload["id"], upload["path"], title, index),
            )
        connection.commit()
        record_admin_action("created", "package", str(package["id"]))
        return redirect(url_for("admin.packages"))
    return _package_editor_context(None, url_for("admin.package_new"))


@admin_bp.route("/packages/<int:package_id>/edit", methods=["GET", "POST"])
@admin_required()
def package_edit(package_id: int):
    package = query_one("SELECT * FROM packages WHERE id = ?", (package_id,))
    if not package:
        abort(404)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        try:
            if not title:
                raise ValueError("Title is required.")
            slug = _unique_slug("packages", request.form.get("slug", "").strip(), title, package_id)
            duration = int(request.form.get("duration", "1"))
            if not 1 <= duration <= 365:
                raise ValueError("Duration must be between 1 and 365 days.")
            price = _number_field("pricing", 0) or 0
            destination_id = request.form.get("destination", type=int)
            status = request.form.get("status", "draft")
            if status not in {"draft", "published", "archived"}:
                raise ValueError("Choose a valid publishing status.")
            if not destination_id or not query_one("SELECT id FROM destinations WHERE id = ?", (destination_id,)):
                raise ValueError("Choose a valid destination.")
            uploads = [
                _admin_upload(image, "packages")
                for image in request.files.getlist("images")
                if image and image.filename
            ]
        except (ValueError, InvalidImageError, MediaStorageError) as exc:
            return _package_editor_context(
                {**package, **request.form},
                url_for("admin.package_edit", package_id=package_id),
                {"form": str(exc)},
            ), 400
        connection = get_db()
        connection.execute(
            """UPDATE packages SET title = ?, name = ?, slug = ?, category = ?,
                      travel_style = ?, description = ?, short_description = ?,
                      destination_id = ?, duration_days = ?, base_price = ?, price = ?,
                      currency = ?, status = ?, is_published = ?, is_featured = ?,
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (
                title, title, slug, request.form.get("category", "tour").strip() or "tour",
                request.form.get("travel_style", "").strip() or None,
                request.form.get("description", "").strip(),
                request.form.get("description", "").strip()[:160],
                destination_id, duration, price, price,
                request.form.get("currency", "SGD").strip().upper() or "SGD",
                status, int(status == "published"), int(request.form.get("featured") == "on"),
                package_id,
            ),
        )
        for image_id in request.form.getlist("remove_images"):
            if image_id.isdigit():
                connection.execute(
                    "DELETE FROM package_images WHERE id = ? AND package_id = ?",
                    (int(image_id), package_id),
                )
        for index, upload in enumerate(uploads):
            connection.execute(
                """INSERT INTO package_images
                   (package_id, media_asset_id, image_url, alt_text, sort_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (package_id, upload["id"], upload["path"], title, index),
            )
        if uploads:
            connection.execute(
                "UPDATE packages SET cover_image_url = ? WHERE id = ?",
                (uploads[0]["path"], package_id),
            )
        else:
            first_image = query_one(
                "SELECT image_url FROM package_images WHERE package_id = ? ORDER BY sort_order, id LIMIT 1",
                (package_id,),
            )
            if first_image:
                connection.execute(
                    "UPDATE packages SET cover_image_url = ? WHERE id = ?",
                    (first_image["image_url"], package_id),
                )
        connection.commit()
        record_admin_action("updated", "package", str(package_id))
        return redirect(url_for("admin.packages"))
    return _package_editor_context(package, url_for("admin.package_edit", package_id=package_id))


@admin_bp.post("/packages/<int:package_id>/delete")
@admin_required()
def package_delete(package_id: int):
    cursor = get_db().execute(
        "UPDATE packages SET status = 'archived', is_published = 0, deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
        (package_id,),
    )
    if cursor.rowcount != 1:
        abort(404)
    get_db().commit()
    record_admin_action("archived", "package", str(package_id))
    return redirect(url_for("admin.packages"))


@admin_bp.get("/experiences")
@admin_required()
def experiences():
    search = request.args.get("q", "").strip()
    records = query_all(
        """SELECT e.*, d.name AS destination_name
           FROM experiences e LEFT JOIN destinations d ON d.id = e.destination_id
           WHERE (? = '' OR e.title LIKE ? OR e.description LIKE ?)
           ORDER BY e.updated_at DESC, e.title""",
        (search, f"%{search}%", f"%{search}%"),
    )
    rows = [{
        "cells": [
            item["title"], item.get("destination_name") or "—",
            f"{item['currency']} {float(item['price'] or 0):,.2f}",
            item["status"].title(), item["updated_at"],
        ],
        "url": url_for("admin.experience_edit", experience_id=item["id"]),
        "delete_url": url_for("admin.experience_delete", experience_id=item["id"]),
    } for item in records]
    return _admin_render(
        "experiences.html", active_page="experiences",
        breadcrumb="Workspace / Experiences", rows=rows,
    )


def _experience_editor_context(experience: dict | None, action_url: str, errors: dict | None = None):
    is_editing = bool(experience and experience.get("id"))
    if is_editing:
        experience = {
            **experience,
            "cover_image": resolve_image(experience.get("cover_image_url")),
        }
    return _admin_render(
        "experience_editor.html", active_page="experiences",
        breadcrumb="Workspace / Experiences", experience=experience,
        destinations=query_all("SELECT id, name FROM destinations ORDER BY name"),
        action_url=action_url, errors=errors, is_editing=is_editing,
    )


def _save_experience(experience_id: int | None = None):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        if not title or not description:
            raise ValueError("Enter a title and description.")
        slug = _unique_slug(
            "experiences", request.form.get("slug", "").strip(), title, experience_id
        )
        price = _number_field("price", 0) or 0
        status = request.form.get("status", "draft")
        if status not in {"draft", "published", "archived"}:
            raise ValueError("Choose a valid publishing status.")
        destination_id = request.form.get("destination", type=int)
        if destination_id and not query_one(
            "SELECT id FROM destinations WHERE id = ?", (destination_id,)
        ):
            raise ValueError("Choose a valid destination.")
        image = _admin_upload(request.files.get("cover_image"), "experiences")
    except (ValueError, InvalidImageError, MediaStorageError) as exc:
        return None, str(exc)
    values = (
        destination_id, title, slug,
        request.form.get("category", "experience").strip() or "experience",
        request.form.get("travel_style", "").strip() or None,
        description, price,
        request.form.get("currency", "SGD").strip().upper() or "SGD",
        image["path"] if image else None, status, int(status == "published"),
    )
    connection = get_db()
    if experience_id is None:
        connection.execute(
            """INSERT INTO experiences
               (destination_id, title, slug, category, travel_style, description,
                price, currency, cover_image_url, status, is_published)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        record = query_one("SELECT id FROM experiences WHERE slug = ?", (slug,))
        saved_id = record["id"]
    else:
        old = query_one("SELECT cover_image_url FROM experiences WHERE id = ?", (experience_id,))
        connection.execute(
            """UPDATE experiences SET destination_id = ?, title = ?, slug = ?,
                      category = ?, travel_style = ?, description = ?, price = ?,
                      currency = ?, cover_image_url = ?, status = ?, is_published = ?,
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (*values[:8], image["path"] if image else old["cover_image_url"], values[9], values[10], experience_id),
        )
        saved_id = experience_id
    connection.commit()
    record_admin_action("created" if experience_id is None else "updated", "experience", str(saved_id))
    return saved_id, None


@admin_bp.route("/experiences/new", methods=["GET", "POST"])
@admin_required()
def experience_new():
    action_url = url_for("admin.experience_new")
    if request.method == "POST":
        saved_id, error = _save_experience()
        if error:
            return _experience_editor_context(
                dict(request.form), action_url, {"form": error}
            ), 400
        return redirect(url_for("admin.experiences"))
    return _experience_editor_context(None, action_url)


@admin_bp.route("/experiences/<int:experience_id>/edit", methods=["GET", "POST"])
@admin_required()
def experience_edit(experience_id: int):
    experience = query_one("SELECT * FROM experiences WHERE id = ?", (experience_id,))
    if not experience:
        abort(404)
    action_url = url_for("admin.experience_edit", experience_id=experience_id)
    if request.method == "POST":
        saved_id, error = _save_experience(experience_id)
        if error:
            return _experience_editor_context(
                {**experience, **request.form}, action_url, {"form": error}
            ), 400
        return redirect(url_for("admin.experiences"))
    return _experience_editor_context(experience, action_url)


@admin_bp.post("/experiences/<int:experience_id>/delete")
@admin_required()
def experience_delete(experience_id: int):
    cursor = get_db().execute(
        "UPDATE experiences SET status = 'archived', is_published = 0 WHERE id = ?",
        (experience_id,),
    )
    if cursor.rowcount != 1:
        abort(404)
    get_db().commit()
    record_admin_action("archived", "experience", str(experience_id))
    return redirect(url_for("admin.experiences"))


@admin_bp.get("/destinations")
@admin_required()
def destinations():
    search = request.args.get("q", "").strip()
    records = query_all(
        """SELECT d.*, COUNT(DISTINCT p.id) AS package_count
           FROM destinations d LEFT JOIN packages p ON p.destination_id = d.id
           WHERE (? = '' OR d.name LIKE ? OR d.country LIKE ?)
           GROUP BY d.id ORDER BY d.updated_at DESC, d.name""",
        (search, f"%{search}%", f"%{search}%"),
    )
    rows = [{
        "cells": [
            row["name"], row["package_count"], "Yes" if row["is_featured"] else "No",
            "Published" if row["is_published"] else "Draft", row["updated_at"],
        ],
        "url": url_for("admin.destination_edit", destination_id=row["id"]),
        "delete_url": url_for("admin.destination_delete", destination_id=row["id"]),
    } for row in records]
    return _admin_render(
        "destinations.html", active_page="destinations",
        breadcrumb="Workspace / Destinations", rows=rows,
    )


def _destination_editor_context(destination: dict | None, action_url: str, errors: dict | None = None):
    is_editing = bool(destination and destination.get("id"))
    if is_editing:
        images = query_all(
            """SELECT id, image_url, alt_text FROM destination_images
               WHERE destination_id = ? ORDER BY sort_order, id""",
            (destination["id"],),
        )
        destination = {
            **destination,
            "published": bool(destination.get("is_published")),
            "hero_image": resolve_image(destination.get("hero_image_url")),
            "travel_styles": destination.get("travel_styles") or "\n".join(_json_values(destination.get("travel_styles_json"))),
            "attractions": destination.get("attractions") or "\n".join(_json_values(destination.get("attractions_json"))),
            "activities": destination.get("activities") or "\n".join(_json_values(destination.get("activities_json"))),
            "images": [
                {**image, "url": resolve_image(image["image_url"])} for image in images
            ],
        }
    return _admin_render(
        "destination_editor.html",
        active_page="destinations",
        breadcrumb="Workspace / Destinations",
        destination=destination,
        errors=errors,
        is_editing=is_editing,
        action_url=action_url,
    )


def _json_values(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        items = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in items] if isinstance(items, list) else []


@admin_bp.route("/destinations/new", methods=["GET", "POST"])
@admin_required()
def destination_new():
    if request.method != "POST":
        return _destination_editor_context(None, url_for("admin.destination_new"))
    name = request.form.get("name", "").strip()
    try:
        if not name or len(name) > 120:
            raise ValueError("Enter a destination name up to 120 characters.")
        description = request.form.get("description", "").strip()
        if not description:
            raise ValueError("Enter a destination description.")
        slug = _unique_slug("destinations", request.form.get("slug", "").strip(), name)
        low_cost = _number_field("estimated_cost_min")
        high_cost = _number_field("estimated_cost_max")
        if low_cost is not None and high_cost is not None and high_cost < low_cost:
            raise ValueError("Maximum estimated cost must be at least the minimum.")
        hero = _admin_upload(request.files.get("hero_image"), "destinations")
        gallery = [
            _admin_upload(image, "destinations")
            for image in request.files.getlist("gallery_images")
            if image and image.filename
        ]
    except (ValueError, InvalidImageError, MediaStorageError) as exc:
        return _destination_editor_context(
            {**request.form, "name": name, "description": description},
            url_for("admin.destination_new"),
            {"form": str(exc)},
        ), 400
    status = request.form.get("published") == "true"
    connection = get_db()
    connection.execute(
        """INSERT INTO destinations
           (name, slug, country, region, city, description, short_description, tagline,
            category, travel_styles_json, attractions_json, activities_json,
            estimated_cost_min, estimated_cost_max, cost_currency, best_season, weather_info, ideal_stay,
            hero_image_url, is_featured, is_published)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, slug, request.form.get("country", "").strip() or None,
            request.form.get("region", "").strip() or None,
            request.form.get("city", "").strip() or None, description,
            request.form.get("short_description", "").strip() or description[:180],
            request.form.get("tagline", "").strip() or None,
            request.form.get("category", "").strip() or None,
            _json_list(request.form.get("travel_styles", "")),
            _json_list(request.form.get("attractions", "")),
            _json_list(request.form.get("activities", "")),
            low_cost, high_cost,
            request.form.get("cost_currency", "SGD").strip().upper() or "SGD",
            request.form.get("best_season", "").strip() or None,
            request.form.get("weather_info", "").strip() or None,
            request.form.get("ideal_stay", "").strip() or None,
            hero["path"] if hero else None,
            int(request.form.get("featured") == "on"), int(status),
        ),
    )
    destination = query_one("SELECT id FROM destinations WHERE slug = ?", (slug,))
    images = ([hero] if hero else []) + gallery
    for index, image in enumerate(images):
        connection.execute(
            """INSERT INTO destination_images
               (destination_id, media_asset_id, image_url, alt_text, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (destination["id"], image["id"], image["path"], name, index),
        )
    connection.commit()
    record_admin_action("created", "destination", str(destination["id"]))
    return redirect(url_for("admin.destinations"))


@admin_bp.route("/destinations/<int:destination_id>/edit", methods=["GET", "POST"])
@admin_required()
def destination_edit(destination_id: int):
    destination = query_one("SELECT * FROM destinations WHERE id = ?", (destination_id,))
    if not destination:
        abort(404)
    action_url = url_for("admin.destination_edit", destination_id=destination_id)
    if request.method != "POST":
        return _destination_editor_context(destination, action_url)
    name = request.form.get("name", "").strip()
    try:
        if not name or len(name) > 120:
            raise ValueError("Enter a destination name up to 120 characters.")
        description = request.form.get("description", "").strip()
        if not description:
            raise ValueError("Enter a destination description.")
        slug = _unique_slug("destinations", request.form.get("slug", "").strip(), name, destination_id)
        low_cost = _number_field("estimated_cost_min")
        high_cost = _number_field("estimated_cost_max")
        if low_cost is not None and high_cost is not None and high_cost < low_cost:
            raise ValueError("Maximum estimated cost must be at least the minimum.")
        hero = _admin_upload(request.files.get("hero_image"), "destinations")
        gallery = [
            _admin_upload(image, "destinations")
            for image in request.files.getlist("gallery_images")
            if image and image.filename
        ]
    except (ValueError, InvalidImageError, MediaStorageError) as exc:
        return _destination_editor_context(
            {**destination, **request.form, "name": name, "description": description},
            action_url,
            {"form": str(exc)},
        ), 400
    hero_path = hero["path"] if hero else destination.get("hero_image_url")
    connection = get_db()
    connection.execute(
        """UPDATE destinations SET name = ?, slug = ?, country = ?, region = ?, city = ?,
                  description = ?, short_description = ?, tagline = ?, category = ?,
                  travel_styles_json = ?, attractions_json = ?, activities_json = ?,
                  estimated_cost_min = ?, estimated_cost_max = ?, cost_currency = ?, best_season = ?,
                  weather_info = ?, ideal_stay = ?, hero_image_url = ?, is_featured = ?,
                  is_published = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (
            name, slug, request.form.get("country", "").strip() or None,
            request.form.get("region", "").strip() or None,
            request.form.get("city", "").strip() or None, description,
            request.form.get("short_description", "").strip() or description[:180],
            request.form.get("tagline", "").strip() or None,
            request.form.get("category", "").strip() or None,
            _json_list(request.form.get("travel_styles", "")),
            _json_list(request.form.get("attractions", "")),
            _json_list(request.form.get("activities", "")),
            low_cost, high_cost,
            request.form.get("cost_currency", "SGD").strip().upper() or "SGD",
            request.form.get("best_season", "").strip() or None,
            request.form.get("weather_info", "").strip() or None,
            request.form.get("ideal_stay", "").strip() or None, hero_path,
            int(request.form.get("featured") == "on"),
            int(request.form.get("published") == "true"), destination_id,
        ),
    )
    for image_id in request.form.getlist("remove_images"):
        if image_id.isdigit():
            connection.execute(
                "DELETE FROM destination_images WHERE id = ? AND destination_id = ?",
                (int(image_id), destination_id),
            )
    new_images = ([hero] if hero else []) + gallery
    for index, image in enumerate(new_images):
        connection.execute(
            """INSERT INTO destination_images
               (destination_id, media_asset_id, image_url, alt_text, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (destination_id, image["id"], image["path"], name, index),
        )
    connection.commit()
    record_admin_action("updated", "destination", str(destination_id))
    return redirect(url_for("admin.destinations"))


@admin_bp.post("/destinations/<int:destination_id>/delete")
@admin_required()
def destination_delete(destination_id: int):
    cursor = get_db().execute(
        "UPDATE destinations SET is_published = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (destination_id,),
    )
    if cursor.rowcount != 1:
        abort(404)
    get_db().commit()
    record_admin_action("unpublished", "destination", str(destination_id))
    return redirect(url_for("admin.destinations"))


@admin_bp.get("/media")
@admin_required()
def media():
    search = request.args.get("q", "").strip()
    return _admin_render(
        "media.html", active_page="media", breadcrumb="Workspace / Media",
        media=_media_library(search),
        total_media_count=_media_library_count(),
    )


@admin_bp.post("/media/upload")
@admin_required()
def media_upload():
    files = [item for item in request.files.getlist("files") if item and item.filename]
    if not files:
        abort(400, description="Choose at least one image to upload.")
    try:
        for upload in files:
            _admin_upload(upload, "library")
    except (InvalidImageError, MediaStorageError) as exc:
        return _admin_render(
            "media.html",
            active_page="media",
            breadcrumb="Workspace / Media",
            media=_media_library(request.args.get("q", "").strip()),
            total_media_count=_media_library_count(),
            error=str(exc),
        ), 400
    record_admin_action("uploaded", "media", str(len(files)))
    return redirect(url_for("admin.media"))


@admin_bp.post("/media/<int:asset_id>/delete")
@admin_required()
def media_delete(asset_id: int):
    asset = query_one(
        """SELECT relative_path, storage_provider, cloudinary_public_id
           FROM media_assets WHERE id = ?""",
        (asset_id,),
    )
    if not asset:
        abort(404)
    connection = get_db()
    path = asset.get("relative_path", "")
    reference_count = query_one(
        """SELECT
             (SELECT COUNT(*) FROM destination_images WHERE media_asset_id = ?) +
             (SELECT COUNT(*) FROM package_images WHERE media_asset_id = ?) +
             (SELECT COUNT(*) FROM review_photos WHERE media_asset_id = ?) +
             (SELECT COUNT(*) FROM destinations WHERE hero_image_url = ?) +
             (SELECT COUNT(*) FROM packages WHERE cover_image_url = ?) +
             (SELECT COUNT(*) FROM experiences WHERE cover_image_url = ?) +
             (SELECT COUNT(*) FROM guides WHERE photo_url = ?) AS count""",
        (asset_id, asset_id, asset_id, path, path, path, path),
    )
    if reference_count and reference_count["count"]:
        abort(409, description="This image is in use. Remove it from its catalog items first.")
    if asset.get("storage_provider") == "cloudinary":
        try:
            delete_cloudinary_image(asset["cloudinary_public_id"])
        except MediaStorageError as exc:
            return _admin_render(
                "media.html",
                active_page="media",
                breadcrumb="Workspace / Media",
                media=_media_library(request.args.get("q", "").strip()),
                total_media_count=_media_library_count(),
                error=str(exc),
            ), 502
    connection.execute("DELETE FROM destination_images WHERE media_asset_id = ?", (asset_id,))
    connection.execute("DELETE FROM package_images WHERE media_asset_id = ?", (asset_id,))
    connection.execute("DELETE FROM review_photos WHERE media_asset_id = ?", (asset_id,))
    connection.execute("DELETE FROM media_assets WHERE id = ?", (asset_id,))
    connection.commit()
    relative_path = path
    if relative_path.startswith("uploads/"):
        from pathlib import Path

        root = Path(current_app.config["UPLOADS_DIR"]).resolve()
        file_path = (root / relative_path.removeprefix("uploads/")).resolve()
        if root in file_path.parents and file_path.is_file():
            file_path.unlink()
    record_admin_action("deleted", "media", str(asset_id))
    return redirect(url_for("admin.media"))


@admin_bp.get("/guides")
@admin_required()
def guides():
    search = request.args.get("q", "").strip()
    records = query_all(
        """SELECT g.*, COALESCE(u.country, '') AS location
           FROM guides g LEFT JOIN users u ON u.id = g.user_id
           WHERE (? = '' OR g.display_name LIKE ? OR g.bio LIKE ? OR g.specialties_json LIKE ?)
           ORDER BY g.updated_at DESC, g.display_name""",
        (search, f"%{search}%", f"%{search}%", f"%{search}%"),
    )
    rows = [{
        "cells": [
            guide["display_name"], guide["location"] or "—", guide["rating"],
            "Active" if guide["is_active"] else "Inactive", guide["created_at"],
        ],
        "url": url_for("admin.guide_edit", guide_id=guide["id"]),
    } for guide in records]
    return _admin_render(
        "guides.html", active_page="guides", breadcrumb="Workspace / Guides",
        rows=rows,
    )


def _guide_editor_context(guide: dict | None, action_url: str, errors: dict | None = None):
    is_editing = bool(guide and guide.get("id"))
    if is_editing:
        guide = {
            **guide,
            "languages": guide.get("languages") or "\n".join(_json_values(guide.get("languages_json"))),
            "specialties": guide.get("specialties") or "\n".join(_json_values(guide.get("specialties_json"))),
            "photo": resolve_image(guide.get("photo_url")),
        }
    return _admin_render(
        "guide_editor.html", active_page="guides", breadcrumb="Workspace / Guides",
        guide=guide, action_url=action_url, errors=errors, is_editing=is_editing,
    )


@admin_bp.route("/guides/new", methods=["GET", "POST"])
@admin_required()
def guide_new():
    if request.method != "POST":
        return _guide_editor_context(None, url_for("admin.guide_new"))
    name = request.form.get("display_name", "").strip()
    bio = request.form.get("bio", "").strip()
    try:
        if not name or len(name) > 120 or not bio:
            raise ValueError("Enter a guide name and biography.")
        photo = _admin_upload(request.files.get("photo"), "guides")
    except (ValueError, InvalidImageError, MediaStorageError) as exc:
        return _guide_editor_context(
            {**request.form, "display_name": name, "bio": bio},
            url_for("admin.guide_new"), {"form": str(exc)},
        ), 400
    connection = get_db()
    connection.execute(
        """INSERT INTO guides
           (display_name, bio, languages_json, specialties_json, photo_url,
            is_verified, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            name, bio, _json_list(request.form.get("languages", "")),
            _json_list(request.form.get("specialties", "")),
            photo["path"] if photo else None,
            int(request.form.get("verified") == "on"),
            int(request.form.get("active") == "on"),
        ),
    )
    guide = query_one("SELECT id FROM guides WHERE display_name = ? ORDER BY id DESC LIMIT 1", (name,))
    connection.commit()
    record_admin_action("created", "guide", str(guide["id"]))
    return redirect(url_for("admin.guides"))


@admin_bp.route("/guides/<int:guide_id>/edit", methods=["GET", "POST"])
@admin_required()
def guide_edit(guide_id: int):
    guide = query_one("SELECT * FROM guides WHERE id = ?", (guide_id,))
    if not guide:
        abort(404)
    action_url = url_for("admin.guide_edit", guide_id=guide_id)
    if request.method != "POST":
        return _guide_editor_context(guide, action_url)
    name = request.form.get("display_name", "").strip()
    bio = request.form.get("bio", "").strip()
    try:
        if not name or len(name) > 120 or not bio:
            raise ValueError("Enter a guide name and biography.")
        photo = _admin_upload(request.files.get("photo"), "guides")
    except (ValueError, InvalidImageError, MediaStorageError) as exc:
        return _guide_editor_context(
            {**guide, **request.form, "display_name": name, "bio": bio},
            action_url, {"form": str(exc)},
        ), 400
    get_db().execute(
        """UPDATE guides SET display_name = ?, bio = ?, languages_json = ?,
                  specialties_json = ?, photo_url = ?, is_verified = ?,
                  is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (
            name, bio, _json_list(request.form.get("languages", "")),
            _json_list(request.form.get("specialties", "")),
            photo["path"] if photo else guide.get("photo_url"),
            int(request.form.get("verified") == "on"),
            int(request.form.get("active") == "on"), guide_id,
        ),
    )
    get_db().commit()
    record_admin_action("updated", "guide", str(guide_id))
    return redirect(url_for("admin.guides"))


@admin_bp.get("/reviews")
@admin_required()
def reviews():
    status = request.args.get("status", "").strip()
    rating = request.args.get("rating", type=int)
    where = ["1 = 1"]
    params = []
    if status in {"pending", "published", "hidden"}:
        where.append("r.status = ?")
        params.append(status)
    if rating in {1, 2, 3, 4, 5}:
        where.append("r.rating = ?")
        params.append(rating)
    records = query_all(
        f"""SELECT r.*, COALESCE(u.full_name, u.name, '') AS reviewer,
                   COALESCE(d.name, p.title, '—') AS subject
            FROM reviews r LEFT JOIN users u ON u.id = r.user_id
            LEFT JOIN destinations d ON d.id = r.destination_id
            LEFT JOIN packages p ON p.id = r.package_id
            WHERE {' AND '.join(where)} ORDER BY r.created_at DESC LIMIT 200""",
        params,
    )
    return _admin_render(
        "reviews.html", active_page="reviews", breadcrumb="Workspace / Reviews",
        reviews=records,
    )


@admin_bp.post("/reviews/<int:review_id>/status")
@admin_required()
def review_update(review_id: int):
    status = request.form.get("status")
    if status not in {"pending", "published", "hidden"}:
        abort(400, description="Choose a valid review status.")
    cursor = get_db().execute(
        "UPDATE reviews SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, review_id),
    )
    if cursor.rowcount != 1:
        abort(404)
    get_db().commit()
    record_admin_action("moderated", "review", str(review_id), {"status": status})
    return redirect(url_for("admin.reviews"))


@admin_bp.get("/bookings")
@admin_required()
def bookings():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    records = query_all(
        """SELECT b.*, COALESCE(b.contact_name, u.full_name, u.name, 'Traveler') AS traveler,
                  COALESCE(b.contact_email, u.email, '') AS email,
                  COALESCE(p.title, d.name, 'Trip') AS trip,
                  COALESCE(b.booked_at, b.created_at) AS travel_date
           FROM bookings b LEFT JOIN users u ON u.id = b.user_id
           LEFT JOIN packages p ON p.id = b.package_id
           LEFT JOIN trips t ON t.id = b.trip_id
           LEFT JOIN destinations d ON d.id = t.destination_id
           WHERE (? = '' OR b.status = ?)
             AND (? = '' OR b.booking_reference LIKE ? OR b.contact_name LIKE ?
                  OR u.full_name LIKE ? OR u.email LIKE ?)
           ORDER BY b.created_at DESC LIMIT 300""",
        (status, status, search, f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"),
    )
    return _admin_render(
        "bookings.html", active_page="bookings", breadcrumb="Workspace / Bookings",
        bookings=records,
    )


@admin_bp.post("/bookings/<int:booking_id>/status")
@admin_required()
def booking_update(booking_id: int):
    status = request.form.get("status")
    if status not in {"pending", "confirmed", "cancelled", "completed"}:
        abort(400, description="Choose a valid booking status.")
    cursor = get_db().execute(
        "UPDATE bookings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, booking_id),
    )
    if cursor.rowcount != 1:
        abort(404)
    get_db().commit()
    record_admin_action("updated", "booking", str(booking_id), {"status": status})
    return redirect(url_for("admin.bookings"))


@admin_bp.get("/users")
@admin_required()
def users():
    search = request.args.get("q", "").strip()
    records = query_all(
        """SELECT u.id, u.name, u.full_name, u.email, u.created_at, u.is_active,
                  (SELECT COUNT(*) FROM trips t WHERE t.user_id = u.id) AS trip_count
           FROM users u WHERE (? = '' OR u.name LIKE ? OR u.full_name LIKE ? OR u.email LIKE ?)
           ORDER BY u.created_at DESC LIMIT 500""",
        (search, f"%{search}%", f"%{search}%", f"%{search}%"),
    )
    rows = [{
        "cells": [
            row.get("full_name") or row.get("name") or "Traveler", row["email"],
            row["trip_count"], row["created_at"],
            "Active" if row["is_active"] else "Deactivated",
        ],
        "url": url_for("admin.users"),
        "editable": False,
        "delete_url": url_for("admin.user_status", user_id=row["id"]),
        "action_label": "Deactivate" if row["is_active"] else "Reactivate",
        "confirm": "Change this traveler account's access?",
    } for row in records]
    return _admin_render(
        "users.html", active_page="users", breadcrumb="Workspace / Travelers",
        rows=rows,
    )


@admin_bp.post("/users/<int:user_id>/status")
@admin_required()
def user_status(user_id: int):
    user = query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
    if not user:
        abort(404)
    new_status = 0 if user["is_active"] else 1
    get_db().execute(
        """UPDATE users SET is_active = ?, session_version = session_version + 1,
                  updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (new_status, user_id),
    )
    get_db().commit()
    record_admin_action("activated" if new_status else "deactivated", "user", str(user_id))
    return redirect(url_for("admin.users"))


@admin_bp.route("/featured", methods=["GET", "POST"])
@admin_required()
def featured():
    connection = get_db()
    if request.method == "POST":
        package_ids = request.form.getlist("featured_packages")
        if len(package_ids) > 3 or any(not value.isdigit() for value in package_ids):
            abort(400, description="Select no more than three valid packages.")
        connection.execute("UPDATE packages SET is_featured = 0")
        for package_id in package_ids:
            connection.execute(
                """UPDATE packages SET is_featured = 1
                   WHERE id = ? AND status = 'published' AND deleted_at IS NULL""",
                (int(package_id),),
            )
        for key in ("hero_eyebrow", "hero_title", "hero_text"):
            value = request.form.get(key, "").strip()
            if len(value) > 1000:
                abort(400, description="Featured homepage copy is too long.")
            connection.execute(
                """INSERT INTO site_settings (setting_key, setting_value)
                   VALUES (?, ?) ON CONFLICT(setting_key) DO UPDATE SET
                   setting_value = excluded.setting_value, updated_at = CURRENT_TIMESTAMP""",
                (key, value),
            )
        connection.commit()
        record_admin_action("updated", "featured-content")
        return redirect(url_for("admin.featured"))
    settings = {
        row["setting_key"]: row["setting_value"]
        for row in query_all("SELECT setting_key, setting_value FROM site_settings")
    }
    packages = query_all(
        """SELECT p.*, d.name AS destination_name FROM packages p
           LEFT JOIN destinations d ON d.id = p.destination_id
           WHERE p.status = 'published' AND p.deleted_at IS NULL
           ORDER BY p.is_featured DESC, p.title"""
    )
    for package in packages:
        package["image"] = resolve_image(package.get("cover_image_url"))
        package["featured"] = bool(package["is_featured"])
        package["location"] = package.get("destination_name") or "Travel"
    return _admin_render(
        "featured.html", active_page="featured", breadcrumb="Content / Featured content",
        featured=settings, packages=packages,
    )


@admin_bp.get("/admin-users")
@admin_required("super_admin")
def admin_users():
    records = query_all(
        "SELECT id, name, email, role, last_login_at, is_active FROM admin_users ORDER BY name, email"
    )
    rows = [{
        "cells": [
            item["name"] or "Admin", item["email"], item["role"],
            item["last_login_at"] or "Never",
            "Active" if item["is_active"] else "Inactive",
        ],
        "url": url_for("admin.admin_users"),
        "editable": False,
        "delete_url": url_for("admin.admin_user_status", admin_id=item["id"]),
        "action_label": "Deactivate" if item["is_active"] else "Reactivate",
        "confirm": "Change this admin account's access?",
    } for item in records]
    return _admin_render(
        "admin_users.html", active_page="admins", breadcrumb="Workspace / Admins",
        rows=rows,
    )


@admin_bp.post("/admin-users")
@admin_required("super_admin")
def admin_user_create():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "admin")
    if not name or len(name) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        abort(400, description="Enter a name and valid email address.")
    if not 12 <= len(password) <= 256:
        abort(400, description="The temporary password must be at least 12 characters.")
    if role not in {"admin", "editor", "support"}:
        abort(400, description="Choose a valid admin role.")
    connection = get_db()
    cursor = connection.execute(
        """INSERT INTO admin_users (email, password_hash, name, role)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO NOTHING""",
        (email, generate_password_hash(password), name, role),
    )
    inserted = connection.execute("SELECT changes()").fetchone()[0]
    if not inserted:
        connection.rollback()
        abort(409, description="An admin account already uses that email.")
    admin_id = cursor.lastrowid
    connection.commit()
    record_admin_action(
        "created",
        "admin-user",
        str(admin_id),
        {"name": name, "email": email, "role": role},
    )
    return redirect(url_for("admin.admin_users"))


@admin_bp.post("/admin-users/<int:admin_id>/status")
@admin_required("super_admin")
def admin_user_status(admin_id: int):
    admin = query_one("SELECT is_active FROM admin_users WHERE id = ?", (admin_id,))
    if not admin:
        abort(404)
    new_status = 0 if admin["is_active"] else 1
    get_db().execute(
        "UPDATE admin_users SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_status, admin_id),
    )
    get_db().commit()
    record_admin_action("activated" if new_status else "deactivated", "admin-user", str(admin_id))
    return redirect(url_for("admin.admin_users"))


@admin_bp.get("/audit")
@admin_required()
def audit():
    search = request.args.get("q", "").strip()
    records = query_all(
        """SELECT l.*, COALESCE(a.name, a.email, 'Former admin') AS admin_name
           FROM admin_audit_logs l LEFT JOIN admin_users a
             ON a.id = COALESCE(l.admin_id, l.admin_user_id)
           WHERE (? = '' OR l.action LIKE ? OR l.entity_type LIKE ? OR a.name LIKE ?)
           ORDER BY l.created_at DESC LIMIT 300""",
        (search, f"%{search}%", f"%{search}%", f"%{search}%"),
    )
    rows = [{
        "cells": [
            f"{item['action']} {item.get('entity_type') or ''} {item.get('entity_id') or ''}".strip(),
            item["admin_name"], item.get("entity_type") or "—", item["created_at"],
            item.get("ip") or "—",
        ],
        "url": url_for("admin.audit"),
    } for item in records]
    return _admin_render(
        "audit.html", active_page="audit", breadcrumb="Workspace / Audit", rows=rows,
    )


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required()
def settings():
    connection = get_db()
    if request.method == "POST":
        values = {
            "app_name": request.form.get("app_name", "").strip(),
            "support_email": request.form.get("support_email", "").strip(),
            "description": request.form.get("description", "").strip(),
            "notify_bookings": "1" if request.form.get("notify_bookings") == "on" else "0",
            "notify_reviews": "1" if request.form.get("notify_reviews") == "on" else "0",
        }
        if not values["app_name"] or len(values["app_name"]) > 100:
            abort(400, description="Enter a site name up to 100 characters.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["support_email"]):
            abort(400, description="Enter a valid support email.")
        if len(values["description"]) > 500:
            abort(400, description="The site description is too long.")
        for key, value in values.items():
            connection.execute(
                """INSERT INTO site_settings (setting_key, setting_value)
                   VALUES (?, ?) ON CONFLICT(setting_key) DO UPDATE SET
                   setting_value = excluded.setting_value, updated_at = CURRENT_TIMESTAMP""",
                (key, value),
            )
        connection.commit()
        current_app.config["APP_NAME"] = values["app_name"]
        record_admin_action("updated", "site-settings")
        return redirect(url_for("admin.settings"))
    settings = {
        row["setting_key"]: row["setting_value"]
        for row in query_all("SELECT setting_key, setting_value FROM site_settings")
    }
    return _admin_render(
        "settings.html", active_page="settings", breadcrumb="Workspace / Settings",
        settings=settings,
    )
