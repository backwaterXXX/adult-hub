"""Sync-history and configured-source issues for Admin v2."""

from __future__ import annotations

from urllib.parse import quote

from admin_v2_issue_builder import (
    _issue,
    _latest_source_failures,
    _latest_source_ids,
    _timestamp,
)


def collect_source_issues(admin, now, health, issues):
    # Configuration and sync history have independent availability. The config
    # reader returns only display metadata; it does not return credentials.
    try:
        configured = admin.admin_data.configured_sources(admin.CONFIG_PATH)
        health["available"]["config"] = bool(configured.get("available"))
    except Exception:
        configured = {"sources": [], "available": False}

    try:
        history = admin._sync_history(100)
        history_ok = isinstance(history, dict) and not history.get("error")
        runs = history.get("runs", []) if isinstance(history, dict) else []
        health["available"]["sync_history"] = bool(history_ok)
        history_truncated = bool(history.get("truncated")) if isinstance(history, dict) else False
        health["truncated"]["sync_history"] = history_truncated
        if runs and isinstance(runs[0], dict):
            health["freshness"]["sync_history_at"] = _timestamp(
                runs[0].get("ended_at") or runs[0].get("started_at"))
        labels = {}
        configured_source_states = {}
        for source_group in configured.get("sources", []):
            if not isinstance(source_group, dict):
                continue
            provider = str(source_group.get("provider") or "telegram").strip().lower()
            group_enabled = bool(source_group.get("enabled", True))
            if not provider:
                continue
            if provider != "telegram":
                configured_source_states[provider] = group_enabled
                continue
            for channel in source_group.get("channels", []):
                if not isinstance(channel, dict):
                    continue
                channel_id = str(channel.get("id") or "").strip()
                if not channel_id:
                    continue
                key = f"telegram:{channel_id}"
                configured_source_states[key] = group_enabled and bool(
                    channel.get("enabled", True)
                )
                labels[key] = str(channel.get("studio") or channel_id or key)[:120]
        for source_id, source, run_time in _latest_source_failures(history):
            # Stale failures are not actionable after a source/provider is
            # removed or disabled. If config itself is unavailable, retain
            # failures rather than guessing that a source no longer exists.
            if (health["available"]["config"]
                    and not configured_source_states.get(source_id, False)):
                continue
            display = str(source.get("label") or labels.get(source_id) or source_id)[:120]
            issues.append(_issue(
                "sync_failure", "high", f"Synchronization failed: {display}",
                "The latest recorded run for this source failed. Check the source and retry synchronization.",
                "source", source_id, run_time, run_time, "Open source",
                f"/admin/v2/?source_id={quote(source_id, safe='')}#sources",
            ))
        # A never-run source is a different operational state from a failed
        # run. Do not infer that from a truncated history window.
        if not history_truncated and health["available"]["config"] and health["available"]["sync_history"]:
            latest_by_id = _latest_source_ids(history)
            for group in configured.get("sources", []):
                if not isinstance(group, dict) or not group.get("enabled", True):
                    continue
                provider = str(group.get("provider") or "")
                for channel in group.get("channels", []):
                    if not isinstance(channel, dict) or not channel.get("enabled", True):
                        continue
                    channel_id = str(channel.get("id") or "").strip()
                    source_id = f"{provider}:{channel_id}" if channel_id else ""
                    if not source_id or source_id in latest_by_id:
                        continue
                    label = str(channel.get("studio") or channel_id)[:120]
                    issues.append(_issue(
                        "sync_never_run", "low", f"No synchronization history: {label}",
                        "This enabled source has no recorded synchronization run yet.",
                        "source", source_id, None, None, "Open source",
                        f"/admin/v2/?source_id={quote(source_id, safe='')}#sources",
                    ))
    except Exception:
        history = {"runs": []}
    if not health["available"]["sync_history"]:
        issues.append(_issue(
            "system", "high", "Sync history unavailable",
            "Recent source synchronization outcomes could not be loaded. Source failure and never-run status may be incomplete.",
            "system", "sync-history", now, now, "Review source history", "/admin/v2/#sources",
        ))
    return configured, history
