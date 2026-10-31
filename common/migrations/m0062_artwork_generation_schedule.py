"""Allow the independent daily artwork generation system schedule."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE system_schedules DROP CONSTRAINT IF EXISTS system_schedules_id_check")
    conn.execute(
        "ALTER TABLE system_schedules ADD CONSTRAINT system_schedules_id_check "
        "CHECK (id = ANY (ARRAY['library-sync'::text, 'database-backup'::text, 'artwork-generation'::text]))"
    )


migration = Migration(62, "0062_artwork_generation_schedule",
                      "Allow the daily artwork generation system schedule", apply)
current_migration = Migration(14, "0014_artwork_generation_schedule",
                              "Allow the daily artwork generation system schedule", apply)
