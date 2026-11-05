#!/usr/bin/env python3
"""Summarize classic Admin usage from Uvicorn access logs without retaining PII.

Only the date, matched legacy route template (or a redacted shape for an
unmapped legacy API route), HTTP method, status class, and aggregate count are
emitted. Raw request paths, query strings, client addresses, user agents, and
request bodies are never copied into the report.
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import re
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tests/fixtures/v1-route-parity.json"
ACCESS_LINE = re.compile(
    r'"(?P<method>[A-Z]+) (?P<target>[^ ]+) HTTP/[^\"]+" (?P<status>[0-9]{3})(?: |$)'
)
LOG_DAY = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})(?: |T)")
LOG_TIMESTAMP = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
    r"(?:[.,]\d+)?(?P<zone>Z|[+-]\d{2}:\d{2})?"
)
PARAMETER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
PRIVACY_EMITTED_FIELDS = [
    "day", "surface", "method", "status_class", "count",
    "access_log_observation.parsed_requests",
    "access_log_observation.days_with_records[].day",
    "access_log_observation.days_with_records[].request_count",
    "access_log_observation.days_with_records[].first_request_at",
    "access_log_observation.days_with_records[].last_request_at",
    "access_log_observation.unknown_timestamp_requests",
    "access_log_observation.note",
]
PRIVACY_OMITTED_FIELDS = [
    "client_address", "query_string", "user_agent", "request_body",
    "raw_unmatched_path", "dynamic_unmatched_segments",
]


def _route_regex(route: str) -> re.Pattern[str]:
    parts = []
    cursor = 0
    for parameter in PARAMETER.finditer(route):
        parts.append(re.escape(route[cursor:parameter.start()]))
        parts.append("[^/]+")
        cursor = parameter.end()
    parts.append(re.escape(route[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


def _route_specs(entries: Iterable[dict]) -> list[tuple[str, str, re.Pattern[str]]]:
    compiled = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        method, route = item.get("method"), item.get("path")
        if not isinstance(method, str) or not isinstance(route, str):
            continue
        compiled.append((method, route, _route_regex(route)))
    return compiled


def _route_inventory(manifest_path: Path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    routes: set[str] = set()
    for group in data.get("groups", []):
        for value in group.get("v1_routes", []):
            if isinstance(value, str):
                routes.add(value)

    legacy = []
    for value in sorted(routes):
        method, route = value.split(" ", 1)
        legacy.append((method, route, _route_regex(route)))
    shared = _route_specs(data.get("shared_resolver_routes", []))
    clients = _route_specs(data.get("client_integration_routes", []))
    return legacy, shared, clients


def _segment_is_known(pattern: list[str], index: int, segments: list[str]) -> bool:
    return (
        len(pattern) > index
        and not PARAMETER.fullmatch(pattern[index])
        and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", pattern[index])
        and pattern[index] == segments[index]
        and all(
            PARAMETER.fullmatch(pattern[prefix_index])
            or pattern[prefix_index] == segments[prefix_index]
            for prefix_index in range(index)
        )
    )


def _unmapped_route_surface(path: str, routes: list[tuple[str, str, re.Pattern[str]]]) -> str:
    """Keep only route words plausible at this position; mask other segments."""
    segments = [segment for segment in path.split("/") if segment]
    patterns = [[part for part in route.split("/") if part] for _, route, _ in routes]
    shape = []
    for index, segment in enumerate(segments):
        known_here = any(
            _segment_is_known(pattern, index, segments) for pattern in patterns
        )
        shape.append(segment if known_here else "{segment}")
    return "unmapped_classic_api /" + "/".join(shape)


def _status_class(status: str) -> str:
    return f"{status[0]}xx"


def _log_day(line: str) -> str:
    day_match = LOG_DAY.match(line)
    return day_match.group("day") if day_match else "unknown"


def _record_access_window(
    windows: dict[str, dict[str, str | None]], day: str, line: str
) -> None:
    timestamp_match = LOG_TIMESTAMP.match(line)
    if day == "unknown" or not timestamp_match:
        return
    stamp = timestamp_match.group("timestamp") + (timestamp_match.group("zone") or "")
    sort_key = stamp[:19].replace("T", " ")
    window = windows.setdefault(
        day, {"first": stamp, "last": stamp, "first_key": sort_key, "last_key": sort_key}
    )
    if sort_key < window["first_key"]:
        window["first"], window["first_key"] = stamp, sort_key
    if sort_key > window["last_key"]:
        window["last"], window["last_key"] = stamp, sort_key


def _non_classic_entry(
    method: str,
    path: str,
    day: str,
    status_class: str,
    shared_routes: list[tuple[str, str, re.Pattern[str]]],
    client_routes: list[tuple[str, str, re.Pattern[str]]],
) -> tuple[str, str, str, str] | None:
    for classification, inventory in (("shared_resolver", shared_routes),
                                       ("client_integration", client_routes)):
        for route_method, route, route_pattern in inventory:
            if method == route_method and route_pattern.fullmatch(path):
                public_route = route[len("/admin"):] if route.startswith("/admin/") else route
                return (
                    day, f"{classification} {route_method} {public_route}",
                    route_method, status_class,
                )
    return None


def _legacy_api_entry(
    method: str,
    legacy_path: str,
    day: str,
    status_class: str,
    routes: list[tuple[str, str, re.Pattern[str]]],
) -> tuple[str, str, str, str] | None:
    for route_method, route, route_pattern in routes:
        if method == route_method and route_pattern.fullmatch(legacy_path):
            return (day, route_method + " " + route, route_method, status_class)
    return None


def _classify_request(
    method: str,
    path: str,
    day: str,
    status_class: str,
    routes: list[tuple[str, str, re.Pattern[str]]],
    shared_routes: list[tuple[str, str, re.Pattern[str]]],
    client_routes: list[tuple[str, str, re.Pattern[str]]],
) -> tuple[str, tuple[str, str, str, str] | None]:
    """Return (bucket, entry); bucket is skip, non_classic, counted, or unmapped."""
    if path in {"/admin/classic", "/admin/classic/"} and method in {"GET", "HEAD"}:
        return "counted", (day, "classic_page", method, status_class)
    if path.startswith("/admin/assets/"):
        return "counted", (day, "classic_asset", method, status_class)
    if not path.startswith("/admin/api/"):
        return "skip", None

    non_classic = _non_classic_entry(
        method, path, day, status_class, shared_routes, client_routes
    )
    if non_classic:
        return "non_classic", non_classic
    legacy_path = path[len("/admin"):]
    entry = _legacy_api_entry(method, legacy_path, day, status_class, routes)
    if entry is not None:
        return "counted", entry
    surface = _unmapped_route_surface(legacy_path, routes)
    return "unmapped", (day, surface, method, status_class)


def _counter_records(counter: Counter) -> list[dict]:
    return [
        {"day": day, "surface": surface, "method": method,
         "status_class": status_class, "count": count}
        for (day, surface, method, status_class), count in sorted(counter.items())
    ]


def _days_with_records(
    access_log_days: Counter[str], access_log_windows: dict[str, dict[str, str | None]]
) -> list[dict]:
    return [
        {"day": day, "request_count": count,
         "first_request_at": access_log_windows.get(day, {}).get("first"),
         "last_request_at": access_log_windows.get(day, {}).get("last")}
        for day, count in sorted(access_log_days.items())
    ]


def _build_report(
    observed: int,
    unmapped_api: int,
    counts: Counter,
    non_classic_counts: Counter,
    parsed_access_requests: int,
    access_log_days: Counter[str],
    access_log_windows: dict[str, dict[str, str | None]],
) -> dict:
    return {
        "observed_requests": observed,
        "unmapped_classic_api_requests": unmapped_api,
        "counts": _counter_records(counts),
        "non_classic_api_requests": sum(non_classic_counts.values()),
        "non_classic_api_counts": _counter_records(non_classic_counts),
        "access_log_observation": {
            "parsed_requests": parsed_access_requests,
            "days_with_records": _days_with_records(access_log_days, access_log_windows),
            "unknown_timestamp_requests": access_log_days.get("unknown", 0),
            "note": "Observed log records are not proof of complete retention or uptime.",
        },
        "privacy": {
            "emitted_fields": list(PRIVACY_EMITTED_FIELDS),
            "omitted": list(PRIVACY_OMITTED_FIELDS),
        },
    }


def summarize(lines: Iterable[str], manifest_path: Path = DEFAULT_MANIFEST) -> dict:
    routes, shared_routes, client_routes = _route_inventory(manifest_path)
    counts: Counter[tuple[str, str, str, str]] = Counter()
    non_classic_counts: Counter[tuple[str, str, str, str]] = Counter()
    access_log_days: Counter[str] = Counter()
    access_log_windows: dict[str, dict[str, str | None]] = {}
    parsed_access_requests = 0
    observed = 0
    unmapped_api = 0

    for line in lines:
        match = ACCESS_LINE.search(line)
        if not match:
            continue

        method = match.group("method")
        parsed_access_requests += 1
        # Strip query data before any classification. Never include `target`
        # itself in output, even when the request path contains a private ID.
        path = match.group("target").split("?", 1)[0]
        day = _log_day(line)
        access_log_days[day] += 1
        _record_access_window(access_log_windows, day, line)
        if path.startswith("/admin/api/v2/"):
            continue

        bucket, entry = _classify_request(
            method, path, day, _status_class(match.group("status")),
            routes, shared_routes, client_routes,
        )
        if bucket == "skip":
            continue
        if bucket == "non_classic":
            non_classic_counts[entry] += 1
            continue
        if bucket == "unmapped":
            unmapped_api += 1

        observed += 1
        counts[entry] += 1

    return _build_report(
        observed, unmapped_api, counts, non_classic_counts,
        parsed_access_requests, access_log_days, access_log_windows,
    )


def _read_logs(paths: list[str]) -> Iterable[str]:
    if not paths or paths == ["-"]:
        yield from sys.stdin
        return
    for raw_path in paths:
        path = Path(raw_path)
        open_text = gzip.open if path.suffix.lower() == ".gz" else open
        with open_text(path, "rt", encoding="utf-8", errors="replace") as stream:
            yield from stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="*", help="Uvicorn access log files; use '-' or omit for stdin")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="V1/V2 route inventory JSON")
    args = parser.parse_args(argv)
    try:
        report = summarize(_read_logs(args.logs), args.manifest)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Could not create classic usage report: {type(exc).__name__}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

