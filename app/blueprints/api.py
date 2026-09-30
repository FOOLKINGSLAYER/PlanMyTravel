from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, g, jsonify, request, session

from app.db import get_db
from app.rate_limit import enforce_rate_limit
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
        user_id = session.get("user_id")
        if not user_id:
            return _error("Please sign in to continue.", 401)
        g.user_id = user_id
        return view(*args, **kwargs)

    return wrapped


@api_bp.get("/health")
def health():
    return jsonify({"status": "ok", "app": current_app.config["APP_NAME"]})


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
    ):
        return _error("Some itinerary details are outside the supported range.", 400)

    connection = get_db()
    destination_row = connection.execute(
        "SELECT id FROM destinations WHERE lower(name) = lower(?) OR lower(slug) = lower(?)",
        (destination.strip(), destination.strip()),
    ).fetchone()
    if not destination_row:
        return _error("Choose a destination from the available list.", 404)

    form = {
        "destination": destination.strip(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "trip_days": day_count,
        "travelers": travelers,
        "total_budget": float(budget),
        "currency": currency.upper(),
        "travel_style": travel_style.strip().lower(),
        "interests": interests,
    }
    try:
        plans = generate_itineraries(form)
    except ItineraryGenerationError as exc:
        return _error(str(exc), 503)

    from app.blueprints.public import _attach_weather, _save_generated_trip

    try:
        trip_id = _save_generated_trip(form, plans)
        destination_coords = connection.execute(
            "SELECT lat, lng FROM destinations WHERE id = ?",
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
    return jsonify({"trip_id": trip_id, "plans": plans}), 201


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
