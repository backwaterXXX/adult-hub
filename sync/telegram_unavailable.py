"""Bounded, reversible quarantine for Telegram posts missing from a healthy channel."""

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


def _quarantine_files(conn, video_id: str, media_dir: Path, quarantine_dir: Path) -> list[dict]:
    rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
    ).fetchall()
    folder = quarantine_dir / hashlib.sha256(video_id.encode()).hexdigest()
    moved = []
    try:
        for (relative_name,) in rows:
            try:
                stream = (media_dir / relative_name).resolve()
                stream.relative_to(media_dir.resolve())
            except (ValueError, OSError, TypeError):
                continue
            for suffix in (".strm", ".nfo", ".jpg", ".thumb_fail"):
                source = stream.with_suffix(suffix)
                if not source.is_file():
                    continue
                destination = folder / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append({"source": str(source), "quarantine": str(destination)})
    except Exception:
        _restore_files(moved)
        raise
    return moved


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
          AND (q.video_id IS NULL OR q.last_checked_at < to_timestamp(%s) - interval '20 hours')
        ORDER BY CASE WHEN q.status='quarantined' THEN 0
                      WHEN q.status='suspect' THEN 1 ELSE 2 END,
                 q.last_checked_at NULLS FIRST, v.first_seen NULLS FIRST, v.id
        LIMIT %s
    """, (prefix_a, prefix_b, now, max(1, min(int(limit), 50)))).fetchall()
    counts = {"checked": 0, "suspect": 0, "quarantined": 0, "restored": 0, "errors": 0}
    for video_id, old_status, old_misses, old_files in rows:
        ref = _ref(video_id)
        if ref is None:
            continue
        channel_ref, message_id, unique_id = ref
        if video_id in current_video_ids:
            status = "available"
        else:
            url = (f"{resolver_api_url.rstrip('/')}/admin/api/telegram/message-check/"
                   f"{quote(channel_ref, safe='')}/{message_id}?unique_id={quote(unique_id, safe='')}")
            try:
                result = fetch_json_with_retry(url, timeout=15.0, desc="telegram/exact-message-check")
                status = result.get("status") if isinstance(result, dict) else None
                if status not in {"available", "missing", "mismatch"}:
                    raise ValueError("unexpected Telegram message check response")
            except Exception as exc:
                counts["errors"] += 1
                logger.info("Telegram availability check inconclusive for %s: %s", video_id, exc)
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
                    pass
                continue
        counts["checked"] += 1
        if status == "available":
            try:
                _restore_files(old_files)
                conn.execute("DELETE FROM telegram_unavailable_quarantine WHERE video_id=%s", (video_id,))
                if old_status == "quarantined":
                    counts["restored"] += 1
            except Exception as exc:
                counts["errors"] += 1
                logger.warning("Could not restore Telegram quarantine for %s: %s", video_id, exc)
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
                except Exception:
                    pass
            counts["errors"] += 1
            logger.warning("Could not quarantine unavailable Telegram item %s: %s", video_id, exc)
    conn.commit()
    return counts
