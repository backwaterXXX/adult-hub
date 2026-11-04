"""Pure helpers for upstream playlist and byte-range proxy handling."""
import posixpath
import re
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin, quote

def looks_like_playlist(url: str, content_type: str = "") -> bool:
    ct = (content_type or "").lower()
    if "mpegurl" in ct:
        return True
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8")

def rewrite_m3u8_playlist(text: str, base_url: str, proxy_prefix: str) -> str:
    """Rewrite segment/variant/key URIs in an HLS playlist to go via proxy.

    Relative URLs are resolved against base_url (post-redirect upstream URL).
    Absolute http(s) URLs are passed through the proxy as-is.
    """
    def _proxied(uri: str) -> str:
        abs_url = urljoin(base_url, uri.strip())
        if abs_url.startswith("http://") or abs_url.startswith("https://"):
            # Extract extension from the upstream URL path so FFmpeg's HLS
            # demuxer recognises the segment type (e.g. .ts, .mp4, .m3u8).
            # Without this, the proxy URL /admin/px?u=... has no recognisable
            # extension and FFmpeg 7+/8+ rejects it with
            # "not in allowed_segment_extensions".
            upstream_path = urlparse(abs_url).path
            ext = posixpath.splitext(upstream_path)[1]  # e.g. ".ts", ".mp4"
            suffix = f"/seg{ext}" if ext else ""
            return proxy_prefix.replace("/px?u=", f"/px{suffix}?u=") + quote(abs_url, safe="")
        return uri

    out_lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out_lines.append(line)
        elif s.startswith("#"):
            out_lines.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{_proxied(m.group(1))}"', line))
        else:
            out_lines.append(_proxied(s))
    return "\n".join(out_lines) + "\n"

def _parse_range_request(value: Optional[str], total: Optional[int] = None) -> Optional[Tuple[int, Optional[int]]]:
    """Parse a client ``Range: bytes=start-end`` / ``bytes=start-`` header.

    Returns ``(offset, length)`` (length None = to end of file) or None when
    the header is absent, malformed or of an unsupported form (multi-range).
    Suffix ranges (``bytes=-N``, used by players for MP4 metadata) resolve
    against ``total`` when it is known; without a known total they return
    None so callers fall back to an honest 200 full response. Callers treat
    None as "serve from the start, status 200" and never emit multipart
    responses.
    """
    if not value:
        return None
    v = value.strip().lower()
    if not v.startswith("bytes="):
        return None
    spec = v[len("bytes="):].strip()
    if not spec or "," in spec:
        return None
    if spec.startswith("-"):
        return _parse_suffix_range(spec[1:], total)
    return _parse_start_range(spec)


def _parse_suffix_range(digits: str, total: Optional[int]) -> Optional[Tuple[int, Optional[int]]]:
    """Resolve ``-N`` (last N bytes) against the known total size."""
    # Suffix range: last N bytes. Needs known total to be truthful.
    try:
        suffix = int(digits.strip())
    except ValueError:
        return None
    if suffix <= 0 or total is None:
        return None
    try:
        size = int(total)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    if suffix >= size:
        return (0, size)
    return (size - suffix, suffix)


def _parse_start_range(spec: str) -> Optional[Tuple[int, Optional[int]]]:
    """Parse ``start-`` or ``start-end`` (both inclusive) into offset and length."""
    parts = spec.split("-", 1)
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0].strip())
    except ValueError:
        return None
    if start < 0:
        return None
    end_s = parts[1].strip()
    if not end_s:
        return (start, None)
    try:
        end = int(end_s)
    except ValueError:
        return None
    if end < start:
        return None
    return (start, end - start + 1)
