from __future__ import annotations

from typing import Any

import requests


def get_weather(latitude: float, longitude: float, date: str) -> dict[str, Any]:
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "start_date": date,
            "end_date": date,
        },
        timeout=12,
    )
    response.raise_for_status()
    return response.json()


def get_route(origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any]:
    origin_lat, origin_lng = origin
    destination_lat, destination_lng = destination
    response = requests.get(
        "https://router.project-osrm.org/route/v1/driving/"
        f"{origin_lng},{origin_lat};{destination_lng},{destination_lat}",
        params={"overview": "false"},
        timeout=12,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != "Ok" or not body.get("routes"):
        raise ValueError("No route was found between these stops.")
    route = body["routes"][0]
    return {
        "distance_km": round(route["distance"] / 1000, 1),
        "duration_min": round(route["duration"] / 60),
    }
