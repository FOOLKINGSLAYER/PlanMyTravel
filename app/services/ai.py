from __future__ import annotations

import json
from typing import Any

import requests
from flask import current_app


TIERS = ("luxury", "comfort", "budget")
PLAN_KEYS = {"tier", "summary", "estimated_total", "days"}
DAY_KEYS = {"title", "items"}
ITEM_KEYS = {
    "title",
    "category",
    "description",
    "start_time",
    "duration_min",
    "est_cost",
    "lat",
    "lng",
}


class ItineraryGenerationError(RuntimeError):
    pass


def validate_itinerary_payload(payload: Any, expected_days: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {"plans"}:
        raise ValueError("Response must contain only a plans list.")
    plans = payload["plans"]
    if not isinstance(plans, list) or len(plans) != 3:
        raise ValueError("Response must contain exactly three plans.")

    tiers = []
    for plan in plans:
        validate_plan(plan, expected_days)
        tier = plan["tier"]
        if tier not in TIERS or tier in tiers:
            raise ValueError("Plan tiers must be luxury, comfort, and budget.")
        tiers.append(tier)
    if set(tiers) != set(TIERS):
        raise ValueError("Response did not include all three plan tiers.")
    return plans


def validate_plan(plan: Any, expected_days: int) -> dict[str, Any]:
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise ValueError("A plan is missing required fields.")
    if plan["tier"] not in TIERS:
        raise ValueError("The plan tier is invalid.")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise ValueError("Plan summary must be non-empty text.")
    if not _is_number(plan["estimated_total"]):
        raise ValueError("Plan estimated_total must be numeric.")
    days = plan["days"]
    if not isinstance(days, list) or len(days) != expected_days:
        raise ValueError("Each plan must have one day for each trip day.")
    for day in days:
        if not isinstance(day, dict) or set(day) != DAY_KEYS:
            raise ValueError("Each day must contain a title and items.")
        if not isinstance(day["title"], str) or not isinstance(day["items"], list):
            raise ValueError("Day title and items have invalid types.")
        for item in day["items"]:
            if not isinstance(item, dict) or set(item) != ITEM_KEYS:
                raise ValueError("An itinerary item has missing or unexpected fields.")
            if not all(isinstance(item[key], str) for key in ("title", "category", "description", "start_time")):
                raise ValueError("Itinerary item text fields have invalid types.")
            if not isinstance(item["duration_min"], int) or item["duration_min"] < 0:
                raise ValueError("Item duration must be a non-negative integer.")
            if not all(_is_number(item[key]) for key in ("est_cost", "lat", "lng")):
                raise ValueError("Item costs and coordinates must be numeric.")
    return plan


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def generate_itineraries(data: dict[str, Any]) -> list[dict[str, Any]]:
    days = data["trip_days"]
    prompt = f"""
Create exactly three practical travel itineraries in strict JSON for this trip:
Destination: {data['destination']}
Dates: {data['start_date']} to {data['end_date']} ({days} days)
Travelers: {data['travelers']}
Total budget: {data['total_budget']} {data['currency']}
Travel style: {data['travel_style']}
Interests: {", ".join(data['interests']) or "general sightseeing"}
Traveler notes: {data.get("personal_notes") or "None"}
Live flight and hotel options, when provided, are real inventory search results.
Do not invent bookings, prices, availability, or provider links. Reflect the
available options in the trip summary where helpful:
{json.dumps(data.get("travel_inventory", {}), ensure_ascii=False)[:8000]}

Return exactly this shape, with no markdown or extra keys:
{{"plans":[{{"tier":"luxury|comfort|budget","summary":"...","estimated_total":0,
"days":[{{"title":"...","items":[{{"title":"...","category":"...","description":"...",
"start_time":"09:00","duration_min":60,"est_cost":0,"lat":0.0,"lng":0.0}}]}}]}}]}}
Use exactly one plan for each tier: luxury, comfort, budget. Each plan must have
exactly {days} days. Keep each plan's estimated_total within the provided budget.
"""
    keys = current_app.config.get("GEMINI_API_KEYS", [])
    if not keys:
        raise ItineraryGenerationError(
            "AI itinerary generation is not configured. Add a Gemini API key to .env."
        )

    last_error = "Gemini returned an invalid response."
    for attempt in range(2):
        for api_key in keys:
            try:
                response = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{current_app.config['GEMINI_MODEL']}:generateContent",
                    params={"key": api_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json"},
                    },
                    timeout=45,
                )
                if response.status_code in {400, 401, 403, 404, 429, 500, 503}:
                    last_error = f"Gemini request failed with status {response.status_code}."
                    continue
                response.raise_for_status()
                body = response.json()
                text = body["candidates"][0]["content"]["parts"][0]["text"]
                plans = validate_itinerary_payload(json.loads(text), days)
                return plans
            except (requests.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc) or last_error
        if attempt == 0:
            prompt += "\nEnsure the JSON matches the schema exactly; this is a correction attempt."
    current_app.logger.warning("Itinerary generation failed after retry: %s", last_error)
    raise ItineraryGenerationError(
        "We couldn't create a valid itinerary right now. Please try again."
    )
