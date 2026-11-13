"""Centralized configuration defaults, environment getters, and validation.

Shared across resolver, sync, backup, and admin services.
Preserves legacy aliases and produces actionable error messages on misconfiguration.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


# -----------------------------------------------------------------------------
# Canonical Defaults
# -----------------------------------------------------------------------------

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_TZ = "Europe/Warsaw"

# Storage & Runtime Paths
DEFAULT_MEDIA_DIR = "/media/adult"
DEFAULT_DATA_DIR = "/data"
DEFAULT_CACHE_DIR = "/cache"
DEFAULT_CONFIG_PATH = "/config/config.yaml"
DEFAULT_ADMIN_SOURCE_OVERRIDES_PATH = "/data/admin-source-overrides.json"
DEFAULT_ADMIN_WORKER_CAPABILITIES_PATH = "/data/cache/worker-capabilities.json"
DEFAULT_LINKCHECK_LOG_PATH = "/data/linkcheck.log"
DEFAULT_SYNC_ACTIVITY_LOG_PATH = "/data/cache/sync_activity.log"
DEFAULT_SYNC_HISTORY_PATH = "/data/cache/sync_history.json"
DEFAULT_SYNC_STATUS_PATH = "/data/cache/sync_status.json"
DEFAULT_SYNC_TRIGGER_PATH = "/data/cache/sync.trigger"

# Database & Backups
DEFAULT_BACKUP_DIR = "/data/backups"
DEFAULT_BACKUP_KEEP_DAYS = 7
DEFAULT_BACKUP_DAILY_AT = "04:30"
DEFAULT_BACKUP_LOCK_PATH = "/data/cache/backup.lock"

# Networking & Routing
DEFAULT_HOST_IP = "127.0.0.1"
DEFAULT_RESOLVER_PORT = 8090
DEFAULT_JELLYFIN_PORT = 8096
DEFAULT_RESOLVER_PUBLIC_URL = "http://127.0.0.1:8090"
DEFAULT_RESOLVER_API_URL = "http://resolver:8090"
DEFAULT_PUBLIC_DOMAIN = "media.example.org"
DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1,::1"

# Access & Security
DEFAULT_ENABLE_ADMIN = True
DEFAULT_ADMIN_PUBLIC = False

# Concurrency & Caching
DEFAULT_RESOLVER_CONCURRENCY_LIMIT = 10
DEFAULT_CACHE_TTL_SECONDS = 1500
DEFAULT_MAX_CACHE_ENTRIES = 2000

# Supervisor Watchdog
DEFAULT_RESOLVER_WATCHDOG_INTERVAL_SECONDS = 10.0
DEFAULT_RESOLVER_WATCHDOG_TIMEOUT_SECONDS = 4.0
DEFAULT_RESOLVER_WATCHDOG_FAILURES = 3
DEFAULT_RESOLVER_WATCHDOG_STARTUP_GRACE_SECONDS = 45.0

# Telegram Provider Tuning
DEFAULT_TELEGRAM_SESSION_PATH = "/data/telegram.session"
DEFAULT_TELEGRAM_PARALLEL_STREAM_ENABLED = False
DEFAULT_TELEGRAM_DOWNLOAD_WORKERS = 3
DEFAULT_TELEGRAM_DOWNLOAD_GLOBAL_WORKERS = 6
DEFAULT_TELEGRAM_DOWNLOAD_BLOCK_BYTES = 524288  # 512 KB
DEFAULT_TELEGRAM_READ_AHEAD_BYTES = 33554432  # 32 MB
DEFAULT_TELEGRAM_STREAM_CACHE_BYTES = 134217728  # 128 MB
DEFAULT_TELEGRAM_STREAM_CACHE_TTL_SECONDS = 120

# Telegram client connection and request timeouts (seconds)
DEFAULT_TELEGRAM_CONNECTION_TIMEOUT_SECONDS = 10
DEFAULT_TELEGRAM_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_TELEGRAM_CONNECTION_RETRIES = 3
DEFAULT_TELEGRAM_REQUEST_RETRIES = 3
DEFAULT_TELEGRAM_FLOOD_SLEEP_THRESHOLD = 60

# Telegram download timeouts (seconds)
DEFAULT_TELEGRAM_DOWNLOAD_BLOCK_TIMEOUT = 300
DEFAULT_TELEGRAM_AVAILABILITY_CHECKS_PER_RUN = 20
DEFAULT_TELEGRAM_MIN_DURATION_SECONDS = 420
DEFAULT_TELEGRAM_MAX_HISTORY = 1

# Fingerprint & pHash Budgets
DEFAULT_TELEGRAM_FINGERPRINTS_ENABLED = True
DEFAULT_TELEGRAM_OSHASH_BACKFILL_PER_RUN = 500
DEFAULT_TELEGRAM_PHASH_ENABLED = True
DEFAULT_TELEGRAM_PHASH_BYTE_BUDGET = 268435456  # 256 MB
DEFAULT_TELEGRAM_PHASH_TIMEOUT_SECONDS = 1200  # 20 minutes
DEFAULT_TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET = 30000000000  # 30 GB
DEFAULT_TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND = 2097152  # 2 MB/s
DEFAULT_TELEGRAM_FINGERPRINT_USAGE_PATH = "/data/cache/telegram-fingerprint-daily-usage.json"

# Visual Detection & pHash Intake
DEFAULT_VIDEO_VISUAL_WORKER_ENABLED = False
DEFAULT_PHASH_UPLOAD_ENABLED = False
DEFAULT_PHASH_SERVER_BYTE_BUDGET = 268435456
DEFAULT_PHASH_SERVER_TIMEOUT_SECONDS = 1200
DEFAULT_PHASH_SERVER_BYTES_PER_SECOND = 2097152
DEFAULT_PHASH_SERVER_IDLE_BYTES_PER_SECOND = 10000000

# Duplicate Auto-Merge
DEFAULT_DUPLICATE_AUTO_MERGE_ENABLED = False
DEFAULT_DUPLICATE_AUTO_MERGE_TELEGRAM_FILE_ID = True
DEFAULT_HIDE_DEAD_STREAMS = True

# Metadata Providers
DEFAULT_STASHDB_API_URL = "https://stashdb.org/graphql"

# Admin v2 Issues
DEFAULT_ADMIN_V2_MONITOR_BACKUPS = False
DEFAULT_ADMIN_V2_QUEUE_WARNING_SECONDS = 3600
DEFAULT_ADMIN_V2_BACKUP_WARNING_SECONDS = 129600


# -----------------------------------------------------------------------------
# Telegram Client Factory Helpers
# -----------------------------------------------------------------------------

def telegram_client_kwargs() -> dict[str, Any]:
    """Return the Telethon ``TelegramClient`` keyword arguments shared by every client.

    Telethon 1.x accepts ``timeout`` as the *connection* timeout only (``int`` or
    ``float``); it has no request-timeout constructor argument. Callers that need a
    request deadline enforce it with ``asyncio.wait_for``.
    """
    return {
        "timeout": DEFAULT_TELEGRAM_CONNECTION_TIMEOUT_SECONDS,
        "connection_retries": DEFAULT_TELEGRAM_CONNECTION_RETRIES,
        "request_retries": DEFAULT_TELEGRAM_REQUEST_RETRIES,
        "flood_sleep_threshold": DEFAULT_TELEGRAM_FLOOD_SLEEP_THRESHOLD,
    }


# -----------------------------------------------------------------------------
# Validation & Parsing Helpers
# -----------------------------------------------------------------------------

class ConfigurationError(ValueError):
    """Raised when an environment variable or setting fails validation."""


def parse_bool(value: Any, default: bool = False, *, name: str = "setting") -> bool:
    """Parse common boolean string representations.

    Raises ConfigurationError on non-empty invalid boolean representations to fail safely.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    clean = str(value).strip().lower()
    if not clean:
        return default
    if clean in ("1", "true", "yes", "on"):
        return True
    if clean in ("0", "false", "no", "off"):
        return False
    raise ConfigurationError(
        f"Setting '{name}' must be a boolean (true/false/1/0/yes/no/on/off), got {value!r}"
    )


