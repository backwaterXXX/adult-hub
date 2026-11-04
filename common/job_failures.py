"""Allow-listed explanations for failed background jobs.

Exception text is used only to classify a failure. It is never returned by the
Admin API because it may contain local paths, URLs, or credentials.
"""

from __future__ import annotations


FAILURE_MESSAGES = {
    "operation_timeout": "A required operation timed out.",
    "connection_failed": "Could not connect to a required service.",
    "permission_denied": "Access to a required resource was denied.",
    "file_missing": "A required file was not found.",
    "stale_operation": "The operation no longer matches current data. Refresh its preview before retrying.",
    "remote_service_failed": "A required service returned an unsuccessful response.",
    "sync_failed": "Synchronization reported a failure. Open Sources for per-source results.",
    "telegram_history_failed": "A Telegram history request failed. Open Sources for per-source results.",
    "artwork_source_missing": "The video file needed to generate artwork is missing from the library.",
    "artwork_destination_unsafe": "The artwork destination is unsafe, so no file was written.",
    "artwork_source_unavailable": "The video could not be read to generate artwork.",
    "artwork_frame_extraction_failed": "Could not extract an artwork frame from the video.",
}

_SAFE_CODES = frozenset(FAILURE_MESSAGES)


def classify_job_failure(action: str | None, result: object) -> str | None:
    """Return a safe category from a persisted result without exposing its text."""
    if not isinstance(result, dict):
        return None
    code = result.get("failure_code")
    if isinstance(code, str) and code in _SAFE_CODES:
        return code
    if result.get("local_state") == "sync failed":
        return _sync_failure_code(result)

    error = result.get("error")
    if not isinstance(error, str):
        return None
    normalized = error.casefold()
    if action == "artwork_generate":
        return _artwork_failure_code(normalized, _failure_stage(result))
    return _generic_failure_code(normalized)


def _sync_failure_code(result: dict) -> str:
    failures = result.get("source_failures")
    if isinstance(failures, list) and any(
        isinstance(item, dict)
        and item.get("detail") == "Telegram fetch/history operation failed."
        for item in failures
    ):
        return "telegram_history_failed"
    return "sync_failed"


def _failure_stage(result: dict) -> str | None:
    progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
    failure_stage = progress.get("stage")
    if not isinstance(failure_stage, str):
        events = result.get("events") if isinstance(result.get("events"), list) else []
        failure_stage = next((event.get("stage") for event in reversed(events)
                              if isinstance(event, dict) and isinstance(event.get("stage"), str)), None)
    return failure_stage


def _artwork_failure_code(normalized: str, failure_stage: str | None) -> str | None:
    if "target media file not found" in normalized or "no such file or directory" in normalized:
        return "artwork_source_missing"
    if "artwork destination must not be a symlink" in normalized:
        return "artwork_destination_unsafe"
    if "timed out" in normalized or "timeout" in normalized:
        return "operation_timeout"
    if "permission denied" in normalized or "permissionerror" in normalized:
        return "permission_denied"
    if any(marker in normalized for marker in (
        "connection refused", "connectionerror",
        "connecterror", "could not connect", "network is unreachable",
        "httpstatuserror", "http 4", "http 5", "status code 4", "status code 5",
    )):
        return "artwork_source_unavailable"
    if failure_stage == "generating_artwork" or "failed to generate artwork" in normalized:
        return "artwork_frame_extraction_failed"
    return None


def _generic_failure_code(normalized: str) -> str | None:
    if "timed out" in normalized or "timeout" in normalized:
        return "operation_timeout"
    if any(marker in normalized for marker in (
        "connection refused", "connectionerror", "connecterror",
        "could not connect", "network is unreachable",
    )):
        return "connection_failed"
    if "permission denied" in normalized or "permissionerror" in normalized:
        return "permission_denied"
    if "no such file or directory" in normalized or "filenotfounderror" in normalized:
        return "file_missing"
    if any(marker in normalized for marker in ("stale plan", "stale preview", "revision conflict")):
        return "stale_operation"
    if any(marker in normalized for marker in ("httpstatuserror", "http 5", "status code 5")):
        return "remote_service_failed"
    return None
