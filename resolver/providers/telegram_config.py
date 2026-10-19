"""Telegram environment and channel configuration helpers."""
import logging
import os

from common.duration import MIN_VIDEO_DURATION_SECONDS
from typing import Any, List, Optional

logger = logging.getLogger("providers.telegram")

def _get_api_id() -> int:
    raw = (os.getenv("TELEGRAM_API_ID") or "").strip()
    if not raw:
        raise RuntimeError(
            "Telegram provider is not configured: set TELEGRAM_API_ID env var."
        )
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            "Telegram provider is not configured: TELEGRAM_API_ID must be numeric."
        ) from None


def _get_api_hash() -> str:
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_hash:
        raise RuntimeError(
            "Telegram provider is not configured: set TELEGRAM_API_HASH env var."
        )
    return api_hash


def _get_session_path() -> str:
    return (os.getenv("TELEGRAM_SESSION_PATH") or "/data/telegram.session").strip() \
        or "/data/telegram.session"


def _get_config():
    try:
        from common.config_loader import load_config
        return load_config()
    except Exception as e:
        logger.debug(f"Could not load config_loader in telegram provider: {e}")
        return None


def _get_channels() -> List[str]:
    channels: List[str] = []
    cfg = _get_config()
    if cfg:
        tg = cfg.get_source("telegram")
        if tg and tg.enabled:
            channels.extend(ch.id for ch in tg.channels if ch.enabled)
    raw = os.getenv("TELEGRAM_CHANNELS") or ""
    for c in raw.split(","):
        c = c.strip()
        if c and c not in channels:
            channels.append(c)
    return channels


def is_channel_authorized(channel_ref: Any) -> bool:
    """True if channel_ref belongs to configured and enabled channels.

    Rejects 'me', 'self', empty values, and channels not present in the
    authorized channel whitelist.
    """
    if channel_ref is None:
        return False
    ref_clean = str(channel_ref).strip()
    if not ref_clean or ref_clean.lower() in ("me", "self"):
        return False

    allowed = _get_channels()
    if not allowed:
        return False

    ref_norm = ref_clean.lower()
    for ch in allowed:
        c = str(ch).strip().lower()
        if not c:
            continue
        if c == ref_norm:
            return True
        if c.lstrip("@") == ref_norm.lstrip("@"):
            return True
        # Handle -100 prefix differences for numeric channel IDs
        c_bare = c[4:] if c.startswith("-100") else (c[1:] if c.startswith("-") else c)
        r_bare = ref_norm[4:] if ref_norm.startswith("-100") else (ref_norm[1:] if ref_norm.startswith("-") else ref_norm)
        if c_bare and r_bare and c_bare == r_bare and c_bare.isdigit():
            return True

    return False


def _get_max_history() -> int:
    try:
        value = int((os.getenv("TELEGRAM_MAX_HISTORY") or "1").strip())
    except ValueError:
        return 1
    return max(1, value)


def _get_min_duration() -> int:
    """Minimum video duration in seconds (default 420 = 7 min)."""
    try:
        value = int((os.getenv("TELEGRAM_MIN_DURATION_SECONDS") or "420").strip())
    except ValueError:
        return 420
    return max(MIN_VIDEO_DURATION_SECONDS, value)


def passes_duration_filter(duration: int, min_duration: Optional[int] = None) -> bool:
    """True when video can be included in the library: unknown (0) or >= minimum.

    Short clips (0 < duration < minimum) are dropped. Unknown duration (0)
    is allowed through to avoid dropping entire channels if metadata parsing fails.
    """
    if duration is None:
        return True
    try:
        seconds = int(duration)
    except (TypeError, ValueError):
        return True
    if seconds <= 0:
        return True
    threshold = max(
        MIN_VIDEO_DURATION_SECONDS,
        min_duration if min_duration is not None else _get_min_duration(),
    )
    return seconds >= threshold


def _entity_ref(channel_id) -> object:
    """Channel identifier in form accepted by Telethon get_entity."""
    text = str(channel_id).strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return text


# ---------------------------------------------------------------------------
# Pure helper functions (no network, testable)
# ---------------------------------------------------------------------------
