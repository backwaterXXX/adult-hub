"""Request body parsing and field validation for the source configuration routes.

Moved verbatim from admin_v2_sources.py. These helpers take the admin module as an
argument where they need its response helpers, so they have no dependency on the routes.
"""

from __future__ import annotations

import json
import logging

from fastapi import Request

logger = logging.getLogger(__name__)


MAX_REQUEST_BYTES = 256 * 1024


async def parse_body(request: Request):
    length = request.headers.get("content-length")
    try:
        if length is not None and int(length) > MAX_REQUEST_BYTES:
            return None
        body = await request.json()
    except Exception:
        logger.debug("Ignored exception in parse_body", exc_info=True)
        return None
    if (not isinstance(body, dict) or set(body) != {"expected_revision", "overrides"}
            or type(body["expected_revision"]) is not int or body["expected_revision"] < 0
            or not isinstance(body["overrides"], dict)):
        return None
    try:
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return body


_CHANNEL_BODY_LIMIT = 4096


async def _read_channel_body(admin, request):
    """Read the small JSON body of a channel route. Returns (body, error_response)."""
    length = request.headers.get("content-length")
    try:
        if length is not None and int(length) > _CHANNEL_BODY_LIMIT:
            return None, admin.JSONResponse({"error": "Request is too large."}, status_code=413)
    except ValueError:
        return None, admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
    raw_body = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw_body) + len(chunk) > _CHANNEL_BODY_LIMIT:
                return None, admin.JSONResponse({"error": "Request is too large."}, status_code=413)
            raw_body.extend(chunk)
        body = json.loads(raw_body)
    except Exception:
        body = None
    return body, None


def _create_channel_body(body):
    """Return the stripped (channel_id, studio) of a valid create body, else None."""
    if (not isinstance(body, dict) or set(body) != {"expected_revision", "channel"}
            or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
            or not isinstance(body.get("channel"), dict)
            or set(body["channel"]) != {"id", "studio"}
            or not isinstance(body["channel"].get("id"), str)
            or not isinstance(body["channel"].get("studio"), str)):
        return None
    return body["channel"]["id"].strip(), body["channel"]["studio"].strip()


def _valid_channel_fields(channel_id, studio):
    """Check the length and control-character rules for a channel ID and studio label."""
    return bool(channel_id and len(channel_id) <= 256
                and studio and len(studio) <= 160
                and not any(ord(char) < 32 for char in channel_id + studio))


def _remove_channel_body(body):
    """Return the stripped channel ID of a valid remove body, else None."""
    if (not isinstance(body, dict) or set(body) != {"expected_revision", "channel_id"}
            or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
            or not isinstance(body.get("channel_id"), str)):
        return None
    return body["channel_id"].strip()


def _valid_channel_id(channel_id):
    """Check the length and control-character rules for a channel ID."""
    return bool(channel_id and len(channel_id) <= 256
                and not any(ord(char) < 32 for char in channel_id))


def _source_access_channel_id(body):
    """Return the stripped channel ID of a valid check body, else None."""
    if (not isinstance(body, dict) or set(body) != {"source_id"}
            or not isinstance(body["source_id"], str)
            or not body["source_id"].strip() or len(body["source_id"]) > 256):
        return None
    return body["source_id"].strip()
