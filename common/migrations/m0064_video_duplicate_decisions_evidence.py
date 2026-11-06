"""Add evidence column to video_duplicate_decisions."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "ALTER TABLE public.video_duplicate_decisions "
        "ADD COLUMN IF NOT EXISTS evidence text DEFAULT ''::text NOT NULL"
    )


migration = Migration(
    64,
    "0064_video_duplicate_decisions_evidence",
    "Add evidence column to video_duplicate_decisions",
    apply,
)
current_migration = Migration(
    16,
    "0016_video_duplicate_decisions_evidence",
    "Add evidence column to video_duplicate_decisions",
    apply,
)
