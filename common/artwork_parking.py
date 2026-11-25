"""Videos whose artwork frame extraction has exhausted its retry budget.

A stream ffmpeg cannot decode never yields a frame, so the daily artwork batch
stops spending its slots on it (``sync.artwork_schedule``) and the admin library
shows why the poster is still missing instead of looking merely unlucky.
"""

from __future__ import annotations

ARTWORK_FRAME_FAILURE_CODE = "artwork_frame_extraction_failed"
ARTWORK_FRAME_FAILURE_LIMIT = 3


def parked_artwork_ids(conn, *, limit: int = ARTWORK_FRAME_FAILURE_LIMIT) -> set[str]:
    """Return the video ids whose artwork extraction failed ``limit`` times."""
    pattern = f'%"{ARTWORK_FRAME_FAILURE_CODE}"%'
    try:
        rows = conn.execute(
            "SELECT payload::jsonb ->> 'video_id' AS video_id FROM video_duplicate_jobs "
            "WHERE action = 'artwork_generate' AND state = 'failed' "
            "AND result LIKE %s "
            "GROUP BY 1 HAVING COUNT(*) >= %s",
            (pattern, limit),
        ).fetchall()
    except Exception:
        return set()
    return {str(row[0]) for row in rows if row and row[0]}
