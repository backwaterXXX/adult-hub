"""Smallest independently safe PostgreSQL catalog query and DTO boundary for Adult Hub Client API v1.

Provides typed DTOs and read-only PostgreSQL queries for:
- GET /api/v1/catalog/videos (browse and search with filtering and deterministic pagination)
- GET /api/v1/catalog/videos/{video_id} (canonical detail with duplicate alias resolution)
- GET /api/v1/catalog/channels (Telegram source channels catalog with active video counts)
- GET /api/v1/catalog/channels/{channel_id} (channel detail and video counts)

Strictly enforces response allowlisting:
- No filesystem, sidecar (.strm, .nfo), or local disk paths
- No private upstream Telegram URLs or credentials
- No internal admin-only audit or diagnostics data
- Preserves canonical video identity, existing duplicate merge/alias resolution,
  metadata overrides, and source-aware channel membership.
- No database writes, migrations, or user state operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from common.db import Record
from common.db import table_columns as _db_table_columns
from common.db import table_names as _db_table_names
from common.managed_sources import canonical_channel_id, channel_aliases
from resolver.admin_catalog_queries import (
    _effective_value,
    _tables_and_columns,
)
from resolver.admin_catalog_serialization import _iso_time, _json_value, _row_value


def _get_existing_tables(conn: Any) -> set[str]:
    """Return table names in the current schema."""
    return set(_db_table_names(conn))


def _get_table_columns(conn: Any, table: str) -> set[str]:
    """Return column names for a table in the current schema."""
    return set(_db_table_columns(conn, table))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Transfer Objects (DTOs) strictly aligned with OpenAPI v1 specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PerformerRef:
    """Performer reference inside a VideoItem."""
    id: str
    name: str
    photo_url: str | None = None
    video_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
        }
        if self.photo_url is not None:
            data["photo_url"] = self.photo_url
        if self.video_count is not None:
            data["video_count"] = self.video_count
        return data


@dataclass(frozen=True)
class TaxonomyItem:
    """Taxonomy entry (category, studio, tag) with movie count."""
    name: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
        }


@dataclass(frozen=True)
class PaginatedTaxonomy:
    """Paginated taxonomy response matching TaxonomyListResponse schema."""
    items: list[TaxonomyItem]
    total_count: int
    offset: int = 0
    limit: int = 100
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [it.to_dict() for it in self.items],
            "total_count": self.total_count,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class PerformerDetail:
    """Performer detail response matching PerformerDetail schema."""
    id: str
    name: str
    disambiguation: str | None = None
    photo_url: str | None = None
    video_count: int = 0
    bio: str | None = None
    nationality: str | None = None
    birth: str | None = None
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "disambiguation": self.disambiguation,
            "photo_url": self.photo_url,
            "video_count": self.video_count,
            "bio": self.bio,
            "nationality": self.nationality,
            "birth": self.birth,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class PaginatedPerformers:
    """Paginated performers response matching PerformerListResponse schema."""
    items: list[PerformerRef]
    total_count: int
    offset: int = 0
    limit: int = 50
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [it.to_dict() for it in self.items],
            "total_count": self.total_count,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class VideoItem:
    """Canonical video representation for Client API v1 browse and detail."""
    id: str
    title: str
    duration_seconds: int
    release_date: str | None = None
    date_added: str | None = None
    studio: str | None = None
    plot: str | None = None
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    performers: list[PerformerRef] = field(default_factory=list)
    poster_url: str | None = None
    backdrop_url: str | None = None
    file_size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    is_hd: bool | None = None
    user_state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to allowlisted JSON-compatible dictionary."""
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "duration_seconds": self.duration_seconds,
            "release_date": self.release_date,
            "date_added": self.date_added,
            "studio": self.studio,
            "plot": self.plot,
            "categories": list(self.categories),
            "tags": list(self.tags),
            "performers": [p.to_dict() for p in self.performers],
            "poster_url": self.poster_url,
            "backdrop_url": self.backdrop_url,
            "file_size_bytes": self.file_size_bytes,
            "width": self.width,
            "height": self.height,
            "is_hd": self.is_hd,
        }
        if self.user_state is not None:
            data["user_state"] = self.user_state
        return data


@dataclass(frozen=True)
class PaginatedVideos:
    """Paginated video collection matching VideoListResponse."""
    items: list[VideoItem]
    total_count: int
    offset: int
    limit: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class ChannelItem:
    """Telegram source channel item matching ChannelItem schema."""
    id: str
    title: str
    video_count: int
    description: str | None = None
    icon_url: str | None = None
    is_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "video_count": self.video_count,
            "is_enabled": self.is_enabled,
        }
        if self.description is not None:
            data["description"] = self.description
        if self.icon_url is not None:
            data["icon_url"] = self.icon_url
        return data


@dataclass(frozen=True)
class PaginatedChannels:
    """Paginated channels response matching PaginatedChannels schema."""
    items: list[ChannelItem]
    total_count: int
    offset: int
    limit: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
        }


# ---------------------------------------------------------------------------
# Allowlisted serialization and projection helpers
# ---------------------------------------------------------------------------

_FIXED_PROJECTION_INDICES: dict[str, int] = {
    "id": 0,
    "title": 1,
    "duration": 2,
    "date_released": 3,
    "first_seen": 4,
    "studio": 5,
    "plot": 6,
    "categories": 7,
    "tags": 8,
    "performers": 9,
    "file_size_bytes": 10,
    "width": 11,
    "height": 12,
}


def _row_val(row: Any, key: str, fallback: Any = None) -> Any:
    val = _row_value(row, key, fallback=None)
    if val is not None:
        return val
    if type(row) is tuple and key in _FIXED_PROJECTION_INDICES:
        idx = _FIXED_PROJECTION_INDICES[key]
        if idx < len(row) and row[idx] is not None:
            return row[idx]
    return fallback


def _determine_is_hd(height: int | None, width: int | None) -> bool | None:
    """Determine HD status solely from actual selected dimensions, returning None (unknown) if absent."""
    if height is not None and height > 0:
        return height >= 720
    if width is not None and width > 0:
        return width >= 1280
    return None


def _resolve_vetted_performer(
    catalog: Mapping[str, tuple[str, str] | str],
    raw_id: str,
    raw_name: str,
) -> tuple[str, str] | None:
    """Look up a performer in the vetted catalog map by raw ID, or by name when no ID is given."""
    val = None
    if raw_id and raw_id in catalog:
        val = catalog[raw_id]
    elif (not raw_id) and raw_name and raw_name.lower() in catalog:
        val = catalog[raw_name.lower()]
    if val is None:
        return None
    if isinstance(val, tuple) and len(val) >= 2:
        return str(val[0]), str(val[1])
    if isinstance(val, str):
        return str(val), raw_name or str(val)
    return None


def _vetted_performer_ref(v_id: str, v_name: str, base_artwork_url: str) -> PerformerRef:
    """Build a performer reference whose artwork points at the vetted catalog performer."""
    photo = f"{base_artwork_url}/performer/{quote(v_id, safe='')}"
    return PerformerRef(id=v_id, name=v_name, photo_url=photo)


def _performer_ref_from_mapping(
    item: dict,
    catalog: Mapping[str, tuple[str, str] | str],
    base_artwork_url: str,
) -> PerformerRef | None:
    """Format one performer object ({id, name}) as a reference, or None when it has neither."""
    raw_id = str(item.get("id") or "").strip()
    raw_name = str(item.get("name") or "").strip()
    vetted = _resolve_vetted_performer(catalog, raw_id, raw_name)
    if vetted:
        v_id, v_name = vetted
        return _vetted_performer_ref(v_id, v_name, base_artwork_url)
    if raw_id:
        # Unresolved raw_id: preserve distinct ID
        return PerformerRef(id=raw_id, name=raw_name or raw_id, photo_url=None)
    if raw_name:
        return PerformerRef(id=f"name:{raw_name}", name=raw_name, photo_url=None)
    # If no raw_name and no raw_id, ignore item
    return None


def _performer_ref_from_name(
    item: str,
    catalog: Mapping[str, tuple[str, str] | str],
    base_artwork_url: str,
) -> PerformerRef | None:
    """Format one bare performer name as a reference, or None when the name is blank."""
    clean_name = item.strip()
    if not clean_name:
        return None
    vetted = _resolve_vetted_performer(catalog, "", clean_name)
    if vetted:
        v_id, v_name = vetted
        return _vetted_performer_ref(v_id, v_name, base_artwork_url)
    return PerformerRef(id=f"name:{clean_name}", name=clean_name, photo_url=None)


def _format_performer_refs(
    raw_performers: Any,
    performers_catalog_map: Mapping[str, tuple[str, str] | str] | None = None,
    base_artwork_url: str = "/api/v1/artwork",
) -> list[PerformerRef]:
    """Format performer references, trusting only vetted catalog IDs and public display names.

    Never leaks stored or upstream URLs and only sets artwork for vetted catalog identifiers.
    Unvetted IDs are discarded; unmapped performers fall back to name:<name> with photo_url=None.
    """
    items = _json_value(raw_performers, [])
    if not isinstance(items, list):
        return []

    refs: list[PerformerRef] = []
    catalog = performers_catalog_map or {}

    for item in items:
        if isinstance(item, dict):
            ref = _performer_ref_from_mapping(item, catalog, base_artwork_url)
        elif isinstance(item, str):
            ref = _performer_ref_from_name(item, catalog, base_artwork_url)
        else:
            continue
        if ref is not None:
            refs.append(ref)

    return refs


