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
    from common.managed_sources import channel_aliases
    from sync.studio_tags import looks_like_channel_label

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
    # The video id carries the channel id the way Telegram reported it, which is
    # not always the spelling the configuration keeps (-100 prefix or not), so a
    # channel matches under both forms.
    channel_ids: list[str] = []
    labels: list[str] = []
    for channel_id, label in sorted(mapping.items()):
        for alias in sorted(channel_aliases(channel_id)):
            channel_ids.append(alias)
            labels.append(label)
    # A channel may be named exactly like the studio that publishes on it. Its
    # label is still a channel label for ``channel_name``, but it must not be
    # moved out of ``studio``: only labels that cannot name a production studio
    # are cleared below.
    studio_pairs = [(alias, label) for alias, label in zip(channel_ids, labels)
                    if looks_like_channel_label(label)]
    studio_ids = [alias for alias, _label in studio_pairs]
    studio_pair_labels = [label for _alias, label in studio_pairs]
    studio_labels = sorted(set(studio_pair_labels))
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
    cleared = 0
    if studio_pairs:
        # The channel label was never a studio: move it to the channel column
        # (already set above) and leave the studio empty for enrichment.
        with conn:
            cleared = len(conn.execute(
                "UPDATE videos v SET studio = '' "
                "FROM (SELECT unnest(%s::text[]) AS channel_id, "
                "             unnest(%s::text[]) AS label) m "
                "WHERE v.provider = 'telegram' "
                "AND coalesce(v.studio, '') = m.label "
                "AND split_part(v.id, ':', 2) = m.channel_id"
                f"{override_guard} "
                "RETURNING 1",
                (studio_ids, studio_pair_labels),
            ).fetchall())
    # A renamed or reused channel leaves labels from the other channel behind;
    # any remaining configured label is still a channel name, never a studio.
    if studio_labels:
        with conn:
            rows = conn.execute(
                "UPDATE videos v SET studio = '' "
                "WHERE v.provider = 'telegram' "
                "AND coalesce(v.studio, '') = ANY(%s)"
                f"{override_guard} "
                "RETURNING 1",
                (studio_labels,),
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
