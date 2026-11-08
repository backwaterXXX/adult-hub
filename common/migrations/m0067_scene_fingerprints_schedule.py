"""Allow the independent scene fingerprints system schedule."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE system_schedules DROP CONSTRAINT IF EXISTS system_schedules_id_check")
    conn.execute(
        "ALTER TABLE system_schedules ADD CONSTRAINT system_schedules_id_check "
        "CHECK (id = ANY (ARRAY['library-sync'::text, 'database-backup'::text, 'artwork-generation'::text, 'scene-fingerprints'::text]))"
    )


migration = Migration(67, "0067_scene_fingerprints_schedule",
                      "Allow the scene fingerprints system schedule", apply)
current_migration = Migration(19, "0019_scene_fingerprints_schedule",
                              "Allow the scene fingerprints system schedule", apply)
