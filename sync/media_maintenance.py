"""Filesystem housekeeping outside the indexed video catalog."""
import time as _time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

def cleanup_old_transcodes(transcodes_dir: Optional[Path] = None, max_age_hours: int = 2, *, logger, clock=_time):
    """Clean up stale transcode files older than max_age_hours from SSD storage."""
    target = transcodes_dir or Path("/transcodes")
    if not target.exists() or not target.is_dir():
        target = Path("./jf-cache/transcodes")
        if not target.exists() or not target.is_dir():
            return

    now = clock.time()
    cutoff = now - (max_age_hours * 3600)
    cleaned = 0
    total_bytes = 0
    try:
        for f in target.iterdir():
            if f.is_file() and not f.name.startswith("."):
                try:
                    if f.stat().st_mtime < cutoff:
                        sz = f.stat().st_size
                        f.unlink()
                        cleaned += 1
                        total_bytes += sz
                except Exception as exc:
                    logger.warning("Could not remove stale transcode file %s: %s",
                                   f.name, type(exc).__name__, exc_info=True)
        if cleaned > 0:
            logger.info("Cleaned %s stale transcode files (%s MB) older than %sh from SSD", cleaned, total_bytes // 1024 // 1024, max_age_hours)
    except Exception as e:
        logger.debug("Transcode cleanup notice: %s", e)


def cleanup_legacy_folders(media_dir: Path, *, logger):
    """Remove only known Adult-Hub sidecars from legacy duplicate folders.

    These names are historical outputs, but users may also have folders with
    the same names. Preserve a folder if it contains anything we cannot
    positively identify as an Adult-Hub stream or its sidecars.
    """
    if not media_dir.exists() or not media_dir.is_dir():
        return
    root = media_dir.resolve()
    for legacy_name in ["Browse", "Kategorie"]:
        legacy_path = media_dir / legacy_name
        if not legacy_path.exists() or not legacy_path.is_dir() or legacy_path.is_symlink():
            continue
        try:
            resolved = legacy_path.resolve()
            if not resolved.is_relative_to(root):
                logger.warning("Skipping legacy folder outside media directory: %s", legacy_path)
                continue
            entries = list(legacy_path.rglob("*"))
            if not _legacy_entries_are_regular(legacy_path, entries, logger):
                continue
            files = [entry for entry in entries if entry.is_file()]
            streams = [entry for entry in files if entry.suffix.lower() == ".strm"]
            if not streams:
                continue

            owned_files = _owned_legacy_files(streams)
            if not owned_files or set(files) != owned_files:
                logger.info("Preserving legacy-named folder with unrecognized content: %s", legacy_path)
                continue

            _remove_legacy_folder(legacy_path, entries, owned_files)
            logger.info("Removed Adult-Hub legacy duplicates from %s", legacy_path)
        except (OSError, UnicodeError) as exc:
            logger.warning("Could not safely clean legacy folder %s: %s", legacy_path, exc)


def _legacy_entries_are_regular(legacy_path: Path, entries: list, logger) -> bool:
    """Reject folders that contain symlinks or special filesystem entries."""
    if any(entry.is_symlink() for entry in entries):
        logger.warning("Skipping legacy folder containing symlinks: %s", legacy_path)
        return False
    if any(not entry.is_dir() and not entry.is_file() for entry in entries):
        logger.warning("Skipping legacy folder with special filesystem entries: %s", legacy_path)
        return False
    return True


def _owned_legacy_files(streams: list[Path]) -> set[Path]:
    """Return the stream files and sidecars, or an empty set if any stream is foreign."""
    owned_files: set[Path] = set()
    for stream in streams:
        content = stream.read_text(encoding="utf-8").strip()
        route = urlparse(content).path
        if not (route.startswith("/admin/play/") or route.startswith("/play/")) \
                or not route.endswith("/stream"):
            return set()
        owned_files.add(stream)
        for suffix in (".nfo", ".jpg", ".thumb_fail"):
            sidecar = stream.with_suffix(suffix)
            if sidecar.exists():
                owned_files.add(sidecar)
    return owned_files


def _remove_legacy_folder(legacy_path: Path, entries: list, owned_files: set[Path]) -> None:
    for path in sorted(owned_files, key=lambda item: len(item.parts), reverse=True):
        path.unlink()
    for directory in sorted(
        (entry for entry in entries if entry.is_dir()),
        key=lambda item: len(item.parts), reverse=True,
    ):
        directory.rmdir()
    legacy_path.rmdir()


