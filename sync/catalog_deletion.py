"""Single dependent-row policy for deleting one catalog video.

Three paths can remove a video (orphan sweep, retention, explicit purge) and the
Telegram channel remover. They must leave the same state behind: current
per-video rows go, while durable job/merge history is kept for audit and undo.
"""

from __future__ import annotations

from common import db


def delete_video_rows(conn: db.Connection, tables: set[str], video_id: str) -> int:
    """Delete one video and its current dependent rows; return removed file rows.

    ``video_duplicate_jobs`` and merge history are deliberately retained: they
    are the audit/undo record of what happened, not live per-video state.
    """
    deleted_synced_files = 0
    if "synced_files" in tables:
        cur = conn.execute("DELETE FROM synced_files WHERE video_id=%s", (video_id,))
        deleted_synced_files += int(getattr(cur, "rowcount", 0) or 0)
    for table in ("video_categories", "video_sources", "dead_streams",
                  "video_fingerprints", "video_metadata_overrides",
                  "video_metadata_migration_issues", "admin_quality_acknowledgements"):
        if table in tables:
            conn.execute(f"DELETE FROM {table} WHERE video_id=%s", (video_id,))
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
    if "video_visual_comparisons" in tables:
        conn.execute(
            "DELETE FROM video_visual_comparisons WHERE left_id=%s OR right_id=%s",
            (video_id, video_id),
        )
    conn.execute("DELETE FROM videos WHERE id=%s", (video_id,))
    return deleted_synced_files


def table_set(conn: db.Connection) -> set[str]:
    """Table names for one deletion batch, tolerant of a legacy catalog."""
    try:
        return set(db.table_names(conn))
    except Exception:
        return set()
