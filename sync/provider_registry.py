from __future__ import annotations

import os
from typing import Any, Dict, Optional, Set


STREAM_MODE_DEFAULT = os.getenv("STREAM_MODE_DEFAULT", "redirect")
_STREAM_MODE_PER_PROVIDER = {
    "telegram": os.getenv("STREAM_MODE_TELEGRAM", "proxy"),
}


def stream_mode_for_provider(video_id: str, config: Optional[Any] = None) -> str:
    provider = video_id.partition(":")[0]
    if config is not None:
        src = config.get_source(provider)
        if src:
            return src.stream_mode
        return config.sync.stream_mode_default
    mode = _STREAM_MODE_PER_PROVIDER.get(provider, STREAM_MODE_DEFAULT)
    return mode if mode in ("redirect", "proxy") else "redirect"


# ---------------------------------------------------------------------------
# Provider metadata (Telegram-only core registry).
# ---------------------------------------------------------------------------

_PROVIDER_INFO: Dict[str, Dict[str, str]] = {
    "telegram": {
        "name": "Telegram",
        "domain": "t.me",
        "base_url": "https://t.me"
    }
}

def get_provider_info(provider_key: str) -> Dict[str, str]:
    """Metadata for NFO/studio files; historical names survive via fallback."""
    key = (provider_key or "").strip().lower()
    if key in _PROVIDER_INFO:
        return _PROVIDER_INFO[key]
    return {"name": provider_key.capitalize(), "domain": provider_key, "base_url": ""}


def known_provider_names() -> Set[str]:
    """Providers the core recognises (Telegram-only)."""
    return set(_PROVIDER_INFO)


# ---------------------------------------------------------------------------
# Retention guard: single gate for destructive upkeep.
# ---------------------------------------------------------------------------

def normalize_cleanup_provider(name: str, config: Optional[Any] = None) -> str:
    """Canonical provider key for cleanup decisions."""
    p = (name or "").strip().lower()
    if config is not None:
        p = config.canonical_provider_name(p)
    return p


def _telegram_channel_of_video(video_id: str) -> Optional[str]:
    """Channel segment of a telegram composite id (telegram:<chan>:...)."""
    parts = (video_id or "").split(":")
    if len(parts) > 2 and parts[0] == "telegram" and parts[1]:
        return parts[1]
    return None


def _normalized_success_set(providers_succeeded: Optional[Set[str]], config: Optional[Any] = None) -> Set[str]:
    out: Set[str] = set()
    for entry in providers_succeeded or ():
        e = (entry or "").strip()
        if not e:
            continue
        if e.startswith("telegram:"):
            out.add("telegram:" + e[len("telegram:"):].strip())
        else:
            out.add(normalize_cleanup_provider(e, config=config))
    return out


def is_cleanup_allowed(video_id: str, provider: Optional[str] = None,
                       providers_succeeded: Optional[Set[str]] = None,
                       config: Optional[Any] = None) -> bool:
    """Single gate allowing destructive upkeep on historical rows/files/URLs.

    Allowed only when the provider/channel is SIMULTANEOUSLY:
      (a) registered (known in known_provider_names()),
      (b) enabled in the effective configuration (present in config.sources
          and enabled; telegram additionally requires the channel to be
          present and per-channel enabled),
      (c) successfully synced in the current run (present in
          ``providers_succeeded`` collected by run_sync; telegram accepts
          channel-specific ``"telegram:<channel>"`` entry with -100-prefix
          variants. The generic ``"telegram"`` entry is only usable when a
          video ID does not identify its channel.

    ``providers_succeeded=None`` (standalone/manual call without a run
    signal) means PROTECT: returns False, nothing is deleted or rewritten.
    """
    if providers_succeeded is None:
        return False
    prov = normalize_cleanup_provider(provider or video_id.split(":", 1)[0], config=config)
    if prov not in known_provider_names():
        return False
    if config is None:
        return False
    src = config.get_source(prov)
    if src is None or not src.enabled:
        return False
    ok = _normalized_success_set(providers_succeeded, config=config)
    if prov == "telegram":
        chan = _telegram_channel_of_video(video_id)
        if chan is None:
            return "telegram" in ok
        if not config.is_telegram_channel_enabled(chan):
            return False
        cands = {chan, chan.lstrip("@")}
        if chan.startswith("-100"):
            cands.add(chan[4:])
        else:
            cands.add("-100" + chan.lstrip("-"))
        return any(f"telegram:{c}" in ok for c in cands)
    return prov in ok