def serialize_client_video(
    row: Any,
    *,
    performers_catalog_map: Mapping[str, tuple[str, str] | str] | None = None,
    base_artwork_url: str = "/api/v1/artwork",
    user_state: dict[str, Any] | None = None,
) -> VideoItem:
    """Serialize a PostgreSQL row into a strictly allowlisted VideoItem DTO."""
    video_id = str(_row_val(row, "id") or "")
    title = str(_row_val(row, "title") or "")
    duration = _row_val(row, "duration")
    duration_seconds = int(duration) if duration is not None else 0

    categories = _json_value(_row_val(row, "categories"), [])
    if not isinstance(categories, list):
        categories = []
    categories = [str(c) for c in categories if c]

    tags = _json_value(_row_val(row, "tags"), [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags if t]

    performers = _format_performer_refs(
        _row_val(row, "performers"),
        performers_catalog_map=performers_catalog_map,
        base_artwork_url=base_artwork_url,
    )

    width = _row_val(row, "width")
    height = _row_val(row, "height")
    width_int = int(width) if width is not None and int(width) > 0 else None
    height_int = int(height) if height is not None and int(height) > 0 else None
    is_hd = _determine_is_hd(height_int, width_int)

    file_size = _row_val(row, "file_size_bytes")
    file_size_bytes = int(file_size) if file_size is not None and file_size >= 0 else None

    poster_url = f"{base_artwork_url}/video/{quote(video_id, safe='')}/poster" if video_id else None
    backdrop_url = f"{base_artwork_url}/video/{quote(video_id, safe='')}/backdrop" if video_id else None

    release_date = _row_val(row, "date_released")
    if release_date is not None:
        release_date = str(release_date)

    date_added = _iso_time(_row_val(row, "first_seen"))
    studio = _row_val(row, "studio")
    if studio is not None:
        studio = str(studio)
    plot = _row_val(row, "plot")
    if plot is not None:
        plot = str(plot)

    return VideoItem(
        id=video_id,
        title=title,
        duration_seconds=duration_seconds,
        release_date=release_date,
        date_added=date_added,
        studio=studio,
        plot=plot,
        categories=categories,
        tags=tags,
        performers=performers,
        poster_url=poster_url,
        backdrop_url=backdrop_url,
        file_size_bytes=file_size_bytes,
        width=width_int,
        height=height_int,
        is_hd=is_hd,
        user_state=user_state,
    )


def _lookup_favorited_canonical_ids(
    conn: Any,
    clean_u_id: str,
    clean_v_ids: list[str],
    has_sources: bool,
    has_decisions: bool,
) -> set[str]:
    """Return the requested canonical video IDs the user has favorited, directly or through aliases."""
    union_parts = [
        "SELECT cuf.video_id AS target_id FROM client_user_favorites cuf WHERE cuf.user_id = %s"
    ]
    params: list[Any] = [clean_u_id]
    if has_sources:
        union_parts.append(
            "SELECT vs.video_id AS target_id FROM client_user_favorites cuf "
            "JOIN video_sources vs ON vs.source_id = cuf.video_id AND vs.is_active = 1 "
            "WHERE cuf.user_id = %s"
        )
        params.append(clean_u_id)
    if has_decisions:
        union_parts.append(
            "SELECT vdd.keeper_id AS target_id FROM client_user_favorites cuf "
            "JOIN video_duplicate_decisions vdd ON vdd.loser_id = cuf.video_id AND vdd.state = 'active' "
            "WHERE cuf.user_id = %s"
        )
        params.append(clean_u_id)

    fav_sql = (
        f"SELECT DISTINCT target_id FROM ({' UNION ALL '.join(union_parts)}) v_target "
        f"WHERE target_id = ANY(%s)"
    )
    params.append(clean_v_ids)
    rows = conn.execute(fav_sql, params).fetchall()
    favorited: set[str] = set()
    for r in rows:
        val = r[0] if isinstance(r, (list, tuple)) else r["target_id"]
        if val:
            favorited.add(str(val))
    return favorited


def _lookup_progress_map(
    conn: Any,
    clean_u_id: str,
    clean_v_ids: list[str],
    has_sources: bool,
    has_decisions: bool,
) -> dict[str, dict[str, Any]]:
    """Return the owner's latest watch progress keyed by requested canonical video ID."""
    wp_union_parts = [
        "SELECT wp.video_id AS wp_vid, wp.video_id AS target_id FROM client_user_watch_progress wp WHERE wp.user_id = %s"
    ]
    wp_params: list[Any] = [clean_u_id]
    if has_sources:
        wp_union_parts.append(
            "SELECT wp.video_id AS wp_vid, vs.video_id AS target_id FROM client_user_watch_progress wp "
            "JOIN video_sources vs ON vs.source_id = wp.video_id AND vs.is_active = 1 WHERE wp.user_id = %s"
        )
        wp_params.append(clean_u_id)
    if has_decisions:
        wp_union_parts.append(
            "SELECT wp.video_id AS wp_vid, vdd.keeper_id AS target_id FROM client_user_watch_progress wp "
            "JOIN video_duplicate_decisions vdd ON vdd.loser_id = wp.video_id AND vdd.state = 'active' WHERE wp.user_id = %s"
        )
        wp_params.append(clean_u_id)

    wp_sql = f"""
            SELECT DISTINCT ON (v_target.target_id)
                v_target.target_id,
                wp.position_seconds,
                wp.duration_seconds,
                wp.played_percentage,
                wp.is_played
            FROM ({' UNION ALL '.join(wp_union_parts)}) v_target
            JOIN client_user_watch_progress wp ON wp.user_id = %s AND wp.video_id = v_target.wp_vid
            WHERE v_target.target_id = ANY(%s)
            ORDER BY v_target.target_id, wp.last_watched_at DESC, wp.played_percentage DESC
        """
    all_wp_params = [*wp_params, clean_u_id, clean_v_ids]
    wp_rows = conn.execute(wp_sql, all_wp_params).fetchall()
    progress_map: dict[str, dict[str, Any]] = {}
    for r in wp_rows:
        tid = str(r[0] if isinstance(r, (list, tuple)) else r["target_id"])
        pos = int(r[1] if isinstance(r, (list, tuple)) else r["position_seconds"])
        dur = int(r[2] if isinstance(r, (list, tuple)) else r["duration_seconds"])
        pct = float(r[3] if isinstance(r, (list, tuple)) else r["played_percentage"])
        is_p = bool(r[4] if isinstance(r, (list, tuple)) else r["is_played"])
        progress_map[tid] = {
            "position_seconds": pos,
            "duration_seconds": dur,
            "played_percentage": pct,
            "is_played": is_p,
        }
    return progress_map


def _user_state_results(
    clean_v_ids: list[str],
    favorited_canonical_ids: set[str],
    progress_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Combine favorite flags and watch progress into one state entry per requested video ID."""
    results: dict[str, dict[str, Any]] = {}
    for vid in clean_v_ids:
        is_fav = vid in favorited_canonical_ids
        if vid in progress_map:
            p = progress_map[vid]
            results[vid] = {
                "is_favorite": is_fav,
                "position_seconds": p["position_seconds"],
                "played_percentage": p["played_percentage"],
                "is_played": p["is_played"],
            }
        else:
            results[vid] = {
                "is_favorite": is_fav,
                "position_seconds": 0,
                "played_percentage": 0.0,
                "is_played": False,
            }
    return results


def _lookup_user_states(
    conn: Any,
    tables: set[str],
    user_id: str,
    video_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Batch-lookup owner-scoped user state (is_favorite, watch progress) for video IDs without N+1 queries.

    Resolves canonical IDs and aliases via video_sources and active video_duplicate_decisions.
    Guarantees no internal fields or secrets leak into returned state.
    """
    if not user_id or not video_ids:
        return {}

    clean_u_id = str(user_id).strip()
    clean_v_ids = [str(v).strip() for v in video_ids if str(v).strip()]
    if not clean_v_ids:
        return {}

    has_favorites = "client_user_favorites" in tables
    has_progress = "client_user_watch_progress" in tables
    has_sources = "video_sources" in tables
    has_decisions = "video_duplicate_decisions" in tables

    favorited_canonical_ids: set[str] = set()
    if has_favorites:
        favorited_canonical_ids = _lookup_favorited_canonical_ids(
            conn, clean_u_id, clean_v_ids, has_sources, has_decisions,
        )
    progress_map: dict[str, dict[str, Any]] = {}
    if has_progress:
        progress_map = _lookup_progress_map(
            conn, clean_u_id, clean_v_ids, has_sources, has_decisions,
        )
    return _user_state_results(clean_v_ids, favorited_canonical_ids, progress_map)


# ---------------------------------------------------------------------------
# LIKE pattern escape helper
# ---------------------------------------------------------------------------

def _escape_like_pattern(text: str) -> str:
    """Escape special LIKE wildcards % and _ and backslash."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_channel_like_pattern(cid: str) -> str:
    """Create a LIKE pattern for a Telegram channel prefix."""
    escaped = _escape_like_pattern(cid)
    return f"telegram:{escaped}:%"


def _get_configured_channel_metadata(
    config_sources: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve registered channel metadata from config_sources or active configuration."""
    if config_sources is not None:
        return list(config_sources)

    from pathlib import Path
    from common.config_loader import find_config_file
    from common.config import DEFAULT_CONFIG_PATH
    from resolver.admin_data import configured_sources

    cfg_path = find_config_file(optional=True) or Path(DEFAULT_CONFIG_PATH)
    if not Path(cfg_path).is_file():
        return []

    try:
        data = configured_sources(str(cfg_path))
    except Exception as exc:
        logger.warning("Failed to load configured sources from %s: %s", cfg_path, exc)
        return []

    channels = []
    for src in data.get("sources", []):
        if src.get("provider") == "telegram":
            src_enabled = bool(src.get("enabled", True))
            for ch in src.get("channels", []):
                ch_copy = dict(ch)
                if not src_enabled:
                    ch_copy["enabled"] = False
                channels.append(ch_copy)
    return channels


def _extract_disabled_channel_ids(
    configured: Sequence[Mapping[str, Any]] | None = None,
    explicit_disabled: set[str] | Sequence[str] | None = None,
) -> set[str]:
    """Collect all normalized channel IDs and aliases that are marked disabled.

    Never fails open: if configured is omitted, reads active configuration so that
    configured-disabled channels are always enforced.
    """
    disabled: set[str] = set()
    if explicit_disabled:
        for item in explicit_disabled:
            if item:
                s = str(item).strip()
                disabled.add(s)
                disabled.add(canonical_channel_id(s))
                disabled.update(channel_aliases(s))
    conf_list = configured if configured is not None else _get_configured_channel_metadata()
    for ch in conf_list:
        if ch.get("enabled") is False and ch.get("id"):
            s = str(ch.get("id")).strip()
            disabled.add(s)
            disabled.add(canonical_channel_id(s))
            disabled.update(channel_aliases(s))
    return disabled


# ---------------------------------------------------------------------------
# Shared Visibility Logic
# ---------------------------------------------------------------------------

def _build_visibility_conditions(
    tables: set[str],
    columns: set[str],
    *,
    disabled_channel_ids: set[str] | None = None,
    table_alias: str = "v",
) -> tuple[list[str], list[Any]]:
    """Build shared, consistent visibility conditions across browse, detail, and counts.

    Guarantees:
    - Exclude retired duplicate losers
    - Exclude unplayable items (playable == 0)
    - Exclude dead streams
    - Check valid duration (duration IS NULL OR duration > 0)
    - Filter disabled channels: video is excluded if primary channel is disabled
      AND it has no active alternative Telegram source from an enabled channel.
      Note: secondary source membership is strictly verified through vs.provider = 'telegram'.
      Heuristic copying of duplicate loser channel membership without a verified source
      is prohibited; full canonical source reconciliation is a Stage 4.5 dependency.
    """
    conditions: list[str] = []
    params: list[Any] = []
    has_decisions = "video_duplicate_decisions" in tables
    has_dead = "dead_streams" in tables
    has_sources = "video_sources" in tables

    # 1. Canonical decisions (exclude active losers)
    if has_decisions:
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM video_duplicate_decisions d WHERE d.loser_id = {table_alias}.id AND d.state = 'active')"
        )

    # 2. Playable status
    if "playable" in columns:
        conditions.append(f"COALESCE({table_alias}.playable, 1) != 0")

    # 3. Dead streams
    if has_dead:
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM dead_streams ds WHERE ds.video_id = {table_alias}.id)"
        )

    # 4. Duration
    if "duration" in columns:
        conditions.append(f"({table_alias}.duration IS NULL OR {table_alias}.duration > 0)")

    # 5. Disabled channels with active alternate source support
    if disabled_channel_ids:
        all_disabled_aliases: set[str] = set()
        for dch in disabled_channel_ids:
            all_disabled_aliases.update(channel_aliases(dch))
        if all_disabled_aliases:
            disabled_patterns = [_escape_channel_like_pattern(a) for a in all_disabled_aliases]
            if has_sources:
                conditions.append(
                    f"""NOT (
                        {table_alias}.id LIKE ANY(%s)
                        AND NOT EXISTS (
                            SELECT 1 FROM video_sources vs
                            WHERE vs.video_id = {table_alias}.id
                              AND vs.provider = 'telegram'
                              AND vs.is_active = 1
                              AND vs.channel_id NOT IN (SELECT unnest(%s::text[]))
                        )
                    )"""
                )
                params.extend([disabled_patterns, list(all_disabled_aliases)])
            else:
                conditions.append(f"{table_alias}.id NOT LIKE ANY(%s)")
                params.append(disabled_patterns)

    return conditions, params


