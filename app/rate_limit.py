from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from flask import abort, request


_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def enforce_rate_limit(bucket: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    key = f"{bucket}:{request.remote_addr or 'unknown'}"
    with _lock:
        attempts = _attempts[key]
        while attempts and now - attempts[0] >= window_seconds:
            attempts.popleft()
        if len(attempts) >= limit:
            abort(429, description="Too many attempts. Please wait and try again.")
        attempts.append(now)
        stale_keys = [
            name
            for name, values in _attempts.items()
            if not values or now - values[-1] >= window_seconds
        ]
        for stale_key in stale_keys:
            del _attempts[stale_key]
