from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from typing import Any
from urllib.parse import quote

import requests
from flask import current_app


class TravelInventoryError(RuntimeError):
    pass


def inventory_result(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str,
    adults: int,
    currency: str = "INR",
    hotel_destination_code: str | None = None,
    flight_destination: str | None = None,
) -> dict[str, Any]:
    flights: list[dict[str, Any]] = []
    hotels: list[dict[str, Any]] = []
    notices = []
    flight_status = "not_configured"
    hotel_status = "not_configured"
    flight_notice = ""
    hotel_notice = ""
    origin_code = _flight_location(origin)
    destination_code = _flight_location(flight_destination or destination)

    if current_app.config.get("SERPAPI_API_KEY"):
        try:
            flights = _search_flights(
                origin_code, destination_code, departure_date,
                return_date, adults, currency,
            )
            flight_status = "available" if flights else "no_results"
            if not flights:
                flight_notice = "No flight offers were returned for this route and date range."
                notices.append(flight_notice)
        except TravelInventoryError as exc:
            flight_status = "unavailable"
            flight_notice = str(exc)
            notices.append(flight_notice)
            current_app.logger.warning("SerpApi flight search unavailable: %s", exc)
    else:
        flight_notice = "Flight search is not configured; add SERPAPI_API_KEY."
        notices.append(flight_notice)

    if (
        current_app.config.get("HOTELBEDS_API_KEY")
        and current_app.config.get("HOTELBEDS_API_SECRET")
    ):
        try:
            hotels = _search_hotels(
                destination,
                departure_date,
                return_date,
                adults,
                currency,
                hotel_destination_code,
            )
            hotel_status = "available" if hotels else "no_results"
            if not hotels:
                hotel_notice = "Hotelbeds returned no hotel offers for this destination and date range."
                notices.append(hotel_notice)
        except TravelInventoryError as exc:
            hotel_status = "unavailable"
            hotel_notice = str(exc)
            notices.append(hotel_notice)
            current_app.logger.warning("Hotelbeds search unavailable: %s", exc)
    else:
        hotel_notice = "Hotel search is not configured; add HOTELBEDS_API_KEY and HOTELBEDS_API_SECRET."
        notices.append(hotel_notice)

    any_available = flight_status == "available" or hotel_status == "available"
    return {
        "status": "available" if any_available else "unavailable",
        "provider": "SerpApi + Hotelbeds",
        "origin_code": origin_code,
        "destination_code": destination_code,
        "flight_status": flight_status,
        "hotel_status": hotel_status,
        "flight_notice": flight_notice,
        "hotel_notice": hotel_notice,
        "flights": flights,
        "hotels": hotels,
        "notice": " ".join(notices) or "Current offers are shown below.",
    }


def _flight_location(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized):
        return value.strip()
    return normalized


def _search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str,
    adults: int,
    currency: str,
) -> list[dict[str, Any]]:
    if not origin or not destination:
        raise TravelInventoryError(
            "Enter the departure and destination airport IATA codes (for example, DEL and CDG) for flight search."
        )
    if not re.fullmatch(r"[A-Z]{3}", origin) or not re.fullmatch(r"[A-Z]{3}", destination):
        raise TravelInventoryError(
            "Flight search needs three-letter IATA airport codes, such as DEL and CDG."
        )
    try:
        start = date.fromisoformat(departure_date)
        end = date.fromisoformat(return_date)
    except (TypeError, ValueError) as exc:
        raise TravelInventoryError("Enter valid dates to search for flights.") from exc
    if end <= start or not 1 <= adults <= 9:
        raise TravelInventoryError("Flight search requires a return date after departure and 1–9 travelers.")
    if not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise TravelInventoryError("Choose a valid three-letter currency code.")

    payload = _serpapi_request({
        "engine": "google_flights",
        "type": "1",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": start.isoformat(),
        "return_date": end.isoformat(),
        "adults": adults,
        "currency": currency.upper(),
        "hl": "en",
    })
    results = []
    for key in ("best_flights", "other_flights"):
        offers = payload.get(key, [])
        if not isinstance(offers, list):
            continue
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            legs = []
            for flight in offer.get("flights", []):
                if not isinstance(flight, dict):
                    continue
                departure = flight.get("departure_airport", {})
                arrival = flight.get("arrival_airport", {})
                legs.append({
                    "from": departure.get("id", "") if isinstance(departure, dict) else "",
                    "from_name": departure.get("name", "") if isinstance(departure, dict) else "",
                    "to": arrival.get("id", "") if isinstance(arrival, dict) else "",
                    "to_name": arrival.get("name", "") if isinstance(arrival, dict) else "",
                    "departure": departure.get("time", "") if isinstance(departure, dict) else "",
                    "arrival": arrival.get("time", "") if isinstance(arrival, dict) else "",
                    "duration": flight.get("duration", ""),
                    "airline": flight.get("airline", ""),
                    "airline_logo": flight.get("airline_logo", ""),
                    "flight_number": flight.get("flight_number", ""),
                })
            price = offer.get("price")
            if legs and isinstance(price, (int, float, str)):
                results.append({
                    "price": price,
                    "currency": currency.upper(),
                    "legs": legs,
                    "stops": len(offer.get("layovers", [])),
                    "source": "SerpApi / Google Flights",
                })
    return sorted(results, key=lambda offer: _price_key(offer["price"]))[:5]