# ---------------------------------------------------------------------------
# Performer catalog lookup helper
# ---------------------------------------------------------------------------

def _lookup_performers_catalog(
    conn: Any,
    tables: set[str],
    *,
    names: Sequence[str] = (),
    raw_ids: Sequence[str] = (),
) -> dict[str, tuple[str, str]]:
    """Resolve performer names and raw IDs to (vetted_id, canonical_name) from performer_catalog."""
    if "performer_catalog" not in tables:
        return {}
    clean_names = [n.strip() for n in names if n.strip()]
    clean_ids = [i.strip() for i in raw_ids if i.strip()]
    if not clean_names and not clean_ids:
        return {}

    result: dict[str, tuple[str, str]] = {}
    conds: list[str] = []
    params: list[Any] = []
    if clean_ids:
        conds.append("id = ANY(%s)")
        params.append(clean_ids)
    if clean_names:
        conds.append("lower(name) = ANY(%s)")
        params.append([n.lower() for n in clean_names])

    if conds:
        where_sql = "WHERE active = true AND (" + " OR ".join(conds) + ")"
        rows = conn.execute(
            f"SELECT id, name FROM performer_catalog {where_sql}",
            params,
        ).fetchall()
        for r in rows:
            p_id = str(r[0] if isinstance(r, (list, tuple)) else r["id"]).strip()
            p_name = str(r[1] if isinstance(r, (list, tuple)) else r["name"]).strip()
            result[p_id] = (p_id, p_name)
            result[p_name.lower()] = (p_id, p_name)

    unresolved_names = [n for n in clean_names if n.lower() not in result]
    if unresolved_names and "performer_catalog_aliases" in tables:
        alias_rows = conn.execute(
            """
            SELECT a.performer_id, c.name, a.alias
            FROM performer_catalog_aliases a
            JOIN performer_catalog c ON c.id = a.performer_id
            WHERE c.active = true AND a.approved = true AND lower(a.alias) = ANY(%s)
            """,
            ([n.lower() for n in unresolved_names],),
        ).fetchall()
        for ar in alias_rows:
            p_id = str(ar[0] if isinstance(ar, (list, tuple)) else ar["performer_id"]).strip()
            c_name = str(ar[1] if isinstance(ar, (list, tuple)) else ar["name"]).strip()
            alias = str(ar[2] if isinstance(ar, (list, tuple)) else ar["alias"]).strip()
            result[alias.lower()] = (p_id, c_name)
            result[p_id] = (p_id, c_name)

    return result


# ---------------------------------------------------------------------------
# Core Video Queries: Browse & Detail
# ---------------------------------------------------------------------------

# Omit unseeded "random" sort from independently supported paginated paths
_CLIENT_SORT_CLAUSES = {
    "latest": "v.first_seen DESC NULLS LAST, v.id ASC",
    "title": "lower(COALESCE({effective_title}, '')) ASC, v.id ASC",
    "duration": "v.duration DESC NULLS LAST, v.id ASC",
}


def client_sort_clause(sort: str, direction: str | None = None, *, effective_title: str) -> str:
    """Build deterministic SQL ORDER BY clause with explicit or default ASC/DESC direction.

    Canonical tie-breaker v.id ASC is always appended.
    """
    chosen_sort = sort if sort in {"latest", "title", "duration"} else "latest"
    if direction is not None:
        clean_dir = direction.lower().strip()
        if clean_dir not in {"asc", "desc"}:
            clean_dir = "desc" if chosen_sort in {"latest", "duration"} else "asc"
    else:
        clean_dir = "desc" if chosen_sort in {"latest", "duration"} else "asc"

    dir_sql = "ASC" if clean_dir == "asc" else "DESC"
    if chosen_sort == "title":
        return f"lower(COALESCE({effective_title}, '')) {dir_sql}, v.id ASC"
    elif chosen_sort == "duration":
        return f"v.duration {dir_sql} NULLS LAST, v.id ASC"
    else:  # latest
        return f"v.first_seen {dir_sql} NULLS LAST, v.id ASC"


def _append_channel_filter(
    conditions: list[str],
    params: list[Any],
    channel_id: str,
    disabled_set: set[str],
    has_sources: bool,
) -> None:
    """Append the explicit Telegram channel filter, honouring disabled channels and aliases."""
    canon_ch = canonical_channel_id(channel_id)
    disabled_canons = {canonical_channel_id(d) for d in disabled_set}
    if canon_ch in disabled_canons or channel_id in disabled_set:
        conditions.append("FALSE")
    else:
        aliases = list(channel_aliases(channel_id))
        patterns = [_escape_channel_like_pattern(a) for a in aliases]
        if has_sources:
            conditions.append(
                """(
                        (v.id LIKE ANY(%s))
                        OR EXISTS (
                            SELECT 1 FROM video_sources vs
                            WHERE vs.video_id = v.id
                              AND vs.provider = 'telegram'
                              AND vs.channel_id = ANY(%s)
                              AND vs.is_active = 1
                        )
                    )"""
            )
            params.extend([patterns, aliases])
        else:
            conditions.append("v.id LIKE ANY(%s)")
            params.append(patterns)


def _append_search_filter(
    conditions: list[str],
    params: list[Any],
    q: str | None,
    columns: set[str],
    has_overrides: bool,
) -> None:
    """Append a literal, case-insensitive search over public text fields only."""
    # Search public text only: title, plot, studio (and v.id)
    # Strictly never search raw performer or tag JSON columns to prevent exposing non-public fields.
    if q and str(q).strip():
        search_term = str(q).strip()
        escaped_q = _escape_like_pattern(search_term)
        search_val = f"%{escaped_q}%"
        search_parts = ["COALESCE(v.id, '') ILIKE %s ESCAPE '\\'"]
        search_params = [search_val]
        for field_name in ("title", "plot", "studio"):
            if field_name in columns:
                search_parts.append(
                    f"COALESCE({_effective_value(field_name, columns, has_overrides)}::text, '') ILIKE %s ESCAPE '\\'"
                )
                search_params.append(search_val)
        conditions.append("(" + " OR ".join(search_parts) + ")")
        params.extend(search_params)


def _append_exact_filters(
    conditions: list[str],
    params: list[Any],
    category: str | None,
    tag: str | None,
    studio: str | None,
    columns: set[str],
    has_overrides: bool,
) -> None:
    """Append the exact-match category, tag and studio filters."""
    if category and str(category).strip():
        cat_val = str(category).strip()
        conditions.append("v.categories ? %s")
        params.append(cat_val)

    if tag and str(tag).strip():
        tag_val = str(tag).strip()
        tag_expr = _effective_value("tags", columns, has_overrides)
        conditions.append(f"{tag_expr} ? %s")
        params.append(tag_val)

    if studio and str(studio).strip():
        studio_val = str(studio).strip()
        studio_expr = _effective_value("studio", columns, has_overrides)
        # Exact studio matching (case-insensitive) without substring wildcards
        conditions.append(f"lower(COALESCE({studio_expr}::text, '')) = lower(%s)")
        params.append(studio_val)


