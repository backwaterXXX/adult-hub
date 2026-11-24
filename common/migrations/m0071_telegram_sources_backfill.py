"""Migration 0071: Backfill primary Telegram video sources."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs: Any) -> None:
    conn.execute("""
        INSERT INTO video_sources (
            video_id,
            source_id,
            provider,
            provider_ref,
            source_url,
            channel_id,
            priority,
            is_active,
            created_at
        )
        SELECT
            v.id AS video_id,
            v.id AS source_id,
            'telegram' AS provider,
            v.id AS provider_ref,
            CASE
                WHEN split_part(v.id, ':', 2) ~ '^-100[0-9]+$' AND split_part(v.id, ':', 3) ~ '^[0-9]+$'
                THEN 'https://t.me/c/' || substr(split_part(v.id, ':', 2), 5) || '/' || split_part(v.id, ':', 3)
                ELSE ''
            END AS source_url,
            split_part(v.id, ':', 2) AS channel_id,
            1 AS priority,
            1 AS is_active,
            COALESCE(v.first_seen, CURRENT_TIMESTAMP) AS created_at
        FROM videos v
        WHERE v.provider = 'telegram'
          AND v.id ~ '^telegram:[^:]+:[0-9]+:[^:]+$'
          AND NOT EXISTS (
              SELECT 1 FROM video_sources vs WHERE vs.source_id = v.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM video_sources vs WHERE vs.video_id = v.id AND vs.is_active = 1
          )
        ON CONFLICT (source_id) DO NOTHING
    """)


migration = Migration(
    71,
    "0071_telegram_sources_backfill",
    "Backfill primary Telegram source row for pre-existing canonical Telegram videos",
    apply,
)
current_migration = Migration(
    23,
    "0023_telegram_sources_backfill",
    "Backfill primary Telegram source row for pre-existing canonical Telegram videos",
    apply,
)
