"""Fill an empty video studio from the channel's configured label."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def channel_studio_map(config) -> dict[str, str]:
    """Return ``{channel_id: studio}`` for every enabled Telegram channel with a label."""
    mapping: dict[str, str] = {}
    if config is None:
        return mapping
    try:
        sources = config.get_enabled_sources()
    except Exception:
        logger.debug("Could not read enabled sources for the studio backfill", exc_info=True)
        return mapping
    for source in sources:
        if getattr(source, "provider", "") != "telegram":
            continue
        for channel in getattr(source, "channels", []) or []:
            label = str(getattr(channel, "studio", "") or "").strip()
            if label:
                mapping[str(channel.id)] = label
    return mapping


def backfill_channel_studio(conn, config) -> int:
    """Set the studio on rows whose channel defines one and whose value is empty.

    New items inherit the channel label while they are collected, but rows that
    predate that rule (or that were merged from an older loser) kept an empty
    studio and showed up under no studio at all in the catalog filters. An
    explicit override, even one that clears the field, is the operator's decision
    and stays untouched.
    """
    mapping = channel_studio_map(config)
    if not mapping:
        return 0
    from common import db

    try:
        tables = db.table_names(conn)
    except Exception:
        return 0
    override_guard = ""
    if "video_metadata_overrides" in tables:
        override_guard = (
            " AND NOT EXISTS (SELECT 1 FROM video_metadata_overrides o "
            "WHERE o.video_id = v.id AND o.fields ? 'studio')"
        )
    values_sql = ", ".join(["(%s, %s)"] * len(mapping))
    params: list[object] = []
    for channel_id, label in sorted(mapping.items()):
        params.extend([channel_id, label])
    with conn:
        rows = conn.execute(
            "UPDATE videos v SET studio = m.label "
            f"FROM (VALUES {values_sql}) AS m(channel_id, label) "
            "WHERE v.provider = 'telegram' "
            "AND split_part(v.id, ':', 2) = m.channel_id "
            "AND coalesce(v.studio, '') = ''"
            f"{override_guard} "
            "RETURNING 1",
            params,
        ).fetchall()
    return len(rows)
