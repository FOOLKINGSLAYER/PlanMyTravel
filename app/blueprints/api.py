from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, g, jsonify, request, session

from app.db import get_db
from app.rate_limit import enforce_rate_limit
from app.security import authenticated_user
from app.services.travel_inventory import inventory_result
from app.services.ai import (
    ItineraryGenerationError,
    generate_itineraries,
    validate_plan,
)


api_bp = Blueprint("api", __name__, url_prefix="/api")


def _error(message: str, status: int):
    return jsonify({"error": {"message": message, "status": status}}), status


def api_login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if not authenticated_user():
            return _error("Please sign in to continue.", 401)
        return view(*args, **kwargs)

    return wrapped


@api_bp.get("/health")
def health():
    return jsonify({"status": "ok", "app": current_app.config["APP_NAME"]})


@api_bp.post("/favorites")
@api_login_required
def save_favorite():
    payload = request.get_json(silent=True)
    item_type = payload.get("item_type") if isinstance(payload, dict) else None
    item_id = payload.get("item_id") if isinstance(payload, dict) else None
    catalog = {
        "destination": ("destinations", "is_published = 1"),
        "package": ("packages", "status = 'published' AND deleted_at IS NULL"),
        "experience": ("experiences", "status = 'published' AND is_published = 1"),
    }
    if item_type not in catalog or not isinstance(item_id, (str, int)) or len(str(item_id)) > 40:
        return _error("Choose a valid item to save.", 400)
    table, visibility = catalog[item_type]
    item = get_db().execute(
        f"SELECT id FROM {table} WHERE id = ? AND {visibility}", (item_id,)
    ).fetchone()
    if not item:
        return _error("That item is no longer available.", 404)
    connection = get_db()
    connection.execute(
        """INSERT INTO saved_items (user_id, item_type, item_id)
           VALUES (?, ?, ?) ON CONFLICT(user_id, item_type, item_id) DO NOTHING""",
        (session["user_id"], item_type, str(item_id)),
    )
    connection.commit()
    return jsonify({"saved": True, "item_type": item_type, "item_id": str(item_id)})


@api_bp.delete("/favorites")
@api_login_required
def remove_favorite():
    payload = request.get_json(silent=True)
    item_type = payload.get("item_type") if isinstance(payload, dict) else None
    item_id = payload.get("item_id") if isinstance(payload, dict) else None
    if item_type not in {"destination", "package", "experience"} or not isinstance(item_id, (str, int)):
        return _error("Choose a valid item to remove.", 400)
    connection = get_db()
    connection.execute(
        "DELETE FROM saved_items WHERE user_id = ? AND item_type = ? AND item_id = ?",
        (session["user_id"], item_type, str(item_id)),
    )
    connection.commit()
    return jsonify({"saved": False, "item_type": item_type, "item_id": str(item_id)})


