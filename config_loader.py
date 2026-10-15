"""Backward-compatibility shim: the loader now lives in ``common``.

Canonical import (use in all new code)::

    from common.config_loader import load_config, ConfigError, parse_and_validate_config

This module re-exports the same names so older ``import config_loader``
statements keep working on sys.path layouts that include the repo root.
"""

from common.config_loader import (
    ALLOWED_SYNC_KEYS,
    ALLOWED_TELEGRAM_CHANNEL,
    ALLOWED_TELEGRAM_DEFAULTS,
    ALLOWED_TELEGRAM_KEYS,
    ALLOWED_TOP_KEYS,
    ALLOWED_WWW_KEYS,
    VALID_STREAM_MODES,
    AppConfig,
    ConfigError,
    SourceConfig,
    SyncConfig,
    TelegramChannelConfig,
    find_config_file,
    load_config,
    parse_and_validate_config,
)

__all__ = [
    "ALLOWED_SYNC_KEYS",
    "ALLOWED_TELEGRAM_CHANNEL",
    "ALLOWED_TELEGRAM_DEFAULTS",
    "ALLOWED_TELEGRAM_KEYS",
    "ALLOWED_TOP_KEYS",
    "ALLOWED_WWW_KEYS",
    "VALID_STREAM_MODES",
    "AppConfig",
    "ConfigError",
    "SourceConfig",
    "SyncConfig",
    "TelegramChannelConfig",
    "find_config_file",
    "load_config",
    "parse_and_validate_config",
]
