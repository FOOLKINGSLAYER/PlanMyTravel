from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db
from app.db_utils import query_all, query_one
from app.rate_limit import enforce_rate_limit
from app.security import login_required
from app.services.ai import ItineraryGenerationError, generate_itineraries
from app.services.media import resolve_image
from app.services.wallet import WalletError, add_funds, charge, wallet_balance


public_bp = Blueprint("public", __name__)


def _published_packages(limit: int = 24) -> list[dict]:
    rows = query_all(
        """SELECT p.*, d.name AS destination_name, d.country AS destination_country
           FROM packages p
           LEFT JOIN destinations d ON d.id = p.destination_id
           WHERE p.status = 'published' AND p.deleted_at IS NULL
             AND (d.is_published = 1 OR d.id IS NULL)
           ORDER BY p.featured DESC, p.created_at DESC
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
        "SELECT relative_path, alt_text FROM package_images WHERE package_id = ? ORDER BY sort_order, created_at",
        (package["id"],),
    )
    image_path = image_rows[0]["relative_path"] if image_rows else None
    title = package.get("title") or package.get("name") or "Travel package"
    duration = package.get("duration_days") or package.get("duration") or 1
    destination = package.get("destination_name") or package.get("destination_country") or "Travel"
    return {
        **package,
        "title": title,
        "summary": package.get("description") or "",
        "location": f"{destination} · {duration} days",
        "duration": f"{duration} days",
        "price": _price_text(minimum_price, package.get("currency", "INR")),
        "pricing": json.dumps(pricing),
        "image": resolve_image(image_path),
        "image_alt": image_rows[0]["alt_text"] if image_rows else title,
        "images": [
            {
                "id": row["relative_path"],
                "url": resolve_image(row["relative_path"]),
                "filename": row["relative_path"],
                "alt": row["alt_text"] or title,
            }
            for row in image_rows
        ],
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


def _attach_guest_trips(user_id: str) -> None:
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
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if not session.get("user_id"):
        _save_guest_trip(trip_id)
    return trip_id


def _persist_itinerary_items(connection, plan_id: str, plan: dict, data: dict) -> None:
    for index, day in enumerate(plan["days"], start=1):
        day_id = uuid4().hex
        day_date = (date.fromisoformat(data["start_date"])).toordinal() + index - 1
        day_iso = date.fromordinal(day_date).isoformat()
        connection.execute(
            """INSERT INTO itinerary_days
               (id, plan_id, day_number, date, title, weather_json, notes)
               VALUES (?, ?, ?, ?, ?, NULL, NULL)""",
            (day_id, plan_id, index, day_iso, day["title"]),
        )
        previous = None
        for sort_order, item in enumerate(day["items"]):
            item_id = uuid4().hex
            route_json = None
            if previous is not None:
                route_json = _cached_route(previous, (item["lat"], item["lng"]))
            connection.execute(
                """INSERT INTO itinerary_items
                   (id, day_id, sort_order, title, category, description, start_time,
                    duration_min, est_cost, lat, lng, image_path, route_from_prev_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    item_id,
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
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "travelers": travelers,
        "total_budget": float(total_budget),
        "currency": "INR",
        "travel_style": str(form_data.get("travel_style", form_data.get("pace", "balanced"))),
        "interests": interests[:15],
    }
    destination_row = query_one(
        "SELECT id, lat, lng FROM destinations WHERE lower(name) = lower(?) OR lower(slug) = lower(?)",
        (destination, destination),
    )
    if not destination_row:
        return None, "Choose a destination from the available list."
    data["trip_days"] = day_count
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
    for day in payload.get("days", []):
        days_list.append(
            {
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
    )


@public_bp.get("/search")
def search():
    packages = _published_packages()
    query = request.args.get("q", "").strip().lower()
    if query:
        packages = [
            item
            for item in packages
            if query in item["title"].lower()
            or query in (item.get("destination_name") or "").lower()
            or query in (item.get("summary") or "").lower()
        ]
    destinations = query_all(
        "SELECT id, name, slug FROM destinations WHERE is_published = 1 ORDER BY name"
    )
    return render_template("search.html", packages=packages, destinations=destinations)


@public_bp.get("/destinations/<slug>")
def destination_detail(slug: str):
    destination = _destination_by_slug(slug)
    if not destination:
        abort(404)
    packages = [
        item for item in _published_packages()
        if item.get("destination_id") == destination["id"]
    ]
    destination = {
        **destination,
        "hero_image": resolve_image(destination.get("hero_image_path")),
    }
    return render_template(
        "destination.html",
        destination=destination,
        packages=packages,
        reviews=query_all(
            """SELECT r.*, u.full_name AS reviewer_name FROM reviews r
               LEFT JOIN users u ON u.id = r.user_id
               WHERE r.destination_id = ? AND r.status = 'published'
               ORDER BY r.created_at DESC LIMIT 12""",
            (destination["id"],),
        ),
    )


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
        enforce_rate_limit("itinerary-generation", 5, 60)
        trip_id, error = _generate_from_form(request.form)
        if error:
            return render_template("itinerary_form.html", error=error), 400
        return redirect(url_for("public.itinerary_results", trip_id=trip_id))
    destinations = query_all(
        "SELECT name, slug FROM destinations WHERE is_published = 1 ORDER BY name"
    )
    return render_template("itinerary_form.html", destinations=destinations)


@public_bp.get("/itinerary/results")
def itinerary_results():
    trip_id = request.args.get("trip_id", "")
    trip = _owned_trip(trip_id) if trip_id else None
    if not trip:
        abort(404)
    plans = _trip_plans(trip_id)
    if not plans:
        abort(404)
    for plan in plans:
        plan["days"] = plan["payload"].get("days", [])
    active = next((plan for plan in plans if plan["tier"] == trip.get("selected_tier")), plans[1] if len(plans) > 1 else plans[0])
    itinerary = _format_itinerary(trip, active)
    return render_template("itinerary_results.html", trip=trip, plans=plans, active_plan=active, itinerary=itinerary)


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
    enforce_rate_limit("itinerary-generation", 5, 60)
    trip_id, error = _generate_from_form(request.form)
    if error:
        return render_template("itinerary_form.html", error=error), 400
    return redirect(url_for("public.itinerary_results", trip_id=trip_id))


@public_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or len(name) > 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return render_template("signup.html", error="Enter your name and a valid email address."), 400
        if len(password) < 8:
            return render_template("signup.html", error="Your password must be at least 8 characters."), 400
        if query_one("SELECT id FROM users WHERE email = ?", (email,)):
            return render_template("signup.html", error="We couldn't create that account. Try signing in."), 409
        user_id = uuid4().hex
        connection = get_db()
        connection.execute(
            """INSERT INTO users (id, email, password_hash, full_name, role, created_at)
               VALUES (?, ?, ?, ?, 'traveler', CURRENT_TIMESTAMP)""",
            (user_id, email, generate_password_hash(password), name),
        )
        _attach_guest_trips(user_id)
        connection.commit()
        session.clear()
        session["user_id"] = user_id
        session.permanent = True
        return redirect(url_for("public.trips"))
    return render_template("signup.html")


@public_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        enforce_rate_limit("traveler-login", 10, 300)
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = query_one(
            "SELECT id, password_hash, is_active FROM users WHERE email = ?",
            (email,),
        )
        if not user or not user.get("is_active") or not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Email or password is incorrect."), 401
        guest_trip_ids = session.get("guest_trip_ids", [])
        _attach_guest_trips(user["id"])
        session.clear()
        session["user_id"] = user["id"]
        session["guest_trip_ids"] = guest_trip_ids
        session.permanent = True
        return redirect(_safe_next(request.args.get("next") or request.form.get("next")))
    return render_template("login.html")


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("public.trips")


@public_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("public.home"))


@public_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = query_one("SELECT id, email, full_name AS name FROM users WHERE id = ?", (g.user_id,))
    if not user:
        session.clear()
        return redirect(url_for("public.login"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 100:
            return render_template("account.html", user=user, error="Enter a name up to 100 characters."), 400
        get_db().execute("UPDATE users SET full_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (name, g.user_id))
        get_db().commit()
        user["name"] = name
    return render_template("account.html", user=user)


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


@public_bp.route("/wallet", methods=["GET", "POST"])
@login_required
def wallet():
    if request.method == "POST":
        try:
            amount = Decimal(request.form.get("amount", "0"))
            if amount > Decimal("1000000"):
                raise WalletError("Top-ups are limited to ₹1,000,000.")
            balance = add_funds(get_db(), g.user_id, amount)
        except (InvalidOperation, WalletError) as exc:
            return render_template("wallet.html", error=str(exc), balance=wallet_balance(get_db(), g.user_id)), 400
        flash(f"Wallet topped up. New balance: ₹{balance:,.2f}", "success")
    return render_template(
        "wallet.html",
        balance=wallet_balance(get_db(), g.user_id),
        transactions=query_all(
            """SELECT wt.*, wt.type AS transaction_type FROM wallet_transactions wt
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
    records = query_all(
        """SELECT g.*, u.full_name AS name, u.profile_image_path AS avatar
           FROM guides g JOIN users u ON u.id = g.user_id
           WHERE g.verified = 1 ORDER BY g.rating_avg DESC, u.full_name LIMIT 40"""
    )
    return render_template("guides.html", guides=records)


@public_bp.get("/guides/<guide_id>")
def guide_profile(guide_id: str):
    guide = query_one(
        """SELECT g.*, u.full_name AS name, u.profile_image_path AS avatar
           FROM guides g JOIN users u ON u.id = g.user_id
           WHERE g.id = ? AND g.verified = 1""",
        (guide_id,),
    )
    if not guide:
        abort(404)
    return render_template("guide_profile.html", guide=guide)


@public_bp.route("/guides/<guide_id>/chat", methods=["GET", "POST"])
@login_required
def guide_chat(guide_id: str):
    guide = query_one(
        """SELECT g.*, u.full_name AS name FROM guides g
           JOIN users u ON u.id = g.user_id WHERE g.id = ? AND g.verified = 1""",
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
               (id, guide_id, traveler_id, body, created_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (uuid4().hex, guide_id, g.user_id, body),
        )
        get_db().commit()
    messages = query_all(
        """SELECT m.*, u.full_name AS sender_name FROM guide_messages m
           LEFT JOIN users u ON u.id = m.traveler_id
           WHERE m.guide_id = ? AND m.traveler_id = ?
           ORDER BY m.created_at""",
        (guide_id, g.user_id),
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
