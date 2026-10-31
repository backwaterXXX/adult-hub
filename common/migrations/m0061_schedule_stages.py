"""Track the outcome of each stage that runs inside a system schedule pass."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE system_schedules ADD COLUMN IF NOT EXISTS last_finished_at TIMESTAMPTZ")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_schedule_stages (
            schedule_id TEXT NOT NULL REFERENCES system_schedules(id) ON DELETE CASCADE,
            id TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            last_result TEXT CHECK (last_result IN ('queued','running','done','failed')),
            last_error TEXT,
            last_run_at TIMESTAMPTZ,
            last_finished_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (schedule_id, id)
        )
    """)


migration = Migration(61, "0061_schedule_stages", "Record dependent system schedule stage results", apply)
current_migration = Migration(13, "0013_schedule_stages", "Record dependent system schedule stage results", apply)
