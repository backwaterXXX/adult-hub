"""Consistent, safe error envelopes for Admin v2 HTTP routes."""

from __future__ import annotations

import json
import logging
import uuid
import asyncio
from collections.abc import Callable

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse


_STATUS_CODES = {
    400: "invalid_request", 401: "unauthorized", 403: "forbidden",
    404: "not_found", 409: "conflict", 413: "request_too_large",
    429: "rate_limited", 503: "unavailable",
}
_LOG = logging.getLogger("resolver.admin_v2")
_ACTIVITY_DATABASE: Callable[[], str | None] | None = None


def configure_activity_database(database_url: Callable[[], str | None]) -> None:
    """Configure the best-effort audit sink with a live database URL lookup."""
    global _ACTIVITY_DATABASE
    _ACTIVITY_DATABASE = database_url


async def _record_mutation(request, response) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if not 200 <= response.status_code < 300:
        return
    path = request.url.path
    if not path.startswith("/admin/api/v2/") or _ACTIVITY_DATABASE is None:
        return
    route = request.scope.get("route")
    route_template = getattr(route, "path", None)
    if not isinstance(route_template, str):
        return
    # FastAPI may leave an included APIRouter's local template unprefixed in
    # the request scope. Keep only that safe template and restore the fixed
    # admin prefix, never substituting actual URL path segments.
    if route_template.startswith("/api/v2/"):
        route_template = "/admin" + route_template
    if not route_template.startswith("/admin/api/v2/"):
        return
    correlation_id = response.headers.get("X-Correlation-ID")
    try:
        from common.admin_action_events import record_event
        await asyncio.to_thread(
            record_event, _ACTIVITY_DATABASE(), method=request.method,
            route=route_template, status_code=response.status_code,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        # The activity sink must never turn a completed user action into an HTTP failure.
        _LOG.warning("Admin v2 activity event could not be recorded (%s)", type(exc).__name__)


def v2_error_response(status: int, message: str, *, headers=None, extras=None) -> JSONResponse:
    """Build the stable Admin v2 error envelope without exposing internals."""
    correlation_id = uuid.uuid4().hex
    response_headers = {
        key: value for key, value in (headers or {}).items()
        if key.lower() not in {"content-length", "content-type", "x-correlation-id"}
    }
    response_headers["X-Correlation-ID"] = correlation_id
    body = {
        "ok": False,
        "error": {
            "code": _STATUS_CODES.get(status, "request_failed"),
            "message": message,
            "retryable": status == 429 or status >= 500,
            "correlation_id": correlation_id,
        },
        **(extras or {}),
    }
    return JSONResponse(body, status_code=status, headers=response_headers)


class V2ErrorRoute(APIRoute):
    """Normalize unsuccessful v2 route responses without changing v1."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            is_v2 = request.url.path.startswith("/admin/api/v2/")
            try:
                response = await original(request)
            except RequestValidationError:
                # The application-level validation handler provides the stable
                # envelope without logging or returning submitted values.
                raise
            except Exception as exc:
                if not is_v2:
                    raise
                # Do not log exception text: it can contain request data or secrets.
                _LOG.error("Admin v2 request failed (%s)", type(exc).__name__)
                response = v2_error_response(500, "The request could not be completed.")
                if is_v2:
                    await _record_mutation(request, response)
                return response
            if is_v2:
                await _record_mutation(request, response)
            if response.status_code < 400 or not is_v2:
                return response

            raw = getattr(response, "body", b"")
            try:
                payload = json.loads(raw) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                payload = {}

            message = "The request could not be completed."
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, str) and error.strip():
                    message = error.strip()
                elif isinstance(error, dict) and isinstance(error.get("message"), str):
                    message = error["message"].strip() or message
                elif response.status_code == 409:
                    message = "The data changed. Refresh and try again."
                elif response.status_code == 403:
                    message = "This action is not allowed."
                extras = {key: value for key, value in payload.items()
                          if key not in {"ok", "error", "detail"}}
            else:
                extras = {}

            status = 400 if response.status_code == 422 else response.status_code
            return v2_error_response(status, message, headers=response.headers, extras=extras)

        return handler