def _performer_id_matches(conn: Any, tables: set[str], clean_pid: str) -> tuple[list[str], list[str]]:
    """Return the catalog IDs and display names that an ID-only performer filter should match."""
    matching_ids: list[str] = [clean_pid]
    matching_names: list[str] = []
    if "performer_catalog" in tables:
        p_rows = conn.execute(
            "SELECT name FROM performer_catalog WHERE id = %s AND active = true",
            (clean_pid,),
        ).fetchall()
        for pr in p_rows:
            matching_names.append(str(pr[0] if isinstance(pr, (list, tuple)) else pr["name"]))
    if "performer_catalog_aliases" in tables:
        a_rows = conn.execute(
            "SELECT c.id, c.name FROM performer_catalog_aliases a JOIN performer_catalog c ON c.id = a.performer_id WHERE a.performer_id = %s AND a.approved = true AND c.active = true",
            (clean_pid,),
        ).fetchall()
        for ar in a_rows:
            c_id = str(ar[0] if isinstance(ar, (list, tuple)) else ar["id"])
            if c_id not in matching_ids:
                matching_ids.append(c_id)
            matching_names.append(str(ar[1] if isinstance(ar, (list, tuple)) else ar["name"]))
    return matching_ids, matching_names


def _append_performer_filter(
    conditions: list[str],
    params: list[Any],
    conn: Any,
    tables: set[str],
    columns: set[str],
    has_overrides: bool,
    performer_id: str | None,
) -> None:
    """Append the performer filter for a 'name:' display name or a catalog/raw performer ID."""
    if performer_id and str(performer_id).strip():
        clean_pid = str(performer_id).strip()
        perf_expr = _effective_value("performers", columns, has_overrides)
        if clean_pid.startswith("name:"):
            target_name = clean_pid[5:].strip()
            # Explicit name filter: match string or ID-less object by name
            conditions.append(
                f"""EXISTS (
                    SELECT 1 FROM jsonb_array_elements(
                        CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                    ) elem
                    WHERE (
                        (jsonb_typeof(elem) = 'string' AND lower(elem #>> '{{}}') = lower(%s))
                        OR (jsonb_typeof(elem) = 'object' AND (elem ->> 'id' IS NULL OR elem ->> 'id' = '') AND lower(elem ->> 'name') = lower(%s))
                    )
                )"""
            )
            params.extend([target_name, target_name])
        else:
            # ID-only lookup: find vetted catalog matches by ID (or vetted alias by ID)
            matching_ids, matching_names = _performer_id_matches(conn, tables, clean_pid)

            if matching_names:
                id_placeholders = ", ".join(["%s"] * len(matching_ids))
                name_placeholders = ", ".join(["%s"] * len(matching_names))
                conditions.append(
                    f"""EXISTS (
                        SELECT 1 FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                        ) elem
                        WHERE (
                            (jsonb_typeof(elem) = 'object' AND elem ->> 'id' IN ({id_placeholders}))
                            OR ((jsonb_typeof(elem) = 'string' OR elem ->> 'id' IS NULL OR elem ->> 'id' = '') AND lower(COALESCE(elem ->> 'name', elem #>> '{{}}')) IN ({name_placeholders}))
                        )
                    )"""
                )
                params.extend(matching_ids)
                params.extend([n.lower() for n in matching_names])
            else:
                # Unresolved raw ID: ONLY match elem ->> 'id' = %s, never match by name!
                conditions.append(
                    f"""EXISTS (
                        SELECT 1 FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                        ) elem
                        WHERE jsonb_typeof(elem) = 'object' AND elem ->> 'id' = %s
                    )"""
                )
                params.append(clean_pid)


def _append_favorite_filter(
    conditions: list[str],
    params: list[Any],
    tables: set[str],
    has_sources: bool,
    user_id: str | None,
    is_favorite: bool | None,
) -> None:
    """Append the favorite or not-favorite filter scoped to the signed-in user."""
    if is_favorite is not None:
        if not user_id or "client_user_favorites" not in tables:
            if is_favorite:
                conditions.append("FALSE")
        else:
            fav_parts = ["cuf.video_id = v.id"]
            if has_sources:
                fav_parts.append(
                    "EXISTS (SELECT 1 FROM video_sources vs WHERE vs.video_id = v.id AND vs.source_id = cuf.video_id AND vs.is_active = 1)"
                )
            if "video_duplicate_decisions" in tables:
                fav_parts.append(
                    "EXISTS (SELECT 1 FROM video_duplicate_decisions vdd WHERE vdd.keeper_id = v.id AND vdd.loser_id = cuf.video_id AND vdd.state = 'active')"
                )
            fav_condition = f"EXISTS (SELECT 1 FROM client_user_favorites cuf WHERE cuf.user_id = %s AND ({' OR '.join(fav_parts)}))"
            if is_favorite:
                conditions.append(fav_condition)
                params.append(str(user_id).strip())
            else:
                conditions.append(f"NOT {fav_condition}")
                params.append(str(user_id).strip())


def _video_projection_sql(columns: set[str], has_overrides: bool) -> str:
    """Return the SELECT list for canonical catalog video rows."""
    select_fields = [
        "v.id",
        f"{_effective_value('title', columns, has_overrides)} AS title",
        "v.duration",
        f"{_effective_value('date_released', columns, has_overrides)} AS date_released",
        "v.first_seen",
        f"{_effective_value('studio', columns, has_overrides)} AS studio",
        f"{_effective_value('plot', columns, has_overrides)} AS plot",
        "v.categories",
        f"{_effective_value('tags', columns, has_overrides)} AS tags",
        f"{_effective_value('performers', columns, has_overrides)} AS performers",
        "v.file_size_bytes",
    ]
    if "width" in columns:
        select_fields.append("v.width")
    if "height" in columns:
        select_fields.append("v.height")

    return ", ".join(select_fields)


def _performer_lookup_keys(rows: Any) -> tuple[set[str], set[str]]:
    """Collect performer display names and raw IDs from a page of video rows."""
    all_perf_names: set[str] = set()
    all_perf_ids: set[str] = set()
    for row in rows:
        perfs = _json_value(_row_val(row, "performers"), [])
        if isinstance(perfs, list):
            for p in perfs:
                if isinstance(p, str) and p.strip():
                    all_perf_names.add(p.strip())
                elif isinstance(p, dict):
                    if p.get("name") and str(p["name"]).strip():
                        all_perf_names.add(str(p["name"]).strip())
                    if p.get("id") and str(p["id"]).strip():
                        all_perf_ids.add(str(p["id"]).strip())
    return all_perf_names, all_perf_ids


def query_catalog_videos(
    conn: Any,
    *,
    q: str | None = None,
    channel_id: str | None = None,
    category: str | None = None,
    performer_id: str | None = None,
    studio: str | None = None,
    tag: str | None = None,
    min_duration: int | None = None,
    sort: str = "latest",
    direction: str | None = None,
    offset: int = 0,
    limit: int = 50,
    user_id: str | None = None,
    is_favorite: bool | None = None,
    disabled_channel_ids: set[str] | None = None,
    config_sources: Sequence[dict[str, Any]] | None = None,
) -> PaginatedVideos:
    """Query paginated canonical catalog videos with exact filtering and sorting."""
    if hasattr(conn, "row_factory") and conn.row_factory is not Record:
        conn.row_factory = Record

    tables, columns = _tables_and_columns(conn)
    has_overrides = "video_metadata_overrides" in tables
    has_sources = "video_sources" in tables

    disabled_set = _extract_disabled_channel_ids(
        configured=config_sources,
        explicit_disabled=disabled_channel_ids,
    )

    # 1. Base Canonical & Visibility Guarantees (shared logic)
    conditions, params = _build_visibility_conditions(
        tables,
        columns,
        disabled_channel_ids=disabled_set if disabled_set else None,
        table_alias="v",
    )

    # 2. Source-Aware Channel Filter (explicit Telegram provider only)
    if channel_id:
        _append_channel_filter(conditions, params, channel_id, disabled_set, has_sources)

    # 3. Search Query (q) with literal LIKE wildcard escaping
    _append_search_filter(conditions, params, q, columns, has_overrides)

    # 4. Exact Filter Semantics
    _append_exact_filters(conditions, params, category, tag, studio, columns, has_overrides)
    _append_performer_filter(conditions, params, conn, tables, columns, has_overrides, performer_id)

    if min_duration is not None and int(min_duration) > 0:
        conditions.append("v.duration >= %s")
        params.append(int(min_duration))

    _append_favorite_filter(conditions, params, tables, has_sources, user_id, is_favorite)

    # WHERE and JOIN clauses
    where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    # Count Query for exact total
    count_sql = f"SELECT count(*) FROM videos v{join_overrides}{where_sql}"
    total_count = int(conn.execute(count_sql, params).fetchone()[0])

    # Sorting (omit random; default to latest)
    effective_title_expr = _effective_value("title", columns, has_overrides)
    sort_sql = client_sort_clause(sort, direction, effective_title=effective_title_expr)

    # Exact API Paging
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), 200))

    # Projection
    projection_sql = _video_projection_sql(columns, has_overrides)

    query_sql = f"""
        SELECT {projection_sql}
        FROM videos v{join_overrides}{where_sql}
        ORDER BY {sort_sql}
        LIMIT %s OFFSET %s
    """
    rows = conn.execute(query_sql, [*params, safe_limit, safe_offset]).fetchall()

    # Performer names and raw IDs batch lookup
    all_perf_names, all_perf_ids = _performer_lookup_keys(rows)
    perf_catalog_map = _lookup_performers_catalog(
        conn, tables, names=list(all_perf_names), raw_ids=list(all_perf_ids)
    )

    user_states: dict[str, dict[str, Any]] = {}
    if user_id:
        row_ids = [str(_row_val(row, "id") or "") for row in rows]
        user_states = _lookup_user_states(conn, tables, str(user_id), row_ids)

    items = [
        serialize_client_video(
            row,
            performers_catalog_map=perf_catalog_map,
            user_state=user_states.get(str(_row_val(row, "id") or "")),
        )
        for row in rows
    ]

    has_more = (safe_offset + len(items)) < total_count

    return PaginatedVideos(
        items=items,
        total_count=total_count,
        offset=safe_offset,
        limit=safe_limit,
        has_more=has_more,
    )


