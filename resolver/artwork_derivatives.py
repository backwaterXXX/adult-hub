"""Lazily generated, disk-cached artwork derivatives for client surfaces.

Desktop and TV clients draw posters a few hundred pixels wide, but the library
serves the original file (often 1280x720 or larger). Re-encoding once on the
server and caching the result keeps the first paint small and stops every client
from decoding full-resolution artwork for a card.

Derivatives never replace the originals: they live in a separate cache keyed by
the content hash of the source bytes, so replacing a poster invalidates its
variants automatically and the media library stays untouched.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# Widths a client may request. A small fixed ladder keeps the cache bounded and
# the responses predictable; anything else is a client bug, not a new variant.
ALLOWED_DERIVATIVE_WIDTHS = (160, 240, 320, 400, 480, 640, 720, 960, 1280)
# Widths the sync pre-generation produces: the card grid and the details panel.
DEFAULT_PREWARM_WIDTHS = (480, 1280)
JPEG_QUALITY = 82
DEFAULT_CACHE_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
# Encoding is CPU-bound; two workers keep a cold screen from starving requests.
_GENERATION_PERMITS = threading.BoundedSemaphore(2)
_PRUNE_LOCK = threading.Lock()
_LAST_PRUNE = 0.0
_PRUNE_INTERVAL_SECONDS = 300.0
_STATE_LOCK = threading.Lock()


_CACHE_ROOT = Path("/cache")
_DATA_ROOT = Path("/data")


def _shared_cache_dir() -> Path | None:
    """Return the derivative directory of a shared volume, when this host has one.

    The image ships an empty ``/cache``, so its presence proves nothing: only a
    mount carries what the resolver serves. Without the mount check the sync pass
    pre-generated posters into the container layer, where nothing ever read them.
    Containers that mount the data volume instead (sync writes ``/data/cache``)
    reach the same host files through it.
    """
    if _CACHE_ROOT.is_dir() and os.path.ismount(_CACHE_ROOT):
        return _CACHE_ROOT / "artwork-derivatives"
    if _DATA_ROOT.is_dir():
        return _DATA_ROOT / "cache" / "artwork-derivatives"
    return None


def derivatives_dir() -> Path:
    """Return the derivative cache directory, creating it when needed."""
    configured = os.getenv("ARTWORK_DERIVATIVE_DIR", "").strip()
    if configured:
        directory = Path(configured)
    else:
        directory = _shared_cache_dir() or (
            Path(__file__).resolve().parents[1] / "data" / "cache" / "artwork-derivatives"
        )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cache_limit_bytes() -> int:
    raw = os.getenv("ARTWORK_DERIVATIVE_CACHE_MB", "").strip()
    try:
        megabytes = int(raw)
    except ValueError:
        return DEFAULT_CACHE_LIMIT_BYTES
    if megabytes <= 0:
        return 0
    return megabytes * 1024 * 1024


def _source_digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()[:32]


def derivative_path(source: bytes, width: int) -> Path:
    return derivatives_dir() / f"{_source_digest(source)}-{width}.jpg"


def _encode(source: bytes, width: int) -> bytes | None:
    try:
        with Image.open(io.BytesIO(source)) as image:
            image.load()
            rgb = image.convert("RGB")
            if rgb.width > width:
                height = max(1, round(rgb.height * width / rgb.width))
                rgb = rgb.resize((width, height), Image.LANCZOS)
            else:
                width = rgb.width
            encoded = io.BytesIO()
            rgb.save(encoded, "JPEG", quality=JPEG_QUALITY, optimize=False)
            return encoded.getvalue()
    except Exception:
        logger.debug("artwork derivative encoding failed at width %s", width, exc_info=True)
        return None


def _write_atomically(path: Path, data: bytes) -> bool:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
        return True
    except OSError:
        logger.debug("could not write artwork derivative %s", path.name, exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _prune(limit_bytes: int, keep: Path | None = None) -> None:
    """Evict the oldest derivatives once the cache exceeds its size limit."""
    global _LAST_PRUNE
    import time

    now = time.monotonic()
    with _PRUNE_LOCK:
        if now - _LAST_PRUNE < _PRUNE_INTERVAL_SECONDS:
            return
        _LAST_PRUNE = now
        directory = derivatives_dir()
        # The prewarm bookkeeping lives next to the derivatives, and a partial
        # write from another process may still be a *.tmp file: evicting either
        # would force a full re-read of the media tree on the next pass.
        reserved = _state_path().name
        try:
            files = [
                (entry, entry.stat())
                for entry in directory.iterdir()
                if entry.is_file()
                and entry.name != reserved
                and not entry.name.endswith(".tmp")
            ]
        except OSError:
            return
        total = sum(stat.st_size for _, stat in files)
        if total <= limit_bytes:
            return
        files.sort(key=lambda item: item[1].st_mtime)
        target = int(limit_bytes * 0.9)
        for entry, stat in files:
            if total <= target:
                break
            if keep is not None and entry == keep:
                continue
            try:
                entry.unlink()
                total -= stat.st_size
            except OSError:
                continue


def get_derivative(source: bytes, width: int) -> bytes | None:
    """Return a JPEG derivative at most ``width`` pixels wide.

    Returns ``None`` for an unsupported width or undecodable source so the
    caller can fall back to the original bytes.
    """
    if width not in ALLOWED_DERIVATIVE_WIDTHS or not source:
        return None
    path = derivative_path(source, width)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("could not read artwork derivative %s", path.name, exc_info=True)

    with _GENERATION_PERMITS:
        # Another worker may have produced it while this one waited.
        try:
            return path.read_bytes()
        except OSError:
            pass
        encoded = _encode(source, width)
        if encoded is None:
            return None
        if _write_atomically(path, encoded):
            limit = _cache_limit_bytes()
            if limit > 0:
                _prune(limit, keep=path)
        return encoded


def prewarm_file(path: Path | str, widths: tuple[int, ...] = DEFAULT_PREWARM_WIDTHS) -> int:
    """Generate the cached variants of one artwork file.

    Returns the number of variants that had to be encoded. Existing files are
    left alone, so repeated passes over the library are cheap.
    """
    try:
        source = Path(path).read_bytes()
    except OSError:
        return 0
    if not source:
        return 0
    generated = 0
    for width in widths:
        if width not in ALLOWED_DERIVATIVE_WIDTHS:
            continue
        if derivative_path(source, width).is_file():
            continue
        if get_derivative(source, width) is not None:
            generated += 1
    return generated


def prewarm_paths(
    paths: list[Path | str],
    widths: tuple[int, ...] = DEFAULT_PREWARM_WIDTHS,
) -> int:
    """Generate variants for the given artwork files (background hooks)."""
    return sum(prewarm_file(path, widths) for path in paths)


def _state_path() -> Path:
    return derivatives_dir() / "prewarm-state.json"


def _load_prewarm_state() -> dict:
    import json

    try:
        raw = _state_path().read_text()
        data = json.loads(raw)
    except (OSError, ValueError):
        return {"files": {}}
    files = data.get("files")
    if not isinstance(files, dict):
        return {"files": {}}
    return {"files": files}


def _save_prewarm_state(state: dict) -> None:
    import json

    path = _state_path()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, separators=(",", ":")))
        os.replace(temporary, path)
    except OSError:
        logger.debug("could not persist artwork prewarm state", exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _artwork_candidates(root_path: Path) -> list[tuple[Path, int, int]]:
    """Return (path, mtime_ns, size) for every artwork file under ``root_path``."""
    candidates: list[tuple[Path, int, int]] = []
    for path in root_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        candidates.append((path, stat_result.st_mtime_ns, stat_result.st_size))
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates


def prewarm_directory(
    root: Path | str,
    *,
    widths: tuple[int, ...] = DEFAULT_PREWARM_WIDTHS,
    limit: int = 200,
) -> dict:
    """Prewarm a bounded batch of artwork under ``root``.

    Newest files first: freshly synced artwork is the artwork clients are most
    likely to request. A small state file remembers ``mtime:size`` per file so
    unchanged artwork is not re-read on every pass.
    """
    root_path = Path(root)
    if limit <= 0 or not root_path.is_dir():
        return {"scanned": 0, "generated_files": 0, "generated_variants": 0, "skipped": 0}

    candidates = _artwork_candidates(root_path)

    with _STATE_LOCK:
        known = dict(_load_prewarm_state()["files"])

    planned: list[tuple[Path, str, str]] = []
    skipped = 0
    for path, mtime_ns, size in candidates:
        if len(planned) >= limit:
            break
        key = str(path)
        signature = f"{mtime_ns}:{size}"
        if known.get(key) == signature:
            skipped += 1
            continue
        planned.append((path, key, signature))

    # Encoding happens outside the lock: a second prewarm (the daily batch plus a
    # CLI run) waits for the state file, not for a hundred JPEG encodes.
    generated_files = 0
    generated_variants = 0
    updates: dict[str, str] = {}
    for path, key, signature in planned:
        variants = prewarm_file(path, widths)
        updates[key] = signature
        if variants:
            generated_files += 1
            generated_variants += variants

    with _STATE_LOCK:
        state = _load_prewarm_state()
        state["files"].update(updates)
        if len(state["files"]) > 200_000:
            # Keep the state bounded; a forgotten entry only costs one re-check.
            state["files"] = dict(list(state["files"].items())[-100_000:])
        _save_prewarm_state(state)

    return {
        "scanned": len(candidates),
        "generated_files": generated_files,
        "generated_variants": generated_variants,
        "skipped": skipped,
    }


def clear_cache() -> None:
    """Remove every generated derivative (tests and operator maintenance)."""
    directory = derivatives_dir()
    for entry in directory.iterdir():
        if entry.is_file():
            try:
                entry.unlink()
            except OSError:
                continue


def _main() -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Pre-generate cached artwork derivatives.")
    parser.add_argument("--root", default=os.getenv("MEDIA_DIR", "/media/adult"))
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--widths",
        default=",".join(str(width) for width in DEFAULT_PREWARM_WIDTHS),
        help="Comma-separated widths from the published ladder.",
    )
    parser.add_argument("--all", action="store_true", help="Ignore the state file and re-check every file.")
    args = parser.parse_args()
    widths = tuple(int(part) for part in args.widths.split(",") if part.strip())
    unknown = [width for width in widths if width not in ALLOWED_DERIVATIVE_WIDTHS]
    if unknown:
        parser.error(f"unsupported widths: {unknown}")
    if args.all:
        try:
            _state_path().unlink()
        except OSError:
            pass
    result = prewarm_directory(args.root, widths=widths, limit=args.limit)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
