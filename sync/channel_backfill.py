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
    column; the rows whose studio equals their own channel label, or the handle of
    a channel that left the configuration, move out, so real studios and explicit
    overrides stay untouched.

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

    # One statement for every channel: a per-channel loop rescanned the whole
    # table once per channel, which made the maintenance pass the slowest
    # statement of the service.
    channel_ids = [channel_id for channel_id, _label in sorted(mapping.items())]
    labels = [mapping[channel_id] for channel_id in channel_ids]
    with conn:
        labelled = len(conn.execute(
            "UPDATE videos v SET channel_name = m.label "
            "FROM (SELECT unnest(%s::text[]) AS channel_id, "
            "             unnest(%s::text[]) AS label) m "
            "WHERE v.provider = 'telegram' "
            "AND coalesce(v.channel_name, '') = '' "
            "AND split_part(v.id, ':', 2) = m.channel_id "
            "RETURNING 1",
            (channel_ids, labels),
        ).fetchall())
    with conn:
        # The channel label was never a studio: move it to the channel column
        # (already set above) and leave the studio empty for enrichment.
        cleared = len(conn.execute(
            "UPDATE videos v SET studio = '' "
            "FROM (SELECT unnest(%s::text[]) AS channel_id, "
            "             unnest(%s::text[]) AS label) m "
            "WHERE v.provider = 'telegram' "
            "AND coalesce(v.studio, '') = m.label "
            "AND split_part(v.id, ':', 2) = m.channel_id"
            f"{override_guard} "
            "RETURNING 1",
            (channel_ids, labels),
        ).fetchall())
    # A renamed or reused channel leaves labels from the other channel behind;
    # any remaining configured label is still a channel name, never a studio.
    all_labels = sorted(set(labels))
    with conn:
        rows = conn.execute(
            "UPDATE videos v SET studio = '' "
            "WHERE v.provider = 'telegram' "
            "AND coalesce(v.studio, '') = ANY(%s)"
            f"{override_guard} "
            "RETURNING 1",
            (all_labels,),
        ).fetchall()
    cleared += len(rows)
    cleared += _clear_channel_handles(conn, override_guard)
    return labelled, cleared


def _clear_channel_handles(conn, override_guard: str) -> int:
    """Drop studios that are really the handle of a channel left out of the config.

    Channels dropped from the configuration keep no label to compare against, so
    the only trace of the old copy is the value being the channel id itself (the
    config migration used to default the label to the id). Values that name a
    known production studio stay, so a channel named after its studio survives.
    """
    from sync.studio_tags import looks_like_channel_label

    rows = conn.execute(
        "SELECT DISTINCT coalesce(studio, '') FROM videos "
        "WHERE coalesce(studio, '') <> '' "
        "AND lower(btrim(studio)) = lower(btrim(split_part(id, ':', 2)))"
    ).fetchall()
    handles = sorted({str(row[0]).strip().casefold() for row in rows
                      if looks_like_channel_label(str(row[0]))})
    if not handles:
        return 0
    with conn:
        cleared = conn.execute(
            "UPDATE videos v SET studio = '' "
            "WHERE lower(btrim(coalesce(v.studio, ''))) = ANY(%s) "
            "AND lower(btrim(coalesce(v.studio, ''))) = lower(btrim(split_part(v.id, ':', 2)))"
            f"{override_guard} "
            "RETURNING 1",
            (handles,),
        ).fetchall()
    return len(cleared)
