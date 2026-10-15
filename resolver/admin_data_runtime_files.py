"""Read-only status reports sourced from local runtime files."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable


def sync_status(status_file: Path | str) -> dict:
    out = {"running": False, "status_text": "Waiting for next run",
           "available": False, "exists": False}
    path = Path(status_file)
    try:
        if path.exists():
            out["exists"] = True
            data = json.loads(path.read_text())
            out.update(data)
            out["exists"] = True
            out["available"] = True
            if out.get("running"):
                out["status_text"] = "Sync in progress..."
            elif out.get("last_success") is True:
                out["status_text"] = f"Completed successfully ({out.get('updated_human', '')})"
            elif out.get("last_success") is False:
                out["status_text"] = f"Sync failed (exit code: {out.get('last_exit_code')})"
    except Exception as exc:
        out["available"] = False
        out["error"] = str(exc)
    return out


def sync_history(
    history_file: Path | str,
    config_path: Path | str,
    limit: int = 30,
    *,
    channel_labels: Callable[[Path | str], dict[str, str]],
    trim_channel_name: Callable[[str], str],
) -> dict:
    try:
        path = Path(history_file)
        data = json.loads(path.read_text()) if path.exists() else {"runs": []}
        runs = data.get("runs", []) if isinstance(data, dict) else []
        safe_limit = max(1, min(limit, 100))
        channel_names = channel_labels(config_path)
        for run in runs[:safe_limit]:
            for source in run.get("sources", []):
                key = str(source.get("source", ""))
                if key.startswith("telegram:") and not source.get("label"):
                    channel = key.split(":", 1)[1]
                    name = channel_names.get(channel, channel)
                    source["label"] = f"tg:[{trim_channel_name(name)}]"
        return {"runs": runs[:safe_limit], "total": len(runs), "truncated": len(runs) > safe_limit}
    except (OSError, ValueError, TypeError):
        return {"runs": [], "error": "Sync history is unavailable."}


def backup_status(backup_dir: Path | str) -> dict:
    files = []
    inventory_available = True
    try:
        with os.scandir(backup_dir) as entries:
            for entry in entries:
                name = entry.name
                is_postgres = name.startswith("adulthub-") and name.endswith(".dump.gz")
                is_legacy = name.startswith("adult_hub-") and name.endswith(".db")
                if not (is_postgres or is_legacy) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                path = Path(backup_dir) / name
                checksum_path = path.with_name(path.name + ".sha256")
                checksum_status = "recorded" if checksum_path.is_file() and not checksum_path.is_symlink() else "not_recorded"
                files.append({"name": name, "size_bytes": stat.st_size, "modified_at": stat.st_mtime,
                              "backup_type": "postgres_custom_dump" if is_postgres else "legacy_database_snapshot",
                              "checksum_status": checksum_status})
    except OSError:
        inventory_available = False
    files.sort(key=lambda item: item["modified_at"], reverse=True)
    latest = files[0] if files else None
    return {
        "available": latest is not None,
        "inventory_available": inventory_available,
        "count": len(files),
        "total_bytes": sum(item["size_bytes"] for item in files),
        "latest": latest,
        "files": files[:100],
    }


def linkcheck_tail(log_path: Path | str, n: int = 20) -> dict:
    path = Path(log_path)
    out: dict = {"path": str(path), "exists": False, "tail": []}
    try:
        if path.exists():
            out["exists"] = True
            out["mtime_human"] = datetime.fromtimestamp(
                path.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M")
            lines = path.read_text(errors="replace").splitlines()
            out["tail"] = lines[-n:]
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out
