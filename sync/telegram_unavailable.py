"""Bounded, reversible quarantine for Telegram posts missing from a healthy channel.

A quarantined post keeps its catalog row, its sources, its categories and its
fingerprints: only the local files move aside, and they move back as soon as the
post answers again. Nothing here expires on a timer, and the cleanup paths skip
quarantined rows (``library_cleanup``) and require an enabled, successfully synced
channel before deleting anything (``is_cleanup_allowed``). That is deliberate:
the metadata and pHash of a blocked channel are what let the deduplication pass
recognise the same scene on a channel that still works. See CONFIGURATION.md,
section 6.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


def _ref(video_id: str):
    if not isinstance(video_id, str) or not video_id.startswith("telegram:"):
        return None
    parts = video_id[len("telegram:"):].rsplit(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2]:
        return None
    return parts[0], int(parts[1]), parts[2]


def _quarantine_folder(quarantine_dir: Path, video_id: str) -> Path:
    return quarantine_dir / hashlib.sha256(video_id.encode()).hexdigest()


_MANIFEST_NAME = ".quarantine-manifest.json"


def _write_quarantine_manifest(folder: Path, video_id: str, moved: list[dict]) -> None:
    """Journal the move so a crash before the DB write can be undone on restart."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _MANIFEST_NAME).write_text(
            json.dumps({"video_id": video_id, "files": moved}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_quarantine_manifest(folder: Path) -> None:
    try:
        (folder / _MANIFEST_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def _safe_quarantine_stream(media_dir: Path, relative_name: str) -> Path | None:
    """Resolve a recorded stream with the same symlink policy as media cleanup."""
    root = media_dir.resolve()
    path = Path(str(relative_name))
    if str(relative_name).startswith("/media/adult/"):
        path = Path(str(relative_name)[len("/media/adult/"):])
    elif path.is_absolute():
        path = Path(path.name)
    lexical = root / path
    if not lexical.is_relative_to(root):
        return None
    cursor = root
    for part in lexical.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    try:
        resolved = lexical.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root) or resolved.suffix != ".strm":
        return None
    return resolved


def _quarantine_files(conn, video_id: str, media_dir: Path, quarantine_dir: Path) -> list[dict]:
    rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
    ).fetchall()
    folder = _quarantine_folder(quarantine_dir, video_id)
    moved = []
    try:
        for (relative_name,) in rows:
            stream = _safe_quarantine_stream(media_dir, relative_name)
            if stream is None:
                continue
            for suffix in (".strm", ".jpg", ".thumb_fail"):
                source = stream.with_suffix(suffix)
                if source.is_symlink() or not source.is_file():
                    continue
                destination = folder / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append({"source": str(source), "quarantine": str(destination)})
    except Exception:
        _restore_files(moved)
        raise
    _write_quarantine_manifest(folder, video_id, moved)
    return moved


def _recover_orphaned_quarantine(conn, quarantine_dir: Path, logger) -> int:
    """Restore files moved by a run that crashed before recording the quarantine."""
    try:
        folders = list(quarantine_dir.iterdir()) if quarantine_dir.is_dir() else []
    except OSError:
        return 0
    restored = 0
    for folder in folders:
        manifest_path = folder / _MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            video_id = str(manifest.get("video_id") or "")
            files = manifest.get("files") or []
            if not video_id or not isinstance(files, list):
                continue
            recorded = conn.execute(
                "SELECT 1 FROM telegram_unavailable_quarantine WHERE video_id=%s", (video_id,)
            ).fetchone()
            decided = conn.execute(
                "SELECT 1 FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'",
                (video_id,),
            ).fetchone()
            if recorded or decided:
                _clear_quarantine_manifest(folder)
                continue
            _restore_files(files)
            _clear_quarantine_manifest(folder)
            restored += 1
            logger.info("Restored quarantined files left by an interrupted run: %s", video_id)
        except Exception as exc:
            logger.warning("Could not recover orphaned quarantine folder %s: %s", folder, exc)
    return restored


def _restore_files(files: Any) -> None:
    if isinstance(files, str):
        files = json.loads(files)
    for item in files or []:
        source = Path(item["source"])
        quarantined = Path(item["quarantine"])
        if not quarantined.exists():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            quarantined.unlink()
        else:
            shutil.move(str(quarantined), str(source))
    for folder in {Path(item["quarantine"]).parent for item in files or []}:
        _clear_quarantine_manifest(folder)


