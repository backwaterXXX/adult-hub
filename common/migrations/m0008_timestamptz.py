"""Migration 0008: store persisted instants as timestamptz (UTC)."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


# A value is migrated only when the column is known to represent an instant.
# backfill_cursor, durations, date_released, and monotonic process timers are
# deliberately excluded because they are not wall-clock timestamps.
TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("videos", "stream_checked"),
    ("videos", "performers_checked_at"),
    ("videos", "performers_audited_at"),
    ("videos", "scene_matched_at"),
    ("videos", "first_seen"),
    ("videos", "last_seen"),
    ("dead_streams", "detected_at"),
    ("synced_files", "created_at"),
    ("video_duplicate_jobs", "created_at"),
    ("video_duplicate_jobs", "updated_at"),
    ("video_duplicate_decisions", "created_at"),
    ("video_duplicate_dismissals", "created_at"),
    ("video_sources", "created_at"),
    ("telegram_channel_state", "backfill_completed_at"),
    ("telegram_channel_state", "updated_at"),
    ("performer_bios", "updated_at"),
    ("performer_photos", "updated_at"),
    ("jellyfin_person_sync", "pbc_updated_at"),
    ("stream_cache", "expire_at"),
    ("stream_cache", "last_accessed"),
    ("diagnostic_reports", "ended_at"),
)
NANOSECOND_COLUMNS = frozenset({("videos", "performers_audited_at")})


def apply(conn: Any, **kwargs) -> None:
    for table, column in TIMESTAMP_COLUMNS:
        info = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
        if not info:
            continue
        data_type = info[0]
        if data_type == "timestamp with time zone":
            continue
        if data_type not in ("bigint", "double precision", "integer", "numeric"):
            raise RuntimeError(f"Cannot convert {table}.{column} from unexpected type {data_type!r}")

        # Most legacy fields are seconds. This one historical audit marker was
        # written by time.time_ns(), so validate and convert it as nanoseconds.
        scale = "1000000000.0" if (table, column) in NANOSECOND_COLUMNS else "1.0"
        seconds = f"({column} / {scale})"

        # Reject overflow and non-finite legacy values before changing any type.
        if data_type == "double precision":
            invalid = conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL AND "
                f"({column} = 'NaN'::double precision OR {column} = 'Infinity'::double precision "
                f"OR {column} = '-Infinity'::double precision OR "
                f"{seconds} < -62135596800 OR {seconds} > 253402300799) LIMIT 1"
            ).fetchone()
        else:
            invalid = conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL AND "
                f"({seconds} < -62135596800 OR {seconds} > 253402300799) LIMIT 1"
            ).fetchone()
        if invalid:
            raise RuntimeError(
                f"{table}.{column} contains epoch values outside the supported "
                "UTC range 0001-01-01 through 9999-12-31; inspect and repair them first"
            )
        conn.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMPTZ "
            f"USING (TIMESTAMPTZ 'epoch' + {seconds} * INTERVAL '1 second')"
        )


migration = Migration(
    version=8,
    name="0008_timestamptz",
    description="Convert persisted Unix-epoch instants to timestamptz in UTC",
    apply_fn=apply,
)
