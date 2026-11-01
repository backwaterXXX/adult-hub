#!/usr/bin/env python3
"""Read-only smoke checks for retained Admin v2 routes and retired-route 404s."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


RETAINED_JSON_ROUTES = (
    ("status", "/admin/api/v2/status"),
    ("capabilities", "/admin/api/v2/capabilities"),
    ("service_probes", "/admin/api/v2/diagnostics/probes"),
    ("sources", "/admin/api/v2/sources/list"),
    ("sync_history", "/admin/api/v2/sources/sync-history?limit=1"),
    ("library", "/admin/api/v2/library/videos?limit=1&offset=0"),
    ("duplicates", "/admin/api/v2/duplicates?limit=1&offset=0&category=same_file"),
    ("metadata_suggestions", "/admin/api/v2/quality/proposals?limit=1&offset=0"),
    ("performers", "/admin/api/v2/performers/catalog?limit=1"),
    ("tasks", "/admin/api/v2/jobs?state=all&limit=1"),
    ("schedules", "/admin/api/v2/schedules"),
    ("backups", "/admin/api/v2/backups"),
)
RETIRED_ROUTES = (
    ("manual_audits", "/admin/api/v2/audits"),
    ("admin_activity", "/admin/api/v2/activity"),
    ("admin_change_history", "/admin/api/v2/activity/changes"),
    ("enrichment_status", "/admin/api/v2/metadata/enrichment"),
    ("enrichment_scan", "/admin/api/v2/metadata/enrichment/scan"),
    ("linkcheck_report", "/admin/api/v2/linkcheck"),
    ("sync_activity_report", "/admin/api/v2/sync/activity"),
    ("source_search", "/admin/api/v2/sources/search?q=smoke"),
    ("quality_findings", "/admin/api/v2/quality/items"),
    ("playback_diagnostics", "/admin/api/v2/diagnostics/playback/status"),
)


def _base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Base URL must be an http(s) origin with an optional path prefix.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _request(base: str, path: str, token: str | None, timeout: float):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(Request(base + path, headers=headers), timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read(2_000_001)
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read(4096)


def run_smoke(base_url: str, token: str | None = None, timeout: float = 10.0, fetcher=None):
    base = _base_url(base_url)
    if timeout <= 0 or timeout > 60:
        raise ValueError("Timeout must be between 0 and 60 seconds.")
    results = []
    for name, path in RETAINED_JSON_ROUTES:
        try:
            status, content_type, body = fetcher(path) if fetcher else _request(base, path, token, timeout)
            payload = json.loads(body.decode("utf-8")) if status == 200 and "json" in content_type.lower() else None
            ok = isinstance(payload, dict) and payload.get("ok", True) is True
            results.append(CheckResult(name, ok, "HTTP 200" if ok else f"HTTP {status}; invalid JSON response"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            status = getattr(exc, "code", None)
            results.append(CheckResult(name, False, f"HTTP {status}" if status else type(exc).__name__))
    for name, path in RETIRED_ROUTES:
        try:
            status, _, _ = fetcher(path) if fetcher else _request(base, path, token, timeout)
            results.append(CheckResult(name, status == 404, "HTTP 404" if status == 404 else f"HTTP {status}; expected 404"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            results.append(CheckResult(name, False, type(exc).__name__))
    for name, path in (("admin_v2", "/admin/v2/"), ("classic_retired", "/admin/classic/")):
        try:
            status, _, _ = fetcher(path) if fetcher else _request(base, path, token, timeout)
            expected = 200 if name == "admin_v2" else 404
            results.append(CheckResult(name, status == expected, f"HTTP {status}"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            results.append(CheckResult(name, False, type(exc).__name__))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ADMIN_BASE_URL"))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--token-env", default="ADMIN_SMOKE_TOKEN")
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("provide --base-url or ADMIN_BASE_URL")
    try:
        results = run_smoke(args.base_url, os.environ.get(args.token_env) or None, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'} {result.name}: {result.detail}")
    passed = sum(result.passed for result in results)
    print(f"{passed}/{len(results)} read-only checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
