"""Add telegram_accounts and telegram_account_channels tables for multi-account support."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.telegram_accounts ("
        "account_id text PRIMARY KEY, "
        "label text NOT NULL DEFAULT '', "
        "phone_mask text NOT NULL DEFAULT '', "
        "enabled boolean NOT NULL DEFAULT true, "
        "session_file text NOT NULL, "
        "created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.telegram_account_channels ("
        "account_id text NOT NULL REFERENCES public.telegram_accounts(account_id) ON DELETE CASCADE, "
        "channel_id text NOT NULL, "
        "access_status text NOT NULL DEFAULT 'unknown', "
        "last_checked_at timestamp with time zone, "
        "error_detail text NOT NULL DEFAULT '', "
        "PRIMARY KEY (account_id, channel_id)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_account_channels_lookup "
        "ON public.telegram_account_channels (channel_id, access_status)"
    )


migration = Migration(
    66,
    "0066_telegram_accounts",
    "Add telegram_accounts and telegram_account_channels tables",
    apply,
)
current_migration = Migration(
    18,
    "0018_telegram_accounts",
    "Add telegram_accounts and telegram_account_channels tables",
    apply,
)
