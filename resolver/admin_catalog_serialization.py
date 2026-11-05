"""Sanitized catalog response projections and local-file status helpers."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
import logging

logger = logging.getLogger(__name__)



_SAFE_UNAVAILABLE_REASONS = {
    "dead CDN stream (probe)": "The CDN stream probe failed.",
    "availability check confirmed unavailable": "A playback check confirmed this item is unavailable.",
}


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default

def _safe_thumbnail(value: Any) -> str | None:
    """Expose only the resolver's Telegram thumbnail proxy, without host/query extras."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        params = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return None
    if (parsed.path != "/admin/api/thumb" or parsed.scheme not in {"", "http", "https"}
            or parsed.username or parsed.password or params.get("provider") != ["telegram"]
            or set(params) != {"provider", "ref"} or len(params.get("ref", [])) != 1):
        return None
    ref = params["ref"][0]
    if not ref or len(ref) > 512 or any(ord(char) < 32 for char in ref):
        return None
    return f"/admin/api/thumb?provider=telegram&ref={quote(ref, safe='')}"

def _row_value(row, name, fallback=None):
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return fallback

def _iso_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    return None

def _has_local_files(get, local_files):
    stream_state = local_files.get("stream") if isinstance(local_files, dict) else None
    return ({"present": True, "missing": False, "unknown": None}.get(stream_state)
            if stream_state in {"present", "missing", "unknown"}
            else bool(get("has_local_files", False)))

def _unavailable_reason(raw_reason, dead_stream, playable):
    unavailable_reason = _SAFE_UNAVAILABLE_REASONS.get(raw_reason) if isinstance(raw_reason, str) else None
    if dead_stream and unavailable_reason is None:
        unavailable_reason = "A dead-stream record is present."
    elif playable == 0 and unavailable_reason is None:
        unavailable_reason = "Marked unplayable."
    return unavailable_reason

def _add_detail_fields(video, get, local_files):
    video["local_files"] = {
        role: local_files.get(role, "unknown") if isinstance(local_files, dict) else "unknown"
        for role in ("stream", "nfo", "artwork")
    }
    if local_files and local_files.get("artwork") == "present" and not video.get("artwork_url") and get("id"):
        video["artwork_url"] = f"/admin/api/v2/library/videos/{quote(str(get('id') or ''), safe='')}/artwork"

def _add_metadata_fields(video, get, metadata_overrides):
    for field in ("title", "plot", "studio", "performers", "tags", "date_released"):
        source_value = get(f"source_{field}", video.get(field))
        if field in {"performers", "tags"}:
            source_value = _json_value(source_value, [])
        video["metadata_fields"][field] = {
            "source": source_value,
            "effective": video.get(field),
            "overridden": isinstance(metadata_overrides, dict) and field in metadata_overrides,
        }

def _missing_fields(video) -> list[str]:
    missing = []
    if not str(video["title"] or "").strip():
        missing.append("title")
    if not video["performers"]:
        missing.append("performers")
    if not str(video["studio"] or "").strip():
        missing.append("studio")
    if not str(video["plot"] or "").strip():
        missing.append("plot")
    if not video["thumbnail"]:
        missing.append("thumbnail")
    return missing

def _serialize_video(row, *, detail: bool, local_files: dict[str, str] | None = None) -> dict[str, Any]:
    get = partial(_row_value, row)

    performers = _json_value(get("performers"), [])
    categories = _json_value(get("categories"), [])
    tags = _json_value(get("tags"), [])
    metadata_overrides = _json_value(get("metadata_overrides"), {})
    file_categories = get("file_categories") or []
    if isinstance(file_categories, str):
        file_categories = [file_categories]
    has_local_files = _has_local_files(get, local_files)
    dead_stream = bool(get("dead_stream", False))
    playable = get("playable")
    unavailable_reason = _unavailable_reason(get("unavailable_reason"), dead_stream, playable)
    video = {
        "id": get("id"),
        "channel_id": _channel_id(get("id")),
        "provider": get("provider"),
        "telegram_url": _safe_telegram_post_url(get("provider"), get("id")),
        "title": get("title"),
        "duration_seconds": get("duration"),
        "file_size_bytes": get("file_size_bytes"),
        "thumbnail": _safe_thumbnail(get("thumbnail")),
        "performers": performers if isinstance(performers, list) else [],
        "categories": categories if isinstance(categories, list) else [],
        "tags": tags if isinstance(tags, list) else [],
        "studio": get("studio"),
        "plot": get("plot"),
        "date_released": get("date_released"),
        "playable": playable,
        "dead_stream": dead_stream,
        "availability": ("unavailable" if dead_stream or playable == 0
                         else "available" if playable == 1 else "unknown"),
        "unavailable_reason": unavailable_reason,
        "unavailable_detected_at": _iso_time(get("unavailable_detected_at")),
        "first_seen": _iso_time(get("first_seen")),
        "last_seen": _iso_time(get("last_seen")),
        "has_local_files": has_local_files,
        "file_count": int(get("file_count", 0) or 0),
        "file_categories": list(file_categories),
        "sources": [],
        "metadata_revision": int(get("metadata_revision", 0) or 0),
        "metadata_fields": {},
        "scene_match_source": get("scene_match_source"),
        "scene_match_external_id": get("scene_match_external_id"),
        "scene_match_url": get("scene_match_url"),
        "artwork_url": f"/admin/api/v2/library/videos/{quote(str(get('id') or ''), safe='')}/artwork" if (get("id") and ((local_files and local_files.get("artwork") == "present") or (has_local_files and not detail))) else None,
    }
    if detail:
        _add_detail_fields(video, get, local_files)
    _add_metadata_fields(video, get, metadata_overrides)
    video["missing_fields"] = _missing_fields(video)
    return video

