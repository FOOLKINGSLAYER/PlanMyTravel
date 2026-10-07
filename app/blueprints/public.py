from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db
from app.db_utils import query_all, query_one
from app.rate_limit import enforce_rate_limit
from app.security import login_required
from app.services.ai import ItineraryGenerationError, generate_itineraries
from app.services.media import (
    InvalidImageError,
    MediaStorageError,
    resolve_image,
    store_image,
)
from app.services.wallet import WalletError, charge, wallet_balance


public_bp = Blueprint("public", __name__)

PROFILE_CURRENCIES = ("USD", "EUR", "GBP", "INR", "SGD")
PROFILE_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "hi": "Hindi",
    "ja": "Japanese",
}
TRAVEL_INTERESTS = (
    "Food & drink",
    "Culture",
    "Nature",
    "Family fun",
    "Shopping",
    "Adventure",
)
TRAVEL_PACES = ("Balanced", "Relaxed", "See it all")


def _published_packages(limit: int = 500) -> list[dict]:
    rows = query_all(
        """SELECT p.*, d.name AS destination_name, d.country AS destination_country
           FROM packages p
           LEFT JOIN destinations d ON d.id = p.destination_id
           WHERE p.status = 'published' AND p.deleted_at IS NULL
             AND (d.is_published = 1 OR d.id IS NULL)
           ORDER BY p.is_featured DESC, p.created_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [_package_context(row) for row in rows]


def _package_context(package: dict) -> dict:
    pricing = _json_value(package.get("pricing_json"), {})
    if not isinstance(pricing, dict):
        pricing = {}
    price_values = [
        value
        for value in pricing.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    ]
    minimum_price = min(price_values, default=package.get("price", 0))
    image_rows = query_all(
        "SELECT id, image_url AS relative_path, alt_text FROM package_images WHERE package_id = ? ORDER BY sort_order, created_at",
        (package["id"],),
    )
    image_path = (
        image_rows[0]["relative_path"]
        if image_rows else package.get("cover_image_url")
    )
    title = package.get("title") or package.get("name") or "Travel package"
    duration = package.get("duration_days") or package.get("duration") or 1
    destination = package.get("destination_name") or package.get("destination_country") or "Travel"
    images = [
        {
            "id": row["id"],
            "url": resolve_image(row["relative_path"]),
            "filename": row["relative_path"],
            "alt": row["alt_text"] or title,
        }
        for row in image_rows
    ]
    if not images:
        images = [{
            "id": "cover",
            "url": resolve_image(image_path),
            "filename": image_path or "",
            "alt": title,
        }]
    return {
        **package,
        "title": title,
        "summary": package.get("description") or "",
        "location": f"{destination} · {duration} days",
        "duration": f"{duration} days",
        "price": _price_text(minimum_price, package.get("currency", "INR")),
        "price_value": minimum_price,
        "price_text": _price_text(minimum_price, package.get("currency", "INR")),
        "pricing": json.dumps(pricing),
        "image": resolve_image(image_path),
        "image_alt": image_rows[0]["alt_text"] if image_rows else title,
        "images": images,
        "itinerary": (
            _json_value(package.get("itinerary_json"), [])
            if isinstance(_json_value(package.get("itinerary_json"), []), list)
            else []
        ),
        "highlights": (
            _json_value(package.get("highlights_json"), [])
            if isinstance(_json_value(package.get("highlights_json"), []), list)
            else []
        ),
        "saved": bool(
            session.get("user_id")
            and query_one(
                """SELECT id FROM saved_items
                   WHERE user_id = ? AND item_type = 'package' AND item_id = ?""",
                (session["user_id"], str(package["id"])),
            )
        ),
    }


def _json_value(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _price_text(value, currency: str = "INR") -> str:
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        amount = Decimal(0)
    symbol = "₹" if currency == "INR" else f"{currency} "
    return f"{symbol}{amount:,.0f}"


def _destination_by_slug(slug: str) -> dict | None:
    return query_one(
        "SELECT * FROM destinations WHERE slug = ? AND is_published = 1",
        (slug,),
    )


def _owned_trip(trip_id: str) -> dict | None:
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if not trip:
        return None
    if trip.get("user_id"):
        if trip["user_id"] != session.get("user_id"):
            return None
    elif trip_id not in session.get("guest_trip_ids", []):
        return None
    return trip


def _trip_plans(trip_id: str) -> list[dict]:
    plans = query_all(
        "SELECT * FROM itinerary_plans WHERE trip_id = ? ORDER BY CASE tier WHEN 'luxury' THEN 1 WHEN 'comfort' THEN 2 ELSE 3 END",
        (trip_id,),
    )
    for plan in plans:
        plan["payload"] = _json_value(plan.get("json_payload"), {})
    return plans


def _save_guest_trip(trip_id: str) -> None:
    guest_ids = list(session.get("guest_trip_ids", []))
    if trip_id not in guest_ids:
        guest_ids.append(trip_id)
    session["guest_trip_ids"] = guest_ids[-20:]


def _attach_guest_trips(user_id: int | str) -> None:
    connection = get_db()
    for trip_id in session.get("guest_trip_ids", []):
        connection.execute(
            "UPDATE trips SET user_id = ? WHERE id = ? AND user_id IS NULL",
            (user_id, trip_id),
        )
    connection.commit()


def _save_generated_trip(data: dict, plans: list[dict]) -> str:
    connection = get_db()
    destination = query_one(
        "SELECT id FROM destinations WHERE lower(name) = lower(?) OR lower(slug) = lower(?)",
        (data["destination"].strip(), data["destination"].strip()),
    )
    if not destination:
        raise ValueError("Choose a destination from the available list.")
    trip_id = uuid4().hex
    try:
        connection.execute(
            """INSERT INTO trips
               (id, user_id, destination_id, start_date, end_date, travelers,
                travel_style, total_budget, currency, selected_tier, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'planning')""",
            (
                trip_id,
                session.get("user_id"),
                destination["id"],
                data["start_date"],
                data["end_date"],
                data["travelers"],
                data["travel_style"],
                data["total_budget"],
                data["currency"],
            ),
        )
        for plan in plans:
            plan_id = uuid4().hex
            connection.execute(
                """INSERT INTO itinerary_plans
                   (id, trip_id, tier, summary, estimated_total, json_payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    plan_id,
                    trip_id,
                    plan["tier"],
                    plan["summary"],
                    plan["estimated_total"],
                    json.dumps(plan, ensure_ascii=False),
                ),
            )
            _persist_itinerary_items(connection, plan_id, plan, data)
        inventory = data.get("inventory")
        if inventory:
            connection.execute(
                """INSERT INTO trip_inventory
                   (trip_id, status, provider, origin_code, destination_code,
                    flight_status, hotel_status, flights_json, hotels_json,
                    flight_notice, hotel_notice, notice, searched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trip_id,
                    inventory.get("status", "unavailable"),
                    inventory.get("provider"),
                    inventory.get("origin_code"),
                    inventory.get("destination_code"),
                    inventory.get("flight_status", "unavailable"),
                    inventory.get("hotel_status", "unavailable"),
                    json.dumps(inventory.get("flights", []), ensure_ascii=False),
                    json.dumps(inventory.get("hotels", []), ensure_ascii=False),
                    inventory.get("flight_notice"),
                    inventory.get("hotel_notice"),
                    inventory.get("notice"),
                    inventory.get("searched_at"),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if not session.get("user_id"):
        _save_guest_trip(trip_id)
    return trip_id


def _persist_itinerary_items(connection, plan_id: str, plan: dict, data: dict) -> None:
    for index, day in enumerate(plan["days"], start=1):
        day_date = (date.fromisoformat(data["start_date"])).toordinal() + index - 1
        day_iso = date.fromordinal(day_date).isoformat()
        day_cursor = connection.execute(
            """INSERT INTO itinerary_days
               (plan_id, day_number, date, title, weather_json, notes)
               VALUES (?, ?, ?, ?, NULL, NULL)""",
            (plan_id, index, day_iso, day["title"]),
        )
        day_id = day_cursor.lastrowid
        previous = None
        for sort_order, item in enumerate(day["items"]):
            route_json = None
            if previous is not None:
                route_json = _cached_route(previous, (item["lat"], item["lng"]))
            connection.execute(
                """INSERT INTO itinerary_items
                   (day_id, sort_order, title, category, description, start_time,
                    duration_min, estimated_cost, lat, lng, route_from_prev_json, currency)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    day_id,
                    sort_order,
                    item["title"],
                    item["category"],
                    item["description"],
                    item["start_time"],
                    item["duration_min"],
                    item["est_cost"],
                    item["lat"],
                    item["lng"],
                    json.dumps(route_json) if route_json is not None else None,
                    data["currency"],
                ),
            )
            previous = (item["lat"], item["lng"])


