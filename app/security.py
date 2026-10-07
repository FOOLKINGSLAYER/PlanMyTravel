from __future__ import annotations

import hmac
import secrets
from functools import wraps
from typing import Any, Callable

from flask import abort, current_app, g, request, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    expected = session.get("_csrf_token", "")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        abort(400, description="The form expired. Refresh the page and try again.")


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not authenticated_user():
            from flask import redirect, url_for

            return redirect(url_for("public.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def authenticated_user() -> dict[str, Any] | None:
    if hasattr(g, "authenticated_user"):
        return g.authenticated_user

    user_id = session.get("user_id")
    session_version = session.get("auth_version")
    user = None
    if user_id and isinstance(session_version, int):
        from app.db_utils import query_one

        user = query_one(
            """SELECT id, email, full_name AS name, profile_image_url,
                      session_version, is_active
               FROM users WHERE id = ?""",
            (user_id,),
        )

    if (
        not user
        or not user.get("is_active")
        or user.get("session_version") != session_version
    ):
        session.pop("user_id", None)
        session.pop("auth_version", None)
        user = None
    else:
        g.user_id = user["id"]

    g.authenticated_user = user
    return user


def admin_required(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            token = request.cookies.get("admin_session", "")
            if not token:
                abort(403)
            try:
                claims = _admin_serializer().loads(
                    token,
                    max_age=int(current_app.config["ADMIN_SESSION_MINUTES"]) * 60,
                )
            except (BadSignature, SignatureExpired):
                abort(403)
            if not isinstance(claims, dict) or not claims.get("id"):
                abort(403)

            from app.db import get_db

            row = get_db().execute(
                "SELECT id, role, is_active FROM admin_users WHERE id = ?",
                (claims["id"],),
            ).fetchone()
            if not row:
                abort(403)
            record = dict(row) if hasattr(row, "keys") else {
                "id": row[0],
                "role": row[1],
                "is_active": row[2],
            }
            if not record["is_active"] or record["role"] != claims.get("role"):
                abort(403)
            if roles and record["role"] not in roles:
                abort(403)
            g.admin_id = record["id"]
            g.admin_role = record["role"]
            return view(*args, **kwargs)

        return wrapped

    return decorate


def _admin_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt="planmytravel-admin-session",
    )


def make_admin_cookie(admin_id: int, role: str) -> str:
    return _admin_serializer().dumps({"id": admin_id, "role": role})
