"""Allow the independent pHash generation system schedule."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE system_schedules DROP CONSTRAINT IF EXISTS system_schedules_id_check")
    conn.execute(
        "ALTER TABLE system_schedules ADD CONSTRAINT system_schedules_id_check "
        "CHECK (id = ANY (ARRAY['library-sync'::text, 'database-backup'::text, 'artwork-generation'::text, 'scene-fingerprints'::text, 'phash-generation'::text]))"
    )


migration = Migration(68, "0068_phash_generation_schedule",
                      "Allow the pHash generation system schedule", apply)
current_migration = Migration(20, "0020_phash_generation_schedule",
                              "Allow the pHash generation system schedule", apply)
