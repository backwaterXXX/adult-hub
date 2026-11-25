"""Persist matched scene metadata to artwork files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, List, Optional
import logging
from common.http_rate_limit import request_with_host_limit

logger = logging.getLogger(__name__)


from sync.scene_scraper_models import ScrapedScene  # noqa: E402

from sync import scene_scrapers as _core  # noqa: E402


class _MediaTargets:
    """Files found for one video. Partially filled when the lookup stops early."""

    def __init__(self) -> None:
        self.poster_paths: List[Path] = []
        self.registered_files = False


def _collect_registered_artwork(conn: Any, video_id: str, media_dir: Path, strict: bool,
                                targets: _MediaTargets) -> None:
    from sync.media_files import resolve_media_path
    file_rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
    ).fetchall()
    targets.registered_files = bool(file_rows)
    if strict and not targets.registered_files:
        raise FileNotFoundError(f"No registered media file for {video_id}")
    for (file_path,) in file_rows:
        stream_path = resolve_media_path(str(file_path), media_dir)
        poster_path = stream_path.with_suffix(".jpg")
        if poster_path not in targets.poster_paths:
            targets.poster_paths.append(poster_path)


def _resolve_poster_paths(conn: Optional[Any], video_id: str, media_dir: Path,
                          strict: bool) -> List[Path]:
    """Return the artwork paths beside the video's stream files; empty when unknown."""
    targets = _MediaTargets()
    if conn is not None:
        try:
            _collect_registered_artwork(conn, video_id, media_dir, strict, targets)
        except Exception:
            if strict:
                raise
    if not targets.poster_paths:
        # Compatibility for stream files that predate the synced_files index.
        parts = video_id.split(":")
        comp_tail = parts[-2] if len(parts) > 2 else parts[-1]
        matches = [path for path in media_dir.glob(f"*{comp_tail}*.strm") if not path.is_symlink()]
        if len(matches) != 1:
            if strict and targets.registered_files:
                raise FileNotFoundError(f"Stream file not found for {video_id}")
            return []
        targets.poster_paths.append(matches[0].with_suffix(".jpg"))
    return targets.poster_paths


def _update_media_files(
    media_dir: Path,
    video_id: str,
    matched: ScrapedScene,
    performers: List[str],
    http_client: Optional[Any] = None,
    conn: Optional[Any] = None,
    strict: bool = False,
) -> bool:
    """Refresh the linked scene poster and report whether a valid external poster was saved."""
    poster_saved = False
    try:
        poster_paths = _resolve_poster_paths(conn, video_id, media_dir, strict)
        if not poster_paths:
            return False
        # Download and refresh the linked scene poster when available.
        if matched.poster_url:
            for poster_path in poster_paths:
                poster_saved = _download_scene_poster(
                    matched.poster_url, poster_path, video_id, http_client=http_client,
                ) or poster_saved
    except Exception as e:
        if strict:
            raise
        _core.logger.debug("Could not update media files for %s: %s", video_id, e)
    return poster_saved


def _download_scene_poster(url: str, jpg_path: Path, video_id: str,
                           http_client: Optional[Any] = None) -> bool:
    """Validate and save a linked scene poster."""
    client = http_client or _core.httpx
    if not client or jpg_path.is_symlink():
        return False
    try:
        resp = request_with_host_limit(
            client, "get", url,
            headers={"User-Agent": _core.USER_AGENT}, timeout=10.0, follow_redirects=True,
        )
        if resp.status_code != 200 or len(resp.content) <= 5000:
            _core.logger.warning("Linked poster download failed for %s (HTTP %s, %s bytes)",
                                 video_id, resp.status_code, len(resp.content))
            return False
        from io import BytesIO
        from PIL import Image
        from common.image_utils import is_black_image
        with Image.open(BytesIO(resp.content)) as downloaded:
            downloaded.load()
            if is_black_image(downloaded):
                _core.logger.warning("Linked poster rejected for %s (black image)", video_id)
                return False
            output = BytesIO()
            downloaded.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
        image_bytes = output.getvalue()
        if len(image_bytes) <= 5000:
            _core.logger.warning("Linked poster rejected for %s (encoded image too small)", video_id)
            return False
        tmp = jpg_path.with_suffix(".jpg.tmp")
        tmp.write_bytes(image_bytes)
        tmp.replace(jpg_path)
        marker = jpg_path.with_suffix(".jpg.external")
        marker_tmp = marker.with_suffix(marker.suffix + ".tmp")
        marker_tmp.write_text(hashlib.sha256(image_bytes).hexdigest(), encoding="ascii")
        marker_tmp.replace(marker)
        try:
            _core.os.utime(jpg_path, (_core.time.time(), _core.time.time()))
        except Exception:
            logger.debug("Ignored exception in _download_scene_poster", exc_info=True)
        return True
    except Exception as image_error:
        _core.logger.warning("Could not save linked poster for %s (%s)", video_id,
                           type(image_error).__name__)
        return False