def parse_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    name: str = "setting",
) -> int:
    """Parse integer with bounds checking and actionable errors."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigurationError(f"Setting '{name}' must be an integer, got boolean {value!r}")
    clean = str(value).strip()
    if not clean:
        return default
    try:
        parsed = int(clean)
    except (TypeError, ValueError) as err:
        raise ConfigurationError(f"Setting '{name}' must be an integer, got {value!r}") from err

    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"Setting '{name}' must be >= {minimum}, got {parsed}")
    if maximum is not None and parsed > maximum:
        raise ConfigurationError(f"Setting '{name}' must be <= {maximum}, got {parsed}")
    return parsed


def parse_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    name: str = "setting",
) -> float:
    """Parse float with bounds checking and actionable errors."""
    if value is None:
        return default
    clean = str(value).strip()
    if not clean:
        return default
    try:
        parsed = float(clean)
    except (TypeError, ValueError) as err:
        raise ConfigurationError(f"Setting '{name}' must be a number, got {value!r}") from err

    if not math.isfinite(parsed):
        raise ConfigurationError(f"Setting '{name}' must be a finite number, got {value!r}")

    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"Setting '{name}' must be >= {minimum}, got {parsed}")
    return parsed


def parse_daily_at(value: Any, default: str = DEFAULT_BACKUP_DAILY_AT, *, name: str = "daily_at") -> str:
    """Validate daily time in HH:MM format (00:00 - 23:59)."""
    if value is None:
        return default
    clean = str(value).strip()
    if not clean:
        return default
    m = re.fullmatch(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$", clean)
    if not m:
        raise ConfigurationError(
            f"Setting '{name}' must be in 24-hour HH:MM format (00:00-23:59), got {value!r}"
        )
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"


def parse_url(value: Any, default: str = "", *, name: str = "URL") -> str:
    """Validate HTTP or HTTPS URL format."""
    if value is None:
        return default
    clean = str(value).strip()
    if not clean:
        return default
    parsed = urlsplit(clean)
    if parsed.scheme not in ("http", "https"):
        raise ConfigurationError(
            f"Setting '{name}' must be a valid HTTP or HTTPS URL, got {value!r}"
        )
    if not parsed.netloc:
        raise ConfigurationError(
            f"Setting '{name}' is missing a valid host: {value!r}"
        )
    return clean


# -----------------------------------------------------------------------------
# Environment Getters with Legacy Alias Support
# -----------------------------------------------------------------------------

def get_database_url(
    *,
    test: bool = False,
    allow_test_fallback: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the configured PostgreSQL connection URL.

    Checks DATABASE_URL then legacy STANDALONE_DATABASE_URL.
    When test=True, checks TEST_DATABASE_URL only, preventing tests from targeting
    production by default unless allow_test_fallback=True is explicitly passed.
    Never exposes credentials in error messages.
    """
    env_map = os.environ if env is None else env
    key = "TEST_DATABASE_URL" if test else "DATABASE_URL"
    url = (env_map.get(key) or "").strip()
    if not url and not test:
        url = (env_map.get("STANDALONE_DATABASE_URL") or "").strip()
    if not url and test and allow_test_fallback and env_map.get("DATABASE_URL"):
        return env_map["DATABASE_URL"]
    if not url:
        raise RuntimeError(
            f"{key} is not configured. Configure PostgreSQL connection, e.g. "
            "postgresql://username:password@hostname:5432/database"
        )
    return url


