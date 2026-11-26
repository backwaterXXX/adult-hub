"""Keep the channel label and the studio apart on every video row."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def channel_studio_map(config) -> dict[str, str]:
    """Return ``{channel_id: label}`` for every enabled Telegram channel with a label."""
    mapping: dict[str, str] = {}
    if config is None:
        return mapping
    try:
        sources = config.get_enabled_sources()
    except Exception:
        logger.debug("Could not read enabled sources for the channel labels", exc_info=True)
        return mapping
    for source in sources:
        if getattr(source, "provider", "") != "telegram":
            continue
        for channel in getattr(source, "channels", []) or []:
            label = str(getattr(channel, "studio", "") or "").strip()
            if label:
                mapping[str(channel.id)] = label
    return mapping


def reconcile_channel_names(conn, config) -> tuple[int, int]:
    """Fill ``videos.channel_name`` and clear channel labels that landed in ``studio``.

    ``studio`` belongs to the production company: the scene scraper or an
    operator override writes it. The channel a Telegram item came from used to be
    copied into the same column, so studio filters mixed "BelAmi" with
    "Lukayan SHOW" and the metadata audit reported every Telegram-only item as
    missing a studio it can never have. The channel label now lives in its own
    column, and only the rows whose studio exactly equals their own channel label
    are moved, so real studios and explicit overrides stay untouched.

    Returns ``(labelled, cleared)``: rows that received a channel name and rows
    whose studio label was moved out.
    """
    mapping = channel_studio_map(config)
    if not mapping:
        return 0, 0
    from common import db

    try:
        tables = db.table_names(conn)
    except Exception:
        return 0, 0
    has_channel_name = "channel_name" in db.table_columns(conn, "videos")
    if not has_channel_name:
        return 0, 0
    override_guard = ""
    if "video_metadata_overrides" in tables:
        override_guard = (
            " AND NOT EXISTS (SELECT 1 FROM video_metadata_overrides o "
            "WHERE o.video_id = v.id AND o.fields ? 'studio')"
        )

    labelled = 0
    cleared = 0
    for channel_id, label in sorted(mapping.items()):
        with conn:
            rows = conn.execute(
                "UPDATE videos v SET channel_name = %s "
                "WHERE v.provider = 'telegram' "
                "AND split_part(v.id, ':', 2) = %s "
                "AND coalesce(v.channel_name, '') = '' "
                "RETURNING 1",
                (label, channel_id),
            ).fetchall()
        labelled += len(rows)
        with conn:
            # The channel label was never a studio: move it to the channel column
            # (already set above) and leave the studio empty for enrichment.
            rows = conn.execute(
                "UPDATE videos v SET studio = '' "
                "WHERE v.provider = 'telegram' "
                "AND split_part(v.id, ':', 2) = %s "
                "AND coalesce(v.studio, '') = %s"
                f"{override_guard} "
                "RETURNING 1",
                (channel_id, label),
            ).fetchall()
        cleared += len(rows)
    return labelled, cleared
