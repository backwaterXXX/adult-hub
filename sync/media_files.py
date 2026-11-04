"""Safe media paths and playback ``.strm`` file handling."""

import hashlib
import os
import re
import tempfile
from common import db
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Set, Tuple


@dataclass(frozen=True)
class MediaFileServices:
    """Runtime behavior supplied by ``sync.py`` to preserve patchability."""

    stream_mode_for_provider: Callable
    resolver_public_url: str
    stream_public_url_telegram: str
    is_cleanup_allowed: Callable
    logger: Any


def resolve_media_path(stored_path: str, media_dir: Path) -> Path:
    """Resolve a recorded media path without allowing it outside the library."""
    p = Path(stored_path)
    if stored_path.startswith("/media/adult/"):
        p = Path(stored_path[len("/media/adult/"):])
    elif p.is_absolute():
        p = Path(p.name)
    root = media_dir.resolve()
    lexical = root / p
    if not lexical.is_relative_to(root):
        raise ValueError(f"Unsafe media path: {stored_path}")
    # Check unresolved components first: resolve() dereferences symlinks.
    cursor = root
    for part in lexical.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Symlink media path is not safe: {stored_path}")
    result = lexical.resolve()
    if not result.is_relative_to(root) or result.suffix != ".strm":
        raise ValueError(f"Unsafe media path: {stored_path}")
    return result


def stable_media_basename(video_id: str) -> str:
    """A provider-qualified filename independent of mutable video titles."""
    provider, _, raw_id = video_id.partition(":")
    if not provider or not raw_id:
        raise ValueError(f"Invalid video ID: {video_id}")
    readable_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id)[:40]
    digest = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:12]
    return f"{provider} [{readable_id}-{digest}]"


def write_stream_file(path: Path, video_id: str, mode: Optional[str] = None, *,
                      services: MediaFileServices) -> bool:
    if mode is None:
        mode = services.stream_mode_for_provider(video_id)
    public_url = (
        services.stream_public_url_telegram
        if video_id.startswith("telegram:") and services.stream_public_url_telegram
        else services.resolver_public_url
    )
    base = f"{public_url.rstrip('/')}/admin/play/{video_id}/stream"
    content = base + ("\n" if mode == "redirect" else "?mode=proxy\n")
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    tmp_path = path.with_suffix(".strm.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return True


def create_stream_file(path: Path, video_id: str, mode: Optional[str] = None, *,
                       services: MediaFileServices) -> bool:
    """Create a known missing stream without replacing a path that appeared meanwhile.

    Returns True when a new file was created and False when a previous attempt already
    created the exact expected file. Any symlink or different existing content is a
    conflict; repair jobs must never overwrite an unreviewed library file.
    """
    if mode is None:
        mode = services.stream_mode_for_provider(video_id)
    public_url = (
        services.stream_public_url_telegram
        if video_id.startswith("telegram:") and services.stream_public_url_telegram
        else services.resolver_public_url
    )
    base = f"{public_url.rstrip('/')}/admin/play/{video_id}/stream"
    content = base + ("\n" if mode == "redirect" else "?mode=proxy\n")
    path = Path(path)

    if _already_restored(path, content):
        return False
    if not path.parent.is_dir():
        raise FileNotFoundError("Registered stream directory is unavailable.")
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".repair", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp_path, path, follow_symlinks=False)
            return True
        except FileExistsError:
            # A worker retry or concurrent file restoration may have won the race.
            if _already_restored(path, content):
                return False
            raise
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _already_restored(path: Path, content: str) -> bool:
    """Return True when path already holds the exact stream file, False when absent."""
    if path.is_symlink():
        raise FileExistsError("Registered stream path is a symlink.")
    if not path.exists():
        return False
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        raise FileExistsError("Registered stream path already contains different data.")
    return True


def rewrite_all_stream_files(
    conn: db.Connection, media_dir: Path,
    providers_succeeded: Optional[Set[str]] = None, *,
    services: MediaFileServices, resolve_path_fn: Callable,
    write_stream_file_fn: Callable,
) -> Tuple[int, int]:
    """Rewrite known streams to the configured URL/mode behind cleanup guard."""
    checked = 0
    rewritten = 0
    try:
        rows = conn.execute("SELECT file_path, video_id FROM synced_files").fetchall()
    except Exception as exc:
        services.logger.warning(f"Stream rewrite: could not list synced files: {exc}")
        return 0, 0
    for file_path, video_id in rows:
        if not video_id or not services.is_cleanup_allowed(
                video_id, providers_succeeded=providers_succeeded):
            continue
        try:
            target = resolve_path_fn(file_path, media_dir)
        except ValueError:
            continue
        if not target.exists():
            continue
        checked += 1
        try:
            if write_stream_file_fn(target, video_id):
                rewritten += 1
        except Exception as exc:
            services.logger.warning(f"Stream rewrite failed for {video_id}: {exc}")
    if rewritten:
        services.logger.info(
            f"Stream rewrite: {rewritten}/{checked} files updated to current mode"
        )
    return checked, rewritten
