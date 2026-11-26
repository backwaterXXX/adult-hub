"""Migration 0004: one open audit finding per video and kind."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# Every audit pass inserted a new "open" row for the same problem, so the admin
# backlog counted rows instead of videos: 2 240 open missing_artwork rows stood
# for 250 videos, and 52 867 missing_metadata rows for 10 595 videos. Keep the
# newest row per (kind, video_id) and make a second open row impossible.
STATEMENTS = (
    "DELETE FROM public.admin_audit_items a "
    "USING public.admin_audit_items b "
    "WHERE a.kind = b.kind AND a.video_id = b.video_id "
    "AND a.status = 'open' AND b.status = 'open' "
    "AND a.video_id IS NOT NULL AND a.id < b.id",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_audit_items_open_video "
    "ON public.admin_audit_items (kind, video_id) "
    "WHERE status = 'open' AND video_id IS NOT NULL",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=4,
    name="0004_admin_audit_open_findings",
    description="One open audit finding per video and kind",
    apply_fn=apply,
)
