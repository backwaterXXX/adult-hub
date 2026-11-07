"""Process-wide sync settings: logger, environment knobs and config.yaml values.

Loaded once at import time. A missing or invalid config is fatal, see the
``SystemExit`` raised below. Other sync modules import the names from here.
"""

import logging
import os
from pathlib import Path

from common.config import (
    ConfigurationError,
    get_cache_dir,
    get_data_dir,
    get_media_dir,
    get_resolver_api_url,
    get_resolver_public_url,
    get_stream_public_url_telegram,
    parse_bool,
)
from common.logging_setup import configure_logging

logger = configure_logging("sync")

# Keep a bounded copy of sync output on the shared data volume so the admin
# panel can show progress without requiring access to container logs.
try:
    from logging.handlers import RotatingFileHandler
    _activity_log = Path(os.getenv("SYNC_ACTIVITY_LOG_PATH") or (get_cache_dir() / "sync_activity.log"))
    _activity_log.parent.mkdir(parents=True, exist_ok=True)
    if not any(getattr(handler, "baseFilename", None) == str(_activity_log.resolve()) for handler in logger.handlers):
        _activity_handler = RotatingFileHandler(
            _activity_log, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        _activity_handler.setLevel(logging.INFO)
        _activity_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(_activity_handler)
except OSError as e:
    logger.warning("Could not open sync activity log: %s", e)

# Configuration from Environment Variables
try:
    RESOLVER_PUBLIC_URL = get_resolver_public_url()  # URL exposed to playback clients
    RESOLVER_API_URL = get_resolver_api_url()        # URL for sync to fetch API
    STREAM_PUBLIC_URL_TELEGRAM = get_stream_public_url_telegram()
    MEDIA_DIR = get_media_dir()
    DB_URL = (os.getenv("DATABASE_URL") or os.getenv("STANDALONE_DATABASE_URL") or "").strip() or None
    DATA_DIR = get_data_dir()
    raw_hide = os.getenv("HIDE_DEAD_STREAMS")
    HIDE_DEAD_STREAMS = False if (raw_hide is not None and not raw_hide.strip()) else parse_bool(raw_hide, True, name="HIDE_DEAD_STREAMS")
except ConfigurationError as ce:
    logger.critical(f"FATAL CONFIG ERROR: {ce}")
    raise SystemExit(f"FATAL CONFIG ERROR: {ce}")

# Configuration from config.yaml (PLAN_KONFIGURACJI.md)
CONFIG = None
try:
    from common.config_loader import load_config, ConfigError
    CONFIG = load_config()
except ConfigError as ce:
    logger.critical(f"FATAL CONFIG ERROR: {ce}")
    raise SystemExit(f"FATAL CONFIG ERROR: {ce}")
except Exception as e:
    logger.critical(f"FATAL: could not load config: {e}")
    raise SystemExit(f"FATAL: could not load config: {e}") from e

RETENTION_DAYS = CONFIG.sync.retention_days

# Telegram plus generic upkeep knobs only.
_tg = CONFIG.get_source("telegram")
SYNC_TELEGRAM = _tg.enabled if _tg else False
TELEGRAM_CHANNELS = [ch.id for ch in _tg.channels if ch.enabled] if _tg else []
TELEGRAM_MAX_PAGES = _tg.defaults.get("max_pages", 1) if _tg else 1

JELLYFIN_URL = os.getenv("JELLYFIN_URL", "")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")

DEAD_STREAM_RESOLVER_TIMEOUT = 120.0
# Denylist entries older than this (and no longer listed) expire.
DEAD_STREAM_DENYLIST_DAYS = 90

# Hide videos whose embeds need a real browser (mirrors the resolver list;
# fetched live from /admin/api/unsupported-embeds when reachable).

PBC_BACKFILL_PER_RUN = CONFIG.sync.pbc_backfill_per_run
TPDB_BACKFILL_PER_RUN = getattr(CONFIG.sync, "tpdb_backfill_per_run", 50)
SCENE_BACKFILL_PER_RUN = getattr(CONFIG.sync, "scene_backfill_per_run", 500)
JELLYFIN_PEOPLE_PER_RUN = CONFIG.sync.jellyfin_people_per_run
