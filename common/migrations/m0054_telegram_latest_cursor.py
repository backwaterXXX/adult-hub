"""Persist resumable newest-message scans for Telegram channels."""
from common.migrations.base import Migration


def apply(conn, **kwargs):
    for name in ("latest_watermark", "latest_cursor", "latest_head"):
        conn.execute(f"ALTER TABLE telegram_channel_state ADD COLUMN IF NOT EXISTS {name} BIGINT")
    conn.execute("UPDATE telegram_channel_state SET backfill_in_progress=1, backfill_cursor=NULL, backfill_completed_at=NULL WHERE backfill_in_progress=0")


migration = Migration(54, "0054_telegram_latest_cursor", "Resume newest Telegram scans by message ID", apply)
current_migration = Migration(6, "0006_telegram_latest_cursor", "Resume newest Telegram scans by message ID", apply)
