"""Add client_sessions, client_session_spent_tokens, client_pairing_sessions, and client_auth_rate_limits tables."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_sessions ("
        "id text NOT NULL, "
        "user_id text NOT NULL, "
        "device_id text NOT NULL, "
        "device_name text DEFAULT '' NOT NULL, "
        "access_token_hash text NOT NULL, "
        "refresh_token_hash text NOT NULL, "
        "previous_refresh_token_hash text, "
        "access_expires_at timestamp with time zone NOT NULL, "
        "refresh_expires_at timestamp with time zone NOT NULL, "
        "revoked_at timestamp with time zone, "
        "created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "CONSTRAINT client_sessions_pkey PRIMARY KEY (id), "
        "CONSTRAINT client_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.client_users(id) ON DELETE CASCADE"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_sessions_access_hash "
        "ON public.client_sessions (access_token_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_sessions_refresh_hash "
        "ON public.client_sessions (refresh_token_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_sessions_prev_refresh "
        "ON public.client_sessions (previous_refresh_token_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_sessions_user_device "
        "ON public.client_sessions (user_id, device_id)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_session_spent_tokens ("
        "session_id text NOT NULL, "
        "token_hash text NOT NULL, "
        "spent_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "CONSTRAINT client_session_spent_tokens_pkey PRIMARY KEY (token_hash), "
        "CONSTRAINT client_session_spent_tokens_session_fkey FOREIGN KEY (session_id) REFERENCES public.client_sessions(id) ON DELETE CASCADE"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_session_spent_session "
        "ON public.client_session_spent_tokens (session_id)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_pairing_sessions ("
        "id text NOT NULL, "
        "pairing_code text NOT NULL, "
        "normalized_code text NOT NULL, "
        "secret_hash text NOT NULL, "
        "device_name text DEFAULT '' NOT NULL, "
        "device_id text DEFAULT '' NOT NULL, "
        "user_id text, "
        "status text DEFAULT 'pending' NOT NULL, "
        "poll_attempts integer DEFAULT 0 NOT NULL, "
        "expires_at timestamp with time zone NOT NULL, "
        "created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "approved_at timestamp with time zone, "
        "exchanged_at timestamp with time zone, "
        "client_ip text DEFAULT '' NOT NULL, "
        "CONSTRAINT client_pairing_sessions_pkey PRIMARY KEY (id), "
        "CONSTRAINT client_pairing_sessions_status_check CHECK (status IN ('pending', 'approved', 'exchanged', 'expired', 'revoked')), "
        "CONSTRAINT client_pairing_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.client_users(id) ON DELETE SET NULL"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_pairing_live_code "
        "ON public.client_pairing_sessions (normalized_code) "
        "WHERE status IN ('pending', 'approved')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_pairing_secret "
        "ON public.client_pairing_sessions (secret_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_pairing_expires "
        "ON public.client_pairing_sessions (expires_at)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_auth_rate_limits ("
        "rate_key text NOT NULL, "
        "window_epoch bigint NOT NULL, "
        "attempts integer DEFAULT 1 NOT NULL, "
        "CONSTRAINT client_auth_rate_limits_pkey PRIMARY KEY (rate_key, window_epoch)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_auth_rate_limits_window "
        "ON public.client_auth_rate_limits (window_epoch)"
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_users_single_owner "
        "ON public.client_users (is_admin) WHERE is_admin = true"
    )


migration = Migration(
    70,
    "0070_client_auth",
    "Add client_sessions, client_session_spent_tokens, client_pairing_sessions, and client_auth_rate_limits tables",
    apply,
)
current_migration = Migration(
    22,
    "0022_client_auth",
    "Add client_sessions, client_session_spent_tokens, client_pairing_sessions, and client_auth_rate_limits tables",
    apply,
)
