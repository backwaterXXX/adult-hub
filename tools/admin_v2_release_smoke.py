#!/usr/bin/env python3
"""Read-only release smoke checks for the Admin v2 runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PEOPLE_PAGE_SIZE = 100
PEOPLE_ALIAS_SAMPLE_MAX_ROWS = 1000
LIBRARY_SEARCH_PAGE_SIZE = 50
LIBRARY_SEARCH_SAMPLE_MAX_ROWS = 500
QUALITY_KINDS = {
    "missing_metadata", "missing_nfo", "invalid_nfo", "nfo_metadata_mismatch", "missing_artwork",
}
RETIRED_ADMIN_API_ROUTES = (
    ("admin_v1_status_retired", "/admin/api/status"),
    ("admin_v1_sources_retired", "/admin/api/sources"),
    ("admin_v1_jobs_retired", "/admin/api/jobs"),
    ("admin_v1_performer_catalog_retired", "/admin/api/performers/catalog"),
    ("admin_v1_library_unavailable_retired", "/admin/api/library/unavailable"),
    ("admin_v1_playback_diagnostics_retired", "/admin/api/playback-diagnostics/status"),
)


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL cannot contain credentials, query, or fragment.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _get(base_url: str, path: str, token: str | None, timeout: float):
    headers = {"Accept": "application/json, text/html;q=0.9"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base_url + path, headers=headers, method="GET")
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read(2_000_001)


def _get_with_headers(base_url: str, path: str, token: str | None, timeout: float):
    headers = {"Accept": "application/javascript, text/javascript;q=0.9"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base_url + path, headers=headers, method="GET")
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return (response.status, response.headers.get("Content-Type", ""),
                {key.lower(): value for key, value in response.headers.items()},
                response.read(2_000_001))


def _get_route_status(base_url: str, path: str, token: str | None, timeout: float):
    """Read one route status without following redirects or consuming its body."""
    headers = {"Accept": "application/json, text/html;q=0.9"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base_url + path, headers=headers, method="GET")
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def _get_retired_classic_status(base_url: str, token: str | None, timeout: float):
    """Check that the removed Classic UI URL stays absent without following redirects."""
    return _get_route_status(base_url, "/admin/classic/", token, timeout)


def _json_response(fetch, path: str):
    status, content_type, body = fetch(path)
    if status != 200:
        raise ValueError(f"HTTP {status}")
    if "json" not in content_type.lower():
        raise ValueError("response was not JSON")
    if len(body) > 2_000_000:
        raise ValueError("response exceeded the 2 MB safety limit")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response was not a JSON object")
    return payload


def run_smoke(base_url: str, token: str | None = None, timeout: float = 10.0,
              fetcher=None, retired_classic_probe=None, asset_fetcher=None) -> list[CheckResult]:
    """Run read-only checks; injectable fetcher makes the contract deterministic."""
    base_url = _normalize_base_url(base_url)
    if timeout <= 0 or timeout > 60:
        raise ValueError("Timeout must be between 0 and 60 seconds.")

    def fetch(path: str):
        if fetcher is not None:
            return fetcher(path)
        return _get(base_url, path, token, timeout)

    results: list[CheckResult] = []
    people_alias_sample: tuple[str, str] | None = None
    people_catalog_valid = False

    def json_check(name: str, path: str, validate):
        try:
            value = _json_response(fetch, path)
            problem = validate(value)
            if problem:
                raise ValueError(problem)
            results.append(CheckResult(name, True, "HTTP 200"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            status = getattr(exc, "code", None)
            detail = f"HTTP {status}" if status else (
                str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            )
            # Never include a body, URL, token, or raw exception representation.
            results.append(CheckResult(name, False, detail[:160]))

    def health(value):
        return None if value.get("ok", True) is True else "health endpoint reported failure"

    def capabilities(value):
        worker = value.get("worker")
        if not isinstance(worker, dict):
            return "worker capabilities are missing"
        actions = worker.get("actions")
        if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
            return "worker action list is invalid"
        if value.get("worker_ready") is not True:
            return "worker is not ready"
        if value.get("actions_enabled") is not True:
            return "Admin writes are disabled"
        if "metadata_backfill" not in actions:
            return "worker does not support metadata_backfill"
        return None

    def metadata(value):
        if value.get("available") is not True:
            return "external metadata status is unavailable"
        if not isinstance(value.get("catalog"), dict) or not isinstance(value.get("sources"), list):
            return "external metadata status has an unexpected shape"
        limits = value.get("batch_limits")
        required_limits = (
            "videos_per_run", "pbc_actors_per_run", "theporndb_actors_per_run",
            "stashdb_actors_per_run",
        )
        if (not isinstance(limits, dict)
                or any(type(limits.get(key)) is not int or limits[key] < 0
                       for key in required_limits)):
            return "configured metadata batch limits are missing or invalid"
        schedule = value.get("sync_schedule")
        if not isinstance(schedule, dict) or type(schedule.get("run_on_startup")) is not bool:
            return "configured metadata sync schedule is missing or invalid"
        if schedule.get("mode") == "daily":
            if (not isinstance(schedule.get("daily_at"), str)
                    or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule["daily_at"])
                    or schedule.get("interval_minutes") is not None):
                return "configured metadata sync schedule is missing or invalid"
        elif schedule.get("mode") == "interval":
            interval = schedule.get("interval_minutes")
            if (type(interval) is not int or interval <= 0
                    or schedule.get("daily_at") is not None):
                return "configured metadata sync schedule is missing or invalid"
        else:
            return "configured metadata sync schedule is missing or invalid"
        return None

    def quality_summary(value):
        if value.get("available") is not True:
            return "Quality summary is unavailable"
        state = value.get("status")
        if state not in {"ready", "no_complete_audit"}:
            return "Quality summary has an unexpected state"
        total = value.get("total")
        counts = value.get("counts")
        history = value.get("history")
        if (type(total) is not int or total < 0 or not isinstance(counts, dict)
                or not isinstance(history, list)
                or not isinstance(value.get("fingerprint_coverage"), dict)):
            return "Quality summary has an unexpected shape"
        latest = value.get("latest_audit")
        if state == "no_complete_audit":
            if latest is not None or total != 0:
                return "Quality summary reports findings without a complete audit"
            reason = value.get("status_reason")
            known_reasons = {
                    "no_audit", "audit_in_progress", "audit_not_complete",
                    "legacy_missing_catalog_total"}
            if reason not in known_reasons and not (
                    reason is None and value.get("current_audit_id") is None
                    and value.get("current_audit_state") is None):
                return "Quality summary does not explain missing complete audit"
        else:
            if (not isinstance(latest, dict)
                    or latest.get("library_inventory_status") not in {"complete", "partial"}
                    or type(latest.get("checked")) is not int or latest["checked"] < 0
                    or type(latest.get("total")) is not int or latest["total"] < 0
                    or latest["checked"] != latest["total"]):
                return "Quality summary lacks verified full catalog coverage"

            def valid_inventory_count(audit):
                count = audit.get("library_inventory_entries")
                return count is None or (type(count) is int and count >= 0)

            def valid_counts(counts, expected_total):
                return (isinstance(counts, dict) and set(counts) == QUALITY_KINDS
                        and all(type(count) is int and count >= 0 for count in counts.values())
                        and sum(counts.values()) == expected_total)

            if (not isinstance(latest.get("id"), str) or not latest["id"]
                    or latest.get("jellyfin_import_state") not in {"checked", "partial", "unknown"}
                    or not valid_inventory_count(latest)
                    or not valid_counts(counts, total)):
                return "Quality summary has an unexpected latest audit projection"
            if not history or len(history) > 8:
                return "Quality summary history has an unexpected shape"
            for entry in history:
                if (not isinstance(entry, dict)
                        or entry.get("library_inventory_status") not in {"complete", "partial"}
                        or type(entry.get("checked")) is not int or entry["checked"] < 0
                        or type(entry.get("total")) is not int or entry["total"] < 0
                        or entry["checked"] != entry["total"]):
                    return "Quality summary history contains an incomplete catalog audit"
                open_total = entry.get("open_total")
                if (type(open_total) is not int or open_total < 0
                        or not valid_counts(entry.get("counts"), open_total)
                        or not valid_inventory_count(entry)):
                    return "Quality summary history has an unexpected projection"
            if history[0].get("id") != latest["id"]:
                return "Quality summary latest audit does not match its history"
        return None

    def quality_items(value):
        if (not isinstance(value.get("items"), list)
                or type(value.get("total")) is not int or value["total"] < 0
                or type(value.get("limit")) is not int or value["limit"] != 1
                or type(value.get("offset")) is not int or value["offset"] != 0
                or (value.get("audit_id") is not None and not isinstance(value.get("audit_id"), str))):
            return "Quality findings page has an unexpected shape"
        if value.get("audit_id") is None and (value["items"] or value["total"] != 0):
            return "Quality findings are present without a complete audit"
        return None

    def v2_status(value):
        if value.get("ok") is not True:
            return "Admin v2 status endpoint reported failure"
        database = value.get("db")
        if not isinstance(database, dict) or database.get("db_ok") is not True:
            return "Admin v2 database status is unavailable"
        sync = value.get("sync")
        if (not isinstance(sync, dict) or type(sync.get("available")) is not bool
                or type(sync.get("running")) is not bool
                or sync.get("freshness") not in {"fresh", "stale", "unknown"}):
            return "Admin v2 sync status has an unexpected shape"
        if not isinstance(value.get("capabilities"), dict):
            return "Admin v2 capabilities are missing from status"
        return None

    def jobs(value):
        if value.get("ok") is not True:
            return "Jobs API reported failure"
        if (not isinstance(value.get("jobs"), list) or type(value.get("total")) is not int
                or value["total"] < 0):
            return "Jobs API response has an unexpected shape"
        return None




    def sources(value):
        if value.get("ok") is not True or value.get("available") is not True:
            return "Sources configuration is unavailable"
        if not isinstance(value.get("sources"), list):
            return "Sources configuration has an unexpected shape"
        if (type(value.get("desired_revision")) is not int or value["desired_revision"] < 0
                or type(value.get("applied_revision_available")) is not bool):
            return "Sources revision state has an unexpected shape"
        applied_revision = value.get("applied_revision")
        if applied_revision is not None and (type(applied_revision) is not int or applied_revision < 0):
            return "Sources revision state has an unexpected shape"
        return None

    def source_history(value):
        if value.get("ok") is not True or value.get("available") is not True:
            return "Source sync history is unavailable"
        if (not isinstance(value.get("runs"), list) or type(value.get("total")) is not int
                or value["total"] < 0 or type(value.get("limit")) is not int or value["limit"] != 1
                or type(value.get("truncated")) is not bool):
            return "Source sync history has an unexpected shape"
        return None

    def issues(value, expected_limit=1):
        if value.get("ok") is not True:
            return "Issue Center API reported failure"
        data = value.get("data")
        if (not isinstance(value.get("issues"), list) or type(value.get("total")) is not int
                or value["total"] < 0 or type(value.get("offset")) is not int or value["offset"] != 0
                or type(value.get("limit")) is not int or value["limit"] != expected_limit
                or type(value.get("truncated")) is not bool or not isinstance(data, dict)
                or type(data.get("incomplete")) is not bool):
            return "Issue Center response has an unexpected shape"
        if data["incomplete"]:
            return "Issue Center data is incomplete"
        return None

    def filtered_issues(value):
        problem = issues(value, expected_limit=50)
        if problem:
            return problem
        if any(issue.get("severity") != "high" or issue.get("kind") != "job_failure"
               for issue in value["issues"] if isinstance(issue, dict)):
            return "Issue Center filters returned a non-matching item"
        if any(not isinstance(issue, dict) for issue in value["issues"]):
            return "Issue Center filters returned an invalid item"
        return None

    def library(value):
        page = value.get("page")
        if (value.get("ok") is not True or not isinstance(value.get("items"), list)
                or not isinstance(page, dict) or type(page.get("limit")) is not int or page["limit"] != 1
                or type(page.get("offset")) is not int or page["offset"] != 0
                or type(page.get("total")) is not int or page["total"] < 0
                or page.get("total_exact") is not True or len(value["items"]) > 1):
            return "Library page has an unexpected or inexact shape"
        return None

    def library_search():
        """Probe title, person, and ID search without emitting sampled values."""
        try:
            def page_at(offset):
                query = urlencode({"limit": LIBRARY_SEARCH_PAGE_SIZE, "offset": offset})
                value = _json_response(fetch, f"/admin/api/v2/library/videos?{query}")
                page = value.get("page")
                items = value.get("items")
                if (value.get("ok") is not True or not isinstance(items, list)
                        or not isinstance(page, dict)
                        or type(page.get("limit")) is not int
                        or page["limit"] != LIBRARY_SEARCH_PAGE_SIZE
                        or type(page.get("offset")) is not int or page["offset"] != offset
                        or type(page.get("total")) is not int or page["total"] < 0
                        or page.get("total_exact") is not True
                        or len(items) != min(LIBRARY_SEARCH_PAGE_SIZE,
                                             max(0, page["total"] - offset))):
                    raise ValueError("Library search sample page is incomplete")
                return items, page["total"]

            items, total = page_at(0)
            sampled_rows = list(items)
            title = next((item["title"].strip() for item in sampled_rows
                          if isinstance(item, dict) and isinstance(item.get("title"), str)
                          and 3 <= len(item["title"].strip()) <= 200), None)
            video_id = next((item["id"].strip() for item in sampled_rows
                             if isinstance(item, dict) and isinstance(item.get("id"), str)
                             and 3 <= len(item["id"].strip()) <= 200), None)
            person = next((name.strip() for item in sampled_rows if isinstance(item, dict)
                           and isinstance(item.get("performers"), list)
                           for name in item["performers"] if isinstance(name, str)
                           and 3 <= len(name.strip()) <= 200), None)
            offset = LIBRARY_SEARCH_PAGE_SIZE
            while (person is None and offset < min(total, LIBRARY_SEARCH_SAMPLE_MAX_ROWS)):
                page_items, _ = page_at(offset)
                sampled_rows.extend(page_items)
                person = next((name.strip() for item in page_items if isinstance(item, dict)
                               and isinstance(item.get("performers"), list)
                               for name in item["performers"] if isinstance(name, str)
                               and 3 <= len(name.strip()) <= 200), None)
                offset += LIBRARY_SEARCH_PAGE_SIZE
            if title is None or video_id is None or person is None:
                raise ValueError("Library lacks a bounded title, person, or ID search sample")

            def contains(value, term):
                if isinstance(value, str):
                    return term.casefold() in value.casefold()
                if isinstance(value, list):
                    return any(contains(item, term) for item in value)
                return False

            def search(term, label, exact_item_match):
                query = urlencode({"q": term, "limit": LIBRARY_SEARCH_PAGE_SIZE, "offset": 0})
                value = _json_response(fetch, f"/admin/api/v2/library/videos?{query}")
                page = value.get("page")
                matches = value.get("items")
                result_total = page.get("total") if isinstance(page, dict) else None
                if (value.get("ok") is not True or not isinstance(matches, list)
                        or not isinstance(page, dict)
                        or type(page.get("limit")) is not int
                        or page["limit"] != LIBRARY_SEARCH_PAGE_SIZE
                        or type(page.get("offset")) is not int or page["offset"] != 0
                        or type(result_total) is not int or result_total < 1
                        or page.get("total_exact") is not True
                        or len(matches) != min(LIBRARY_SEARCH_PAGE_SIZE, result_total)):
                    raise ValueError(f"Library {label} search returned an incomplete page")
                fields = ("id", "title", "performers", "studio", "plot", "tags")
                if any(not isinstance(item, dict) or not any(
                        contains(item.get(field), term) for field in fields)
                       for item in matches):
                    raise ValueError(f"Library {label} search returned a non-matching item")
                if not any(exact_item_match(item) for item in matches):
                    raise ValueError(f"Library {label} search omitted its sampled match")

            search(title, "title", lambda item: contains(item.get("title"), title))
            search(video_id, "ID", lambda item: item.get("id") == video_id)
            def has_person(item):
                names = item.get("performers")
                return isinstance(names, list) and any(
                    isinstance(name, str) and name.casefold() == person.casefold()
                    for name in names
                )
            search(person, "person", has_person)
            results.append(CheckResult("library_search", True, "HTTP 200"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            status = getattr(exc, "code", None)
            detail = f"HTTP {status}" if status else (
                str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            )
            results.append(CheckResult("library_search", False, detail[:160]))

    def duplicates(value):
        reported_limit = value.get("limit")
        if (value.get("ok") is not True or not isinstance(value.get("pairs"), list)
                or len(value["pairs"]) > 1 or type(value.get("total")) is not int
                or value["total"] < 0 or type(value.get("scanned")) is not int
                or value["scanned"] < 0 or type(value.get("offset")) is not int or value["offset"] != 0
                or (reported_limit is not None
                    and (type(reported_limit) is not int or reported_limit != 1))
                or value.get("complete") is not True
                or value.get("candidate_limit_reached") is not False):
            return "Same-file duplicate scan is incomplete or has an unexpected shape"
        return None

    def audits(value):
        if (value.get("ok") is not True or not isinstance(value.get("items"), list)
                or len(value["items"]) > 1 or type(value.get("total")) is not int
                or value["total"] < 0 or type(value.get("offset")) is not int or value["offset"] != 0
                or type(value.get("limit")) is not int or value["limit"] != 1):
            return "Audit history has an unexpected shape"
        return None

    def performer_proposals(value):
        if (not isinstance(value.get("items"), list) or len(value["items"]) > 1
                or type(value.get("total")) is not int or value["total"] < 0
                or type(value.get("limit")) is not int or value["limit"] != 1
                or type(value.get("offset")) is not int or value["offset"] != 0):
            return "Performer proposals have an unexpected shape"
        return None

    def performer_catalog(value):
        items = value.get("items")
        if (not isinstance(items, list) or len(items) > 1
                or any(not isinstance(item, dict)
                       or not isinstance(item.get("id"), str)
                       or not isinstance(item.get("name"), str) for item in items)):
            return "Performer catalogue has an unexpected shape"
        return None

    def activity_page(value):
        if (value.get("ok") is not True or not isinstance(value.get("items"), list)
                or len(value["items"]) > 1 or type(value.get("total")) is not int
                or value["total"] < 0 or type(value.get("limit")) is not int or value["limit"] != 1
                or type(value.get("offset")) is not int or value["offset"] != 0):
            return "Admin activity page has an unexpected shape"
        return None

    def schedules(value):
        if value.get("ok") is not True or not isinstance(value.get("items"), list):
            return "Schedule inventory is unavailable"
        if any(not isinstance(item, dict) or type(item.get("enabled")) is not bool
               or type(item.get("revision")) is not int or item["revision"] < 0
               for item in value["items"]):
            return "Schedule inventory has an unexpected item shape"
        return None

    def backups(value):
        total_bytes = value.get("total_bytes")
        if (value.get("ok") is not True or value.get("available") is not True
                or value.get("inventory_available") is not True
                or not isinstance(value.get("items"), list)
                or type(value.get("count")) is not int or value["count"] < 0
                or (total_bytes is not None and (type(total_bytes) is not int or total_bytes < 0))
                or type(value.get("restore_test_available")) is not bool):
            return "Backup inventory is unavailable or has an unexpected shape"
        return None

    def playback_status(value):
        if (value.get("ok") is not True or type(value.get("enabled")) is not bool
                or any(type(value.get(key)) is not int or value[key] < 0
                       for key in ("active_count", "saved_count", "retention_days"))):
            return "Playback diagnostics status has an unexpected shape"
        return None

    def playback_reports(value):
        if (value.get("ok") is not True or not isinstance(value.get("items"), list)
                or len(value["items"]) > 1 or type(value.get("limit")) is not int
                or value["limit"] != 1):
            return "Playback diagnostics reports have an unexpected shape"
        return None

    def people_catalog(value):
        nonlocal people_alias_sample, people_catalog_valid
        people_catalog_valid = False
        people_alias_sample = None
        page_offset = 0
        page = value
        while page_offset < PEOPLE_ALIAS_SAMPLE_MAX_ROWS:
            if page.get("ok") is not True or page.get("available") is not True:
                return "People catalog is unavailable"
            items = page.get("items")
            total = page.get("total")
            if (not isinstance(items, list) or type(total) is not int or total < 0
                    or type(page.get("offset")) is not int or page["offset"] != page_offset
                    or type(page.get("limit")) is not int
                    or page["limit"] != PEOPLE_PAGE_SIZE):
                return "People catalog response has an unexpected page shape"
            if len(items) != min(PEOPLE_PAGE_SIZE, max(0, total - page_offset)):
                return "People catalog response has an incomplete page"
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or type(item.get("video_count")) is not int or item["video_count"] < 0
                        or type(item.get("has_bio")) is not bool or type(item.get("has_photo")) is not bool
                        or not isinstance(item.get("aliases"), list)
                        or any(not isinstance(alias, str) for alias in item["aliases"])):
                    return "People catalog response has an unexpected item shape"
            if total > page_offset and not items:
                return "People catalog omitted a requested page"
            people_alias_sample = next(
                ((item["name"], alias.strip()) for item in items for alias in item["aliases"]
                 if alias.strip()
                 and alias.strip().casefold() not in item["name"].casefold()
                 and item["name"].casefold() not in alias.strip().casefold()),
                None,
            )
            if people_alias_sample is not None:
                people_catalog_valid = True
                return None
            page_offset += PEOPLE_PAGE_SIZE
            if page_offset >= total or page_offset >= PEOPLE_ALIAS_SAMPLE_MAX_ROWS:
                people_catalog_valid = True
                return None
            query = urlencode({"limit": PEOPLE_PAGE_SIZE, "offset": page_offset})
            page = _json_response(fetch, f"/admin/api/v2/organize/people?{query}")
        people_catalog_valid = True
        return None

    def people_alias_search(value):
        if value.get("ok") is not True or value.get("available") is not True:
            return "People alias search is unavailable"
        assert people_alias_sample is not None
        canonical_name, alias = people_alias_sample
        total = value.get("total")
        if type(total) is not int or total < 1:
            return "People alias search returned an unexpected page"
        offset = 0
        page = value
        while offset < min(total, PEOPLE_ALIAS_SAMPLE_MAX_ROWS):
            items = page.get("items")
            if (page.get("ok") is not True or page.get("available") is not True
                    or not isinstance(items, list) or len(items) > PEOPLE_PAGE_SIZE
                    or type(page.get("total")) is not int or page["total"] != total
                    or type(page.get("offset")) is not int or page["offset"] != offset
                    or type(page.get("limit")) is not int or page["limit"] != PEOPLE_PAGE_SIZE):
                return "People alias search returned an unexpected page"
            if len(items) != min(PEOPLE_PAGE_SIZE, max(0, total - offset)):
                return "People alias search returned an incomplete page"
            if total > offset and not items:
                return "People alias search omitted a requested page"
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or not isinstance(item.get("aliases"), list)
                        or any(not isinstance(value, str) for value in item["aliases"])):
                    return "People alias search returned an unexpected item shape"
                if (item["name"].casefold() == canonical_name.casefold()
                        and any(value.casefold() == alias.casefold() for value in item["aliases"])):
                    return None
            offset += PEOPLE_PAGE_SIZE
            if offset < min(total, PEOPLE_ALIAS_SAMPLE_MAX_ROWS):
                query = urlencode({"q": alias, "limit": PEOPLE_PAGE_SIZE, "offset": offset})
                page = _json_response(fetch, f"/admin/api/v2/organize/people?{query}")
        if total > PEOPLE_ALIAS_SAMPLE_MAX_ROWS:
            return "People alias search exceeded the bounded verification window"
        return "People alias search did not return its canonical person"

    json_check("health", "/admin/health", health)

    def html_check(name: str, path: str, marker: bytes):
        try:
            status, content_type, body = fetch(path)
            valid = status == 200 and "text/html" in content_type.lower() and marker in body
            results.append(CheckResult(name, valid,
                                       "HTTP 200" if valid else f"HTTP {status}; expected matching Admin HTML"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            status = getattr(exc, "code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            results.append(CheckResult(name, False, detail))

    def enrichment_asset_check():
        # Injected API fixtures have no response headers; use a dedicated asset
        # fetcher in contract tests and the real HTTP response in release smoke.
        if asset_fetcher is None and fetcher is not None:
            return
        path = "/admin/assets-v2/enrichment.js"
        try:
            if asset_fetcher is not None:
                status, content_type, headers, body = asset_fetcher(path)
            else:
                status, content_type, headers, body = _get_with_headers(
                    base_url, path, token, timeout)
            valid_headers = isinstance(headers, dict)
            cache_control = (next((value for key, value in headers.items()
                                   if str(key).lower() == "cache-control"), "")
                             if valid_headers else "")
            valid_asset = (
                status == 200 and "javascript" in content_type.lower()
                and len(body) <= 2_000_000
                and "no-store" in {part.strip().lower() for part in cache_control.split(",")}
                and b".trim().toLowerCase()" in body
                and b"Scene fingerprint" in body
            )
            results.append(CheckResult(
                "enrichment_asset_uncached", valid_asset,
                "HTTP 200; no-store and scene fingerprint label verified" if valid_asset
                else (f"HTTP {status}" if status != 200 else
                      "Enrichment asset cache policy or scene fingerprint label is invalid"),
            ))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            status = getattr(exc, "code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            results.append(CheckResult("enrichment_asset_uncached", False, detail))

    html_check("admin_primary_route", "/admin/", b"/admin/assets-v2/admin-v2.css")
    html_check("admin_v2_page", "/admin/v2/", b"/admin/assets-v2/admin-v2.css")
    enrichment_asset_check()
    try:
        status = (retired_classic_probe() if retired_classic_probe is not None
                  else fetch("/admin/classic/")[0] if fetcher is not None
                  else _get_retired_classic_status(base_url, token, timeout))
        valid = status == 404
        results.append(CheckResult(
            "admin_classic_retired", valid,
            "HTTP 404" if valid else f"HTTP {status}; expected retired route to be absent",
        ))
    except Exception as exc:  # injected fetchers may signal an unmapped route
        status = getattr(exc, "code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        results.append(CheckResult("admin_classic_retired", False, detail))

    for name, path in RETIRED_ADMIN_API_ROUTES:
        try:
            status = fetch(path)[0] if fetcher is not None else _get_route_status(
                base_url, path, token, timeout,
            )
            valid = status == 404
            results.append(CheckResult(
                name, valid,
                "HTTP 404" if valid else f"HTTP {status}; expected retired Admin API route to be absent",
            ))
        except Exception as exc:
            status = getattr(exc, "code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            results.append(CheckResult(name, False, detail[:160]))

    json_check("admin_v2_status", "/admin/api/v2/status", v2_status)
    json_check("capabilities", "/admin/api/v2/capabilities", capabilities)
    json_check("metadata_status", "/admin/api/v2/metadata/enrichment", metadata)
    json_check("sources_list", "/admin/api/v2/sources/list", sources)
    json_check("sources_sync_history", "/admin/api/v2/sources/sync-history?limit=1", source_history)
    json_check("issues", "/admin/api/v2/issues?limit=1&offset=0", issues)
    json_check("issues_filtered", "/admin/api/v2/issues?severity=high&kind=job_failure&limit=50&offset=0",
               filtered_issues)
    json_check("library", "/admin/api/v2/library/videos?limit=1&offset=0", library)
    library_search()
    json_check("duplicates", "/admin/api/v2/duplicates?limit=1&offset=0&category=same_file", duplicates)
    json_check("audit_history", "/admin/api/v2/audits?limit=1&offset=0", audits)
    json_check("admin_activity", "/admin/api/v2/activity?limit=1&offset=0", activity_page)
    json_check("admin_change_history", "/admin/api/v2/activity/changes?limit=1&offset=0", activity_page)
    json_check("schedules", "/admin/api/v2/schedules", schedules)
    json_check("backup_inventory", "/admin/api/v2/backups", backups)
    json_check("playback_diagnostics_status", "/admin/api/v2/diagnostics/playback/status", playback_status)
    json_check("playback_diagnostics_reports", "/admin/api/v2/diagnostics/playback/reports?limit=1", playback_reports)
    json_check("performer_proposals", "/admin/api/v2/performers/proposals?limit=1&offset=0", performer_proposals)
    json_check("performer_catalog", "/admin/api/v2/performers/catalog?limit=1", performer_catalog)
    people_page_query = urlencode({"limit": PEOPLE_PAGE_SIZE, "offset": 0})
    json_check("people_catalog", f"/admin/api/v2/organize/people?{people_page_query}", people_catalog)
    if people_alias_sample is not None:
        query = urlencode({"q": people_alias_sample[1], "limit": PEOPLE_PAGE_SIZE, "offset": 0})
        json_check("people_alias_search", f"/admin/api/v2/organize/people?{query}", people_alias_search)
    else:
        results.append(CheckResult(
            "people_alias_search", people_catalog_valid,
            "No alias sample in the bounded people scan" if people_catalog_valid else "People catalog check failed",
        ))
    json_check("quality_summary", "/admin/api/v2/quality/summary", quality_summary)
    json_check("quality_items", "/admin/api/v2/quality/items?limit=1&offset=0", quality_items)
    json_check("metadata_backfill_job_filter",
               "/admin/api/v2/jobs?action=metadata_backfill&state=all&limit=1", jobs)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ADMIN_BASE_URL"),
                        help="Admin base URL (or ADMIN_BASE_URL); no path beyond an optional prefix")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--token-env", default="ADMIN_SMOKE_TOKEN",
                        help="Environment variable holding an optional bearer token")
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("provide --base-url or ADMIN_BASE_URL")
    token = os.environ.get(args.token_env) or None
    try:
        results = run_smoke(args.base_url, token=token, timeout=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'} {result.name}: {result.detail}")
    passed = sum(result.passed for result in results)
    print(f"{passed}/{len(results)} read-only checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
