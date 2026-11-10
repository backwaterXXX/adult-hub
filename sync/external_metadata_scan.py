"""Bounded backfill passes for external video metadata providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def run_external_metadata_backfill(
    conn: Any,
    media_dir: Path,
    report_progress: Callable[[int, int, str], None],
    *,
    pbc_limit: int,
    tpdb_limit: int,
    scenes_limit: int,
    logger: Any,
) -> dict:
    """Run all bounded metadata passes, continuing when an individual provider fails."""
    from sync.pbc import backfill_pbc
    from sync.theporndb import backfill_tpdb
    from sync.stashdb import backfill_stashdb
    from sync.scene_scrapers import backfill_scenes

    passes = (
        ("metadata_pbc", lambda: backfill_pbc(conn, limit=pbc_limit)),
        ("metadata_tpdb", lambda: backfill_tpdb(conn, limit=tpdb_limit)),
        # StashDB intentionally shares the existing TPDB per-run limit.
        ("metadata_stashdb", lambda: backfill_stashdb(conn, limit=tpdb_limit)),
        ("metadata_scenes", lambda: backfill_scenes(conn, media_dir, limit=scenes_limit)),
    )
    checked = enriched = failed = 0
    for index, (stage, run_pass) in enumerate(passes, start=1):
        report_progress(index - 1, len(passes), stage)
        try:
            counts = run_pass()
            if isinstance(counts, tuple) and len(counts) == 2:
                checked += max(0, int(counts[0] or 0))
                enriched += max(0, int(counts[1] or 0))
        except Exception as exc:
            failed += 1
            logger.warning("External metadata pass %d failed (%s).", index, type(exc).__name__)
        report_progress(index, len(passes), stage)
    return {
        "local_state": "metadata_scan_completed",
        "providers_completed": len(passes) - failed,
        "providers_failed": failed,
        "entities_checked": checked,
        "entities_enriched": enriched,
    }
