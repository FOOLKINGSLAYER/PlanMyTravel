from __future__ import annotations

import sys

import requests

from app.config import load_config


def main() -> int:
    config = load_config()
    api_token = config["TURSO_API_TOKEN"]
    organization = config["TURSO_ORG"]
    if not api_token or not organization:
        print("Set TURSO_API_TOKEN and TURSO_ORG in .env first.", file=sys.stderr)
        return 2

    name = input("New Turso database name [planmytravel]: ").strip() or "planmytravel"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    response = requests.post(
        f"https://api.turso.tech/v1/organizations/{organization}/databases",
        headers=headers,
        json={"name": name, "group": "default"},
        timeout=30,
    )
    if response.status_code not in {200, 201, 409}:
        raise RuntimeError(f"Turso database creation failed ({response.status_code}).")
    payload = response.json()
    hostname = payload.get("database", {}).get("Hostname")
    if not hostname:
        raise RuntimeError("Turso created a database but did not return its hostname.")

    token_response = requests.post(
        f"https://api.turso.tech/v1/organizations/{organization}/databases/{name}/auth/tokens",
        headers=headers,
        json={"expiration": "never"},
        timeout=30,
    )
    token_response.raise_for_status()
    auth_token = token_response.json().get("jwt")
    if not auth_token:
        raise RuntimeError("Turso did not return a database auth token.")
    print(f"Database URL: libsql://{hostname}")
    print(f"Database auth token: {auth_token}")
    print("Add both values to .env. Keep the token secret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
