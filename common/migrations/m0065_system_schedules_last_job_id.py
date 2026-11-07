"""Add last_job_id column to system_schedules."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "ALTER TABLE public.system_schedules "
        "ADD COLUMN IF NOT EXISTS last_job_id text"
    )


migration = Migration(
    65,
    "0065_system_schedules_last_job_id",
    "Add last_job_id column to system_schedules",
    apply,
)
current_migration = Migration(
    17,
    "0017_system_schedules_last_job_id",
    "Add last_job_id column to system_schedules",
    apply,
)
