"""Process-wide sync settings: logger, environment knobs and config.yaml values.

Loaded once at import time. A missing or invalid config is fatal, see the
``SystemExit`` raised below. Other sync modules import the names from here.
"""

import logging
import os
from pathlib import Path

from common.logging_setup import configure_logging

logger = configure_logging("sync")

# Keep a bounded copy of sync output on the shared data volume so the admin
# panel can show progress without requiring access to container logs.
try:
    from logging.handlers import RotatingFileHandler
    _activity_log = Path(os.getenv("SYNC_ACTIVITY_LOG_PATH", "/data/cache/sync_activity.log"))
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
RESOLVER_PUBLIC_URL = os.getenv("RESOLVER_PUBLIC_URL", "http://127.0.0.1:8090") # URL exposed to playback clients
RESOLVER_API_URL = os.getenv("RESOLVER_API_URL", "http://localhost:8090")          # URL for sync to fetch API
STREAM_PUBLIC_URL_TELEGRAM = os.getenv("STREAM_PUBLIC_URL_TELEGRAM", "").strip()
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./media/adult")).resolve()
DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "./data")).resolve()

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

HIDE_DEAD_STREAMS = os.getenv("HIDE_DEAD_STREAMS", "true").lower() in ["true", "1", "yes"]
DEAD_STREAM_RESOLVER_TIMEOUT = 120.0
# Denylist entries older than this (and no longer listed) expire.
DEAD_STREAM_DENYLIST_DAYS = 90

# Hide videos whose embeds need a real browser (mirrors the resolver list;
# fetched live from /admin/api/unsupported-embeds when reachable).

PBC_BACKFILL_PER_RUN = CONFIG.sync.pbc_backfill_per_run
TPDB_BACKFILL_PER_RUN = getattr(CONFIG.sync, "tpdb_backfill_per_run", 50)
SCENE_BACKFILL_PER_RUN = getattr(CONFIG.sync, "scene_backfill_per_run", 500)
JELLYFIN_PEOPLE_PER_RUN = CONFIG.sync.jellyfin_people_per_run
