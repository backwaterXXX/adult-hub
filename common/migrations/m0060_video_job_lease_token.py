"""Track which worker process owns a processing durable job."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "ALTER TABLE video_duplicate_jobs ADD COLUMN IF NOT EXISTS lease_token TEXT"
    )


migration = Migration(60, "0060_video_job_lease_token", "Track the worker that claimed a durable job", apply)
current_migration = Migration(12, "0012_video_job_lease_token", "Track the worker that claimed a durable job", apply)