def _cached_route(origin: tuple[float, float], destination: tuple[float, float]):
    from app.services.cache import get_cached, set_cached
    from app.services.travel_data import get_route

    key = f"route:{origin[0]:.4f}:{origin[1]:.4f}:{destination[0]:.4f}:{destination[1]:.4f}"
    cached = get_cached(key)
    if cached is not None:
        return cached
    try:
        route = get_route(origin, destination)
    except Exception as exc:
        current_app.logger.info("Route enrichment unavailable: %s", type(exc).__name__)
        return None
    set_cached(key, route, 86400)
    return route


def _attach_weather(plan_id: str, plan: dict, start_date: str, latitude, longitude) -> None:
    if latitude is None or longitude is None:
        return
    from app.services.cache import get_cached, set_cached
    from app.services.travel_data import get_weather

    connection = get_db()
    for index, day in enumerate(plan["days"], start=1):
        day_date = (date.fromisoformat(start_date)).fromordinal(
            date.fromisoformat(start_date).toordinal() + index - 1
        ).isoformat()
        key = f"weather:{latitude:.3f}:{longitude:.3f}:{day_date}"
        weather = get_cached(key)
        if weather is None:
            try:
                weather = get_weather(latitude, longitude, day_date)
            except Exception as exc:
                current_app.logger.info("Weather enrichment unavailable: %s", type(exc).__name__)
                continue
            set_cached(key, weather, 21600)
        connection.execute(
            "UPDATE itinerary_days SET weather_json = ? WHERE plan_id = ? AND day_number = ?",
            (json.dumps(weather, ensure_ascii=False), plan_id, index),
        )
    connection.commit()


