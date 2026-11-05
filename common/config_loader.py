"""Shared configuration loader and validator for Adult-Hub (sync and resolver).

Reads and validates config.yaml according to PLAN_KONFIGURACJI.md.
"""

from dataclasses import dataclass, field
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import yaml

logger = logging.getLogger(__name__)

VALID_STREAM_MODES = {"redirect", "proxy"}


class ConfigError(ValueError):
    """Raised when configuration file is missing, invalid, or violates schema."""


@dataclass
class TelegramChannelConfig:
    id: str
    enabled: bool = True
    studio: Optional[str] = None
    max_pages: int = 1
    min_duration_seconds: int = 420
    stream_mode: str = "proxy"
    history_batch_size: int = 100
    tags_whitelist: List[str] = field(default_factory=list)
    tags_blacklist: List[str] = field(default_factory=list)

    def canonical_id(self) -> str:
        """Strip @ prefix or normalize for lookup."""
        return str(self.id).strip()


@dataclass
class SourceConfig:
    provider: str
    enabled: bool = True
    address: Optional[str] = None
    stream_mode: str = "redirect"
    sections: List[str] = field(default_factory=list)
    max_pages: int = 1
    defaults: Dict[str, Any] = field(default_factory=dict)
    channels: List[TelegramChannelConfig] = field(default_factory=list)

    def get_channel(self, channel_id: str) -> Optional[TelegramChannelConfig]:
        target = str(channel_id).strip()
        for ch in self.channels:
            if ch.id == target:
                return ch
            if ch.id.lstrip("@") == target.lstrip("@"):
                return ch
            # If target has or lacks -100 prefix
            if ch.id.startswith("-100") and target == ch.id[4:]:
                return ch
            if target.startswith("-100") and ch.id == target[4:]:
                return ch
        return None


@dataclass
class SyncConfig:
    interval_minutes: int = 60
    daily_at: Optional[str] = None
    run_on_startup: bool = True
    retention_days: int = 30
    retry_backoff_base_seconds: int = 30
    retry_backoff_max_seconds: int = 1800
    stream_mode_default: str = "redirect"
    category_fetch_delay_seconds: float = 2.5
    pbc_backfill_per_run: int = 50
    tpdb_backfill_per_run: int = 50
    scene_backfill_per_run: int = 500
    jellyfin_people_per_run: int = 50
    # How long the runner waits for a sync child to reach its next channel
    # boundary after a shutdown signal, before it ends the child. Docker gives
    # the container this long plus the stop_grace_period margin in compose.
    shutdown_grace_seconds: int = 180


@dataclass
class AppConfig:
    sync: SyncConfig = field(default_factory=SyncConfig)
    sources: Dict[str, SourceConfig] = field(default_factory=dict)
    managed_revision: int = 0

    def canonical_provider_name(self, provider_name: str) -> str:
        """Return the canonical provider name (provider aliases are not supported)."""
        return provider_name

    def get_source(self, provider_name: str) -> Optional[SourceConfig]:
        p = self.canonical_provider_name(provider_name)
        return self.sources.get(p)

    def get_enabled_sources(self) -> List[SourceConfig]:
        return [s for s in self.sources.values() if s.enabled]

    def get_disabled_provider_names(self) -> Set[str]:
        return {name for name, s in self.sources.items() if not s.enabled}

    def get_telegram_channel(self, channel_id: str) -> Optional[TelegramChannelConfig]:
        tg = self.get_source("telegram")
        if not tg:
            return None
        return tg.get_channel(channel_id)

    def is_telegram_channel_enabled(self, channel_id: str) -> bool:
        tg = self.get_source("telegram")
        if not tg or not tg.enabled:
            return False
        ch = tg.get_channel(channel_id)
        if not ch:
            return False
        return ch.enabled

    def get_disabled_telegram_channel_ids(self) -> Set[str]:
        tg = self.get_source("telegram")
        if not tg:
            return set()
        disabled = set()
        for ch in tg.channels:
            if not ch.enabled or not tg.enabled:
                disabled.add(ch.id)
                if ch.id.startswith("-100"):
                    disabled.add(ch.id[4:])
                else:
                    disabled.add(f"-100{ch.id.lstrip('-')}")
        return disabled


