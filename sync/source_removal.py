"""Explicitly remove one Telegram channel and its catalog data."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

import yaml
from common import db


def _channel_ids(channel_id: str) -> set[str]:
    values = {channel_id}
    if channel_id.startswith("-100"):
        values.add(channel_id[4:])
    else:
        values.add("-100" + channel_id.lstrip("-"))
    return values


def _escape_like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _source_matches(value, channel_ids: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.removeprefix("telegram:")
    return candidate in channel_ids


def _acquire_channel_removal_lock(channel_id: str, timeout: float = 30.0):
    """Acquire a channel-scoped lock so this channel cannot be concurrently removed or re-added."""
    from common.managed_sources import channel_lock_path

    target = channel_lock_path(channel_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        f = target.open("a+b")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except (BlockingIOError, OSError):
            f.close()
            if time.monotonic() >= deadline:
                raise RuntimeError("Could not acquire channel removal lock; another removal is in progress.")
            time.sleep(0.1)


def _release_channel_removal_lock(lock_file):
    if lock_file is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass


def _verify_channel_not_configured(channel_id: str, timeout: float = 30.0) -> None:
    """Verify that the channel is disabled/removed in effective config within a bounded timeout."""
    from common.config_loader import find_config_file
    from common.managed_sources import apply_overrides, config_lock, overrides_path, read_overrides

    aliases = _channel_ids(channel_id)
    target = overrides_path()
    deadline = time.monotonic() + timeout
    while True:
        try:
            with config_lock(target, timeout=min(5.0, max(0.5, deadline - time.monotonic()))):
                base = yaml.safe_load(find_config_file().read_text(encoding="utf-8")) or {}
                effective = apply_overrides(base, read_overrides(target))
                telegram = next((source for source in effective.get("sources", [])
                                 if source.get("provider") == "telegram"), {})
                still_configured = any(
                    isinstance(item, dict) and str(item.get("id", "")).strip() in aliases
                    for item in telegram.get("channels", [])
                )
                if not still_configured:
                    return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("Channel remains configured; no videos or history were deleted.") from exc
        if time.monotonic() >= deadline:
            raise RuntimeError("Channel remains configured; no videos or history were deleted.")
        time.sleep(0.1)


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

    channel_lock = _acquire_channel_removal_lock(channel_id)
    try:
        _verify_channel_not_configured(channel_id)

        aliases = sorted(_channel_ids(channel_id))
        prefixes = [f"telegram:{value}:" for value in aliases]
        tables = set(db.table_names(conn))

        conditions = ["id LIKE %s ESCAPE '\\'" for _ in prefixes]
        params = [_escape_like_prefix(prefix) for prefix in prefixes]
        direct_rows = conn.execute(
            "SELECT id FROM videos WHERE provider='telegram' AND (" + " OR ".join(conditions) + ")",
            params,
        ).fetchall()
        direct_ids = {str(row[0]) for row in direct_rows}

        target_edges = []
        if "video_sources" in tables:
            source_conditions = ["channel_id = ANY(%s)"]
            source_params: list[Any] = [aliases]
            for prefix in prefixes:
                source_conditions.append("source_id LIKE %s ESCAPE '\\'")
                source_params.append(_escape_like_prefix(prefix))
            target_edges = conn.execute(
                "SELECT video_id, source_id FROM video_sources WHERE provider='telegram' AND ("
                + " OR ".join(source_conditions) + ")",
                source_params,
            ).fetchall()
        target_source_ids = {str(row[1]) for row in target_edges}
        candidate_ids = sorted(direct_ids | {str(row[0]) for row in target_edges})

        deletable_ids: list[str] = []
        preserved_shared = 0

        if candidate_ids:
            existing_rows = conn.execute(
                "SELECT id FROM videos WHERE id = ANY(%s)",
                (candidate_ids,),
            ).fetchall()
            existing_ids = {str(row[0]) for row in existing_rows}

            video_sources_map: dict[str, list[tuple]] = {}
            if "video_sources" in tables and existing_ids:
                source_rows = conn.execute(
                    "SELECT video_id, source_id, provider, channel_id, is_active "
                    "FROM video_sources WHERE video_id = ANY(%s)",
                    (list(existing_ids),),
                ).fetchall()
                for s_row in source_rows:
                    v_id = str(s_row[0])
                    video_sources_map.setdefault(v_id, []).append(s_row)

            for video_id in sorted(existing_ids):
                other_active = False
                if "video_sources" in tables:
                    rows = video_sources_map.get(video_id, [])
                    other_active = any(
                        bool(row[4]) and not (
                            str(row[1]) in target_source_ids
                            or (str(row[2]) == "telegram" and str(row[3] or "") in aliases)
                        )
                        for row in rows
                    )
                if other_active:
                    preserved_shared += 1
                else:
                    deletable_ids.append(video_id)

        video_files_map: dict[str, list[str]] = {}
        if deletable_ids and "synced_files" in tables:
            file_rows = conn.execute(
                "SELECT video_id, file_path FROM synced_files WHERE video_id = ANY(%s) ORDER BY file_path",
                (deletable_ids,),
            ).fetchall()
            for row in file_rows:
                v_id = str(row[0])
                video_files_map.setdefault(v_id, []).append(str(row[1]))

        # Validate all file paths upfront before taking any destructive action (W6)
        for video_id in deletable_ids:
            for stored_path in video_files_map.get(video_id, []):
                try:
                    services.resolve_media_path(stored_path, media_dir)
                except ValueError as exc:
                    services.logger.warning("Refusing unsafe channel-removal path for %s", video_id)
                    raise RuntimeError(
                        "A catalog file path failed safety checks; no catalog row was deleted."
                    ) from exc

        record_progress(conn, job_id, "removing_source_data", completed=0,
                        total=len(deletable_ids))

        deleted_videos = 0
        deleted_files = 0
        deleted_synced_files = 0
        for index, video_id in enumerate(deletable_ids, start=1):
            paths = video_files_map.get(video_id, [])
            file_families = []
            for stored_path in paths:
                stream_path = services.resolve_media_path(stored_path, media_dir)
                file_families.append(stream_path)

            for stream_path in file_families:
                for suffix in (".strm", ".nfo", ".jpg", ".thumb_fail"):
                    candidate = stream_path.with_suffix(suffix)
                    try:
                        if candidate.exists() or candidate.is_symlink():
                            candidate.unlink()
                            deleted_files += 1
                    except OSError as exc:
                        raise RuntimeError(
                            f"A managed video file could not be removed; catalog cleanup stopped after deleting {deleted_videos} videos and {deleted_files} files."
                        ) from exc

            with conn:
                if "synced_files" in tables:
                    cur = conn.execute("DELETE FROM synced_files WHERE video_id=%s", (video_id,))
                    deleted_synced_files += getattr(cur, "rowcount", 0) or 0
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
                source_conditions.append("source_id LIKE %s ESCAPE '\\'")
                source_params.append(_escape_like_prefix(prefix))
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
            "synced_files_deleted": deleted_synced_files,
            "shared_videos_preserved": preserved_shared,
            "history_runs_removed": removed_runs,
            "history_source_rows_removed": removed_source_rows,
        }
    finally:
        _release_channel_removal_lock(channel_lock)
