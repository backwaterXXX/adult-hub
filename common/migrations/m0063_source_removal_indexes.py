"""Index keeper_id and right_id for bounded source removal cleanup."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_duplicate_decisions_keeper_id "
        "ON public.video_duplicate_decisions USING btree (keeper_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_duplicate_dismissals_right_id "
        "ON public.video_duplicate_dismissals USING btree (right_id)"
    )


migration = Migration(
    63,
    "0063_source_removal_indexes",
    "Index keeper_id and right_id for bounded source removal cleanup",
    apply,
)
current_migration = Migration(
    15,
    "0015_source_removal_indexes",
    "Index keeper_id and right_id for bounded source removal cleanup",
    apply,
)