ALLOWED_TOP_KEYS = {"sync", "sources"}
ALLOWED_TELEGRAM_KEYS = {"provider", "enabled", "defaults", "channels"}
ALLOWED_TELEGRAM_DEFAULTS = {
    "stream_mode",
    "max_pages",
    "min_duration_seconds",
    "history_batch_size",
    "tags_whitelist",
    "tags_blacklist",
}
ALLOWED_TELEGRAM_CHANNEL = {
    "id",
    "enabled",
    "studio",
    "max_pages",
    "min_duration_seconds",
    "stream_mode",
    "history_batch_size",
    "tags_whitelist",
    "tags_blacklist",
}
ALLOWED_SYNC_KEYS = {
    "interval_minutes",
    "daily_at",
    "run_on_startup",
    "retention_days",
    "retry_backoff_base_seconds",
    "retry_backoff_max_seconds",
    "stream_mode_default",
    "category_fetch_delay_seconds",
    "pbc_backfill_per_run",
    "tpdb_backfill_per_run",
    "scene_backfill_per_run",
    "jellyfin_people_per_run",
    "shutdown_grace_seconds",
}


def _validate_stream_mode(mode: Any, context: str) -> str:
    if not isinstance(mode, str) or mode.lower() not in VALID_STREAM_MODES:
        raise ConfigError(f"Invalid stream_mode '{mode}' in {context}. Must be one of {sorted(VALID_STREAM_MODES)}")
    return mode.lower()


def _validate_positive_int(value: Any, name: str, context: str, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"Field '{name}' in {context} must be an integer >= {minimum}, got {value!r}")
    try:
        val = int(value)
        if val < minimum:
            raise ValueError()
        return val
    except (TypeError, ValueError):
        raise ConfigError(f"Field '{name}' in {context} must be an integer >= {minimum}, got {value!r}")


def _validate_bool(value: Any, name: str, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"Field '{name}' in {context} must be a boolean, got {value!r}")
    return value


