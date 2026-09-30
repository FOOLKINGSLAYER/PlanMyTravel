from __future__ import annotations

from typing import Any, Iterable

from app.db import get_db


def row_dict(row: Any, cursor: Any | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    if cursor is None or not cursor.description:
        raise TypeError("Database rows must expose keys or cursor column metadata.")
    return {
        column[0]: value
        for column, value in zip(cursor.description, row, strict=True)
    }


def query_all(sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cursor = get_db().execute(sql, tuple(parameters))
    return [row_dict(row, cursor) for row in cursor.fetchall()]  # type: ignore[misc]


def query_one(sql: str, parameters: Iterable[Any] = ()) -> dict[str, Any] | None:
    cursor = get_db().execute(sql, tuple(parameters))
    return row_dict(cursor.fetchone(), cursor)


def execute(sql: str, parameters: Iterable[Any] = ()) -> Any:
    connection = get_db()
    cursor = connection.execute(sql, tuple(parameters))
    connection.commit()
    return cursor
