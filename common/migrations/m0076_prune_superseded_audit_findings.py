"""Prune audit finding snapshots the quality centre no longer reads.

Each completed audit pass stores its own findings, and until now nothing removed
the older passes: prod held 547k ``missing_metadata`` rows for 28k videos. The
quality centre reads only the newest eight completed passes
(``QUALITY_HISTORY_LIMIT``), so everything before them is dead weight.

Findings of a queued or running audit are never touched.
"""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

_AUDIT_ACTIONS = ("consistency_audit", "metadata_quality_audit")
_KEEP_COMPLETE_AUDITS = 8
_LEGACY_AUDIT_RETENTION_DAYS = 7

_PRUNE_SQL = """
WITH done_audits AS (
    SELECT id, created_at, COALESCE(result::jsonb->>'audit_batch_id', '') AS batch_id
    FROM video_duplicate_jobs
    WHERE action = ANY(%s) AND state = 'done'
), kept_batches AS (
    SELECT batch_id
    FROM done_audits
    WHERE batch_id <> ''
    GROUP BY batch_id
    ORDER BY max(created_at) DESC
    LIMIT %s
), kept_jobs AS (
    SELECT id
    FROM done_audits
    WHERE batch_id IN (SELECT batch_id FROM kept_batches)
    UNION
    SELECT id
    FROM done_audits
    WHERE batch_id = ''
      AND created_at >= CURRENT_TIMESTAMP - make_interval(days => %s)
)
DELETE FROM admin_audit_items i
WHERE NOT (i.job_id IN (SELECT id FROM kept_jobs))
  AND NOT EXISTS (
      SELECT 1 FROM video_duplicate_jobs j
      WHERE j.id = i.job_id AND j.state IN ('queued', 'processing')
  )
"""


def apply(conn: Any, **kwargs: Any) -> None:
    conn.execute(_PRUNE_SQL, (
        list(_AUDIT_ACTIONS),
        _KEEP_COMPLETE_AUDITS,
        _LEGACY_AUDIT_RETENTION_DAYS,
    ))


migration = Migration(
    76,
    "0076_prune_superseded_audit_findings",
    "Delete audit finding snapshots older than the quality history window",
    apply,
)