def _validate_string_list(value: Any, name: str, context: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ConfigError(f"Field '{name}' in {context} must be a list of non-empty strings, got {value!r}")
    return [x.strip() for x in value]


def _parse_sync_daily_at(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str):
        val = v.strip()
        m = re.fullmatch(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$", val)
        if not m:
            raise ConfigError(
                f"Field 'daily_at' in sync must be a valid time in HH:MM format (00:00-23:59), got {v!r}"
            )
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    raise ConfigError(
        f"Field 'daily_at' in sync must be a string in HH:MM format, got {v!r}"
    )


def _parse_fetch_delay(k: str, v: Any) -> float:
    try:
        delay = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"Field '{k}' in sync must be a float/number, got {v!r}")
    if not math.isfinite(delay) or delay < 0:
        raise ConfigError(f"Field '{k}' in sync must be a finite non-negative number")
    return delay


def _apply_sync_setting(sync_cfg: SyncConfig, k: str, v: Any) -> None:
    if k == "run_on_startup":
        setattr(sync_cfg, k, _validate_bool(v, k, "sync"))
    elif k == "daily_at":
        setattr(sync_cfg, k, _parse_sync_daily_at(v))
    elif k in ("category_fetch_delay_seconds",):
        setattr(sync_cfg, k, _parse_fetch_delay(k, v))
    elif k == "stream_mode_default":
        setattr(sync_cfg, k, _validate_stream_mode(v, "sync"))
    elif k in ("retention_days",):
        setattr(sync_cfg, k, _validate_positive_int(v, k, "sync", minimum=0))
    else:
        setattr(sync_cfg, k, _validate_positive_int(v, k, "sync", minimum=1))


def _parse_sync_section(sync_raw: Any) -> SyncConfig:
    if not isinstance(sync_raw, dict):
        raise ConfigError("'sync' section must be a dictionary.")
    unknown_sync = set(sync_raw.keys()) - ALLOWED_SYNC_KEYS
    if unknown_sync:
        raise ConfigError(f"Unknown key(s) in 'sync' section: {sorted(unknown_sync)}")

    sync_cfg = SyncConfig()
    for k, v in sync_raw.items():
        _apply_sync_setting(sync_cfg, k, v)
    return sync_cfg


def _parse_provider_name(idx: int, raw_src: dict) -> str:
    provider = raw_src.get("provider")
    if not provider or not isinstance(provider, str) or not provider.strip():
        raise ConfigError(f"Source at index {idx} is missing required non-empty 'provider' field.")
    provider = provider.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+", provider):
        raise ConfigError(
            f"Provider name at index {idx} must contain only lowercase letters and digits."
        )
    return provider


def _parse_telegram_defaults(raw_src: dict) -> Dict[str, Any]:
    defaults_raw = raw_src.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ConfigError("'defaults' in telegram source must be a dictionary.")
    unknown_defs = set(defaults_raw.keys()) - ALLOWED_TELEGRAM_DEFAULTS
    if unknown_defs:
        raise ConfigError(f"Unknown field(s) in telegram defaults: {sorted(unknown_defs)}")

    def_mode = _validate_stream_mode(defaults_raw.get("stream_mode", "proxy"), "telegram defaults")
    def_max_pages = _validate_positive_int(defaults_raw.get("max_pages", 1), "max_pages", "telegram defaults")
    def_min_duration = _validate_positive_int(defaults_raw.get("min_duration_seconds", 420), "min_duration_seconds", "telegram defaults", minimum=0)
    def_batch_size = _validate_positive_int(defaults_raw.get("history_batch_size", 100), "history_batch_size", "telegram defaults")
    def_tags_wl = _validate_string_list(defaults_raw.get("tags_whitelist", []), "tags_whitelist", "telegram defaults") if "tags_whitelist" in defaults_raw else []
    def_tags_bl = _validate_string_list(defaults_raw.get("tags_blacklist", []), "tags_blacklist", "telegram defaults") if "tags_blacklist" in defaults_raw else []

    return {
        "stream_mode": def_mode,
        "max_pages": def_max_pages,
        "min_duration_seconds": def_min_duration,
        "history_batch_size": def_batch_size,
        "tags_whitelist": def_tags_wl,
        "tags_blacklist": def_tags_bl,
    }


def _parse_telegram_channel(ch_idx: int, ch_raw: Any, defaults: Dict[str, Any],
                            seen_channel_ids: Set[str]) -> TelegramChannelConfig:
    def_mode = defaults["stream_mode"]
    def_max_pages = defaults["max_pages"]
    def_min_duration = defaults["min_duration_seconds"]
    def_batch_size = defaults["history_batch_size"]
    def_tags_wl = defaults["tags_whitelist"]
    def_tags_bl = defaults["tags_blacklist"]
    if not isinstance(ch_raw, dict):
        raise ConfigError(f"Channel at index {ch_idx} in telegram source must be a dictionary.")
    unknown_ch = set(ch_raw.keys()) - ALLOWED_TELEGRAM_CHANNEL
    if unknown_ch:
        raise ConfigError(f"Unknown field(s) in telegram channel at index {ch_idx}: {sorted(unknown_ch)}")

    ch_id = ch_raw.get("id")
    if ch_id is None or str(ch_id).strip() == "":
        raise ConfigError(f"Telegram channel at index {ch_idx} requires non-empty 'id'.")
    ch_id_str = str(ch_id).strip()

    if ch_id_str in seen_channel_ids:
        raise ConfigError(f"Duplicate telegram channel id '{ch_id_str}' in telegram source.")
    seen_channel_ids.add(ch_id_str)

    ch_enabled = _validate_bool(ch_raw.get("enabled", True), "enabled", f"telegram channel '{ch_id_str}'")
    studio = ch_raw.get("studio")
    if studio is not None and not isinstance(studio, str):
        raise ConfigError(f"Field 'studio' in telegram channel '{ch_id_str}' must be a string.")
    studio_str = studio.strip() if studio else None

    mode = _validate_stream_mode(ch_raw.get("stream_mode", def_mode), f"telegram channel '{ch_id_str}'")
    max_pages = _validate_positive_int(ch_raw.get("max_pages", def_max_pages), "max_pages", f"telegram channel '{ch_id_str}'")
    min_dur = _validate_positive_int(ch_raw.get("min_duration_seconds", def_min_duration), "min_duration_seconds", f"telegram channel '{ch_id_str}'", minimum=0)
    batch_size = _validate_positive_int(ch_raw.get("history_batch_size", def_batch_size), "history_batch_size", f"telegram channel '{ch_id_str}'")

    tags_wl = _validate_string_list(ch_raw["tags_whitelist"], "tags_whitelist", f"telegram channel '{ch_id_str}'") if "tags_whitelist" in ch_raw else list(def_tags_wl)
    tags_bl = _validate_string_list(ch_raw["tags_blacklist"], "tags_blacklist", f"telegram channel '{ch_id_str}'") if "tags_blacklist" in ch_raw else list(def_tags_bl)

    return TelegramChannelConfig(
        id=ch_id_str,
        enabled=ch_enabled,
        studio=studio_str,
        max_pages=max_pages,
        min_duration_seconds=min_dur,
        stream_mode=mode,
        history_batch_size=batch_size,
        tags_whitelist=tags_wl,
        tags_blacklist=tags_bl,
    )


def _parse_telegram_source(raw_src: dict, provider: str) -> SourceConfig:
    unknown_tg = set(raw_src.keys()) - ALLOWED_TELEGRAM_KEYS
    if "address" in raw_src:
        raise ConfigError("Telegram source does not take 'address'; configure channels with 'id' instead.")
    if unknown_tg:
        raise ConfigError(f"Unknown field(s) in telegram source: {sorted(unknown_tg)}")

    tg_enabled = _validate_bool(raw_src.get("enabled", True), "enabled", "source 'telegram'")
    validated_defaults = _parse_telegram_defaults(raw_src)

    channels_raw = raw_src.get("channels", [])
    if not isinstance(channels_raw, list):
        raise ConfigError("'channels' in telegram source must be a list.")

    channels_list: List[TelegramChannelConfig] = []
    seen_channel_ids: Set[str] = set()
    for ch_idx, ch_raw in enumerate(channels_raw):
        channels_list.append(_parse_telegram_channel(ch_idx, ch_raw, validated_defaults, seen_channel_ids))

    return SourceConfig(
        provider=provider,
        enabled=tg_enabled,
        address=None,
        stream_mode=validated_defaults["stream_mode"],
        defaults=validated_defaults,
        channels=channels_list,
    )


def parse_and_validate_config(data: Any) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigError("Configuration file root must be a YAML dictionary.")

    unknown_top = set(data.keys()) - ALLOWED_TOP_KEYS
    if unknown_top:
        raise ConfigError(f"Unknown top-level configuration key(s): {sorted(unknown_top)}")

    # 1. Parse Sync Config
    sync_cfg = _parse_sync_section(data.get("sync", {}))

    # 2. Parse Sources
    sources_raw = data.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigError("'sources' section must be a list of sources.")

    sources_map: Dict[str, SourceConfig] = {}
    for idx, raw_src in enumerate(sources_raw):
        if not isinstance(raw_src, dict):
            raise ConfigError(f"Source at index {idx} must be a dictionary.")

        provider = _parse_provider_name(idx, raw_src)

        if provider in sources_map:
            raise ConfigError(f"Duplicate provider '{provider}' found in sources. Each provider can only have one configuration entry.")

        if provider == "telegram":
            sources_map[provider] = _parse_telegram_source(raw_src, provider)
        else:
            # Adult Hub is Telegram-only: every other provider is unsupported.
            raise ConfigError(
                f"Unsupported source provider '{provider}'. Adult Hub is Telegram-only; "
                "remove this source entry or configure the 'telegram' provider."
            )

    return AppConfig(sync=sync_cfg, sources=sources_map)


def find_config_file(explicit_path: Optional[str] = None) -> Path:
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("/config/config.yaml"))
    candidates.append(Path("/app/config.yaml"))
    candidates.append(Path(__file__).parent / "config.yaml")
    candidates.append(Path.cwd() / "config.yaml")

    for p in candidates:
        if p.is_file():
            return p.resolve()

    tried = [str(c) for c in candidates]
    raise ConfigError(f"Configuration file 'config.yaml' not found. Tried paths: {tried}")