def get_admin_write_token(env: Mapping[str, str] | None = None) -> str:
    """Return the admin mutation bearer token.

    Accepts ADMIN_WRITE_TOKEN with legacy fallback to ADMIN_DUPLICATE_TOKEN.
    Fails closed if the configured token is whitespace-only.
    """
    env_map = os.environ if env is None else env
    raw = env_map.get("ADMIN_WRITE_TOKEN")
    if raw is not None and raw != "" and not raw.strip():
        raise ConfigurationError(
            "Setting 'ADMIN_WRITE_TOKEN' cannot be whitespace-only. "
            "Configure a valid token or leave empty/unset to enable unauthenticated admin writes."
        )
    legacy_raw = env_map.get("ADMIN_DUPLICATE_TOKEN")
    if legacy_raw is not None and legacy_raw != "" and not legacy_raw.strip():
        raise ConfigurationError(
            "Setting 'ADMIN_DUPLICATE_TOKEN' cannot be whitespace-only. "
            "Configure a valid token or leave empty/unset to enable unauthenticated admin writes."
        )
    return (raw or "").strip() or (legacy_raw or "").strip()


def get_admin_public(env: Mapping[str, str] | None = None) -> bool:
    """Return whether admin management routes are exposed publicly."""
    env_map = os.environ if env is None else env
    raw = env_map.get("ADMIN_PUBLIC")
    if raw is None or raw == "":
        raw = env_map.get("STANDALONE_ADMIN_PUBLIC")
    return parse_bool(raw, DEFAULT_ADMIN_PUBLIC, name="ADMIN_PUBLIC")


