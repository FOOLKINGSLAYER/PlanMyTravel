from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from flask import g, request

from app.db import get_db


def record_admin_action(
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    changes: dict[str, Any] | None = None,
) -> None:
    get_db().execute(
        """INSERT INTO admin_audit_logs
           (id, admin_id, action, entity_type, entity_id, diff_json, ip, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            uuid4().hex,
            getattr(g, "admin_id", None),
            action,
            entity_type,
            entity_id,
            json.dumps(changes, ensure_ascii=False) if changes is not None else None,
            request.remote_addr,
        ),
    )
    get_db().commit()
