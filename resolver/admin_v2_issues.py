"""Read-only issue center API for Admin v2.

The router receives the importing admin module so access checks and patch
points behave the same whether ``admin`` is imported as a package or top-level.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query, Request
from resolver.admin_v2_errors import V2ErrorRoute
from resolver.admin_v2_issue_builder import (
    QUALITY_ISSUE_LABELS,
    _build_issues as _build_issues_impl,
    _issue_summary,
    _iso_time,  # noqa: F401 - preserve the existing test/import patch point
    _timestamp,
)


def _build_issues(admin, now):
    """Keep the historical patch point while delegating issue aggregation."""
    return _build_issues_impl(admin, now)


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/issues")
    async def admin_v2_issues(
        request: Request,
        severity: str = "",
        kind: str = "",
        status: str = "open",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally"}, status_code=403)
        if severity not in {"", "critical", "high", "medium", "low"}:
            return admin.JSONResponse({"error": "Invalid severity filter."}, status_code=400)
        allowed_kinds = {"", "sync_failure", "sync_never_run", "sync_backlog", "job_failure", "job_stalled", "unavailable_video", "backup", "quality_finding", "quality_audit", "system", *QUALITY_ISSUE_LABELS}
        if kind not in allowed_kinds:
            return admin.JSONResponse({"error": "Invalid issue kind filter."}, status_code=400)
        if status not in {"", "open", "all"}:
            return admin.JSONResponse({"error": "Invalid status filter."}, status_code=400)
        now = int(time.time())
        issues, health = await admin.asyncio.to_thread(_build_issues, admin, now)
        filtered = [issue for issue in issues
                    if (not severity or issue["severity"] == severity)
                    and (not kind or (issue["kind"] == "quality_finding" and issue["object_id"] == kind
                                      if kind in QUALITY_ISSUE_LABELS else issue["kind"] == kind))
                    and (status == "all" or issue["status"] == status)]
        priority = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        filtered.sort(key=lambda item: (
            priority.get(item["severity"], 4),
            -(_timestamp(item["last_seen"]) or 0), item["kind"], item["object_id"] or ""))
        # Overview tiles read the group summary from this same, unfiltered
        # result, so filters and pagination never move the five groups.
        summary = _issue_summary(issues, health)
        return {
            "ok": True,
            "issues": filtered[offset:offset + limit],
            "total": len(filtered),
            "offset": offset,
            "limit": limit,
            "truncated": offset + limit < len(filtered),
            "summary": summary,
            "data": health,
        }

    return router


__all__ = ["build_router"]
