"""Admin-triggered StashDB/ThePornDB artwork lookup and download."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("sync.scene_artwork_download")


def download_artwork_for_video(conn, media_dir: Path, video_id: str) -> dict:
    """Find a conservative scene match and save its poster beside the stream."""
    row = conn.execute(
        "SELECT title,duration,scene_match_source,scene_match_external_id "
        "FROM videos WHERE id=%s",
        (video_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Video no longer exists in the library.")
    title, duration, match_source, external_id = row

    file_rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path",
        (video_id,),
    ).fetchall()
    try:
        from .media_files import resolve_media_path
    except ImportError:
        from media_files import resolve_media_path
    root = media_dir.resolve()
    artwork_path = None
    for file_row in file_rows:
        stream_path = resolve_media_path(str(file_row[0]), media_dir)
        candidate = stream_path.with_suffix(".jpg")
        if candidate.is_relative_to(root) and not candidate.is_symlink():
            artwork_path = candidate
            break
    if artwork_path is None:
        raise ValueError("Target media file not found in library.")

    try:
        from .scene_scraper_fingerprint_sources import (
            search_stashdb_scene_by_oshash,
            search_tpdb_scene_by_oshash,
        )
        from .scene_scraper_media_files import _download_scene_poster
        from .scene_scraper_text_sources import (
            fetch_stashdb_scene_by_id,
            fetch_tpdb_scene_by_id,
            search_scene_by_exact_title,
        )
    except ImportError:
        from scene_scraper_fingerprint_sources import search_stashdb_scene_by_oshash, search_tpdb_scene_by_oshash
        from scene_scraper_media_files import _download_scene_poster
        from scene_scraper_text_sources import fetch_stashdb_scene_by_id, fetch_tpdb_scene_by_id, search_scene_by_exact_title

    matches = []
    lookup_error = False
    scene_id = str(external_id or "").strip()
    source = str(match_source or "").casefold()
    if scene_id:
        if "stashdb" in source:
            linked_lookups = (("StashDB", fetch_stashdb_scene_by_id),)
        elif "theporndb" in source or "tpdb" in source:
            linked_lookups = (("ThePornDB", fetch_tpdb_scene_by_id),)
        else:
            linked_lookups = (("StashDB", fetch_stashdb_scene_by_id), ("ThePornDB", fetch_tpdb_scene_by_id))
        for provider, lookup in linked_lookups:
            try:
                scene = lookup(scene_id)
                if scene and scene.poster_url:
                    matches.append((provider, scene))
            except Exception as exc:
                lookup_error = True
                logger.warning("Linked scene lookup failed (%s %s): %s",
                               provider, scene_id, exc)

    def save_matches(start: int = 0):
        for provider, scene in matches[start:]:
            if _download_scene_poster(scene.poster_url, artwork_path, video_id):
                return provider
        return None

    saved_provider = save_matches()
    if saved_provider:
        return {"downloaded": True, "provider": saved_provider}

    fingerprints = {}
    if conn.execute("SELECT to_regclass('public.video_fingerprints')").fetchone()[0]:
        fingerprints = {
            str(algorithm or "").upper(): str(value or "").lower()
            for algorithm, value in conn.execute(
                "SELECT algorithm,fingerprint FROM video_fingerprints "
                "WHERE video_id=%s AND algorithm='OSHASH'",
                (video_id,),
            ).fetchall()
        }
    oshash = fingerprints.get("OSHASH", "")
    if oshash:
        for provider, lookup in (("StashDB", search_stashdb_scene_by_oshash), ("ThePornDB", search_tpdb_scene_by_oshash)):
            try:
                scene = lookup(oshash, duration=duration)
                if scene and scene.poster_url:
                    matches.append((provider, scene))
                    saved_provider = save_matches(len(matches) - 1)
                    if saved_provider:
                        return {"downloaded": True, "provider": saved_provider}
            except Exception as exc:
                lookup_error = True
                logger.warning("OSHASH scene lookup failed (%s): %s", provider, exc)

    if title and duration:
        try:
            scene = search_scene_by_exact_title(str(title), duration=int(duration))
            if scene and scene.poster_url:
                provider = "StashDB" if "stashdb" in str(scene.source).casefold() else "ThePornDB"
                matches.append((provider, scene))
                saved_provider = save_matches(len(matches) - 1)
                if saved_provider:
                    return {"downloaded": True, "provider": saved_provider}
        except Exception as exc:
            lookup_error = True
            logger.warning("Title scene lookup failed: %s", exc)

    if matches:
        return {"downloaded": False, "local_state": "artwork_download_failed"}
    if lookup_error:
        # A provider request failed; reporting "download failed" here would
        # claim a poster was found when the search never completed.
        return {"downloaded": False, "local_state": "artwork_lookup_failed"}
    return {"downloaded": False, "local_state": "artwork_not_found"}
