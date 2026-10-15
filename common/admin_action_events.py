"""Minimal, privacy-preserving activity history for Admin v2 mutations."""

from __future__ import annotations

import re

from common import db


_CORRELATION_ID = re.compile(r"^[a-f0-9]{32}$")
_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_OUTCOMES = {"accepted", "completed"}
_AREAS = {"library", "sources", "performers", "quality", "jobs", "system"}
_AREA_ROUTE_PREFIXES = {
    "library": ("library", "videos"),
    "sources": ("sources",),
    "performers": ("performers",),
    "quality": ("quality", "audits", "operations"),
    "jobs": ("jobs", "schedules", "phash"),
    "system": ("maintenance", "diagnostics", "backups", "activity"),
}


def record_event(db_url: str | None, *, method: str, route: str,
                 status_code: int, correlation_id: str | None = None) -> None:
    """Persist only route template and outcome; never persist request/response data."""
    method = str(method).upper()
    route = str(route)
    status_code = int(status_code)
    if (method not in _METHODS or not route.startswith("/admin/api/v2/")
            or len(route) > 256 or not 200 <= status_code < 300):
        return
    if correlation_id is not None and not _CORRELATION_ID.fullmatch(str(correlation_id)):
        correlation_id = None
    outcome = "accepted" if status_code == 202 else "completed"
    conn = db.connect(db_url)
    try:
        with conn:
            conn.execute(
                "INSERT INTO admin_action_events "
                "(method,route,status_code,outcome,correlation_id) VALUES (%s,%s,%s,%s,%s)",
                (method, route, status_code, outcome, correlation_id),
            )
    finally:
        conn.close()


def list_events(db_url: str | None, *, limit: int = 50, offset: int = 0,
                method: str | None = None, outcome: str | None = None,
                area: str | None = None) -> dict:
    """Return bounded, newest-first activity history without actor or payload claims."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(offset) is not int or not 0 <= offset <= 2_147_483_647:
        raise ValueError("offset must be a non-negative integer")
    method = str(method or "").upper()
    outcome = str(outcome or "").lower()
    area = str(area or "").lower()
    if method and method not in _METHODS:
        raise ValueError("method is not supported")
    if outcome and outcome not in _OUTCOMES:
        raise ValueError("outcome is not supported")
    if area and area not in _AREAS:
        raise ValueError("area is not supported")
    clauses, params = [], []
    if method:
        clauses.append("method=%s")
        params.append(method)
    if outcome:
        clauses.append("outcome=%s")
        params.append(outcome)
    if area:
        prefixes = _AREA_ROUTE_PREFIXES[area]
        clauses.append("(" + " OR ".join("route LIKE %s" for _ in prefixes) + ")")
        params.extend(f"/admin/api/v2/{prefix}/%" for prefix in prefixes)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = db.connect(db_url)
    try:
        rows = conn.execute(
            "SELECT id,created_at,method,route,status_code,outcome,correlation_id "
            f"FROM admin_action_events{where} ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
        total = int(conn.execute(f"SELECT count(*) FROM admin_action_events{where}", params).fetchone()[0])
        items = []
        for row in rows:
            created_at = row[1]
            if hasattr(created_at, "isoformat"):
                created_at = created_at.isoformat()
            items.append({
                "id": int(row[0]), "created_at": created_at, "method": row[2],
                "route": row[3], "status_code": int(row[4]), "outcome": row[5],
                "correlation_id": row[6], "actor_type": "shared_admin",
            })
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    finally:
        conn.close()
