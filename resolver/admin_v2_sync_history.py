"""Safe projection for sync history shown in Admin v2."""

from __future__ import annotations

import math
import re


def safe_sync_history(report):
    """Project sync history onto fields the Sources UI needs; omit HTTP hosts/raw errors."""
    if not isinstance(report, dict) or report.get("error") or not isinstance(report.get("runs"), list):
        return {"runs": [], "total": 0, "truncated": False, "available": False,
                "error": "Sync history is unavailable."}

    def count(value):
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else None

    def seconds(value):
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0 or value > 31_536_000:
            return None
        return round(value, 3)

    runs = []
    for run in report["runs"]:
        if not isinstance(run, dict):
            continue
        safe_run = {}
        for key in ("started_at", "ended_at"):
            value = run.get(key)
            if type(value) is int and 0 <= value <= 2**53 - 1:
                safe_run[key] = value
        for key in ("managed_config_revision",):
            value = count(run.get(key))
            if value is not None:
                safe_run[key] = value
        for key in ("duration_seconds",):
            value = seconds(run.get(key))
            if value is not None:
                safe_run[key] = value
        if type(run.get("success")) is bool:
            safe_run["success"] = run["success"]
        mode = run.get("sync_mode")
        if isinstance(mode, str) and mode in {"scheduled", "new", "backfill"}:
            safe_run["sync_mode"] = mode
        source_filter = run.get("source_filter")
        if isinstance(source_filter, str) and len(source_filter) <= 256:
            safe_run["source_filter"] = source_filter

        safe_sources = []
        for source in run.get("sources", []) if isinstance(run.get("sources"), list) else []:
            if not isinstance(source, dict):
                continue
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
                value = count(source.get(key))
                if value is not None:
                    safe_source[key] = value
            value = seconds(source.get("duration_seconds"))
            if value is not None:
                safe_source["duration_seconds"] = value
            error = source.get("error")
            if isinstance(error, str) and re.fullmatch(r"Sync failed \([A-Za-z0-9_]{1,80}\)\.", error):
                safe_source["error"] = error
            detail = source.get("detail")
            source_id = source.get("source")
            if (isinstance(source_id, str) and source_id.startswith("telegram:")
                    and isinstance(detail, str) and 0 < len(detail) <= 700):
                detail = re.sub(r"https?://[^\s\]\[()<>\"']+", "[link hidden]", detail)
                detail = re.sub(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*",
                                "[path hidden]", detail)
                detail = re.sub(r"\b[A-Za-z]:\\(?:[^\s\\]+\\)*[^\s,;]+", "[path hidden]", detail)
                detail = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", detail)
                detail = re.sub(
                    r"(?i)(\b(?:token|api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;]+",
                    r"\1[redacted]", detail,
                )
                detail = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[redacted-token]", detail)
                safe_source["detail"] = detail[:700]
            safe_sources.append(safe_source)
        safe_run["sources"] = safe_sources

        safe_stages = {}
        stages = run.get("stages") if isinstance(run.get("stages"), dict) else {}
        for name, value in stages.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(value, dict):
                continue
            stage = {}
            calls = count(value.get("calls"))
            elapsed = seconds(value.get("total_seconds"))
            if calls is not None:
                stage["calls"] = calls
            if elapsed is not None:
                stage["total_seconds"] = elapsed
            if stage:
                safe_stages[name] = stage
        safe_run["stages"] = safe_stages

        http = run.get("http") if isinstance(run.get("http"), dict) else {}
        safe_http = {key: value for key in ("failed_attempts", "retries", "rate_limited")
                     if (value := count(http.get(key))) is not None}
        safe_run["http"] = safe_http

        poll = run.get("jellyfin_refresh_poll") if isinstance(run.get("jellyfin_refresh_poll"), dict) else {}
        safe_poll = {}
        for key in ("calls", "timeouts"):
            value = count(poll.get(key))
            if value is not None:
                safe_poll[key] = value
        for key in ("total_seconds", "max_seconds"):
            value = seconds(poll.get(key))
            if value is not None:
                safe_poll[key] = value
        safe_run["jellyfin_refresh_poll"] = safe_poll
        runs.append(safe_run)

    total = count(report.get("total"))
    return {"runs": runs, "total": total if total is not None else len(runs),
            "truncated": report.get("truncated") is True or (total is not None and total > len(runs)),
            "available": True}
