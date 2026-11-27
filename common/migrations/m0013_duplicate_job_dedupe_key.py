"""Migration 0013: indexed canonical identity for enqueue deduplication."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# Enqueue used to lock and parse every queued/processing payload of an action.
# New rows store the canonical identity once; legacy NULL rows are still parsed
# until they leave the queue.
STATEMENTS = (
    "ALTER TABLE public.video_duplicate_jobs ADD COLUMN IF NOT EXISTS dedupe_key text",
    "CREATE INDEX IF NOT EXISTS idx_video_duplicate_jobs_dedupe "
    "ON public.video_duplicate_jobs (action, dedupe_key) "
    "WHERE state IN ('queued', 'processing')",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=13,
    name="0013_duplicate_job_dedupe_key",
    description="Store the canonical pending-job identity for indexed deduplication",
    apply_fn=apply,
)