def _search_hotels(
    destination: str,
    check_in: str,
    check_out: str,
    adults: int,
    currency: str,
    destination_code: str | None,
) -> list[dict[str, Any]]:
    if not destination:
        raise TravelInventoryError("A destination is required for hotel search.")
    try:
        start = date.fromisoformat(check_in)
        end = date.fromisoformat(check_out)
    except (TypeError, ValueError) as exc:
        raise TravelInventoryError("Enter valid dates to search hotel availability.") from exc
    if end <= start or not 1 <= adults <= 9:
        raise TravelInventoryError("Hotel search requires a return date after arrival and 1–9 travelers.")
    if not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise TravelInventoryError("Choose a valid three-letter currency code.")

    hotel_destination = (destination_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,10}", hotel_destination):
        hotel_destination = _hotelbeds_destination_code(destination)
    payload = _hotelbeds_request(
        "POST",
        "/hotel-api/1.0/hotels",
        {
            "stay": {"checkIn": start.isoformat(), "checkOut": end.isoformat()},
            "occupancies": [{"rooms": 1, "adults": adults, "children": 0}],
            "destination": {"code": hotel_destination},
            "filter": {"maxHotels": 20},
        },
    )
    hotel_data = payload.get("hotels", {})
    records = hotel_data.get("hotels", []) if isinstance(hotel_data, dict) else []
    offers = []
    if not isinstance(records, list):
        return offers
    for hotel in records:
        if not isinstance(hotel, dict):
            continue
        for room in hotel.get("rooms", []):
            if not isinstance(room, dict):
                continue
            for rate in room.get("rates", []):
                if not isinstance(rate, dict):
                    continue
                price = rate.get("net")
                if price is None:
                    continue
                offers.append({
                    "hotel_code": str(hotel.get("code", "")),
                    "name": hotel.get("name", "Hotel"),
                    "category": hotel.get("categoryName") or hotel.get("categoryCode"),
                    "destination": hotel.get("destinationName") or hotel_destination,
                    "room": room.get("name", ""),
                    "board": rate.get("boardName", ""),
                    "price": price,
                    "currency": rate.get("currency", currency.upper()),
                    "check_in": start.isoformat(),
                    "check_out": end.isoformat(),
                    "source": "Hotelbeds",
                })
    ranked = sorted(offers, key=lambda offer: _price_key(offer["price"]))
    unique_offers = []
    seen_hotels = set()
    for offer in ranked:
        hotel_key = offer["hotel_code"] or offer["name"].casefold()
        if hotel_key in seen_hotels:
            continue
        seen_hotels.add(hotel_key)
        unique_offers.append(offer)
        if len(unique_offers) == 5:
            break
    for offer in unique_offers:
        if offer["hotel_code"]:
            try:
                offer["image_url"] = _hotelbeds_hotel_image_url(offer["hotel_code"])
            except TravelInventoryError as exc:
                current_app.logger.warning(
                    "Hotelbeds image lookup failed for hotel %s: %s",
                    offer["hotel_code"],
                    exc,
                )
                offer["image_url"] = None
    return unique_offers


