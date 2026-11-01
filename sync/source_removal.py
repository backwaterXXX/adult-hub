"""Explicitly remove one Telegram channel and its catalog data."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

import yaml
from common import db


def _channel_ids(channel_id: str) -> set[str]:
    values = {channel_id}
    if channel_id.startswith("-100"):
        values.add(channel_id[4:])
    else:
        values.add("-100" + channel_id.lstrip("-"))
    return values


def _source_matches(value, channel_ids: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.removeprefix("telegram:")
    return candidate in channel_ids


def _acquire_removed_channel_lock(channel_id: str, timeout: float = 30.0):
    """Wait for the API to disable the channel, then hold its config lock."""
    from common.config_loader import find_config_file
    from common.managed_sources import apply_overrides, overrides_path, read_overrides

    aliases = _channel_ids(channel_id)
    target = overrides_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        lock = lock_path.open("a+b")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            base = yaml.safe_load(find_config_file().read_text(encoding="utf-8")) or {}
            effective = apply_overrides(base, read_overrides(target))
            telegram = next((source for source in effective.get("sources", [])
                             if source.get("provider") == "telegram"), {})
            still_configured = any(
                isinstance(item, dict) and str(item.get("id", "")).strip() in aliases
                for item in telegram.get("channels", [])
            )
            if not still_configured:
                return lock
        except Exception:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            raise
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        if time.monotonic() >= deadline:
            raise RuntimeError("Channel remains configured; no videos or history were deleted.")
        time.sleep(0.25)


def _remove_sync_history(channel_id: str) -> tuple[int, int]:
    path = Path(os.getenv("SYNC_HISTORY_FILE", "/data/cache/sync_history.json"))
    if not path.exists():
        return 0, 0
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Sync history could not be read; channel deletion is incomplete.") from exc
    if not isinstance(report, dict) or not isinstance(report.get("runs"), list):
        raise RuntimeError("Sync history has an invalid structure; channel deletion is incomplete.")

    aliases = _channel_ids(channel_id)
    retained = []
    removed_runs = 0
    removed_source_rows = 0
    for run in report["runs"]:
        if not isinstance(run, dict):
            retained.append(run)
            continue
        if _source_matches(run.get("source_filter"), aliases):
            removed_runs += 1
            continue
        sources = run.get("sources")
        if isinstance(sources, list):
            filtered = [item for item in sources
                        if not isinstance(item, dict) or not _source_matches(item.get("source"), aliases)]
            removed_source_rows += len(sources) - len(filtered)
            if len(filtered) != len(sources):
                updated_run = dict(run)
                updated_run["sources"] = filtered
                retained.append(updated_run)
                continue
        retained.append(run)

    if removed_runs or removed_source_rows:
        updated_report = dict(report)
        updated_report["runs"] = retained
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(updated_report, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    return removed_runs, removed_source_rows


def remove_telegram_channel(conn, media_dir: Path, channel_id: str, job_id: str, services,
                            record_progress) -> dict:
    """Delete channel-owned videos, their managed files, and source sync history.

    The source-config lock prevents the same channel from being re-added while
    its catalog data is being removed. The queue worker also holds the global
    sync lock, so history writes and catalog syncs cannot overlap this cleanup.
    """
    if not isinstance(channel_id, str) or not channel_id or len(channel_id) > 256:
        raise ValueError("Channel removal requires a valid Telegram channel ID.")

    config_lock = _acquire_removed_channel_lock(channel_id)
    try:
        aliases = sorted(_channel_ids(channel_id))
        prefixes = [f"telegram:{value}:" for value in aliases]
        tables = set(db.table_names(conn))

        conditions = []
        params = []
        for prefix in prefixes:
            conditions.append("left(id, length(%s)) = %s")
            params.extend((prefix, prefix))
        direct_rows = conn.execute(
            "SELECT id FROM videos WHERE provider='telegram' AND (" + " OR ".join(conditions) + ")",
            params,
        ).fetchall()
        direct_ids = {str(row[0]) for row in direct_rows}

        target_edges = []
        if "video_sources" in tables:
            source_conditions = ["channel_id = ANY(%s)"]
            source_params = [aliases]
            for prefix in prefixes:
                source_conditions.append("left(source_id, length(%s)) = %s")
                source_params.extend((prefix, prefix))
            target_edges = conn.execute(
                "SELECT video_id, source_id FROM video_sources WHERE provider='telegram' AND ("
                + " OR ".join(source_conditions) + ")",
                source_params,
            ).fetchall()
        target_source_ids = {str(row[1]) for row in target_edges}
        candidate_ids = direct_ids | {str(row[0]) for row in target_edges}
        deletable_ids = []
        preserved_shared = 0
        for video_id in sorted(candidate_ids):
            other_active = False
            if "video_sources" in tables:
                rows = conn.execute(
                    "SELECT source_id, provider, channel_id, is_active FROM video_sources WHERE video_id=%s",
                    (video_id,),
                ).fetchall()
                other_active = any(
                    bool(row[3]) and not (
                        str(row[0]) in target_source_ids
                        or (str(row[1]) == "telegram" and str(row[2] or "") in aliases)
                    )
                    for row in rows
                )
            exists = conn.execute("SELECT 1 FROM videos WHERE id=%s", (video_id,)).fetchone()
            if exists and other_active:
                preserved_shared += 1
            elif exists:
                deletable_ids.append(video_id)

        record_progress(conn, job_id, "removing_source_data", completed=0,
                        total=len(deletable_ids))

        deleted_videos = 0
        deleted_files = 0
        for index, video_id in enumerate(deletable_ids, start=1):
            paths = conn.execute(
                "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path",
                (video_id,),
            ).fetchall()
            file_families = []
            for (stored_path,) in paths:
                shared = conn.execute(
                    "SELECT 1 FROM synced_files WHERE file_path=%s AND video_id<>%s LIMIT 1",
                    (stored_path, video_id),
                ).fetchone()
                if shared:
                    continue
                try:
                    stream_path = services.resolve_media_path(stored_path, media_dir)
                except ValueError as exc:
                    services.logger.warning("Refusing unsafe channel-removal path for %s", video_id)
                    raise RuntimeError(
                        "A catalog file path failed safety checks; no catalog row was deleted."
                    ) from exc
                file_families.append(stream_path)

            for stream_path in file_families:
                for suffix in (".strm", ".nfo", ".jpg", ".thumb_fail"):
                    candidate = stream_path.with_suffix(suffix)
                    try:
                        if candidate.exists() or candidate.is_symlink():
                            candidate.unlink()
                            deleted_files += 1
                    except OSError as exc:
                        raise RuntimeError("A managed video file could not be removed; catalog cleanup stopped.") from exc

            with conn:
                if "video_sources" in tables:
                    conn.execute("DELETE FROM video_sources WHERE video_id=%s", (video_id,))
                if "dead_streams" in tables:
                    conn.execute("DELETE FROM dead_streams WHERE video_id=%s", (video_id,))
                if "video_duplicate_decisions" in tables:
                    conn.execute(
                        "DELETE FROM video_duplicate_decisions WHERE loser_id=%s OR keeper_id=%s",
                        (video_id, video_id),
                    )
                if "video_duplicate_dismissals" in tables:
                    conn.execute(
                        "DELETE FROM video_duplicate_dismissals WHERE left_id=%s OR right_id=%s",
                        (video_id, video_id),
                    )
                if "admin_quality_acknowledgements" in tables:
                    conn.execute("DELETE FROM admin_quality_acknowledgements WHERE video_id=%s", (video_id,))
                conn.execute("DELETE FROM videos WHERE id=%s", (video_id,))
                deleted_videos += 1
            record_progress(conn, job_id, "removing_source_data", completed=index,
                            total=len(deletable_ids))

        if "video_sources" in tables:
            source_conditions = ["channel_id = ANY(%s)"]
            source_params = [aliases]
            for prefix in prefixes:
                source_conditions.append("left(source_id, length(%s)) = %s")
                source_params.extend((prefix, prefix))
            with conn:
                conn.execute(
                    "DELETE FROM video_sources WHERE provider='telegram' AND ("
                    + " OR ".join(source_conditions) + ")",
                    source_params,
                )
        if "telegram_channel_state" in tables:
            with conn:
                conn.execute("DELETE FROM telegram_channel_state WHERE channel_id = ANY(%s)", (aliases,))

        removed_runs, removed_source_rows = _remove_sync_history(channel_id)
        return {
            "local_state": "source_removed",
            "channel_id": channel_id,
            "videos_deleted": deleted_videos,
            "files_deleted": deleted_files,
            "shared_videos_preserved": preserved_shared,
            "history_runs_removed": removed_runs,
            "history_source_rows_removed": removed_source_rows,
        }
    finally:
        fcntl.flock(config_lock.fileno(), fcntl.LOCK_UN)
        config_lock.close()
