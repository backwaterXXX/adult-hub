"""Keep only the audit finding snapshots the quality centre still reads.

Every completed audit pass writes its own set of findings, so a library with a
persistent problem (say, missing plots) accumulated one open row per video per
pass forever: the table reached hundreds of thousands of rows while the UI only
ever reads the newest ``QUALITY_HISTORY_LIMIT`` passes. This prunes the older
snapshots, which keeps the table proportional to the catalog instead of to the
number of audit runs.
"""

from __future__ import annotations

from typing import Any

AUDIT_ACTIONS = ("consistency_audit", "metadata_quality_audit")

# Must stay in step with common.admin_quality.QUALITY_HISTORY_LIMIT: the quality
# centre reads exactly these passes, so anything older has no reader.
KEEP_COMPLETE_AUDITS = 8

# Audits recorded before batch IDs existed have no pairing information; keep a
# conservative recent window for them instead of deleting by guesswork.
LEGACY_AUDIT_RETENTION_DAYS = 7


def _kept_job_ids(conn, keep: int) -> list[str]:
    """Return the audit job IDs whose findings must survive, newest batches first."""
    batches = conn.execute(
        "SELECT COALESCE(result::jsonb->>'audit_batch_id', ''), max(created_at) AS newest "
        "FROM video_duplicate_jobs "
        "WHERE action = ANY(%s) AND state = 'done' "
        "GROUP BY 1 ORDER BY newest DESC LIMIT %s",
        (list(AUDIT_ACTIONS), keep),
    ).fetchall()
    kept_batches = [str(row[0]) for row in batches if row[0]]
    kept: list[str] = []
    if kept_batches:
        rows = conn.execute(
            "SELECT id FROM video_duplicate_jobs "
            "WHERE action = ANY(%s) AND state = 'done' "
            "AND COALESCE(result::jsonb->>'audit_batch_id', '') = ANY(%s)",
            (list(AUDIT_ACTIONS), kept_batches),
        ).fetchall()
        kept.extend(str(row[0]) for row in rows)
    legacy = conn.execute(
        "SELECT id FROM video_duplicate_jobs "
        "WHERE action = ANY(%s) AND state = 'done' "
        "AND COALESCE(result::jsonb->>'audit_batch_id', '') = '' "
        "AND created_at >= CURRENT_TIMESTAMP - make_interval(days => %s)",
        (list(AUDIT_ACTIONS), LEGACY_AUDIT_RETENTION_DAYS),
    ).fetchall()
    kept.extend(str(row[0]) for row in legacy)
    return list(dict.fromkeys(kept))


def prune_superseded_findings(conn: Any, *, keep: int = KEEP_COMPLETE_AUDITS) -> int:
    """Delete findings of completed audits older than the last ``keep`` passes.

    Findings of a queued or running audit are never touched.
    """
    if type(keep) is not int or keep < 1:
        raise ValueError("The audit history window must be a positive integer.")
    kept = _kept_job_ids(conn, keep)
    cursor = conn.execute(
        "DELETE FROM admin_audit_items i "
        "WHERE NOT (i.job_id = ANY(%s)) "
        "AND NOT EXISTS (SELECT 1 FROM video_duplicate_jobs j "
        "                WHERE j.id=i.job_id AND j.state IN ('queued','processing'))",
        (kept,),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))
