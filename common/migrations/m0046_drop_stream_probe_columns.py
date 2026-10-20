"""Drop dead stream-probe bookkeeping columns from videos."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    # Availability evidence lives in video_availability_checks and dead_streams;
    # these per-row probe counters belonged to the removed sync scan pass.
    conn.execute("ALTER TABLE videos DROP COLUMN IF EXISTS stream_fails")
    conn.execute("ALTER TABLE videos DROP COLUMN IF EXISTS stream_checked")


migration = Migration(
    version=46,
    name="0046_drop_stream_probe_columns",
    description="Drop videos.stream_fails and videos.stream_checked",
    apply_fn=apply,
)