def _channel_id(video_id: Any) -> str | None:
    """Return the stable Telegram channel portion of a composite video ID."""
    if not isinstance(video_id, str) or not video_id.startswith("telegram:"):
        return None
    parts = video_id.split(":", 2)
    return parts[1] if len(parts) == 3 and parts[1] else None

def _combine_file_states(states: list[str]) -> str:
    if not states or "unknown" in states:
        return "unknown"
    if "missing" in states:
        return "missing"
    return "present"

def _ready_media_root(media_root: str | os.PathLike[str]) -> str | None:
    """Return the media root as a string when it is a readable directory, else None."""
    try:
        root = os.fspath(media_root)
        root_info = os.stat(root)
        if not stat.S_ISDIR(root_info.st_mode):
            return None
        # Confirm the media root is mounted/readable before classifying a child
        # that is absent from the directory as genuinely missing.
        with os.scandir(root):
            pass
    except (OSError, TypeError, ValueError):
        return None
    return root

def _strm_stem(raw_name: Any) -> str | None:
    """Return the stem of a plain ``.strm`` file name, or None when the name is rejected."""
    if (not isinstance(raw_name, str) or not raw_name or raw_name in {".", ".."}
            or "/" in raw_name or "\\" in raw_name or any(ord(char) < 32 for char in raw_name)):
        return None
    name = os.path.basename(raw_name)
    if name != raw_name or not name.lower().endswith(".strm"):
        return None
    return name[:-5]

def _local_candidate_state(candidate: str, role: str) -> str:
    try:
        info = os.lstat(candidate)
        state = "unknown" if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) else "present"
        if state == "present" and role == "artwork":
            try:
                from common.image_utils import is_black_image
                if is_black_image(candidate):
                    state = "missing"
            except Exception:
                logger.debug("Ignored exception in _local_file_status", exc_info=True)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    return state

def _local_file_status(file_names: list[Any], media_root: str | os.PathLike[str]) -> dict[str, str]:
    """Check registered media roles without following or exposing filesystem paths."""
    unknown = {"stream": "unknown", "nfo": "unknown", "artwork": "unknown"}
    root = _ready_media_root(media_root)
    if root is None:
        return unknown

    role_states = {"stream": [], "nfo": [], "artwork": []}
    if not isinstance(file_names, list) or not file_names:
        return unknown
    for raw_name in file_names:
        stem = _strm_stem(raw_name)
        if stem is None:
            for states in role_states.values():
                states.append("unknown")
            continue
        for role, suffix in (("stream", ".strm"), ("nfo", ".nfo"), ("artwork", ".jpg")):
            candidate = os.path.join(root, stem + suffix)
            role_states[role].append(_local_candidate_state(candidate, role))
    return {role: _combine_file_states(states) for role, states in role_states.items()}

def _resolve_media_root(media_root: str | os.PathLike[str]) -> Path | None:
    try:
        root = Path(media_root).resolve()
        if not root.is_dir():
            return None
    except (OSError, TypeError, ValueError):
        return None
    return root

def _resolve_artwork_candidate(root: Path, stem: str) -> Path | None:
    candidate = (root / f"{stem}.jpg").resolve()
    try:
        if candidate.parent != root:
            return None
        info = os.lstat(candidate)
        if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
            try:
                from common.image_utils import is_black_image
                if is_black_image(candidate):
                    return None
            except Exception:
                logger.debug("Ignored exception in _resolve_local_artwork_path", exc_info=True)
            return candidate
    except (OSError, ValueError):
        return None
    return None

def _resolve_local_artwork_path(file_names: list[Any], media_root: str | os.PathLike[str]) -> Path | None:
    """Return the absolute Path to the local artwork file if present, else None."""
    root = _resolve_media_root(media_root)
    if root is None:
        return None

    if not isinstance(file_names, list) or not file_names:
        return None

    for raw_name in file_names:
        stem = _strm_stem(raw_name)
        if stem is None:
            continue
        candidate = _resolve_artwork_candidate(root, stem)
        if candidate is not None:
            return candidate
    return None

def _safe_telegram_post_url(provider: Any, video_id: Any) -> str | None:
    """Build a post link only from a canonical numeric Telegram video ID."""
    if str(provider or "").lower() != "telegram" or not isinstance(video_id, str):
        return None
    parts = video_id.split(":")
    if len(parts) not in {3, 4} or parts[0] != "telegram":
        return None
    channel_id, message_id = parts[1], parts[2]
    channel_digits = channel_id[1:] if channel_id.startswith("-") else channel_id
    if (not channel_digits or not channel_digits.isascii() or not channel_digits.isdigit()
            or int(channel_digits) <= 0
            or not message_id.isascii() or not message_id.isdigit() or int(message_id) <= 0):
        return None
    if len(parts) == 4 and not parts[3]:
        return None
    channel = channel_id[4:] if channel_id.startswith("-100") else channel_id.lstrip("-")
    if not channel:
        return None
    return f"https://t.me/c/{channel}/{message_id}"