@api_bp.post("/itineraries/generate")
def generate_itinerary():
    enforce_rate_limit("itinerary-generation", 5, 60)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("Provide itinerary details as JSON.", 400)

    destination = payload.get("destination")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    travel_style = payload.get("travel_style", "balanced")
    currency = payload.get("currency", "INR")
    interests = payload.get("interests", [])
    personal_notes = payload.get("notes", "")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        travelers = int(payload.get("travelers", 1))
        budget = Decimal(str(payload.get("total_budget")))
    except (TypeError, ValueError, InvalidOperation):
        return _error("Check your travel dates, traveler count, and budget.", 400)
    day_count = (end - start).days + 1
    if (
        not isinstance(destination, str)
        or not destination.strip()
        or len(destination) > 120
        or end < start
        or day_count > 30
        or not 1 <= travelers <= 20
        or not budget.is_finite()
        or budget <= 0
        or budget > Decimal("100000000")
        or not isinstance(travel_style, str)
        or not re.fullmatch(r"[A-Za-z -]{2,40}", travel_style)
        or not isinstance(currency, str)
        or not re.fullmatch(r"[A-Za-z]{3}", currency)
        or not isinstance(interests, list)
        or len(interests) > 15
        or any(not isinstance(item, str) or len(item) > 60 for item in interests)
        or not isinstance(personal_notes, str)
        or len(personal_notes) > 1000
    ):
        return _error("Some itinerary details are outside the supported range.", 400)

    connection = get_db()
    destination_row = connection.execute(
        "SELECT id FROM destinations WHERE lower(name) = lower(?) OR lower(slug) = lower(?)",
        (destination.strip(), destination.strip()),
    ).fetchone()
    if not destination_row:
        return _error("Choose a destination from the available list.", 404)
    if not current_app.config.get("GEMINI_API_KEYS"):
        return _error(
            "AI trip planning is not configured. Add a Gemini API key to the server environment.",
            503,
        )

    form = {
        "destination": destination.strip(),
        "origin": str(payload.get("origin", "")).strip(),
        "flight_destination": str(payload.get("flight_destination", "")).strip(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "trip_days": day_count,
        "travelers": travelers,
        "total_budget": float(budget),
        "currency": currency.upper(),
        "travel_style": travel_style.strip().lower(),
        "interests": interests,
        "personal_notes": personal_notes.strip(),
    }
    form["inventory"] = inventory_result(
        form["origin"],
        form["destination"],
        form["start_date"],
        form["end_date"],
        travelers,
        form["currency"],
        flight_destination=form["flight_destination"],
    )
    if form["inventory"]["status"] == "available":
        form["travel_inventory"] = {
            "flights": form["inventory"]["flights"],
            "hotels": form["inventory"]["hotels"],
        }
    try:
        plans = generate_itineraries(form)
    except ItineraryGenerationError as exc:
        return _error(str(exc), 503)

    from app.blueprints.public import _attach_weather, _save_generated_trip

    try:
        trip_id = _save_generated_trip(form, plans)
        destination_coords = connection.execute(
            "SELECT latitude AS lat, longitude AS lng FROM destinations WHERE id = ?",
            (destination_row[0],),
        ).fetchone()
        if destination_coords:
            saved_plans = connection.execute(
                "SELECT id, tier FROM itinerary_plans WHERE trip_id = ?",
                (trip_id,),
            ).fetchall()
            for saved_plan in saved_plans:
                plan = next(item for item in plans if item["tier"] == saved_plan[1])
                _attach_weather(
                    saved_plan[0],
                    plan,
                    start.isoformat(),
                    destination_coords[0],
                    destination_coords[1],
                )
    except Exception:
        connection.rollback()
        current_app.logger.exception("Could not save generated itinerary.")
        return _error("Your plans could not be saved. Please retry.", 500)
    return jsonify({
        "trip_id": trip_id,
        "plans": plans,
        "inventory": form["inventory"],
    }), 201


def _current_user_id() -> str:
    return session["user_id"]


@api_bp.patch("/trips/<string:trip_id>/tier")
@api_login_required
def select_tier(trip_id: int):
    payload = request.get_json(silent=True)
    tier = payload.get("tier") if isinstance(payload, dict) else None
    if tier not in {"luxury", "comfort", "budget"}:
        return _error("Choose a valid itinerary tier.", 400)
    connection = get_db()
    cursor = connection.execute(
        "UPDATE trips SET selected_tier = ? WHERE id = ? AND user_id = ?",
        (tier, trip_id, _current_user_id()),
    )
    if cursor.rowcount != 1:
        return _error("Trip not found.", 404)
    connection.commit()
    return jsonify({"trip_id": trip_id, "selected_tier": tier})


@api_bp.put("/itineraries/<string:plan_id>")
@api_login_required
def update_itinerary(plan_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("Provide an itinerary plan as JSON.", 400)
    connection = get_db()
    owner = connection.execute(
        """SELECT t.start_date, t.end_date
           FROM itinerary_plans p JOIN trips t ON t.id = p.trip_id
           WHERE p.id = ? AND t.user_id = ?""",
        (plan_id, _current_user_id()),
    ).fetchone()
    if not owner:
        return _error("Itinerary not found.", 404)
    expected_days = (date.fromisoformat(owner[1]) - date.fromisoformat(owner[0])).days + 1
    try:
        plan = validate_plan(payload, expected_days)
    except (ValueError, TypeError):
        return _error("The edited itinerary has an invalid format.", 400)
    cursor = connection.execute(
        """UPDATE itinerary_plans
           SET summary = ?, estimated_total = ?, json_payload = ?
           WHERE id = ? AND trip_id IN (SELECT id FROM trips WHERE user_id = ?)""",
        (
            plan["summary"],
            plan["estimated_total"],
            json.dumps(plan, ensure_ascii=False),
            plan_id,
            _current_user_id(),
        ),
    )
    if cursor.rowcount != 1:
        return _error("Itinerary not found.", 404)
    connection.commit()
    return jsonify({"plan": plan})
