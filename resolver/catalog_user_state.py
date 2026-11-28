"""Owner-scoped favorite and watch-progress projections for catalog videos."""

from __future__ import annotations

from typing import Any, Sequence


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
    for row in rows:
        value = row[0] if isinstance(row, (list, tuple)) else row["target_id"]
        if value:
            favorited.add(str(value))
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
    for row in wp_rows:
        target_id = str(row[0] if isinstance(row, (list, tuple)) else row["target_id"])
        position = int(row[1] if isinstance(row, (list, tuple)) else row["position_seconds"])
        duration = int(row[2] if isinstance(row, (list, tuple)) else row["duration_seconds"])
        percentage = float(row[3] if isinstance(row, (list, tuple)) else row["played_percentage"])
        is_played = bool(row[4] if isinstance(row, (list, tuple)) else row["is_played"])
        progress_map[target_id] = {
            "position_seconds": position,
            "duration_seconds": duration,
            "played_percentage": percentage,
            "is_played": is_played,
        }
    return progress_map


def _user_state_results(
    clean_v_ids: list[str],
    favorited_canonical_ids: set[str],
    progress_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Combine favorite flags and watch progress into one state entry per requested video ID."""
    results: dict[str, dict[str, Any]] = {}
    for video_id in clean_v_ids:
        is_favorite = video_id in favorited_canonical_ids
        if video_id in progress_map:
            progress = progress_map[video_id]
            results[video_id] = {
                "is_favorite": is_favorite,
                "position_seconds": progress["position_seconds"],
                "played_percentage": progress["played_percentage"],
                "is_played": progress["is_played"],
            }
        else:
            results[video_id] = {
                "is_favorite": is_favorite,
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
    """Batch-lookup owner-scoped user state for video IDs without N+1 queries.

    Resolves canonical IDs and aliases via video_sources and active duplicate
    decisions. Guarantees no internal fields or secrets leak into returned state.
    """
    if not user_id or not video_ids:
        return {}

    clean_u_id = str(user_id).strip()
    clean_v_ids = [str(video_id).strip() for video_id in video_ids if str(video_id).strip()]
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
