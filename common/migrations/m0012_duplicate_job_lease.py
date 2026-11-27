"""Migration 0012: every processing duplicate job has an owner lease."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# A worker may only finish/heartbeat a job while it still holds the lease the
# claim query wrote. The trigger backfills a token for legacy writers (and test
# fixtures) so the constraint can be strict without breaking old rows.
STATEMENTS = (
    """CREATE OR REPLACE FUNCTION public.ensure_video_duplicate_job_lease()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.state = 'processing'
               AND (NEW.lease_token IS NULL OR NEW.lease_token = '') THEN
                NEW.lease_token := md5(random()::text || clock_timestamp()::text);
            END IF;
            RETURN NEW;
        END;
    $$""",
    "DROP TRIGGER IF EXISTS video_duplicate_jobs_ensure_lease ON public.video_duplicate_jobs",
    "CREATE TRIGGER video_duplicate_jobs_ensure_lease "
    "BEFORE INSERT OR UPDATE ON public.video_duplicate_jobs "
    "FOR EACH ROW EXECUTE FUNCTION public.ensure_video_duplicate_job_lease()",
    "UPDATE public.video_duplicate_jobs "
    "SET lease_token = md5(random()::text || clock_timestamp()::text) "
    "WHERE state = 'processing' AND (lease_token IS NULL OR lease_token = '')",
    "ALTER TABLE public.video_duplicate_jobs "
    "DROP CONSTRAINT IF EXISTS video_duplicate_jobs_processing_lease_check",
    "ALTER TABLE public.video_duplicate_jobs "
    "ADD CONSTRAINT video_duplicate_jobs_processing_lease_check "
    "CHECK (state <> 'processing' OR lease_token IS NOT NULL)",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=12,
    name="0012_duplicate_job_lease",
    description="Require an owner lease for every processing duplicate job",
    apply_fn=apply,
)