def get_stashdb_api_key(env: Mapping[str, str] | None = None) -> str:
    """Return StashDB API key with legacy fallback to STASH_API_KEY."""
    env_map = os.environ if env is None else env
    key = (env_map.get("STASHDB_API_KEY") or "").strip()
    if not key:
        key = (env_map.get("STASH_API_KEY") or "").strip()
    return key


def get_theporndb_api_token(env: Mapping[str, str] | None = None) -> str:
    """Return ThePornDB API token with alternative fallback to THEPORNDB_API_KEY."""
    env_map = os.environ if env is None else env
    token = (env_map.get("THEPORNDB_API_TOKEN") or "").strip()
    if not token:
        token = (env_map.get("THEPORNDB_API_KEY") or "").strip()
    return token


def get_stream_public_url_telegram(env: Mapping[str, str] | None = None) -> str:
    """Return Telegram stream public URL, falling back to RESOLVER_PUBLIC_URL."""
    env_map = os.environ if env is None else env
    url = (env_map.get("STREAM_PUBLIC_URL_TELEGRAM") or "").strip()
    if not url:
        url = (env_map.get("RESOLVER_PUBLIC_URL") or DEFAULT_RESOLVER_PUBLIC_URL).strip()
    return parse_url(url, DEFAULT_RESOLVER_PUBLIC_URL, name="STREAM_PUBLIC_URL_TELEGRAM")


def get_resolver_public_url(env: Mapping[str, str] | None = None) -> str:
    """Return client-facing resolver URL for media stream links."""
    env_map = os.environ if env is None else env
    raw = (env_map.get("RESOLVER_PUBLIC_URL") or DEFAULT_RESOLVER_PUBLIC_URL).strip()
    return parse_url(raw, DEFAULT_RESOLVER_PUBLIC_URL, name="RESOLVER_PUBLIC_URL")


def get_resolver_api_url(env: Mapping[str, str] | None = None) -> str:
    """Return in-stack resolver API URL for sync runner to query resolver."""
    env_map = os.environ if env is None else env
    raw = (env_map.get("RESOLVER_API_URL") or DEFAULT_RESOLVER_API_URL).strip()
    return parse_url(raw, DEFAULT_RESOLVER_API_URL, name="RESOLVER_API_URL")


def get_media_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return resolved media directory Path."""
    env_map = os.environ if env is None else env
    raw = env_map.get("MEDIA_DIR") or env_map.get("MEDIA_PATH")
    if raw:
        return Path(raw).resolve()
    # In container, prefer /media/adult if exists; otherwise local ./media/adult
    if Path(DEFAULT_MEDIA_DIR).exists():
        return Path(DEFAULT_MEDIA_DIR).resolve()
    return Path("./media/adult").resolve()


def get_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return resolved data directory Path."""
    env_map = os.environ if env is None else env
    raw = env_map.get("DATA_DIR")
    if raw:
        return Path(raw).resolve()
    if Path(DEFAULT_DATA_DIR).exists():
        return Path(DEFAULT_DATA_DIR).resolve()
    return Path("./data").resolve()


def get_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return resolved cache directory Path."""
    env_map = os.environ if env is None else env
    raw = env_map.get("CACHE_DIR")
    if raw:
        return Path(raw).resolve()
    if Path(DEFAULT_CACHE_DIR).exists():
        return Path(DEFAULT_CACHE_DIR).resolve()
    return get_data_dir(env=env) / "cache"


def get_backup_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return resolved backup directory Path."""
    env_map = os.environ if env is None else env
    raw = env_map.get("BACKUP_DIR")
    if raw:
        return Path(raw).resolve()
    if Path(DEFAULT_BACKUP_DIR).exists():
        return Path(DEFAULT_BACKUP_DIR).resolve()
    return get_data_dir(env=env) / "backups"


def get_backup_keep_days(env: Mapping[str, str] | None = None) -> int:
    """Return backup retention in days (minimum 1)."""
    env_map = os.environ if env is None else env
    return parse_int(
        env_map.get("BACKUP_KEEP_DAYS"),
        DEFAULT_BACKUP_KEEP_DAYS,
        minimum=1,
        name="BACKUP_KEEP_DAYS",
    )


def get_backup_daily_at(env: Mapping[str, str] | None = None) -> str:
    """Return daily backup time string in HH:MM format."""
    env_map = os.environ if env is None else env
    return parse_daily_at(env_map.get("BACKUP_DAILY_AT"), DEFAULT_BACKUP_DAILY_AT, name="BACKUP_DAILY_AT")