def _resolve_video_detail_target(conn: Any, tables: set[str], clean_id: str) -> str | None:
    """Return the canonical video ID for a requested ID, or None when the video is hidden."""
    if "video_duplicate_decisions" not in tables:
        return clean_id
    row = conn.execute(
        "SELECT keeper_id FROM video_duplicate_decisions WHERE loser_id = %s AND state = 'active'",
        (clean_id,),
    ).fetchone()
    if not row:
        return clean_id
    keeper_id = str(row[0] if isinstance(row, (list, tuple)) else row["keeper_id"])
    # If keeper_id == loser_id, item was quarantined/hidden -> unavailable
    if keeper_id == clean_id:
        return None
    return keeper_id


def _fetch_video_detail_row(
    conn: Any,
    tables: set[str],
    columns: set[str],
    target_id: str,
    disabled_channel_ids: set[str] | None,
    config_sources: Sequence[dict[str, Any]] | None,
):
    """Fetch the visible canonical video row for detail views, or None."""
    has_overrides = "video_metadata_overrides" in tables

    disabled_set = _extract_disabled_channel_ids(
        configured=config_sources,
        explicit_disabled=disabled_channel_ids,
    )
    vis_conditions, vis_params = _build_visibility_conditions(
        tables,
        columns,
        disabled_channel_ids=disabled_set,
        table_alias="v",
    )

    conditions = ["v.id = %s"] + vis_conditions
    params: list[Any] = [target_id] + vis_params

    where_sql = " WHERE " + " AND ".join(conditions)
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    select_fields = [
        "v.id",
        f"{_effective_value('title', columns, has_overrides)} AS title",
        "v.duration",
        f"{_effective_value('date_released', columns, has_overrides)} AS date_released",
        "v.first_seen",
        f"{_effective_value('studio', columns, has_overrides)} AS studio",
        f"{_effective_value('plot', columns, has_overrides)} AS plot",
        "v.categories",
        f"{_effective_value('tags', columns, has_overrides)} AS tags",
        f"{_effective_value('performers', columns, has_overrides)} AS performers",
        "v.file_size_bytes",
    ]
    if "width" in columns:
        select_fields.append("v.width")
    if "height" in columns:
        select_fields.append("v.height")

    projection_sql = ", ".join(select_fields)

    query_sql = f"SELECT {projection_sql} FROM videos v{join_overrides}{where_sql} LIMIT 1"
    return conn.execute(query_sql, params).fetchone()


def _lookup_video_performers_catalog(conn: Any, tables: set[str], row: Any) -> dict:
    """Map the performers stored on a video row to their vetted catalog entries."""
    perf_catalog_map = {}
    perfs = _json_value(_row_val(row, "performers"), [])
    if isinstance(perfs, list):
        names = []
        raw_ids = []
        for p in perfs:
            if isinstance(p, str) and p.strip():
                names.append(p.strip())
            elif isinstance(p, dict):
                if p.get("name") and str(p["name"]).strip():
                    names.append(str(p["name"]).strip())
                if p.get("id") and str(p["id"]).strip():
                    raw_ids.append(str(p["id"]).strip())
        perf_catalog_map = _lookup_performers_catalog(
            conn, tables, names=names, raw_ids=raw_ids
        )
    return perf_catalog_map


def query_catalog_video_detail(
    conn: Any,
    video_id: str,
    *,
    user_id: str | None = None,
    disabled_channel_ids: set[str] | None = None,
    config_sources: Sequence[dict[str, Any]] | None = None,
) -> VideoItem | None:
    """Query full video details by canonical ID, resolving duplicate merge aliases."""
    if not video_id or not str(video_id).strip():
        return None

    if hasattr(conn, "row_factory") and conn.row_factory is not Record:
        conn.row_factory = Record

    clean_id = str(video_id).strip()
    tables, columns = _tables_and_columns(conn)

    # 1. Alias Resolution for Merged Duplicates
    target_id = _resolve_video_detail_target(conn, tables, clean_id)
    if target_id is None:
        return None

    # 2. Query target canonical video using shared visibility conditions
    row = _fetch_video_detail_row(
        conn, tables, columns, target_id, disabled_channel_ids, config_sources,
    )
    if not row:
        return None

    # Performer catalog lookup
    perf_catalog_map = _lookup_video_performers_catalog(conn, tables, row)

    user_state = None
    if user_id:
        st_map = _lookup_user_states(conn, tables, str(user_id), [target_id])
        user_state = st_map.get(target_id)

    return serialize_client_video(
        row,
        performers_catalog_map=perf_catalog_map,
        user_state=user_state,
    )


# ---------------------------------------------------------------------------
# Core Channels Queries: Catalog & Detail
# ---------------------------------------------------------------------------