def _generate_from_form(form_data) -> tuple[str | None, str | None]:
    destination = str(form_data.get("destination", "")).strip()
    try:
        start = date.fromisoformat(form_data.get("start_date", ""))
        end = date.fromisoformat(form_data.get("end_date", ""))
        travelers = int(str(form_data.get("travelers", "1")).split()[0])
        total_budget = Decimal(str(form_data.get("total_budget", "0")))
    except (ValueError, TypeError, InvalidOperation):
        return None, "Enter valid travel dates, travelers, and a total budget."
    interests = form_data.getlist("interests") if hasattr(form_data, "getlist") else form_data.get("interests", [])
    if isinstance(interests, str):
        interests = [interests]
    day_count = (end - start).days + 1
    if (
        not destination
        or len(destination) > 120
        or day_count < 1
        or day_count > 30
        or travelers < 1
        or travelers > 20
        or not total_budget.is_finite()
        or total_budget <= 0
        or total_budget > Decimal("100000000")
    ):
        return None, "Some trip details are outside the supported range."
    data = {
        "destination": destination,
        "origin": str(form_data.get("origin", "")).strip(),
        "flight_destination": str(form_data.get("flight_destination", "")).strip(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "travelers": travelers,
        "total_budget": float(total_budget),
        "currency": "INR",
        "travel_style": str(form_data.get("travel_style", form_data.get("pace", "balanced"))),
        "interests": interests[:15],
        "personal_notes": str(form_data.get("notes", "")).strip()[:1000],
    }
    destination_row = query_one(
        """SELECT id, latitude AS lat, longitude AS lng FROM destinations
           WHERE lower(name) = lower(?) OR lower(slug) = lower(?)""",
        (destination, destination),
    )
    if not destination_row:
        return None, "Choose a destination from the available list."
    data["trip_days"] = day_count
    if not current_app.config.get("GEMINI_API_KEYS"):
        return None, "AI trip planning is not configured. Add a Gemini API key to the server environment."
    from app.services.travel_inventory import inventory_result

    data["inventory"] = inventory_result(
        data["origin"],
        destination,
        start.isoformat(),
        end.isoformat(),
        travelers,
        data["currency"],
        flight_destination=str(form_data.get("flight_destination", "")).strip(),
    )
    if data["inventory"]["status"] == "available":
        data["travel_inventory"] = {
            "flights": data["inventory"]["flights"],
            "hotels": data["inventory"]["hotels"],
        }
    try:
        plans = generate_itineraries(data)
        trip_id = _save_generated_trip(data, plans)
        for plan in plans:
            row = query_one(
                "SELECT id FROM itinerary_plans WHERE trip_id = ? AND tier = ?",
                (trip_id, plan["tier"]),
            )
            if row:
                _attach_weather(
                    row["id"],
                    plan,
                    start.isoformat(),
                    destination_row.get("lat"),
                    destination_row.get("lng"),
                )
        return trip_id, None
    except (ItineraryGenerationError, ValueError) as exc:
        return None, str(exc)


def _format_itinerary(trip: dict, plan: dict) -> dict:
    payload = plan["payload"]
    days_list = []
    trip_start = date.fromisoformat(trip["start_date"])
    for day_index, day in enumerate(payload.get("days", [])):
        days_list.append(
            {
                "date": date.fromordinal(trip_start.toordinal() + day_index).strftime("%a, %d %b"),
                "title": day.get("title", "Day of discovery"),
                "activities": [
                    {
                        "time": item.get("start_time"),
                        "title": item.get("title"),
                        "description": item.get("description"),
                        "cost": item.get("est_cost"),
                    }
                    for item in day.get("items", [])
                ],
            }
        )
    return {
        "id": plan["id"],
        "trip_id": trip["id"],
        "title": trip.get("destination_name") or "Your trip",
        "start_date": trip.get("start_date") or "",
        "days": len(days_list),
        "days_list": days_list,
        "notes": "",
        "selected_tier": plan.get("tier"),
        "summary": plan.get("summary"),
        "estimated_total": plan.get("estimated_total"),
    }


@public_bp.get("/")
def home():
    destinations = query_all(
        "SELECT * FROM destinations WHERE is_published = 1 ORDER BY name LIMIT 8"
    )
    return render_template(
        "index.html",
        featured_packages=_published_packages(6),
        destinations=destinations,
        featured={
            row["setting_key"]: row["setting_value"]
            for row in query_all(
                "SELECT setting_key, setting_value FROM site_settings"
            )
        },
    )


def _search_catalog(args) -> dict:
    query = args.get("q", "").strip().casefold()
    category = args.get("category", "").strip().casefold()
    travel_style = args.get("style", "").strip().casefold()
    minimum_rating = args.get("rating", type=float)
    minimum_budget = args.get("budget_min", type=float)
    maximum_budget = args.get("budget_max", type=float)
    page = max(1, args.get("page", 1, type=int))
    page_size = 12

    destinations = query_all(
        """SELECT d.*,
                  COALESCE((SELECT AVG(r.rating) FROM reviews r
                            WHERE r.destination_id = d.id AND r.status = 'published'),
                           d.rating, 0) AS search_rating
           FROM destinations d WHERE d.is_published = 1"""
    )
    results = []
    for destination in destinations:
        styles = _json_value(destination.get("travel_styles_json"), [])
        if not isinstance(styles, list):
            styles = []
        item = {
            "kind": "Destination",
            "id": destination["id"],
            "title": destination["name"],
            "summary": destination.get("short_description") or destination.get("description") or "",
            "location": destination.get("country") or destination.get("region") or "",
            "url": url_for("public.destination_detail", slug=destination["slug"]),
            "image": resolve_image(destination.get("hero_image_url") or destination.get("image_url")),
            "rating": float(destination.get("search_rating") or 0),
            "price_value": destination.get("estimated_cost_min"),
            "category_value": destination.get("category") or "",
            "style_values": " ".join(str(style) for style in styles),
            "featured": bool(destination.get("is_featured")),
        }
        results.append(item)

    for package in _published_packages():
        item = {
            "kind": "Package",
            "id": package["id"],
            "title": package["title"],
            "summary": package.get("short_description") or package.get("description") or "",
            "location": package.get("destination_name") or package.get("destination_country") or "",
            "url": url_for("public.package_detail", slug=package["slug"]),
            "image": package["image"],
            "rating": float(package.get("rating") or 0),
            "price_value": float(package.get("price_value") or package.get("base_price") or 0),
            "price_text": package.get("price_text") or package.get("price"),
            "category_value": package.get("category") or "",
            "style_values": package.get("travel_style") or package.get("category") or "",
            "featured": bool(package.get("is_featured")),
        }
        results.append(item)

    guides = query_all(
        """SELECT g.id, g.display_name AS title, g.bio AS summary, g.rating,
                  g.specialties_json, g.languages_json, u.country
           FROM guides g LEFT JOIN users u ON u.id = g.user_id
           WHERE g.is_active = 1"""
    )
    for guide in guides:
        specialties = _json_value(guide.get("specialties_json"), [])
        languages = _json_value(guide.get("languages_json"), [])
        specialty_text = " ".join(str(value) for value in specialties) if isinstance(specialties, list) else ""
        results.append({
            "kind": "Guide",
            "id": guide["id"],
            "title": guide["title"],
            "summary": guide.get("summary") or "",
            "location": guide.get("country") or specialty_text,
            "url": url_for("public.guide_profile", guide_id=guide["id"]),
            "image": resolve_image(None),
            "rating": float(guide.get("rating") or 0),
            "price_value": None,
            "category_value": specialty_text,
            "style_values": specialty_text + " " + (" ".join(languages) if isinstance(languages, list) else ""),
            "featured": False,
        })

    experiences = query_all(
        """SELECT e.*, d.name AS destination_name, d.country AS destination_country
           FROM experiences e LEFT JOIN destinations d ON d.id = e.destination_id
           WHERE e.status = 'published' AND e.is_published = 1
             AND (d.is_published = 1 OR d.id IS NULL)"""
    )
    for experience in experiences:
        experience["image"] = resolve_image(experience.get("cover_image_url"))
        results.append({
            "kind": "Experience",
            "id": experience["id"],
            "title": experience["title"],
            "summary": experience.get("description") or "",
            "location": experience.get("destination_name") or experience.get("destination_country") or "",
            "url": url_for("public.experience_detail", slug=experience["slug"]),
            "image": resolve_image(experience.get("cover_image_url")),
            "rating": float(experience.get("rating") or 0),
            "price_value": float(experience.get("price") or 0),
            "price_text": _price_text(experience.get("price"), experience.get("currency", "SGD")),
            "category_value": experience.get("category") or "",
            "style_values": experience.get("travel_style") or experience.get("category") or "",
            "featured": False,
        })

    def matches(item: dict) -> bool:
        searchable = " ".join(
            str(item.get(key) or "")
            for key in ("title", "summary", "location", "category_value", "style_values")
        ).casefold()
        if query and query not in searchable:
            return False
        if category and category not in str(item.get("category_value", "")).casefold():
            return False
        if travel_style and travel_style not in str(item.get("style_values", "")).casefold():
            return False
        price = item.get("price_value")
        if minimum_budget is not None or maximum_budget is not None:
            if price is None:
                return False
            if minimum_budget is not None and price < minimum_budget:
                return False
            if maximum_budget is not None and price > maximum_budget:
                return False
        if minimum_rating is not None and item["rating"] < minimum_rating:
            return False
        return True

    results = [item for item in results if matches(item)]
    sort_by = args.get("sort", "recommended")
    if sort_by == "price":
        results.sort(key=lambda item: (item["price_value"] is None, item["price_value"] or 0, item["title"].casefold()))
    elif sort_by == "rating":
        results.sort(key=lambda item: (-item["rating"], item["title"].casefold()))
    elif sort_by == "name":
        results.sort(key=lambda item: item["title"].casefold())
    else:
        results.sort(key=lambda item: (not item["featured"], -item["rating"], item["title"].casefold()))

    total = len(results)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(page, page_count)
    catalog = {
        "results": results[(page - 1) * page_size:page * page_size],
        "total": total,
        "page": page,
        "page_count": page_count,
    }
    user_id = session.get("user_id")
    if user_id:
        saved = query_all(
            "SELECT item_type, item_id FROM saved_items WHERE user_id = ?",
            (user_id,),
        )
        saved_keys = {(row["item_type"], str(row["item_id"])) for row in saved}
        for item in catalog["results"]:
            item["saved"] = (item["kind"].lower(), str(item["id"])) in saved_keys
    return catalog


@public_bp.get("/search")
def search():
    catalog = _search_catalog(request.args)
    template = "search_results.html" if request.args.get("partial") == "1" else "search.html"
    filters = {
        key: value for key, value in request.args.items()
        if key not in {"partial", "page"}
    }
    prev_url = None
    next_url = None
    if catalog["page"] > 1:
        prev_filters = dict(filters)
        prev_filters["page"] = catalog["page"] - 1
        prev_url = url_for("public.search", **prev_filters)
    if catalog["page"] < catalog["page_count"]:
        next_filters = dict(filters)
        next_filters["page"] = catalog["page"] + 1
        next_url = url_for("public.search", **next_filters)
    return render_template(
        template,
        **catalog,
        filters=filters,
        prev_url=prev_url,
        next_url=next_url,
    )


@public_bp.get("/destinations/<slug>")
def destination_detail(slug: str):
    destination = _destination_by_slug(slug)
    if not destination:
        abort(404)
    packages = [
        item for item in _published_packages()
        if item.get("destination_id") == destination["id"]
    ]
    gallery = query_all(
        """SELECT image_url, alt_text, caption FROM destination_images
           WHERE destination_id = ? ORDER BY sort_order, id""",
        (destination["id"],),
    )
    if not gallery and (destination.get("hero_image_url") or destination.get("image_url")):
        gallery = [{
            "image_url": destination.get("hero_image_url") or destination.get("image_url"),
            "alt_text": destination["name"],
            "caption": "",
        }]
    if not gallery:
        gallery = [{"image_url": None, "alt_text": destination["name"], "caption": ""}]
    destination = {
        **destination,
        "hero_image": resolve_image(destination.get("hero_image_url") or destination.get("image_url") or destination.get("hero_image_path")),
        "gallery": [
            {
                "url": resolve_image(image["image_url"]),
                "alt": image.get("alt_text") or destination["name"],
                "caption": image.get("caption") or "",
            }
            for image in gallery
        ],
        "attractions": _json_value(destination.get("attractions_json"), []),
        "activities": _json_value(destination.get("activities_json"), []),
        "travel_styles": _json_value(destination.get("travel_styles_json"), []),
    }
    guides = query_all(
        """SELECT g.id, g.display_name AS name, g.bio, g.rating, g.specialties_json
           FROM guides g LEFT JOIN users u ON u.id = g.user_id
           WHERE g.is_active = 1 AND (
               lower(COALESCE(u.country, '')) = lower(?) OR
               lower(g.specialties_json) LIKE ? OR lower(COALESCE(g.bio, '')) LIKE ?)
           ORDER BY g.rating DESC, g.display_name LIMIT 6""",
        (destination.get("country") or destination["name"], f"%{destination['name'].lower()}%", f"%{destination['name'].lower()}%"),
    )
    experiences = query_all(
        """SELECT * FROM experiences WHERE destination_id = ?
           AND status = 'published' AND is_published = 1
           ORDER BY rating DESC, title LIMIT 8""",
        (destination["id"],),
    )
    for experience in experiences:
        experience["image"] = resolve_image(experience.get("cover_image_url"))
    favorite = bool(session.get("user_id") and query_one(
        "SELECT id FROM saved_items WHERE user_id = ? AND item_type = 'destination' AND item_id = ?",
        (session["user_id"], str(destination["id"])),
    ))
    return render_template(
        "destination.html",
        destination=destination,
        packages=packages,
        guides=guides,
        experiences=experiences,
        favorite=favorite,
        reviews=query_all(
            """SELECT r.*, COALESCE(u.full_name, u.name, '') AS reviewer_name FROM reviews r
               LEFT JOIN users u ON u.id = r.user_id
               WHERE r.destination_id = ? AND r.status = 'published'
               ORDER BY r.created_at DESC LIMIT 12""",
            (destination["id"],),
        ),
    )


@public_bp.get("/experiences/<slug>")
def experience_detail(slug: str):
    experience = query_one(
        """SELECT e.*, d.name AS destination_name, d.slug AS destination_slug
           FROM experiences e LEFT JOIN destinations d ON d.id = e.destination_id
           WHERE e.slug = ? AND e.status = 'published' AND e.is_published = 1""",
        (slug,),
    )
    if not experience:
        abort(404)
    experience["image"] = resolve_image(experience.get("cover_image_url"))
    experience["saved"] = bool(
        session.get("user_id")
        and query_one(
            """SELECT id FROM saved_items
               WHERE user_id = ? AND item_type = 'experience' AND item_id = ?""",
            (session["user_id"], str(experience["id"])),
        )
    )
    return render_template("experience_detail.html", experience=experience)


@public_bp.get("/packages/<slug>")
def package_detail(slug: str):
    package = query_one(
        """SELECT p.*, d.name AS destination_name, d.country AS destination_country
           FROM packages p LEFT JOIN destinations d ON d.id = p.destination_id
           WHERE p.slug = ? AND p.status = 'published' AND p.deleted_at IS NULL""",
        (slug,),
    )
    if not package:
        abort(404)
    package = _package_context(package)
    return render_template(
        "package_detail.html",
        package=package,
        reviews=query_all(
            """SELECT r.*, u.full_name AS reviewer_name FROM reviews r
               LEFT JOIN users u ON u.id = r.user_id
               WHERE r.package_id = ? AND r.status = 'published'
               ORDER BY r.created_at DESC LIMIT 12""",
            (package["id"],),
        ),
    )


@public_bp.route("/itinerary", methods=["GET", "POST"])
def itinerary_form():
    if request.method == "POST":
        if not g.get("user_id"):
            session["pending_itinerary"] = _pending_itinerary(request.form)
            return redirect(url_for("public.login", next=url_for("public.itinerary_form")))
        enforce_rate_limit("itinerary-generation", 5, 60)
        trip_id, error = _generate_from_form(request.form)
        if error:
            return _render_itinerary_form(error=error), 400
        session.pop("pending_itinerary", None)
        return redirect(url_for("public.itinerary_results", trip_id=trip_id))
    return _render_itinerary_form(
        destination=request.args.get("destination", "").strip()
    )


def _pending_itinerary(form_data) -> dict[str, str | list[str]]:
    values: dict[str, str | list[str]] = {
        key: str(form_data.get(key, "")).strip()
        for key in (
            "destination",
            "origin",
            "flight_destination",
            "start_date",
            "end_date",
            "travelers",
            "total_budget",
            "pace",
        )
    }
    values["notes"] = str(form_data.get("notes", ""))[:1000]
    interests = form_data.getlist("interests") if hasattr(form_data, "getlist") else []
    values["interests"] = [
        item[:60] for item in interests[:15] if isinstance(item, str)
    ]
    return values


def _render_itinerary_form(
    error: str | None = None,
    destination: str = "",
):
    return render_template(
        "itinerary_form.html",
        error=error,
        destinations=query_all(
            "SELECT name, country, slug FROM destinations WHERE is_published = 1 ORDER BY name"
        ),
        destination=destination,
        pending_itinerary=session.get("pending_itinerary", {}),
    )


@public_bp.get("/itinerary/results")
def itinerary_results():
    trip_id = request.args.get("trip_id", "")
    trip = _owned_trip(trip_id) if trip_id else None
    if not trip:
        abort(404)
    destination = query_one(
        "SELECT name, country, latitude, longitude FROM destinations WHERE id = ?",
        (trip["destination_id"],),
    )
    if destination:
        trip.update({
            "destination_name": destination["name"],
            "destination_country": destination.get("country") or "",
            "destination_latitude": destination.get("latitude"),
            "destination_longitude": destination.get("longitude"),
        })
    plans = _trip_plans(trip_id)
    if not plans:
        abort(404)
    for plan in plans:
        plan["days"] = plan["payload"].get("days", [])
    active = next((plan for plan in plans if plan["tier"] == trip.get("selected_tier")), plans[1] if len(plans) > 1 else plans[0])
    itinerary = _format_itinerary(trip, active)
    inventory = query_one("SELECT * FROM trip_inventory WHERE trip_id = ?", (trip_id,))
    if inventory:
        inventory["flights"] = _json_value(inventory.get("flights_json"), [])
        inventory["hotels"] = _json_value(inventory.get("hotels_json"), [])
    else:
        inventory = {
            "origin_code": "",
            "destination_code": "",
            "flight_status": "unavailable",
            "hotel_status": "unavailable",
            "flight_notice": "Flight search data was not saved for this trip. Edit your trip to search again.",
            "hotel_notice": "Hotel search data was not saved for this trip. Edit your trip to search again.",
            "flights": [],
            "hotels": [],
        }
    return render_template(
        "itinerary_results.html",
        trip=trip,
        plans=plans,
        active_plan=active,
        itinerary=itinerary,
        inventory=inventory,
    )


@public_bp.get("/itinerary/edit")
def itinerary_editor():
    trip_id = request.args.get("trip_id", "")
    trip = _owned_trip(trip_id) if trip_id else None
    if not trip:
        abort(404)
    plans = _trip_plans(trip_id)
    tier = request.args.get("tier", trip.get("selected_tier") or "comfort")
    plan = next((item for item in plans if item["tier"] == tier), None)
    if not plan:
        abort(404)
    return render_template("itinerary_editor.html", trip=trip, itinerary=_format_itinerary(trip, plan), plan=plan)


@public_bp.route("/itinerary/results", methods=["POST"])
def itinerary_results_post():
    if not g.get("user_id"):
        session["pending_itinerary"] = _pending_itinerary(request.form)
        return redirect(url_for("public.login", next=url_for("public.itinerary_form")))
    enforce_rate_limit("itinerary-generation", 5, 60)
    trip_id, error = _generate_from_form(request.form)
    if error:
        return _render_itinerary_form(error=error), 400
    session.pop("pending_itinerary", None)
    return redirect(url_for("public.itinerary_results", trip_id=trip_id))


@public_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        next_url = _safe_next(request.form.get("next"))
        enforce_rate_limit("traveler-signup", 5, 3600)
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if (
            not name
            or len(name) > 100
            or len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            return render_template("signup.html", error="Enter your name and a valid email address.", next_url=next_url), 400
        if len(password) < 8 or len(password) > 256:
            return render_template(
                "signup.html",
                error="Your password must be between 8 and 256 characters.",
                next_url=next_url,
            ), 400
        if request.form.get("terms") != "on":
            return render_template(
                "signup.html", error="Please agree to the terms to create an account.",
                next_url=next_url,
            ), 400
        if query_one("SELECT id FROM users WHERE lower(email) = ?", (email,)):
            return render_template("signup.html", error="We couldn't create that account. Try signing in.", next_url=next_url), 409
        connection = get_db()
        connection.execute(
            """INSERT INTO users (email, password_hash, name, full_name, role, created_at)
               VALUES (?, ?, ?, ?, 'traveler', CURRENT_TIMESTAMP)
               ON CONFLICT(email) DO NOTHING""",
            (email, generate_password_hash(password), name, name),
        )
        inserted = connection.execute("SELECT changes()").fetchone()[0]
        if not inserted:
            connection.rollback()
            return render_template(
                "signup.html",
                error="We couldn't create that account. Try signing in.",
                next_url=next_url,
            ), 409
        user = query_one(
            "SELECT id, session_version FROM users WHERE lower(email) = ?",
            (email,),
        )
        if not user:
            connection.rollback()
            return render_template(
                "signup.html",
                error="We couldn't complete account setup. Please try again.",
                next_url=next_url,
            ), 503
        connection.commit()
        user_id = user["id"]
        pending_itinerary = session.get("pending_itinerary")
        _attach_guest_trips(user_id)
        session.clear()
        session["user_id"] = user_id
        session["auth_version"] = user["session_version"]
        if pending_itinerary:
            session["pending_itinerary"] = pending_itinerary
        session.permanent = True
        return redirect(next_url)
    return render_template("signup.html", next_url=_safe_next(request.args.get("next")))


@public_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        enforce_rate_limit("traveler-login", 10, 300)
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = query_one(
            """SELECT id, password_hash, is_active, session_version
               FROM users WHERE lower(email) = ?""",
            (email,),
        )
        if (
            not user
            or not user.get("is_active")
            or len(password) > 256
            or not check_password_hash(user["password_hash"], password)
        ):
            return render_template(
                "login.html",
                error="Email or password is incorrect.",
                next_url=_safe_next(request.args.get("next") or request.form.get("next")),
            ), 401
        guest_trip_ids = session.get("guest_trip_ids", [])
        pending_itinerary = session.get("pending_itinerary")
        _attach_guest_trips(user["id"])
        session.clear()
        session["user_id"] = user["id"]
        session["auth_version"] = user["session_version"]
        session["guest_trip_ids"] = guest_trip_ids
        if pending_itinerary:
            session["pending_itinerary"] = pending_itinerary
        session.permanent = request.form.get("remember") == "on"
        return redirect(_safe_next(request.args.get("next") or request.form.get("next")))
    return render_template(
        "login.html",
        next_url=_safe_next(request.args.get("next")),
    )


def _safe_next(target: str | None) -> str:
    if (
        target
        and target.startswith("/")
        and not target.startswith(("//", "/\\"))
        and "\\" not in target
        and not urlsplit(target).netloc
    ):
        return target
    return url_for("public.trips")


def _account_profile(user_id: int | str) -> dict | None:
    user = query_one(
        """SELECT id, email, name, full_name, phone, date_of_birth, country,
                  preferred_currency, profile_image_url, avatar_url, bio
           FROM users WHERE id = ? AND is_active = 1""",
        (user_id,),
    )
    if not user:
        return None
    full_name = user.get("full_name") or user.get("name") or ""
    name_parts = full_name.split(maxsplit=1)
    user["first_name"] = name_parts[0] if name_parts else ""
    user["last_name"] = name_parts[1] if len(name_parts) > 1 else ""
    user["profile_picture_url"] = resolve_image(
        user.get("profile_image_url") or user.get("avatar_url")
    )
    return user


@public_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("public.home"))


@public_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = _account_profile(g.user_id)
    if not user:
        session.clear()
        return redirect(url_for("public.login"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        name = " ".join(part for part in (first_name, last_name) if part)
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        date_of_birth = request.form.get("date_of_birth", "").strip()
        country = request.form.get("country", "").strip()
        bio = request.form.get("bio", "").strip()
        currency = request.form.get("preferred_currency", "").strip().upper()

        error = None
        if not name or len(name) > 100:
            error = "Enter a first name and last name up to 100 characters combined."
        elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter a valid email address."
        elif len(phone) > 40 or len(country) > 100 or len(bio) > 500:
            error = "Phone, country, or bio is longer than allowed."
        elif currency not in PROFILE_CURRENCIES:
            error = "Choose a supported currency."
        elif date_of_birth:
            try:
                date.fromisoformat(date_of_birth)
            except ValueError:
                error = "Enter a valid date of birth."

        if not error and query_one(
            "SELECT id FROM users WHERE lower(email) = ? AND id != ?",
            (email, g.user_id),
        ):
            error = "That email address is already in use."

        picture = request.files.get("profile_picture")
        picture_path = user.get("profile_image_url") or user.get("avatar_url")
        if not error and picture and picture.filename:
            try:
                picture_path = str(store_image(picture, "users")["url"])
            except (InvalidImageError, MediaStorageError) as exc:
                error = str(exc)

        if error:
            user.update(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                date_of_birth=date_of_birth,
                country=country,
                bio=bio,
                preferred_currency=currency,
            )
            return render_template("account.html", user=user, error=error), 400

        connection = get_db()
        connection.execute(
            """UPDATE users
               SET name = ?, full_name = ?, email = ?, phone = ?, date_of_birth = ?,
                   country = ?, bio = ?, preferred_currency = ?,
                   profile_image_url = ?, avatar_url = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND is_active = 1""",
            (
                name,
                name,
                email,
                phone or None,
                date_of_birth or None,
                country or None,
                bio or None,
                currency,
                picture_path,
                picture_path,
                g.user_id,
            ),
        )
        connection.commit()
        user.update(
            first_name=first_name,
            last_name=last_name,
            name=name,
            full_name=name,
            email=email,
            phone=phone,
            date_of_birth=date_of_birth,
            country=country,
            bio=bio,
            preferred_currency=currency,
            profile_image_url=picture_path,
            profile_picture_url=resolve_image(picture_path),
        )
        return render_template(
            "account.html", user=user, success="Your profile has been updated."
        )
    return render_template("account.html", user=user)


@public_bp.route("/account/preferences", methods=["GET", "POST"])
@login_required
def account_preferences():
    user = query_one(
        "SELECT preferences_json FROM users WHERE id = ? AND is_active = 1",
        (g.user_id,),
    )
    if not user:
        session.clear()
        return redirect(url_for("public.login"))
    preferences = _json_value(user.get("preferences_json"), {})
    if not isinstance(preferences, dict):
        preferences = {}

    if request.method == "POST":
        language = request.form.get("preferred_language", "")
        pace = request.form.get("travel_pace", "")
        interests = request.form.getlist("travel_interests")
        if language not in PROFILE_LANGUAGES:
            return render_template(
                "account_preferences.html",
                preferences=preferences,
                languages=PROFILE_LANGUAGES,
                interests=TRAVEL_INTERESTS,
                paces=TRAVEL_PACES,
                error="Choose a supported language.",
            ), 400
        if pace not in TRAVEL_PACES or any(
            interest not in TRAVEL_INTERESTS for interest in interests
        ):
            return render_template(
                "account_preferences.html",
                preferences=preferences,
                languages=PROFILE_LANGUAGES,
                interests=TRAVEL_INTERESTS,
                paces=TRAVEL_PACES,
                error="Choose valid travel preferences.",
            ), 400

        preferences.update(
            preferred_language=language,
            travel_pace=pace,
            travel_interests=interests,
        )
        get_db().execute(
            """UPDATE users SET preferences_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND is_active = 1""",
            (json.dumps(preferences, ensure_ascii=False), g.user_id),
        )
        get_db().commit()
        return render_template(
            "account_preferences.html",
            preferences=preferences,
            languages=PROFILE_LANGUAGES,
            interests=TRAVEL_INTERESTS,
            paces=TRAVEL_PACES,
            success="Your preferences have been saved.",
        )

    return render_template(
        "account_preferences.html",
        preferences=preferences,
        languages=PROFILE_LANGUAGES,
        interests=TRAVEL_INTERESTS,
        paces=TRAVEL_PACES,
    )


@public_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    user = query_one(
        "SELECT password_hash, session_version FROM users WHERE id = ? AND is_active = 1",
        (g.user_id,),
    )
    if not user:
        session.clear()
        return redirect(url_for("public.login"))
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not check_password_hash(user["password_hash"], current_password):
            return render_template(
                "account_password.html", error="Your current password is incorrect."
            ), 400
        if len(new_password) < 8 or len(new_password) > 256:
            return render_template(
                "account_password.html",
                error="Your new password must be between 8 and 256 characters.",
            ), 400
        if new_password != confirm_password:
            return render_template(
                "account_password.html", error="The new passwords do not match."
            ), 400

        new_version = int(user["session_version"]) + 1
        get_db().execute(
            """UPDATE users
               SET password_hash = ?, session_version = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND is_active = 1""",
            (generate_password_hash(new_password), new_version, g.user_id),
        )
        get_db().commit()
        session["auth_version"] = new_version
        return render_template(
            "account_password.html",
            success="Your password has been changed. Other signed-in sessions have been logged out.",
        )
    return render_template("account_password.html")


@public_bp.post("/account/deactivate")
@login_required
def deactivate_account():
    password = request.form.get("password", "")
    confirmation = request.form.get("confirmation", "")
    user = query_one(
        "SELECT password_hash FROM users WHERE id = ? AND is_active = 1",
        (g.user_id,),
    )
    if (
        not user
        or not check_password_hash(user["password_hash"], password)
        or confirmation != "deactivate"
    ):
        profile = _account_profile(g.user_id)
        if not profile:
            session.clear()
            return redirect(url_for("public.login"))
        return render_template(
            "account.html",
            user=profile,
            error="Enter your password and type deactivate to confirm.",
        ), 400
    get_db().execute(
        """UPDATE users
           SET is_active = 0, session_version = session_version + 1,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (g.user_id,),
    )
    get_db().commit()
    session.clear()
    return redirect(url_for("public.home"))


@public_bp.get("/checkout")
def checkout():
    package_id = request.args.get("package_id", "")
    package = query_one(
        """SELECT p.*, d.name AS destination_name FROM packages p
           LEFT JOIN destinations d ON d.id = p.destination_id
           WHERE (p.id = ? OR p.slug = ?) AND p.status = 'published' AND p.deleted_at IS NULL""",
        (package_id, package_id),
    )
    if not package:
        abort(404)
    return render_template(
        "checkout.html",
        package=_package_context(package),
        booking={
            "package_id": package["id"],
            "package_title": package["title"],
            "travelers": request.args.get("travelers", "2"),
            "date": request.args.get("date", ""),
        },
    )


@public_bp.post("/checkout")
@login_required
def create_booking():
    package_id = request.form.get("package_id", "")
    package = query_one(
        """SELECT * FROM packages WHERE id = ? AND status = 'published'
           AND deleted_at IS NULL""",
        (package_id,),
    )
    if not package:
        abort(404)
    try:
        travelers = int(request.form.get("travelers", "1"))
        departure_date = date.fromisoformat(request.form.get("date", ""))
    except (ValueError, TypeError):
        return render_template("checkout.html", package=_package_context(package), error="Enter a valid travel date and traveler count."), 400
    if not 1 <= travelers <= 20:
        return render_template("checkout.html", package=_package_context(package), error="Choose between 1 and 20 travelers."), 400
    pricing = _json_value(package.get("pricing_json"), {})
    base_price = package.get("price") or pricing.get("comfort") or 0
    try:
        total = Decimal(str(base_price)) * travelers
    except (InvalidOperation, TypeError):
        total = Decimal(0)
    if total <= 0:
        return render_template("checkout.html", package=_package_context(package), error="This package is not bookable yet."), 400
    destination = query_one("SELECT destination_id FROM packages WHERE id = ?", (package_id,))
    trip_id = uuid4().hex
    booking_id = uuid4().hex
    connection = get_db()
    try:
        connection.execute(
            """INSERT INTO trips
               (id, user_id, destination_id, start_date, end_date, travelers,
                travel_style, total_budget, currency, selected_tier, status)
               VALUES (?, ?, ?, ?, ?, ?, 'package', ?, ?, 'comfort', 'booked')""",
            (
                trip_id,
                g.user_id,
                destination["destination_id"],
                departure_date.isoformat(),
                departure_date.isoformat(),
                travelers,
                str(total),
                package.get("currency", "INR"),
            ),
        )
        connection.execute(
            """INSERT INTO bookings
               (id, user_id, trip_id, package_id, status, travelers_count, total_amount,
                currency, booked_at, created_at)
               VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (
                booking_id,
                g.user_id,
                trip_id,
                package_id,
                travelers,
                str(total),
                package.get("currency", "INR"),
            ),
        )
        charge(connection, g.user_id, total, trip_id, booking_id)
    except WalletError as exc:
        connection.rollback()
        wallet = query_one("SELECT balance, currency FROM wallets WHERE user_id = ?", (g.user_id,))
        return render_template("checkout.html", package=_package_context(package), error=str(exc), wallet=wallet), 402
    except Exception:
        connection.rollback()
        current_app.logger.exception("Booking checkout failed.")
        abort(500)
    return redirect(url_for("public.booking_confirmation", booking_id=booking_id))


@public_bp.get("/booking-confirmation/<booking_id>")
@login_required
def booking_confirmation(booking_id: str):
    booking = query_one(
        """SELECT b.*, p.title AS package_title FROM bookings b
           LEFT JOIN packages p ON p.id = b.package_id
           WHERE b.id = ? AND b.user_id = ?""",
        (booking_id, g.user_id),
    )
    if not booking:
        abort(404)
    return render_template("booking_confirmation.html", booking=booking)


@public_bp.get("/wallet")
@login_required
def wallet():
    wallet_record = query_one(
        "SELECT balance, currency FROM wallets WHERE user_id = ?", (g.user_id,)
    )
    currency = wallet_record["currency"] if wallet_record else "INR"
    balance = wallet_balance(get_db(), g.user_id)
    currency_prefix = "₹" if currency == "INR" else f"{currency} "
    return render_template(
        "wallet.html",
        wallet={
            "balance": f"{currency_prefix}{balance:,.2f}",
            "currency": currency,
        },
        transactions=query_all(
            """SELECT wt.id, wt.type, wt.amount, wt.currency, wt.status,
                      wt.description, wt.created_at
               FROM wallet_transactions wt
               JOIN wallets w ON w.id = wt.wallet_id WHERE w.user_id = ?
               ORDER BY wt.created_at DESC LIMIT 25""",
            (g.user_id,),
        ),
    )


@public_bp.get("/trips")
@login_required
def trips():
    records = query_all(
        """SELECT t.*, d.name AS destination_name FROM trips t
           LEFT JOIN destinations d ON d.id = t.destination_id
           WHERE t.user_id = ? ORDER BY t.created_at DESC""",
        (g.user_id,),
    )
    return render_template("trips.html", trips=records)


@public_bp.route("/trips/<trip_id>", methods=["GET", "POST"])
@login_required
def trip_dashboard(trip_id: str):
    trip = query_one(
        """SELECT t.*, d.name AS destination_name FROM trips t
           LEFT JOIN destinations d ON d.id = t.destination_id
           WHERE t.id = ? AND t.user_id = ?""",
        (trip_id, g.user_id),
    )
    if not trip:
        abort(404)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "Other").strip()
        try:
            amount = Decimal(request.form.get("amount", "0"))
            if not title or len(title) > 120 or amount <= 0 or amount > Decimal("1000000"):
                raise ValueError
        except (InvalidOperation, ValueError):
            return render_template("trip_dashboard.html", trip=trip, expenses=[], error="Enter an expense title and positive amount."), 400
        connection = get_db()
        connection.execute(
            """INSERT INTO expenses (id, trip_id, user_id, category, amount, title, spent_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (uuid4().hex, trip_id, g.user_id, category[:60], str(amount), title),
        )
        connection.commit()
    expenses = query_all(
        "SELECT * FROM expenses WHERE trip_id = ? AND user_id = ? ORDER BY spent_at DESC",
        (trip_id, g.user_id),
    )
    bookings = query_all("SELECT * FROM bookings WHERE trip_id = ? AND user_id = ?", (trip_id, g.user_id))
    return render_template("trip_dashboard.html", trip=trip, expenses=expenses, bookings=bookings)


@public_bp.get("/guides")
def guides():
    search = request.args.get("q", "").strip().casefold()
    records = query_all(
        """SELECT g.id, g.display_name AS name, g.bio, g.rating, g.is_verified AS verified,
                  COALESCE(u.full_name, u.name, g.display_name) AS full_name,
                  COALESCE(g.photo_url, u.profile_image_url, u.avatar_url, '') AS avatar,
                  COALESCE(u.country, 'Singapore') AS location,
                  JSON_EXTRACT(g.languages_json, '$[0]') AS primary_language,
                  JSON_EXTRACT(g.specialties_json, '$[0]') AS specialty
              FROM guides g
              LEFT JOIN users u ON u.id = g.user_id
              WHERE g.is_active = 1
              ORDER BY g.rating DESC, g.display_name
              LIMIT 40"""
    )
    if search:
        records = [
            guide for guide in records
            if search in (guide.get("name") or "").casefold()
            or search in (guide.get("bio") or "").casefold()
            or search in (guide.get("specialty") or "").casefold()
            or search in (guide.get("location") or "").casefold()
        ]
    for guide in records:
        guide["avatar"] = resolve_image(guide.get("avatar") or None)
        guide["photo"] = guide.get("avatar")
        guide["url"] = f"/guides/{guide['id']}"
        guide["location"] = guide.get("location") or "Singapore specialist"
        guide["languages"] = guide.get("primary_language") or "English"
        guide["rating"] = guide.get("rating") or 4.9
        guide["bio"] = guide.get("bio") or "Here to help you uncover the places and moments you’ll remember."
    return render_template("guides.html", guides=records)


@public_bp.get("/guides/<guide_id>")
def guide_profile(guide_id: str):
    guide = query_one(
        """SELECT g.*, COALESCE(u.full_name, u.name, g.display_name) AS name,
                  COALESCE(g.photo_url, u.profile_image_url, u.avatar_url, '') AS avatar,
                  COALESCE(u.country, 'Singapore') AS location,
                  JSON_EXTRACT(g.languages_json, '$[0]') AS primary_language,
                  JSON_EXTRACT(g.specialties_json, '$[0]') AS specialty
              FROM guides g
              LEFT JOIN users u ON u.id = g.user_id
              WHERE g.id = ? AND g.is_active = 1""",
        (guide_id,),
    )
    if not guide:
        abort(404)
    guide["avatar"] = resolve_image(guide.get("avatar") or None)
    guide["photo"] = guide.get("avatar")
    guide["location"] = guide.get("location") or "Singapore specialist"
    guide["languages"] = guide.get("primary_language") or "English"
    guide["rating"] = guide.get("rating") or 4.9
    guide["bio"] = guide.get("bio") or "Here to help you uncover the places and moments you’ll remember."
    return render_template("guide_profile.html", guide=guide)


@public_bp.route("/guides/<guide_id>/chat", methods=["GET", "POST"])
@login_required
def guide_chat(guide_id: str):
    guide = query_one(
        """SELECT g.*, COALESCE(u.full_name, u.name, g.display_name) AS name
              FROM guides g
              LEFT JOIN users u ON u.id = g.user_id
              WHERE g.id = ? AND g.is_active = 1""",
        (guide_id,),
    )
    if not guide:
        abort(404)
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if not body or len(body) > 2000:
            return render_template("guide_chat.html", guide=guide, messages=[], error="Write a message up to 2,000 characters."), 400
        get_db().execute(
            """INSERT INTO guide_messages
               (id, guide_id, sender_user_id, recipient_user_id, body, created_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (uuid4().hex, guide_id, g.user_id, None, body),
        )
        get_db().commit()
    messages = query_all(
        """SELECT m.*, u.full_name AS sender_name FROM guide_messages m
           LEFT JOIN users u ON u.id = m.sender_user_id
           WHERE m.guide_id = ? AND (m.sender_user_id = ? OR m.recipient_user_id = ?)
           ORDER BY m.created_at""",
        (guide_id, g.user_id, g.user_id),
    )
    return render_template("guide_chat.html", guide=guide, messages=messages)


@public_bp.route("/reviews", methods=["GET", "POST"])
def reviews():
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("public.login", next="/reviews"))
        try:
            rating = int(request.form.get("rating", "0"))
        except ValueError:
            rating = 0
        trip_id = request.form.get("trip_id", "")
        trip = query_one(
            """SELECT id, destination_id FROM trips
               WHERE id = ? AND user_id = ? AND status = 'completed'""",
            (trip_id, session["user_id"]),
        )
        if not trip or not 1 <= rating <= 5:
            return render_template("reviews.html", reviews=_public_reviews(), error="Reviews require a completed trip and a rating from 1 to 5."), 400
        title = request.form.get("title", "").strip()[:160]
        body = request.form.get("body", "").strip()[:5000]
        if query_one(
            "SELECT id FROM reviews WHERE user_id = ? AND trip_id = ?",
            (session["user_id"], trip_id),
        ):
            return render_template("reviews.html", reviews=_public_reviews(), error="You have already reviewed this trip."), 409
        try:
            get_db().execute(
                """INSERT INTO reviews
                   (id, user_id, destination_id, trip_id, rating, title, body, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'published', CURRENT_TIMESTAMP)""",
                (uuid4().hex, session["user_id"], trip["destination_id"], trip_id, rating, title, body),
            )
            get_db().commit()
        except Exception:
            get_db().rollback()
            current_app.logger.exception("Could not save a verified trip review.")
            abort(500)
        return redirect(url_for("public.reviews"))
    return render_template("reviews.html", reviews=_public_reviews())


def _public_reviews():
    return query_all(
        """SELECT r.*, u.full_name AS reviewer_name, d.name AS destination_name
           FROM reviews r JOIN users u ON u.id = r.user_id
           LEFT JOIN destinations d ON d.id = r.destination_id
           WHERE r.status = 'published' ORDER BY r.created_at DESC LIMIT 40"""
    )


@public_bp.get("/pricing")
def pricing():
    return render_template("pricing.html")
