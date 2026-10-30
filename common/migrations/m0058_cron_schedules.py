"""Store cron expressions directly and add settings for independent system jobs."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE admin_schedules ADD COLUMN IF NOT EXISTS cron_expression TEXT")
    conn.execute("ALTER TABLE admin_schedules DROP CONSTRAINT IF EXISTS admin_schedules_check")
    conn.execute("ALTER TABLE admin_schedules DROP CONSTRAINT IF EXISTS admin_schedules_schedule_type_check")
    conn.execute("""
        UPDATE admin_schedules
        SET cron_expression = payload::jsonb ->> '_cron',
            payload = (payload::jsonb - '_cron')::text,
            schedule_type = 'cron', interval_minutes = NULL, daily_at = NULL
        WHERE cron_expression IS NULL
          AND jsonb_typeof(payload::jsonb) = 'object'
          AND payload::jsonb ? '_cron'
    """)
    conn.execute("""
        UPDATE admin_schedules
        SET cron_expression = split_part(daily_at, ':', 2)::integer::text || ' ' ||
                              split_part(daily_at, ':', 1)::integer::text || ' * * *',
            schedule_type = 'cron', daily_at = NULL
        WHERE schedule_type = 'daily' AND daily_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
    """)
    conn.execute("""
        UPDATE admin_schedules
        SET cron_expression = CASE
                WHEN interval_minutes = 60 THEN '0 * * * *'
                WHEN interval_minutes = 1440 THEN '0 0 * * *'
                WHEN interval_minutes < 60 THEN '*/' || interval_minutes::text || ' * * * *'
                ELSE '0 */' || (interval_minutes / 60)::text || ' * * *'
            END,
            schedule_type = 'cron', interval_minutes = NULL
        WHERE schedule_type = 'interval' AND (
            (interval_minutes <= 60 AND 60 % interval_minutes = 0)
            OR (interval_minutes > 60 AND interval_minutes % 60 = 0 AND 1440 % interval_minutes = 0)
        )
    """)
    conn.execute("""
        ALTER TABLE admin_schedules ADD CONSTRAINT admin_schedules_check CHECK (
            (schedule_type = 'interval' AND interval_minutes IS NOT NULL AND interval_minutes >= 15 AND interval_minutes <= 43200 AND daily_at IS NULL AND cron_expression IS NULL)
            OR (schedule_type = 'daily' AND interval_minutes IS NULL AND daily_at IS NOT NULL AND daily_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$' AND cron_expression IS NULL)
            OR (schedule_type = 'cron' AND interval_minutes IS NULL AND daily_at IS NULL
                AND cron_expression IS NOT NULL AND length(cron_expression) >= 1 AND length(cron_expression) <= 100)
        )
    """)
    conn.execute("ALTER TABLE admin_schedules ADD CONSTRAINT admin_schedules_schedule_type_check CHECK (schedule_type IN ('interval','daily','cron'))")
    from common.admin_schedules import next_cron
    now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    converted = conn.execute(
        "SELECT id,cron_expression,enabled FROM admin_schedules WHERE schedule_type='cron'"
    ).fetchall()
    for schedule_id, expression, enabled in converted:
        conn.execute(
            "UPDATE admin_schedules SET next_run_at=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (next_cron(expression, after=now) if enabled else None, schedule_id),
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_schedules (
            id TEXT PRIMARY KEY CHECK (id IN ('library-sync','database-backup')),
            name TEXT NOT NULL,
            cron_expression TEXT,
            legacy_interval_minutes INTEGER,
            legacy_daily_at TEXT,
            needs_conversion BOOLEAN NOT NULL DEFAULT FALSE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            next_run_at TIMESTAMPTZ,
            last_run_at TIMESTAMPTZ,
            last_result TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK ((cron_expression IS NOT NULL AND length(cron_expression) >= 1 AND length(cron_expression) <= 100
                    AND legacy_interval_minutes IS NULL AND legacy_daily_at IS NULL AND needs_conversion = FALSE)
                OR (cron_expression IS NULL AND needs_conversion = TRUE
                    AND ((legacy_interval_minutes IS NOT NULL AND legacy_interval_minutes >= 15 AND legacy_interval_minutes <= 43200 AND legacy_daily_at IS NULL)
                         OR (legacy_interval_minutes IS NULL AND legacy_daily_at IS NOT NULL AND legacy_daily_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'))))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_system_schedules_due ON system_schedules (next_run_at) WHERE enabled=TRUE")


migration = Migration(58, "0058_cron_schedules", "Store cron schedules and system schedule settings", apply)
current_migration = Migration(10, "0010_cron_schedules", "Store cron schedules and system schedule settings", apply)
