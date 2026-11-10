"""Admin-triggered StashDB/ThePornDB artwork lookup and download."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger("sync.scene_artwork_download")


def _import_scene_sources():
    """Load provider lookups and the poster downloader at call time."""
    from sync.scene_scraper_fingerprint_sources import (
        search_stashdb_scene_by_oshash,
        search_tpdb_scene_by_oshash,
    )
    from sync.scene_scraper_media_files import _download_scene_poster
    from sync.scene_scraper_text_sources import (
        fetch_stashdb_scene_by_id,
        fetch_tpdb_scene_by_id,
        search_scene_by_exact_title,
    )
    return SimpleNamespace(
        search_stashdb_scene_by_oshash=search_stashdb_scene_by_oshash,
        search_tpdb_scene_by_oshash=search_tpdb_scene_by_oshash,
        download_scene_poster=_download_scene_poster,
        fetch_stashdb_scene_by_id=fetch_stashdb_scene_by_id,
        fetch_tpdb_scene_by_id=fetch_tpdb_scene_by_id,
        search_scene_by_exact_title=search_scene_by_exact_title,
    )


def _artwork_target(conn, media_dir: Path, video_id: str) -> Path:
    """Return the ``.jpg`` path beside the first safe stream file of the video."""
    from sync.media_files import resolve_media_path
    file_rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path",
        (video_id,),
    ).fetchall()
    root = media_dir.resolve()
    for file_row in file_rows:
        stream_path = resolve_media_path(str(file_row[0]), media_dir)
        candidate = stream_path.with_suffix(".jpg")
        if candidate.is_relative_to(root) and not candidate.is_symlink():
            return candidate
    raise ValueError("Target media file not found in library.")


def _oshash_for_video(conn, video_id: str) -> str:
    """Return the stored OSHASH fingerprint for the video, or an empty string."""
    if not conn.execute("SELECT to_regclass('public.video_fingerprints')").fetchone()[0]:
        return ""
    fingerprints = {
        str(algorithm or "").upper(): str(value or "").lower()
        for algorithm, value in conn.execute(
            "SELECT algorithm,fingerprint FROM video_fingerprints "
            "WHERE video_id=%s AND algorithm='OSHASH'",
            (video_id,),
        ).fetchall()
    }
    return fingerprints.get("OSHASH", "")


class _ArtworkSearch:
    """Collects scene matches for one video and saves the first usable poster.

    Providers are queried lazily, in order, so a later provider is only
    contacted when earlier ones did not yield a saved poster.
    """

    def __init__(self, video_id: str, artwork_path: Path, duration, sources):
        self.video_id = video_id
        self.artwork_path = artwork_path
        self.duration = duration
        self.sources = sources
        self.matches: list[tuple[str, object]] = []
        self.lookup_error = False

    def save_from(self, start: int = 0):
        for provider, scene in self.matches[start:]:
            if self.sources.download_scene_poster(scene.poster_url, self.artwork_path, self.video_id):
                return provider
        return None

    def add_linked_matches(self, scene_id: str, source: str) -> None:
        if not scene_id:
            return
        if "stashdb" in source:
            linked_lookups = (("StashDB", self.sources.fetch_stashdb_scene_by_id),)
        elif "theporndb" in source or "tpdb" in source:
            linked_lookups = (("ThePornDB", self.sources.fetch_tpdb_scene_by_id),)
        else:
            linked_lookups = (("StashDB", self.sources.fetch_stashdb_scene_by_id),
                              ("ThePornDB", self.sources.fetch_tpdb_scene_by_id))
        for provider, lookup in linked_lookups:
            try:
                scene = lookup(scene_id)
                if scene and scene.poster_url:
                    self.matches.append((provider, scene))
            except Exception as exc:
                self.lookup_error = True
                logger.warning("Linked scene lookup failed (%s %s): %s",
                               provider, scene_id, exc)

    def try_oshash(self, oshash: str):
        lookups = (("StashDB", self.sources.search_stashdb_scene_by_oshash),
                   ("ThePornDB", self.sources.search_tpdb_scene_by_oshash))
        for provider, lookup in lookups:
            try:
                scene = lookup(oshash, duration=self.duration)
                if scene and scene.poster_url:
                    self.matches.append((provider, scene))
                    saved_provider = self.save_from(len(self.matches) - 1)
                    if saved_provider:
                        return saved_provider
            except Exception as exc:
                self.lookup_error = True
                logger.warning("OSHASH scene lookup failed (%s): %s", provider, exc)
        return None

    def try_title(self, title, duration):
        if not (title and duration):
            return None
        try:
            scene = self.sources.search_scene_by_exact_title(str(title), duration=int(duration))
            if scene and scene.poster_url:
                provider = "StashDB" if "stashdb" in str(scene.source).casefold() else "ThePornDB"
                self.matches.append((provider, scene))
                return self.save_from(len(self.matches) - 1)
        except Exception as exc:
            self.lookup_error = True
            logger.warning("Title scene lookup failed: %s", exc)
        return None

    def outcome(self) -> dict:
        if self.matches:
            return {"downloaded": False, "local_state": "artwork_download_failed"}
        if self.lookup_error:
            # A provider request failed; reporting "download failed" here would
            # claim a poster was found when the search never completed.
            return {"downloaded": False, "local_state": "artwork_lookup_failed"}
        return {"downloaded": False, "local_state": "artwork_not_found"}


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

    artwork_path = _artwork_target(conn, media_dir, video_id)
    search = _ArtworkSearch(video_id, artwork_path, duration, _import_scene_sources())
    search.add_linked_matches(str(external_id or "").strip(), str(match_source or "").casefold())
    saved_provider = search.save_from()
    if saved_provider:
        return {"downloaded": True, "provider": saved_provider}

    oshash = _oshash_for_video(conn, video_id)
    if oshash:
        saved_provider = search.try_oshash(oshash)
        if saved_provider:
            return {"downloaded": True, "provider": saved_provider}

    saved_provider = search.try_title(title, duration)
    if saved_provider:
        return {"downloaded": True, "provider": saved_provider}
    return search.outcome()