def _hotelbeds_destination_code(destination: str) -> str:
    cache = current_app.extensions.setdefault("hotelbeds_destination_codes", {})
    cache_key = destination.strip().casefold()
    if cache_key in cache:
        return cache[cache_key]
    known_destinations = {
        "singapore": "SIN",
        "singapore city": "SIN",
    }
    if cache_key in known_destinations:
        cache[cache_key] = known_destinations[cache_key]
        return known_destinations[cache_key]
    payload = _hotelbeds_request(
        "GET",
        "/hotel-content-api/1.0/locations/destinations",
        params={"fields": "all", "language": "ENG", "from": 1, "to": 1000},
    )
    destinations = payload.get("destinations", [])
    if not isinstance(destinations, list):
        destinations = []
    wanted = destination.strip().casefold()
    matches = []
    for record in destinations:
        if not isinstance(record, dict):
            continue
        name_data = record.get("name", {})
        name = name_data.get("content", "") if isinstance(name_data, dict) else ""
        if name and isinstance(record.get("code"), str):
            matches.append((name.strip().casefold(), record["code"]))
    exact = next((code for name, code in matches if name == wanted), None)
    partial = next(
        (code for name, code in matches if wanted in name or name in wanted),
        None,
    )
    code = exact or partial
    if not code:
        raise TravelInventoryError(
            f"Hotelbeds could not match “{destination}” to a destination. Check the hotel destination list or configure its code."
        )
    cache[cache_key] = code
    return code


def _hotelbeds_hotel_image_url(hotel_code: str) -> str | None:
    cache = current_app.extensions.setdefault("hotelbeds_hotel_images", {})
    if hotel_code in cache:
        return cache[hotel_code]
    payload = _hotelbeds_request(
        "GET",
        f"/hotel-content-api/1.0/hotels/{quote(hotel_code, safe='')}/details",
        params={"language": "ENG", "useSecondaryLanguage": "true"},
    )
    hotel = payload.get("hotel", payload)
    images = hotel.get("images", []) if isinstance(hotel, dict) else []
    paths = [
        image.get("path")
        for image in images
        if isinstance(image, dict)
        and isinstance(image.get("path"), str)
        and re.fullmatch(r"[A-Za-z0-9/_\-.]+", image["path"])
        and ".." not in image["path"].split("/")
    ] if isinstance(images, list) else []
    image_url = (
        f"https://photos.hotelbeds.com/giata/bigger/{paths[0]}"
        if paths else None
    )
    cache[hotel_code] = image_url
    return image_url


def _serpapi_request(params: dict[str, Any]) -> dict[str, Any]:
    params["api_key"] = current_app.config["SERPAPI_API_KEY"]
    try:
        response = requests.get(
            "https://serpapi.com/search.json",
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise TravelInventoryError("SerpApi could not be reached; retry the flight search.") from exc
    if response.status_code in {401, 403}:
        raise TravelInventoryError("SerpApi rejected the API key.")
    if response.status_code == 429:
        raise TravelInventoryError("SerpApi rate limit reached; retry later.")
    if response.status_code >= 400:
        raise TravelInventoryError("SerpApi could not search flights for those trip details.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TravelInventoryError("SerpApi returned an unreadable flight-search response.") from exc
    if not isinstance(payload, dict):
        raise TravelInventoryError("SerpApi returned an invalid flight-search response.")
    if payload.get("error"):
        raise TravelInventoryError(
            "SerpApi rejected the search request. Check the API key, account quota, and route parameters."
        )
    return payload


def _hotelbeds_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = str(current_app.config["HOTELBEDS_API_KEY"])
    secret = str(current_app.config["HOTELBEDS_API_SECRET"])
    timestamp = str(int(time.time()))
    signature = hashlib.sha256(
        f"{api_key}{secret}{timestamp}".encode("utf-8")
    ).hexdigest()
    base_url = str(current_app.config["HOTELBEDS_BASE_URL"]).rstrip("/")
    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            headers={
                "Api-key": api_key,
                "X-Signature": signature,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise TravelInventoryError("Hotelbeds could not be reached; retry the hotel search.") from exc
    if response.status_code in {401, 403}:
        raise TravelInventoryError(
            "Hotelbeds rejected the API credentials. Confirm your key is active in the selected environment."
        )
    if response.status_code == 429:
        raise TravelInventoryError("Hotelbeds rate limit reached; retry later.")
    if response.status_code >= 400:
        raise TravelInventoryError(
            f"Hotelbeds hotel search failed with HTTP {response.status_code}. Check the destination and dates."
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise TravelInventoryError("Hotelbeds returned an unreadable response.") from exc
    if not isinstance(result, dict):
        raise TravelInventoryError("Hotelbeds returned an invalid response.")
    errors = result.get("error")
    if errors:
        messages = [
            item.get("message", "Hotel search failed.")
            for item in errors if isinstance(item, dict)
        ] if isinstance(errors, list) else []
        raise TravelInventoryError(" ".join(messages)[:300] or "Hotelbeds hotel search failed.")
    return result


def _price_key(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")
