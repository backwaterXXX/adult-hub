"""MediaWiki API client for Porn Base Central performer metadata."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import quote
from common.http_rate_limit import request_with_host_limit, wait_for_host_slot

from sync.provider_common import decode_http_payload

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("pbc")

API_URL = (
    "https://pbc.xxx/w/api.php"
    "?action=query&prop=revisions&rvprop=content&format=json&formatversion=2"
    "&redirects=1&titles="
)
USER_AGENT = "AdultHubSync/1.0 (performer-bio-backfill)"
TIMEOUT = 15.0


def page_title(name: str) -> str:
    """Map a performer name to a MediaWiki page title.

    "Rico Marlon" -> "Rico_Marlon" (strip, whitespace runs -> "_").
    """
    if not isinstance(name, str):
        return ""
    return re.sub(r"\s+", "_", name.strip())


def _decode_http_payload(payload) -> Optional[dict]:
    """Best-effort conversion of an http_get result to parsed JSON."""
    return decode_http_payload(payload, logger)


def _api_get_json(url: str, http_get):
    """GET a MediaWiki API URL and return the decoded (not yet validated) JSON value."""
    if http_get is not None:
        wait_for_host_slot(url)
        try:
            raw = http_get(url, timeout=TIMEOUT)
        except TypeError:
            raw = http_get(url)
        return _decode_http_payload(raw)
    headers = {"User-Agent": USER_AGENT}
    if httpx is not None:
        resp = request_with_host_limit(
            httpx, "get", url, headers=headers, timeout=TIMEOUT,
            follow_redirects=True,
        )
        if getattr(resp, "status_code", 200) != 200:
            return None
        return resp.json()
    import urllib.request  # stdlib fallback, mirrors sync/sync.py

    req = urllib.request.Request(url, headers=headers)
    wait_for_host_slot(url)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _revision_wikitext(rev: dict) -> Optional[str]:
    content = rev.get("content")
    if not isinstance(content, str):
        slots = rev.get("slots") or {}
        main = (slots.get("main") or {}) if isinstance(slots, dict) else {}
        content = main.get("content") if isinstance(main, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _extract_wikitext(data: dict) -> Optional[str]:
    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return None
    page = pages[0] if isinstance(pages[0], dict) else {}
    if page.get("missing"):
        return None
    revisions = page.get("revisions") or []
    if not revisions or not isinstance(revisions[0], dict):
        return None
    return _revision_wikitext(revisions[0])


def fetch_wikitext(title, http_get=None, *, strict: bool = False) -> Optional[str]:
    """Fetch raw wikitext for a MediaWiki page title via the API.

    Returns the wikitext string, or None when the page is missing or any
    error occurs. ``http_get`` is an injectable callable for tests; it may
    return parsed JSON, bytes/str of JSON, or a response-like object.
    """
    if not isinstance(title, str) or not title.strip():
        return None
    url = API_URL + quote(title.strip(), safe="")
    try:
        data = _api_get_json(url, http_get)
        if not isinstance(data, dict):
            return None
        return _extract_wikitext(data)
    except Exception as e:
        logger.debug("fetch_wikitext(%r) failed: %s", title, e)
        if strict:
            raise
        return None


SEARCH_API_URL = (
    "https://pbc.xxx/w/api.php"
    "?action=query&list=search&srnamespace=0&format=json&formatversion=2"
    "&srlimit=3&srsearch="
)


def search_page_title(name: str, http_get=None, *, strict: bool = False) -> Optional[str]:
    """Search PBC for alternative page titles when direct title lookup fails.

    Returns the top search result's title, or None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    url = SEARCH_API_URL + quote(name.strip(), safe="")
    try:
        data = _api_get_json(url, http_get)
        if not isinstance(data, dict):
            return None
        search_results = (data.get("query") or {}).get("search") or []
        if search_results and isinstance(search_results[0], dict):
            res_title = search_results[0].get("title")
            if isinstance(res_title, str) and res_title.strip():
                return res_title.strip()
    except Exception as e:
        logger.debug("search_page_title(%r) failed: %s", name, e)
        if strict:
            raise
    return None
