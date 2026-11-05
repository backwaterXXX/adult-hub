"""Safe projection for sync history shown in Admin v2."""

from __future__ import annotations

import math
import re

_MAX_SAFE_INT = 2**53 - 1
_MAX_SECONDS = 31_536_000


def _count(value):
    return value if type(value) is int and 0 <= value <= _MAX_SAFE_INT else None


def _seconds(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0 or value > _MAX_SECONDS:
        return None
    return round(value, 3)


def _redact_detail(detail: str) -> str:
    detail = re.sub(r"https?://[^\s\]\[()<>\"']+", "[link hidden]", detail)
    detail = re.sub(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*",
                    "[path hidden]", detail)
    detail = re.sub(r"\b[A-Za-z]:\\(?:[^\s\\]+\\)*[^\s,;]+", "[path hidden]", detail)
    detail = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", detail)
    detail = re.sub(
        r"(?i)(\b(?:token|api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]", detail,
    )
    return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[redacted-token]", detail)


def _safe_source(source: dict) -> dict:
    safe_source = {}
    for key in ("source", "label"):
        value = source.get(key)
        if isinstance(value, str) and len(value) <= 256:
            safe_source[key] = value
    if type(source.get("success")) is bool:
        safe_source["success"] = source["success"]
    if (isinstance(source.get("source"), str) and source["source"].startswith("telegram:")
            and isinstance(source.get("warnings"), list)
            and any(isinstance(warning, str) and warning.startswith("history batch: ") for warning in source["warnings"])):
        safe_source["history_deferred"] = True
    for key in ("added", "removed", "videos"):
        value = _count(source.get(key))
        if value is not None:
            safe_source[key] = value
    value = _seconds(source.get("duration_seconds"))
    if value is not None:
        safe_source["duration_seconds"] = value
    error = source.get("error")
    if isinstance(error, str) and re.fullmatch(r"Sync failed \([A-Za-z0-9_]{1,80}\)\.", error):
        safe_source["error"] = error
    detail = source.get("detail")
    source_id = source.get("source")
    if (isinstance(source_id, str) and source_id.startswith("telegram:")
            and isinstance(detail, str) and 0 < len(detail) <= 700):
        safe_source["detail"] = _redact_detail(detail)[:700]
    return safe_source


def _safe_stages(run: dict) -> dict:
    safe_stages = {}
    stages = run.get("stages") if isinstance(run.get("stages"), dict) else {}
    for name, value in stages.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(value, dict):
            continue
        stage = {}
        calls = _count(value.get("calls"))
        elapsed = _seconds(value.get("total_seconds"))
        if calls is not None:
            stage["calls"] = calls
        if elapsed is not None:
            stage["total_seconds"] = elapsed
        if stage:
            safe_stages[name] = stage
    return safe_stages


def _safe_poll(run: dict) -> dict:
    poll = run.get("jellyfin_refresh_poll") if isinstance(run.get("jellyfin_refresh_poll"), dict) else {}
    safe_poll = {}
    for key in ("calls", "timeouts"):
        value = _count(poll.get(key))
        if value is not None:
            safe_poll[key] = value
    for key in ("total_seconds", "max_seconds"):
        value = _seconds(poll.get(key))
        if value is not None:
            safe_poll[key] = value
    return safe_poll


def _safe_run(run: dict) -> dict:
    safe_run = {}
    for key in ("started_at", "ended_at"):
        value = run.get(key)
        if type(value) is int and 0 <= value <= _MAX_SAFE_INT:
            safe_run[key] = value
    value = _count(run.get("managed_config_revision"))
    if value is not None:
        safe_run["managed_config_revision"] = value
    value = _seconds(run.get("duration_seconds"))
    if value is not None:
        safe_run["duration_seconds"] = value
    if type(run.get("success")) is bool:
        safe_run["success"] = run["success"]
    if type(run.get("interrupted")) is bool:
        safe_run["interrupted"] = run["interrupted"]
    mode = run.get("sync_mode")
    if isinstance(mode, str) and mode in {"scheduled", "new", "backfill"}:
        safe_run["sync_mode"] = mode
    source_filter = run.get("source_filter")
    if isinstance(source_filter, str) and len(source_filter) <= 256:
        safe_run["source_filter"] = source_filter

    sources = run.get("sources") if isinstance(run.get("sources"), list) else []
    safe_run["sources"] = [_safe_source(source) for source in sources if isinstance(source, dict)]
    safe_run["stages"] = _safe_stages(run)

    http = run.get("http") if isinstance(run.get("http"), dict) else {}
    safe_run["http"] = {key: value for key in ("failed_attempts", "retries", "rate_limited")
                        if (value := _count(http.get(key))) is not None}
    safe_run["jellyfin_refresh_poll"] = _safe_poll(run)
    return safe_run


def safe_sync_history(report):
    """Project sync history onto fields the Sources UI needs; omit HTTP hosts/raw errors."""
    if not isinstance(report, dict) or report.get("error") or not isinstance(report.get("runs"), list):
        return {"runs": [], "total": 0, "truncated": False, "available": False,
                "error": "Sync history is unavailable."}

    runs = [_safe_run(run) for run in report["runs"] if isinstance(run, dict)]

    total = _count(report.get("total"))
    return {"runs": runs, "total": total if total is not None else len(runs),
            "truncated": report.get("truncated") is True or (total is not None and total > len(runs)),
            "available": True}
