from __future__ import annotations

import hashlib

from flask import Flask

from app.services import travel_inventory


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def test_search_uses_serpapi_flights_and_hotelbeds_availability(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        HOTELBEDS_API_KEY="test-hotelbeds-key",
        HOTELBEDS_API_SECRET="test-hotelbeds-secret",
        HOTELBEDS_BASE_URL="https://api.test.hotelbeds.com",
        SERPAPI_API_KEY="test-serpapi-key",
    )
    hotelbeds_calls = []
    serpapi_calls = []
    monkeypatch.setattr(travel_inventory.time, "time", lambda: 123456)

    def fake_get(url, **kwargs):
        serpapi_calls.append((url, kwargs))
        assert kwargs["params"]["api_key"] == "test-serpapi-key"
        return FakeResponse({
            "best_flights": [{
                "price": 25000,
                "flights": [{
                    "departure_airport": {
                        "id": "BOM",
                        "name": "Mumbai",
                        "time": "2027-01-01 08:00",
                    },
                    "arrival_airport": {
                        "id": "HKT",
                        "name": "Phuket",
                        "time": "2027-01-01 14:00",
                    },
                    "duration": 360,
                    "airline": "Example Air",
                }],
                "layovers": [],
            }],
            "other_flights": [],
        })

    def fake_request(method, url, **kwargs):
        hotelbeds_calls.append((method, url, kwargs))
        headers = kwargs["headers"]
        timestamp = headers["X-Signature"]
        assert len(timestamp) == 64
        if url.endswith("/hotel-content-api/1.0/locations/destinations"):
            return FakeResponse({
                "destinations": [{
                    "code": "HKT",
                    "name": {"content": "Phuket"},
                }],
            })
        if url.endswith("/hotel-api/1.0/hotels"):
            assert kwargs["json"]["destination"] == {"code": "HKT"}
            return FakeResponse({
                "hotels": {"hotels": [{
                    "name": "Example Hotel",
                    "categoryName": "4 STARS",
                    "rooms": [{
                        "name": "Standard room",
                        "rates": [{
                            "net": 12000,
                            "currency": "INR",
                            "boardName": "Breakfast",
                        }],
                    }],
                }]},
            })
        raise AssertionError(f"Unexpected Hotelbeds request: {url}")

    monkeypatch.setattr(travel_inventory.requests, "get", fake_get)
    monkeypatch.setattr(travel_inventory.requests, "request", fake_request)
    with app.app_context():
        inventory = travel_inventory.inventory_result(
            "BOM",
            "Phuket",
            "2027-01-01",
            "2027-01-05",
            2,
            flight_destination="HKT",
        )

    assert inventory["status"] == "available"
    assert inventory["flight_status"] == "available"
    assert inventory["hotel_status"] == "available"
    assert inventory["flights"][0]["legs"][0]["airline"] == "Example Air"
    assert inventory["hotels"][0]["name"] == "Example Hotel"
    assert len(serpapi_calls) == 1
    assert len(hotelbeds_calls) == 2
    hotelbeds_signature = hotelbeds_calls[-1][2]["headers"]["X-Signature"]
    assert hotelbeds_signature == hashlib.sha256(
        b"test-hotelbeds-keytest-hotelbeds-secret123456"
    ).hexdigest()


def test_missing_credentials_are_reported_without_fake_inventory():
    app = Flask(__name__)
    app.config.update(
        HOTELBEDS_API_KEY="",
        HOTELBEDS_API_SECRET="",
        SERPAPI_API_KEY="",
    )
    with app.app_context():
        inventory = travel_inventory.inventory_result(
            "BOM", "Phuket", "2027-01-01", "2027-01-05", 2,
            flight_destination="HKT",
        )

    assert inventory["status"] == "unavailable"
    assert inventory["flight_status"] == "not_configured"
    assert inventory["hotel_status"] == "not_configured"
    assert inventory["flights"] == []
    assert inventory["hotels"] == []
    assert "SERPAPI_API_KEY" in inventory["flight_notice"]
    assert "HOTELBEDS_API_KEY" in inventory["hotel_notice"]


def test_flight_search_rejects_non_iata_locations():
    app = Flask(__name__)
    app.config.update(SERPAPI_API_KEY="test-serpapi-key")
    with app.app_context():
        inventory = travel_inventory.inventory_result(
            "Mumbai", "Phuket", "2027-01-01", "2027-01-05", 2
        )

    assert inventory["flight_status"] == "unavailable"
    assert "three-letter IATA" in inventory["flight_notice"]


def test_hotelbeds_content_image_urls_are_cached(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        HOTELBEDS_API_KEY="test-hotelbeds-key",
        HOTELBEDS_API_SECRET="test-hotelbeds-secret",
        HOTELBEDS_BASE_URL="https://api.test.hotelbeds.com",
    )
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "hotel": {
                "images": [
                    {"path": "07/000001/hotel-front.jpg"},
                    {"path": "../private.jpg"},
                ]
            }
        }

    monkeypatch.setattr(travel_inventory, "_hotelbeds_request", fake_request)
    with app.app_context():
        first = travel_inventory._hotelbeds_hotel_image_url("000001")
        second = travel_inventory._hotelbeds_hotel_image_url("000001")
        singapore_code = travel_inventory._hotelbeds_destination_code("Singapore")

    assert first == "https://photos.hotelbeds.com/giata/bigger/07/000001/hotel-front.jpg"
    assert second == first
    assert singapore_code == "SIN"
    assert len(calls) == 1
