"""Migration 0006: Data normalization for synced files and categories."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from common.migrations.base import Migration

logger = logging.getLogger(__name__)


def apply(conn: Any, *, map_category_fn: Callable[[str], str | None] | None = None, **kwargs) -> None:
    # 1. Normalize container absolute paths /media/adult/... to relative filenames
    conn.execute("""
        UPDATE synced_files
        SET file_path = REPLACE(file_path, '/media/adult/', '')
        WHERE file_path LIKE '/media/adult/%'
    """)

    # 2. Fix synced_files.video_id to match videos.id (composite ID)
    conn.execute("""
        UPDATE synced_files
        SET video_id = (
            SELECT v.id FROM videos v
            WHERE v.id = synced_files.video_id
               OR v.id LIKE '%:' || synced_files.video_id
            LIMIT 1
        )
        WHERE video_id NOT LIKE '%:%'
          AND EXISTS (
            SELECT 1 FROM videos v
            WHERE v.id = synced_files.video_id
               OR v.id LIKE '%:' || synced_files.video_id
        )
    """)

    # 3. Populate video_categories from videos table if empty
    vc_count = conn.execute("SELECT count(*) FROM video_categories").fetchone()[0]
    if vc_count == 0:
        cur = conn.execute(
            "SELECT id, categories FROM videos WHERE categories IS NOT NULL AND categories != ''"
        )
        skipped = 0
        populated = 0
        for vid_id, cats_val in cur.fetchall():
            try:
                if cats_val.startswith("["):
                    cat_list = json.loads(cats_val)
                else:
                    cat_list = [c.strip() for c in cats_val.split(",") if c.strip()]
                for cat in cat_list:
                    m_cat = map_category_fn(cat) if map_category_fn else cat
                    if m_cat and not str(m_cat).startswith("Browse"):
                        conn.execute(
                            "INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (vid_id, m_cat),
                        )
                        populated += 1
            except Exception:
                skipped += 1
                logger.warning(
                    "0006_data_normalizations failed to normalize categories for video %s: %r",
                    vid_id,
                    cats_val,
                    exc_info=True,
                )
        if skipped > 0:
            logger.warning(
                "0006_data_normalizations populated video_categories (inserted=%d, skipped=%d)",
                populated,
                skipped,
            )
        else:
            logger.info("0006_data_normalizations populated video_categories (inserted=%d)", populated)


migration = Migration(
    version=6,
    name="0006_data_normalizations",
    description="Normalize synced file paths, composite IDs, and seed video_categories if empty",
    apply_fn=apply,
)
