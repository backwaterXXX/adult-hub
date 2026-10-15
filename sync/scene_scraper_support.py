"""Shared HTTP and parsing dependencies for provider scene scrapers."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from common.http_rate_limit import request_with_host_limit

try:
    import httpx
except ImportError:
    httpx = None

try:
    from sync.scene_scraper_models import ScrapedScene
except ModuleNotFoundError:
    from scene_scraper_models import ScrapedScene

logger = logging.getLogger("scene_scrapers")
DEFAULT_TIMEOUT = 12.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
GAMMA_IMAGE_CDN = "https://images03-fame.gammacdn.com"

__all__ = [
    "Any",
    "DEFAULT_TIMEOUT",
    "Dict",
    "GAMMA_IMAGE_CDN",
    "List",
    "Optional",
    "ScrapedScene",
    "Tuple",
    "USER_AGENT",
    "httpx",
    "json",
    "logger",
    "quote",
    "re",
    "request_with_host_limit",
    "time",
]