def _fetch_source_aware_channel_counts(
    conn: Any,
    tables: set[str],
    columns: set[str],
    *,
    disabled_channel_ids: set[str] | None = None,
    config_sources: Sequence[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Calculate deduplicated canonical video counts grouped by canonical channel ID."""
    if hasattr(conn, "row_factory") and conn.row_factory is not Record:
        conn.row_factory = Record

    has_sources = "video_sources" in tables

    disabled_set = _extract_disabled_channel_ids(
        configured=config_sources,
        explicit_disabled=disabled_channel_ids,
    )
    disabled_canons = {canonical_channel_id(d) for d in disabled_set}

    vis_conditions, vis_params = _build_visibility_conditions(
        tables,
        columns,
        disabled_channel_ids=disabled_set if disabled_set else None,
        table_alias="v",
    )
    where_vis = (" WHERE " + " AND ".join(vis_conditions)) if vis_conditions else ""

    source_branch = ""
    if has_sources:
        source_branch = """
        UNION
        SELECT
            vs.video_id,
            vs.channel_id
        FROM video_sources vs
        JOIN visible_videos vv ON vv.id = vs.video_id
        WHERE vs.provider = 'telegram'
          AND vs.is_active = 1
          AND vs.channel_id IS NOT NULL
          AND vs.channel_id != ''
        """

    # In psycopg, literal percent inside parameter-bound SQL must be doubled (%%)
    # when parameters are sent. For parameterless execution, single percent (%) is expected.
    has_params = bool(vis_params)
    like_pattern = "telegram:%%" if has_params else "telegram:%"

    sql = f"""
    WITH visible_videos AS (
        SELECT v.id
        FROM videos v
        {where_vis}
    ),
    primary_channel_memberships AS (
        SELECT
            vv.id AS video_id,
            split_part(split_part(vv.id, ':', 2), ':', 1) AS channel_id
        FROM visible_videos vv
        WHERE vv.id LIKE '{like_pattern}'
    ),
    all_memberships AS (
        SELECT video_id, channel_id FROM primary_channel_memberships
        WHERE channel_id IS NOT NULL AND channel_id != ''
        {source_branch}
    )
    SELECT DISTINCT video_id, channel_id
    FROM all_memberships
    """

    if has_params:
        rows = conn.execute(sql, vis_params).fetchall()
    else:
        rows = conn.execute(sql).fetchall()

    # Deduplicate canonical video and canonical channel across raw alias forms
    canon_video_pairs: set[tuple[str, str]] = set()
    for r in rows:
        vid = str(r[0] if isinstance(r, (list, tuple)) else r["video_id"]).strip()
        cid = str(r[1] if isinstance(r, (list, tuple)) else r["channel_id"]).strip()
        if vid and cid:
            canon_c = canonical_channel_id(cid)
            if disabled_canons and (canon_c in disabled_canons or cid in disabled_set):
                continue
            canon_video_pairs.add((vid, canon_c))

    canon_counts: dict[str, int] = {}
    for _, canon_cid in canon_video_pairs:
        canon_counts[canon_cid] = canon_counts.get(canon_cid, 0) + 1

    return canon_counts


def query_catalog_channels(
    conn: Any,
    *,
    config_sources: Sequence[dict[str, Any]] | None = None,
    disabled_channel_ids: set[str] | None = None,
    sort: str = "title",
    offset: int = 0,
    limit: int = 50,
    base_artwork_url: str = "/api/v1/artwork",
) -> PaginatedChannels:
    """Query paginated registered Telegram source channels with active counts."""
    if hasattr(conn, "row_factory") and conn.row_factory is not Record:
        conn.row_factory = Record

    tables, columns = _tables_and_columns(conn)
    configured = _get_configured_channel_metadata(config_sources)

    # Disabled channels from config to prevent leakage via counts
    disabled_from_config = _extract_disabled_channel_ids(
        configured=configured,
        explicit_disabled=disabled_channel_ids,
    )
    disabled_canons = {canonical_channel_id(d) for d in disabled_from_config}

    canon_counts = _fetch_source_aware_channel_counts(
        conn,
        tables,
        columns,
        disabled_channel_ids=disabled_from_config if disabled_from_config else None,
        config_sources=configured,
    )

    channel_items: list[ChannelItem] = []
    seen_canons: set[str] = set()

    # Process configured channels first (deduplicating configured aliases for stable representation)
    for ch in configured:
        ch_id = str(ch.get("id") or "").strip()
        if not ch_id:
            continue
        canon = canonical_channel_id(ch_id)
        if canon in seen_canons:
            continue
        seen_canons.add(canon)

        title = str(ch.get("studio") or ch.get("title") or ch_id)
        is_enabled = bool(ch.get("enabled", True))
        if not is_enabled or canon in disabled_canons or ch_id in disabled_from_config:
            is_enabled = False
            v_count = 0
        else:
            v_count = canon_counts.get(canon, 0)
        icon_url = f"{base_artwork_url}/channel/{quote(ch_id, safe='')}"

        channel_items.append(
            ChannelItem(
                id=ch_id,
                title=title,
                description=None,  # Do not expose unvetted configured descriptions
                video_count=v_count,
                icon_url=icon_url,
                is_enabled=is_enabled,
            )
        )

    # Include any active channels present in DB counts but omitted from config (using canonical totals)
    for canon, v_count in canon_counts.items():
        if canon not in seen_canons and v_count > 0:
            seen_canons.add(canon)
            channel_items.append(
                ChannelItem(
                    id=canon,
                    title=f"Channel {canon}",
                    description=None,
                    video_count=v_count,
                    icon_url=f"{base_artwork_url}/channel/{quote(canon, safe='')}",
                    is_enabled=True,
                )
            )

    # Sort channels
    if sort == "video_count":
        channel_items.sort(key=lambda c: (-c.video_count, c.title.lower(), c.id))
    elif sort == "latest":
        channel_items.sort(key=lambda c: (c.title.lower(), c.id))
    else:  # default "title"
        channel_items.sort(key=lambda c: (c.title.lower(), c.id))

    total_count = len(channel_items)
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), 200))
    paged_items = channel_items[safe_offset : safe_offset + safe_limit]
    has_more = (safe_offset + len(paged_items)) < total_count

    return PaginatedChannels(
        items=paged_items,
        total_count=total_count,
        offset=safe_offset,
        limit=safe_limit,
        has_more=has_more,
    )


def query_catalog_channel_detail(
    conn: Any,
    channel_id: str,
    *,
    config_sources: Sequence[dict[str, Any]] | None = None,
    disabled_channel_ids: set[str] | None = None,
    base_artwork_url: str = "/api/v1/artwork",
) -> ChannelItem | None:
    """Query details and active video count for a single Telegram source channel."""
    if not channel_id or not str(channel_id).strip():
        return None

    clean_cid = str(channel_id).strip()
    canon_cid = canonical_channel_id(clean_cid)
    aliases = list(channel_aliases(clean_cid))

    tables, columns = _tables_and_columns(conn)
    configured = _get_configured_channel_metadata(config_sources)

    disabled_from_config = _extract_disabled_channel_ids(
        configured=configured,
        explicit_disabled=disabled_channel_ids,
    )
    disabled_canons = {canonical_channel_id(d) for d in disabled_from_config}

    canon_counts = _fetch_source_aware_channel_counts(
        conn,
        tables,
        columns,
        disabled_channel_ids=disabled_from_config if disabled_from_config else None,
        config_sources=configured,
    )

    v_count = canon_counts.get(canon_cid, 0)

    target_conf = None
    for ch in configured:
        cid = str(ch.get("id") or "").strip()
        if cid in aliases or canonical_channel_id(cid) == canon_cid:
            target_conf = ch
            break

    is_disabled_channel = (
        (target_conf is not None and target_conf.get("enabled") is False)
        or canon_cid in disabled_canons
        or clean_cid in disabled_from_config
    )

    if target_conf is None and (v_count == 0 or is_disabled_channel):
        return None

    # Stable ID strictly matches list representation (configured id or canonical id)
    stable_id = str(target_conf.get("id") or canon_cid).strip() if target_conf else canon_cid

    title = str(
        (target_conf.get("studio") or target_conf.get("title") if target_conf else None)
        or f"Channel {canon_cid}"
    )
    is_enabled = False if is_disabled_channel else (bool(target_conf.get("enabled", True)) if target_conf else True)
    if not is_enabled:
        v_count = 0
    icon_url = f"{base_artwork_url}/channel/{quote(stable_id, safe='')}"

    return ChannelItem(
        id=stable_id,
        title=title,
        description=None,  # Do not expose unvetted configured descriptions
        video_count=v_count,
        icon_url=icon_url,
        is_enabled=is_enabled,
    )


# ---------------------------------------------------------------------------
# Taxonomy Queries: Categories, Studios, Tags, Performers
# ---------------------------------------------------------------------------

def query_catalog_categories(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List video categories with distinct visible movie counts and deterministic ordering."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _get_existing_tables(conn)
    columns = _get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _extract_disabled_channel_ids()
    conditions, params = _build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

    cat_expr = _effective_value("categories", columns, has_overrides)

    and_conds = []
    and_params = []
    if q and q.strip():
        and_conds.append("trim(cat.name) ILIKE %s ESCAPE '\\'")
        and_params.append(f"%{_escape_like_pattern(q.strip())}%")

    and_sql = (" AND " + " AND ".join(and_conds)) if and_conds else ""

    count_sql = f"""
        SELECT COUNT(DISTINCT trim(cat.name))
        FROM (
            SELECT jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({cat_expr}) = 'array' THEN {cat_expr} ELSE '[]'::jsonb END
            ) AS name
            FROM videos v{join_overrides}
            {where_sql}
        ) cat
        WHERE trim(cat.name) <> '' {and_sql}
    """
    total_row = conn.execute(count_sql, params + and_params).fetchone()
    total_count = int(total_row[0] if total_row else 0)

    items_sql = f"""
        SELECT
            trim(cat.name) AS name,
            COUNT(DISTINCT cat.id) AS count
        FROM (
            SELECT
                v.id,
                jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof({cat_expr}) = 'array' THEN {cat_expr} ELSE '[]'::jsonb END
                ) AS name
            FROM videos v{join_overrides}
            {where_sql}
        ) cat
        WHERE trim(cat.name) <> '' {and_sql}
        GROUP BY trim(cat.name)
        ORDER BY count DESC, name ASC
        LIMIT %s OFFSET %s
    """
    query_params = params + and_params + [limit, offset]
    rows = conn.execute(items_sql, query_params).fetchall()

    items = [
        TaxonomyItem(
            name=str(r[0] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            count=int(r[1] if isinstance(r, (list, tuple)) else r["count"]),
        )
        for r in rows
    ]

    return PaginatedTaxonomy(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


def query_catalog_studios(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List studios with distinct visible movie counts and deterministic ordering."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _get_existing_tables(conn)
    columns = _get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _extract_disabled_channel_ids()
    conditions, params = _build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    studio_expr = _effective_value("studio", columns, has_overrides)

    conditions.append(f"{studio_expr} IS NOT NULL AND trim({studio_expr}::text) <> ''")

    if q and q.strip():
        conditions.append(f"{studio_expr}::text ILIKE %s ESCAPE '\\'")
        params.append(f"%{_escape_like_pattern(q.strip())}%")

    where_sql = "WHERE " + " AND ".join(conditions)

    count_sql = f"""
        SELECT COUNT(DISTINCT trim({studio_expr}::text))
        FROM videos v{join_overrides}
        {where_sql}
    """
    total_row = conn.execute(count_sql, params).fetchone()
    total_count = int(total_row[0] if total_row else 0)

    items_sql = f"""
        SELECT
            trim({studio_expr}::text) AS name,
            COUNT(DISTINCT v.id) AS count
        FROM videos v{join_overrides}
        {where_sql}
        GROUP BY trim({studio_expr}::text)
        ORDER BY count DESC, name ASC
        LIMIT %s OFFSET %s
    """
    query_params = params + [limit, offset]
    rows = conn.execute(items_sql, query_params).fetchall()

    items = [
        TaxonomyItem(
            name=str(r[0] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            count=int(r[1] if isinstance(r, (list, tuple)) else r["count"]),
        )
        for r in rows
    ]

    return PaginatedTaxonomy(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


def query_catalog_tags(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List tags with distinct visible movie counts and deterministic ordering."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _get_existing_tables(conn)
    columns = _get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _extract_disabled_channel_ids()
    conditions, params = _build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

    tags_expr = _effective_value("tags", columns, has_overrides)

    and_conds = []
    and_params = []
    if q and q.strip():
        and_conds.append("trim(tag.name) ILIKE %s ESCAPE '\\'")
        and_params.append(f"%{_escape_like_pattern(q.strip())}%")

    and_sql = (" AND " + " AND ".join(and_conds)) if and_conds else ""

    count_sql = f"""
        SELECT COUNT(DISTINCT trim(tag.name))
        FROM (
            SELECT jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({tags_expr}) = 'array' THEN {tags_expr} ELSE '[]'::jsonb END
            ) AS name
            FROM videos v{join_overrides}
            {where_sql}
        ) tag
        WHERE trim(tag.name) <> '' {and_sql}
    """
    total_row = conn.execute(count_sql, params + and_params).fetchone()
    total_count = int(total_row[0] if total_row else 0)

    items_sql = f"""
        SELECT
            trim(tag.name) AS name,
            COUNT(DISTINCT tag.id) AS count
        FROM (
            SELECT
                v.id,
                jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof({tags_expr}) = 'array' THEN {tags_expr} ELSE '[]'::jsonb END
                ) AS name
            FROM videos v{join_overrides}
            {where_sql}
        ) tag
        WHERE trim(tag.name) <> '' {and_sql}
        GROUP BY trim(tag.name)
        ORDER BY count DESC, name ASC
        LIMIT %s OFFSET %s
    """
    query_params = params + and_params + [limit, offset]
    rows = conn.execute(items_sql, query_params).fetchall()

    items = [
        TaxonomyItem(
            name=str(r[0] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            count=int(r[1] if isinstance(r, (list, tuple)) else r["count"]),
        )
        for r in rows
    ]

    return PaginatedTaxonomy(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


def query_catalog_performers(
    conn: Any,
    *,
    q: str | None = None,
    letter: str | None = None,
    photo_only: bool = False,
    sort: str = "name",
    direction: str | None = None,
    offset: int = 0,
    limit: int = 50,
    base_artwork_url: str = "/api/v1/artwork",
) -> PaginatedPerformers:
    """List performers with bounded pagination, optional filters, and movie counts."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _get_existing_tables(conn)
    columns = _get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _extract_disabled_channel_ids()
    conditions, params = _build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    perf_expr = _effective_value("performers", columns, has_overrides)

    cat_join = ""
    cat_alias_join = ""
    cat_id_expr = "COALESCE(NULLIF(elem.raw_id, ''), 'name:' || elem.raw_name)"
    cat_name_expr = "COALESCE(NULLIF(elem.raw_name, ''), elem.raw_id)"

    if "performer_catalog" in tables:
        cat_join = """
            LEFT JOIN LATERAL (
                SELECT pc.id AS id, pc.name AS name
                FROM performer_catalog pc
                WHERE (
                    (elem.raw_id IS NOT NULL AND elem.raw_id <> '' AND pc.id = elem.raw_id)
                    OR (
                        (elem.raw_id IS NULL OR elem.raw_id = '')
                        AND elem.raw_name IS NOT NULL AND elem.raw_name <> ''
                        AND lower(pc.name) = lower(elem.raw_name)
                    )
                )
                AND pc.active = true
                ORDER BY CASE WHEN elem.raw_id IS NOT NULL AND pc.id = elem.raw_id THEN 0 ELSE 1 END, pc.id ASC
                LIMIT 1
            ) cat ON true
        """
        cat_id_expr = "COALESCE(cat.id, NULLIF(elem.raw_id, ''), 'name:' || elem.raw_name)"
        cat_name_expr = "COALESCE(cat.name, NULLIF(elem.raw_name, ''), elem.raw_id)"

        if "performer_catalog_aliases" in tables:
            cat_alias_join = """
                LEFT JOIN LATERAL (
                    SELECT c.id AS id, c.name AS name
                    FROM performer_catalog_aliases a
                    JOIN performer_catalog c ON c.id = a.performer_id
                    WHERE c.active = true AND a.approved = true
                      AND (
                          (elem.raw_id IS NOT NULL AND elem.raw_id <> '' AND a.performer_id = elem.raw_id)
                          OR (
                              (elem.raw_id IS NULL OR elem.raw_id = '')
                              AND elem.raw_name IS NOT NULL AND elem.raw_name <> ''
                              AND lower(a.alias) = lower(elem.raw_name)
                          )
                      )
                    ORDER BY CASE WHEN elem.raw_id IS NOT NULL AND a.performer_id = elem.raw_id THEN 0 ELSE 1 END, c.id ASC, a.id ASC
                    LIMIT 1
                ) cat_alias ON cat.id IS NULL
            """
            cat_id_expr = "COALESCE(cat.id, cat_alias.id, NULLIF(elem.raw_id, ''), 'name:' || elem.raw_name)"
            cat_name_expr = "COALESCE(cat.name, cat_alias.name, NULLIF(elem.raw_name, ''), elem.raw_id)"

    elem_conds: list[str] = []
    elem_params: list[Any] = []

    if q and q.strip():
        elem_conds.append("(perf.canonical_name ILIKE %s ESCAPE '\\' OR perf.raw_name ILIKE %s ESCAPE '\\')")
        pattern = f"%{_escape_like_pattern(q.strip())}%"
        elem_params.extend([pattern, pattern])

    if letter and letter.strip():
        elem_conds.append("perf.canonical_name ILIKE %s ESCAPE '\\'")
        elem_params.append(f"{_escape_like_pattern(letter.strip())}%")

    if photo_only:
        if "performer_photos" in tables:
            elem_conds.append(
                "EXISTS (SELECT 1 FROM performer_photos ph WHERE (lower(ph.name) = lower(perf.canonical_name) OR lower(ph.name) = lower(perf.raw_name)) AND ph.thumb IS NOT NULL AND ph.thumb <> '')"
            )
        else:
            elem_conds.append("FALSE")

    all_conds = list(conditions) + elem_conds
    where_sql = ("WHERE " + " AND ".join(all_conds)) if all_conds else ""
    all_params = list(params) + elem_params

    # 1. Total Count
    count_sql = f"""
        SELECT COUNT(DISTINCT perf.canonical_id)
        FROM videos v{join_overrides},
        LATERAL (
            SELECT
                elem.raw_id,
                elem.raw_name,
                {cat_id_expr} AS canonical_id,
                {cat_name_expr} AS canonical_name
            FROM (
                SELECT
                    elem_json->>'id' AS raw_id,
                    CASE
                        WHEN jsonb_typeof(elem_json) = 'string' THEN trim(elem_json#>>'{{}}')
                        ELSE trim(elem_json->>'name')
                    END AS raw_name
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                ) elem_json
            ) elem
            {cat_join}
            {cat_alias_join}
            WHERE elem.raw_name IS NOT NULL AND elem.raw_name <> ''
        ) perf
        {where_sql}
    """
    total_row = conn.execute(count_sql, all_params).fetchone()
    total_count = int(total_row[0] if total_row else 0)

    # 2. Sort order
    if sort in ("video_count", "count"):
        dir_sql = "ASC" if direction == "asc" else "DESC"
        order_sql = f"video_count {dir_sql}, perf.canonical_name ASC, perf.canonical_id ASC"
    else:
        dir_sql = "DESC" if direction == "desc" else "ASC"
        order_sql = f"perf.canonical_name {dir_sql}, video_count DESC, perf.canonical_id ASC"

    # 3. Page query
    items_sql = f"""
        SELECT
            perf.canonical_id AS id,
            perf.canonical_name AS name,
            COUNT(DISTINCT v.id) AS video_count
        FROM videos v{join_overrides},
        LATERAL (
            SELECT
                elem.raw_id,
                elem.raw_name,
                {cat_id_expr} AS canonical_id,
                {cat_name_expr} AS canonical_name
            FROM (
                SELECT
                    elem_json->>'id' AS raw_id,
                    CASE
                        WHEN jsonb_typeof(elem_json) = 'string' THEN trim(elem_json#>>'{{}}')
                        ELSE trim(elem_json->>'name')
                    END AS raw_name
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                ) elem_json
            ) elem
            {cat_join}
            {cat_alias_join}
            WHERE elem.raw_name IS NOT NULL AND elem.raw_name <> ''
        ) perf
        {where_sql}
        GROUP BY perf.canonical_id, perf.canonical_name
        ORDER BY {order_sql}
        LIMIT %s OFFSET %s
    """
    page_params = all_params + [limit, offset]
    rows = conn.execute(items_sql, page_params).fetchall()

    if not rows:
        return PaginatedPerformers(
            items=[],
            total_count=total_count,
            offset=offset,
            limit=limit,
            has_more=False,
        )

    items = [
        PerformerRef(
            id=str(r[0] if isinstance(r, (list, tuple)) else r["id"]).strip(),
            name=str(r[1] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            photo_url=None,
            video_count=int(r[2] if isinstance(r, (list, tuple)) else r["video_count"]),
        )
        for r in rows
    ]

    return PaginatedPerformers(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


def _lookup_vetted_performer_by_name(conn: Any, tables: set[str], raw_name: str) -> tuple[str, str] | None:
    """Find an active vetted performer by display name or approved alias. Returns (id, name) or None."""
    if "performer_catalog" in tables:
        row = conn.execute(
            """
                SELECT id, name FROM performer_catalog
                WHERE lower(name) = lower(%s) AND active = true
                ORDER BY CASE WHEN name = %s THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
            (raw_name, raw_name),
        ).fetchone()
        if row:
            return (
                str(row[0] if isinstance(row, (list, tuple)) else row["id"]).strip(),
                str(row[1] if isinstance(row, (list, tuple)) else row["name"]).strip(),
            )
    if "performer_catalog" in tables and "performer_catalog_aliases" in tables:
        arow = conn.execute(
            """
                    SELECT c.id, c.name
                    FROM performer_catalog_aliases a
                    JOIN performer_catalog c ON c.id = a.performer_id
                    WHERE lower(a.alias) = lower(%s)
                      AND a.approved = true AND c.active = true
                    ORDER BY CASE WHEN a.alias = %s THEN 0 ELSE 1 END, c.id ASC, a.id ASC
                    LIMIT 1
                    """,
            (raw_name, raw_name),
        ).fetchone()
        if arow:
            return (
                str(arow[0] if isinstance(arow, (list, tuple)) else arow["id"]).strip(),
                str(arow[1] if isinstance(arow, (list, tuple)) else arow["name"]).strip(),
            )
    return None


def _lookup_vetted_performer_by_id(conn: Any, tables: set[str], lookup_id: str) -> tuple[str, str] | None:
    """Find an active vetted performer by catalog ID or approved alias. Returns (id, name) or None."""
    if "performer_catalog" in tables:
        row = conn.execute(
            """
                SELECT id, name FROM performer_catalog
                WHERE id = %s AND active = true
                ORDER BY id ASC
                LIMIT 1
                """,
            (lookup_id,),
        ).fetchone()
        if row:
            return (
                str(row[0] if isinstance(row, (list, tuple)) else row["id"]).strip(),
                str(row[1] if isinstance(row, (list, tuple)) else row["name"]).strip(),
            )
    if "performer_catalog" in tables and "performer_catalog_aliases" in tables:
        arow = conn.execute(
            """
                    SELECT c.id, c.name
                    FROM performer_catalog_aliases a
                    JOIN performer_catalog c ON c.id = a.performer_id
                    WHERE a.performer_id = %s
                      AND a.approved = true AND c.active = true
                    ORDER BY c.id ASC, a.id ASC
                    LIMIT 1
                    """,
            (lookup_id,),
        ).fetchone()
        if arow:
            return (
                str(arow[0] if isinstance(arow, (list, tuple)) else arow["id"]).strip(),
                str(arow[1] if isinstance(arow, (list, tuple)) else arow["name"]).strip(),
            )
    return None


def _lookup_unresolved_performer_name(conn: Any, join_overrides: str, perf_expr: str, lookup_id: str) -> str | None:
    """Return the display name stored with an unresolved raw performer ID in videos, or None."""
    # Check if this raw ID exists as an unresolved raw ID in videos
    v_row = conn.execute(
        f"""
                SELECT elem ->> 'name'
                FROM videos v{join_overrides},
                LATERAL jsonb_array_elements(
                    CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                ) elem
                WHERE jsonb_typeof(elem) = 'object'
                  AND elem ->> 'id' = %s
                  AND elem ->> 'name' IS NOT NULL
                  AND elem ->> 'name' <> ''
                ORDER BY v.id ASC
                LIMIT 1
                """,
        (lookup_id,),
    ).fetchone()
    if not v_row:
        return None
    return str(v_row[0] if isinstance(v_row, (list, tuple)) else v_row["name"]).strip()


def _resolve_performer_identity(
    conn: Any,
    tables: set[str],
    clean_id: str,
    join_overrides: str,
    perf_expr: str,
) -> tuple[str | None, str | None, bool] | None:
    """Resolve a requested performer ID or name to (canonical_id, canonical_name, is_vetted).

    Returns None when the requested ID is unknown and matches no video, which callers report as 404.
    """
    is_explicit_name = clean_id.startswith("name:")
    if is_explicit_name:
        raw_name = clean_id[5:].strip()
        lookup_id = None
    else:
        lookup_id = clean_id
        raw_name = None

    canonical_id: str | None = None
    canonical_name: str | None = None
    is_vetted = False

    if is_explicit_name and raw_name:
        # Explicit name: lookup by name or alias in vetted catalog
        resolved = _lookup_vetted_performer_by_name(conn, tables, raw_name)
        if resolved:
            canonical_id, canonical_name = resolved
            is_vetted = True

        if not canonical_name:
            canonical_id = f"name:{raw_name}"
            canonical_name = raw_name
    elif lookup_id:
        # ID-only lookup: do NOT match on display name or aliases of different people!
        resolved = _lookup_vetted_performer_by_id(conn, tables, lookup_id)
        if resolved:
            canonical_id, canonical_name = resolved
            is_vetted = True

        if not canonical_name:
            unresolved_name = _lookup_unresolved_performer_name(conn, join_overrides, perf_expr, lookup_id)
            if unresolved_name is None:
                # Unmatched ID not found in catalog and not found in any video: 404
                return None
            canonical_id = lookup_id
            canonical_name = unresolved_name

    return canonical_id, canonical_name, is_vetted


def _lookup_performer_bio(conn: Any, tables: set[str], canonical_name: str):
    """Return (bio, birth, nationality, aliases) from performer_bios, or empty values when no row matches."""
    bio_cols = _get_table_columns(conn, "performer_bios")
    nat_sql = "nationality" if "nationality" in bio_cols else "''"
    b_row = conn.execute(
        f"SELECT bio, birth, aliases, {nat_sql} FROM performer_bios WHERE lower(name) = lower(%s) ORDER BY CASE WHEN name = %s THEN 0 ELSE 1 END, updated_at DESC NULLS LAST, name ASC LIMIT 1",
        (canonical_name, canonical_name),
    ).fetchone()
    if not b_row:
        return None, None, None, []
    bio_val = str(b_row[0] or "").strip()
    birth_val = str(b_row[1] or "").strip()
    nat_val = str(b_row[3] or "").strip()
    aliases = _json_value(b_row[2], [])
    if not isinstance(aliases, list):
        aliases = []
    return bio_val or None, birth_val or None, nat_val or None, aliases


def _merge_catalog_aliases(conn: Any, canonical_name: str, aliases: list[str]) -> list[str]:
    """Append approved performer_aliases rows for a canonical name to the alias list."""
    a_rows = conn.execute(
        "SELECT alias FROM performer_aliases WHERE lower(canonical) = lower(%s)",
        (canonical_name,),
    ).fetchall()
    for ar in a_rows:
        al = str(ar[0] if isinstance(ar, (list, tuple)) else ar["alias"]).strip()
        if al and al not in aliases:
            aliases.append(al)
    return aliases


def _performer_has_photo(conn: Any, canonical_name: str) -> bool:
    """Return True when performer_photos holds a non-empty thumbnail for the performer."""
    p_row = conn.execute(
        "SELECT thumb FROM performer_photos WHERE lower(name) = lower(%s) ORDER BY CASE WHEN name = %s THEN 0 ELSE 1 END, updated_at DESC NULLS LAST, name ASC LIMIT 1",
        (canonical_name, canonical_name),
    ).fetchone()
    return bool(p_row and (p_row[0] if isinstance(p_row, (list, tuple)) else p_row["thumb"]))


def _performer_video_match(
    perf_expr: str,
    canonical_id: str | None,
    canonical_name: str | None,
    is_vetted: bool,
) -> tuple[list[str], list[Any]]:
    """Return the extra WHERE clause and params that select videos featuring this performer."""
    if canonical_id and canonical_id.startswith("name:"):
        clause = f"""EXISTS (
                SELECT 1 FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                ) elem
                WHERE (
                    (jsonb_typeof(elem) = 'string' AND lower(elem #>> '{{}}') = lower(%s))
                    OR (jsonb_typeof(elem) = 'object' AND (elem ->> 'id' IS NULL OR elem ->> 'id' = '') AND lower(elem ->> 'name') = lower(%s))
                )
            )"""
        return [clause], [canonical_name, canonical_name]
    if is_vetted and canonical_id:
        clause = f"""EXISTS (
                SELECT 1 FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                ) elem
                WHERE (
                    (jsonb_typeof(elem) = 'object' AND elem ->> 'id' = %s)
                    OR ((jsonb_typeof(elem) = 'string' OR elem ->> 'id' IS NULL OR elem ->> 'id' = '') AND lower(COALESCE(elem ->> 'name', elem #>> '{{}}')) = lower(%s))
                )
            )"""
        return [clause], [canonical_id, canonical_name]
    if canonical_id:
        # Unresolved raw ID: ONLY match elem ->> 'id' = %s without mapping to different same-name performer!
        clause = f"""EXISTS (
                SELECT 1 FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof({perf_expr}) = 'array' THEN {perf_expr} ELSE '[]'::jsonb END
                ) elem
                WHERE jsonb_typeof(elem) = 'object' AND elem ->> 'id' = %s
            )"""
        return [clause], [canonical_id]
    return [], []


def _count_performer_videos(conn: Any, join_overrides: str, conditions: list[str], params: list[Any]) -> int:
    """Count distinct visible videos matching the WHERE conditions."""
    where_sql = "WHERE " + " AND ".join(conditions)
    count_row = conn.execute(f"SELECT COUNT(DISTINCT v.id) FROM videos v{join_overrides} {where_sql}", params).fetchone()
    return int(count_row[0] if count_row else 0)


def _performer_in_catalog(conn: Any, tables: set[str], canonical_id: str | None) -> bool:
    """Return True when a vetted, active performer_catalog row exists for a non-name canonical ID."""
    if "performer_catalog" in tables and canonical_id and not canonical_id.startswith("name:"):
        return bool(
            conn.execute(
                "SELECT 1 FROM performer_catalog WHERE id = %s AND active = true LIMIT 1",
                (canonical_id,),
            ).fetchone()
        )
    return False


def query_catalog_performer_detail(
    conn: Any,
    performer_id: str,
    *,
    base_artwork_url: str = "/api/v1/artwork",
) -> PerformerDetail | None:
    """Get metadata, movie count, and biographical details for a performer."""
    clean_id = performer_id.strip()
    if not clean_id:
        return None

    is_explicit_name = clean_id.startswith("name:")

    tables = _get_existing_tables(conn)
    columns = _get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""
    perf_expr = _effective_value("performers", columns, has_overrides)

    identity = _resolve_performer_identity(conn, tables, clean_id, join_overrides, perf_expr)
    if identity is None:
        return None
    canonical_id, canonical_name, is_vetted = identity

    # Bio, birth, nationality, aliases
    bio: str | None = None
    birth: str | None = None
    nationality: str | None = None
    aliases: list[str] = []

    # Unresolved raw IDs must NOT map to a different same-name performer's bio/photos
    populate_bio_photo = is_vetted or is_explicit_name

    if populate_bio_photo and "performer_bios" in tables and canonical_name:
        bio, birth, nationality, aliases = _lookup_performer_bio(conn, tables, canonical_name)

    if populate_bio_photo and "performer_aliases" in tables and canonical_name:
        aliases = _merge_catalog_aliases(conn, canonical_name, aliases)

    aliases = sorted(set(aliases))

    # Photos
    has_photo = False
    if populate_bio_photo and "performer_photos" in tables and canonical_name:
        has_photo = _performer_has_photo(conn, canonical_name)

    # Safe same-origin artwork URL when performer has photo
    photo_url: str | None = (
        f"{base_artwork_url}/performer/{quote(canonical_id, safe='')}"
        if has_photo
        else None
    )

    # Count visible videos
    disabled_set = _extract_disabled_channel_ids()
    conditions, params = _build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    match_conditions, match_params = _performer_video_match(perf_expr, canonical_id, canonical_name, is_vetted)
    conditions.extend(match_conditions)
    params.extend(match_params)
    video_count = _count_performer_videos(conn, join_overrides, conditions, params)

    # 404 Guard: if 0 videos and no bio/photo/catalog presence, return None
    if video_count == 0 and bio is None and not has_photo:
        if not _performer_in_catalog(conn, tables, canonical_id):
            return None

    return PerformerDetail(
        id=canonical_id,
        name=canonical_name,
        disambiguation=None,
        photo_url=photo_url,
        video_count=video_count,
        bio=bio,
        nationality=nationality,
        birth=birth,
        aliases=aliases,
    )
