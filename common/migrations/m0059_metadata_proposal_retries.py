"""Track bounded retries for trusted metadata proposal application."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE metadata_review_proposals ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE metadata_review_proposals ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ")
    conn.execute("ALTER TABLE metadata_review_proposals ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
    conn.execute("ALTER TABLE metadata_review_proposals ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_review_retry "
        "ON metadata_review_proposals (next_attempt_at,id) WHERE status='pending'"
    )


migration = Migration(59, "0059_metadata_proposal_retries", "Back off failed trusted metadata proposals", apply)
current_migration = Migration(11, "0011_metadata_proposal_retries", "Back off failed trusted metadata proposals", apply)
