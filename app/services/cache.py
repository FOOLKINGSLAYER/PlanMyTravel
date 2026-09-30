from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_db


def get_cached(key: str) -> Any | None:
    connection = get_db()
    row = connection.execute(
        "SELECT value, expires_at FROM cache WHERE cache_key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    expires_at = row[1]
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                connection.execute("DELETE FROM cache WHERE cache_key = ?", (key,))
                connection.commit()
                return None
        except ValueError:
            connection.execute("DELETE FROM cache WHERE cache_key = ?", (key,))
            connection.commit()
            return None
    return json.loads(row[0])


def set_cached(key: str, value: Any, ttl_seconds: int = 3600) -> None:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).isoformat()
    connection = get_db()
    connection.execute(
        """INSERT INTO cache (cache_key, value, expires_at, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(cache_key) DO UPDATE SET
               value = excluded.value,
               expires_at = excluded.expires_at,
               updated_at = CURRENT_TIMESTAMP""",
        (key, json.dumps(value, ensure_ascii=False), expires_at),
    )
    connection.commit()