_CACHED_CONFIG: Optional[AppConfig] = None
_CACHED_CONFIG_KEY: Optional[tuple] = None


def _config_file_signature(path: Path) -> tuple | None:
    """Return a cheap invalidation key for an atomically replaced config file."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (str(path.resolve()), stat.st_ino, stat.st_size, stat.st_mtime_ns)


def load_config(config_path: Optional[str] = None, force_reload: bool = False) -> AppConfig:
    global _CACHED_CONFIG, _CACHED_CONFIG_KEY
    from common.managed_sources import overrides_path
    overlay_path = overrides_path()
    file_path = find_config_file(config_path)
    cacheable = config_path is None
    base_signature = _config_file_signature(file_path)
    overlay_signature = _config_file_signature(overlay_path)
    cache_key = (base_signature, overlay_signature)
    if (cacheable and _CACHED_CONFIG is not None and not force_reload
            and _CACHED_CONFIG_KEY == cache_key):
        return _CACHED_CONFIG

    has_managed_overlay = overlay_signature is not None
    try:
        content = file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if data is None:
            data = {}
    except Exception as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError(f"Failed to read/parse YAML in {file_path}: {e}") from e

    revision = 0
    if has_managed_overlay:
        from common.managed_sources import apply_overrides, read_overrides
        overlay = read_overrides(overlay_path)
        revision = overlay["revision"]
        data = apply_overrides(data, overlay)
    config = parse_and_validate_config(data)
    config.managed_revision = revision
    if cacheable:
        _CACHED_CONFIG = config
        _CACHED_CONFIG_KEY = cache_key
    logger.info(f"Loaded and validated configuration from {file_path}")
    return config