def reconcile_telegram_channel(
    conn, channel_id: str, media_dir: Path, quarantine_dir: Path,
    resolver_api_url: str, fetch_json_with_retry: Callable, logger: Any,
    *, current_video_ids: set[str] | None = None, limit: int = 20,
    now: int,
) -> dict[str, int]:
    """Check at most ``limit`` exact IDs after caller confirms channel sync success."""
    current_video_ids = current_video_ids or set()
    prefix_a = f"telegram:{channel_id}:%"
    normalized = (channel_id[4:] if channel_id.startswith("-100")
                 else "-100" + channel_id.lstrip("-"))
    prefix_b = f"telegram:{normalized}:%"
    rows = conn.execute("""
        SELECT v.id, q.status, q.miss_count, q.files
        FROM videos v LEFT JOIN telegram_unavailable_quarantine q ON q.video_id=v.id
        WHERE v.provider='telegram' AND (v.id LIKE %s OR v.id LIKE %s)
          AND (q.video_id IS NOT NULL OR NOT (v.id = ANY(%s)))
          AND (q.video_id IS NULL OR q.last_checked_at < to_timestamp(%s) - interval '20 hours')
        ORDER BY COALESCE(q.last_checked_at, v.last_seen, v.first_seen) ASC NULLS FIRST, v.id
        LIMIT %s
    """, (prefix_a, prefix_b, list(current_video_ids), now, max(1, min(int(limit), 50)))).fetchall()
    counts = {"checked": 0, "available": 0, "suspect": 0, "quarantined": 0, "restored": 0, "errors": 0}
    for video_id, old_status, old_misses, old_files in rows:
        ref = _ref(video_id)
        if ref is None:
            continue
        channel_ref, message_id, unique_id = ref
        if video_id in current_video_ids:
            status = "available"
        else:
            url = _exact_message_check_url(resolver_api_url, channel_ref, message_id, unique_id)
            try:
                status = _fetch_message_status(url, fetch_json_with_retry)
            except Exception as exc:
                counts["errors"] += 1
                logger.info("Telegram availability check inconclusive for %s: %s", video_id, exc)
                _retain_inconclusive(conn, video_id, channel_id, message_id, unique_id,
                                     old_status, old_misses, old_files, now, exc, logger)
                continue
        counts["checked"] += 1
        if status == "available":
            _restore_available(conn, video_id, old_status, old_files, now, counts, logger)
            continue
        misses = int(old_misses or 0) + 1
        next_status = "quarantined" if misses >= 2 else "suspect"
        files = old_files or []
        try:
            if next_status == "quarantined" and old_status != "quarantined":
                files = _quarantine_files(conn, video_id, media_dir, quarantine_dir)
            conn.execute("""
                INSERT INTO telegram_unavailable_quarantine
                    (video_id,channel_id,message_id,file_unique_id,status,miss_count,last_checked_at,files,last_error)
                VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s::jsonb,'')
                ON CONFLICT(video_id) DO UPDATE SET status=EXCLUDED.status,
                    miss_count=EXCLUDED.miss_count,last_checked_at=EXCLUDED.last_checked_at,
                    files=EXCLUDED.files,last_error=''
            """, (video_id, channel_id, message_id, unique_id, next_status,
                  misses, now, json.dumps(files)))
            counts[next_status] += 1
        except Exception as exc:
            if next_status == "quarantined" and old_status != "quarantined":
                try:
                    _restore_files(files)
                except Exception as restore_exc:
                    logger.warning("Could not restore files during failed quarantine of %s: %s", video_id, restore_exc)
            counts["errors"] += 1
            logger.warning("Could not quarantine unavailable Telegram item %s: %s", video_id, exc)
    conn.commit()
    return counts


def _exact_message_check_url(resolver_api_url: str, channel_ref: str, message_id, unique_id: str) -> str:
    return (f"{resolver_api_url.rstrip('/')}/admin/api/telegram/message-check/"
            f"{quote(channel_ref, safe='')}/{message_id}?unique_id={quote(unique_id, safe='')}")


def _fetch_message_status(url: str, fetch_json_with_retry: Callable) -> str:
    result = fetch_json_with_retry(url, timeout=15.0, desc="telegram/exact-message-check")
    status = result.get("status") if isinstance(result, dict) else None
    if status not in {"available", "missing", "mismatch"}:
        raise ValueError("unexpected Telegram message check response")
    return status


def _retain_inconclusive(conn, video_id, channel_id, message_id, unique_id,
                         old_status, old_misses, old_files, now, exc, logger) -> None:
    """Keep the previous quarantine state when the check could not give an answer."""
    retained_status = old_status if old_status in {"suspect", "quarantined"} else "failed"
    try:
        conn.execute("""
            INSERT INTO telegram_unavailable_quarantine
                (video_id,channel_id,message_id,file_unique_id,status,miss_count,last_checked_at,files,last_error)
            VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s::jsonb,%s)
            ON CONFLICT(video_id) DO UPDATE SET
                last_checked_at=EXCLUDED.last_checked_at,
                last_error=EXCLUDED.last_error
        """, (video_id, channel_id, message_id, unique_id, retained_status,
              int(old_misses or 0), now, json.dumps(old_files or []), str(exc)[:500]))
    except Exception:
        logger.warning("Could not record inconclusive Telegram availability check for %s; "
                       "the quarantine row was not updated", video_id, exc_info=True)


def _restore_available(conn, video_id, old_status, old_files, now, counts, logger) -> None:
    """Move files back from quarantine and clear the quarantine row for a Telegram item that answered."""
    try:
        _restore_files(old_files)
        conn.execute("UPDATE videos SET last_seen=GREATEST(COALESCE(last_seen, to_timestamp(%s)), to_timestamp(%s)) WHERE id=%s", (now, now, video_id))
        conn.execute("DELETE FROM telegram_unavailable_quarantine WHERE video_id=%s", (video_id,))
        counts["available"] += 1
        if old_status == "quarantined":
            counts["restored"] += 1
    except Exception as exc:
        counts["errors"] += 1
        logger.warning("Could not restore Telegram quarantine for %s: %s", video_id, exc)


