"""Add client_users, client_user_watch_progress, and client_user_favorites tables."""

from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_users ("
        "id text NOT NULL, "
        "username text NOT NULL, "
        "password_hash text NOT NULL, "
        "is_admin boolean DEFAULT false NOT NULL, "
        "is_active boolean DEFAULT true NOT NULL, "
        "created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "CONSTRAINT client_users_pkey PRIMARY KEY (id)"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_users_username "
        "ON public.client_users (lower(username))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_user_watch_progress ("
        "user_id text NOT NULL, "
        "video_id text NOT NULL, "
        "position_seconds integer DEFAULT 0 NOT NULL, "
        "duration_seconds integer DEFAULT 0 NOT NULL, "
        "played_percentage double precision DEFAULT 0.0 NOT NULL, "
        "is_played boolean DEFAULT false NOT NULL, "
        "last_watched_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "CONSTRAINT client_user_watch_progress_pkey PRIMARY KEY (user_id, video_id), "
        "CONSTRAINT client_user_watch_progress_position_seconds_check CHECK ((position_seconds >= 0)), "
        "CONSTRAINT client_user_watch_progress_duration_seconds_check CHECK ((duration_seconds >= 0)), "
        "CONSTRAINT client_user_watch_progress_played_percentage_check CHECK (((played_percentage >= (0)::double precision) AND (played_percentage <= (100)::double precision))), "
        "CONSTRAINT client_user_watch_progress_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.client_users(id) ON DELETE CASCADE"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_user_watch_progress_video_id "
        "ON public.client_user_watch_progress (video_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_user_watch_progress_recent "
        "ON public.client_user_watch_progress (user_id, last_watched_at DESC)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.client_user_favorites ("
        "user_id text NOT NULL, "
        "video_id text NOT NULL, "
        "created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "CONSTRAINT client_user_favorites_pkey PRIMARY KEY (user_id, video_id), "
        "CONSTRAINT client_user_favorites_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.client_users(id) ON DELETE CASCADE"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_user_favorites_video_id "
        "ON public.client_user_favorites (video_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_user_favorites_created "
        "ON public.client_user_favorites (user_id, created_at DESC)"
    )


migration = Migration(
    69,
    "0069_direct_play_user_state",
    "Add client_users, client_user_watch_progress, and client_user_favorites tables",
    apply,
)
current_migration = Migration(
    21,
    "0021_direct_play_user_state",
    "Add client_users, client_user_watch_progress, and client_user_favorites tables",
    apply,
)
