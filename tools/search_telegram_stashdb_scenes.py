#!/usr/bin/env python3
"""Search StashDB scene titles for the dev rows in the performer review CSV.

Results are suggestions only. This tool never writes to StashDB or edits the
source review CSV; it writes a separate candidates CSV for human review.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import OrderedDict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.http_rate_limit import wait_for_host_slot

QUERY = """
query SearchScenes($term: String!, $limit: Int) {
  searchScenes(term: $term, limit: $limit) {
    count
    scenes {
      id title release_date duration
      studio { name }
      performers { performer { name } }
      urls { url }
    }
  }
}
"""
GENERIC = re.compile(
    r"^(?:video bonus|vídeo bônus|video|vídeo|bonus|especial|telegram|"
    r"onlyfans|full video|new video|sem título|untitled|\d{1,5})$",
    re.I,
)
TITLE_RE = re.compile(
    r"(?:título|titulo|title)\s*[:：]\s*(.*?)(?=\s*(?:📢|👥|\bcanal\b|$))",
    re.I | re.S,
)
MENTION_RE = re.compile(r"@[\w.]+")


def clean_term(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\.(?:mp4|mkv|avi|mov|wmv|webm)$", "", value, flags=re.I)
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = MENTION_RE.sub(" ", value)
    value = re.sub(r"https?://\S+", " ", value, flags=re.I)
    value = re.sub(r"[#*_`]+", " ", value)
    value = re.sub(r"[_|]+", " ", value)
    value = re.sub(r"[^\w\s&'’.,:()!-]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" -–—|,.:;! ")
    return value[:180].strip()


def title_term(row: dict[str, str]) -> str:
    caption = unicodedata.normalize("NFKC", row.get("source_text", ""))
    match = TITLE_RE.search(caption)
    if match:
        term = clean_term(match.group(1))
        if term and not GENERIC.fullmatch(term):
            return term
    filename = clean_term(row.get("source_filename", ""))
    if filename and not GENERIC.fullmatch(filename):
        return filename
    caption = re.sub(r"🎬\s*Produtora\s*:[^\n]*", " ", caption, flags=re.I)
    caption = re.sub(r"(?:📢|👥).*", " ", caption)
    caption = clean_term(caption)
    if caption and not GENERIC.fullmatch(caption):
        return caption
    return ""


def request_scenes(endpoint: str, token: str, term: str, limit: int) -> tuple[int, list[dict]]:
    body = json.dumps({"query": QUERY, "variables": {"term": term, "limit": limit}}).encode()
    req = Request(
        endpoint,
        data=body,
        headers={
            "ApiKey": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AdultHubSync/1.0 (Telegram scene candidate search)",
        },
        method="POST",
    )
    wait_for_host_slot(endpoint)
    with urlopen(req, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        messages = "; ".join(str(e.get("message", "GraphQL error")) for e in payload["errors"])
        raise RuntimeError(messages[:500])
    result = payload.get("data", {}).get("searchScenes") or {}
    return int(result.get("count") or 0), result.get("scenes") or []


SCENE_FIELDS = [
    "review_id", "query", "query_result_count", "rank", "stashdb_scene_id",
    "stashdb_scene_url", "title", "studio", "release_date", "duration",
    "performers", "match_status",
]


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="docs/csv/telegram-performer-review-prefilled-all-dev.csv")
    parser.add_argument("--output", default="docs/csv/telegram-performer-stashdb-title-candidates.csv")
    parser.add_argument("--limit", type=int, default=8, help="maximum StashDB results per query")
    parser.add_argument("--max-queries", type=int, default=0, help="optional query cap for a trial run")
    return parser.parse_args()


def _read_dev_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as source:
        return [r for r in csv.DictReader(source) if r.get("split") == "dev"]


def _group_by_term(rows: list[dict[str, str]]):
    by_term: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    skipped_rows = []
    for row in rows:
        term = title_term(row)
        if not term:
            skipped_rows.append(row)
            continue
        by_term.setdefault(term.casefold(), []).append({**row, "_term": term})
    return by_term, skipped_rows


def _scene_row(row: dict[str, str], term: str, count: int, rank: int, scene: dict) -> dict:
    sid = scene.get("id", "")
    return {
        "review_id": row["review_id"],
        "query": term,
        "query_result_count": count,
        "rank": rank,
        "stashdb_scene_id": sid,
        "stashdb_scene_url": f"https://stashdb.org/scenes/{sid}" if sid else "",
        "title": scene.get("title", ""),
        "studio": (scene.get("studio") or {}).get("name", ""),
        "release_date": scene.get("release_date", ""),
        "duration": scene.get("duration", ""),
        "performers": "; ".join(
            (entry.get("performer") or {}).get("name", "")
            for entry in scene.get("performers", [])
            if (entry.get("performer") or {}).get("name")
        ),
        "match_status": "candidate_only",
    }


def _write_term_results(writer, term: str, linked_rows, count: int, scenes) -> int:
    matched = 0
    for row in linked_rows:
        if not scenes:
            writer.writerow({"review_id": row["review_id"], "query": term, "query_result_count": count, "match_status": "no_results"})
            continue
        matched += 1
        for rank, scene in enumerate(scenes, 1):
            writer.writerow(_scene_row(row, term, count, rank, scene))
    return matched


def main() -> int:
    args = _parse_args()

    token = os.environ.get("STASHDB_API_KEY", "").strip() or os.environ.get("STASH_API_KEY", "").strip()
    if not token:
        print("StashDB API key is not configured in this environment.", file=sys.stderr)
        return 2
    endpoint = os.environ.get("STASHDB_API_URL", "https://stashdb.org/graphql")
    rows = _read_dev_rows(args.input)

    by_term, skipped_rows = _group_by_term(rows)
    terms = list(by_term.items())
    if args.max_queries:
        terms = terms[: args.max_queries]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    skipped = len(skipped_rows)
    searched = matched = failures = 0
    with output.open("w", newline="", encoding="utf-8-sig") as dest:
        writer = csv.DictWriter(dest, fieldnames=SCENE_FIELDS)
        writer.writeheader()
        for row in skipped_rows:
            writer.writerow({"review_id": row["review_id"], "match_status": "skipped_no_usable_title"})
        for index, (_, linked_rows) in enumerate(terms, 1):
            term = linked_rows[0]["_term"]
            try:
                count, scenes = request_scenes(endpoint, token, term, args.limit)
                searched += 1
            except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as exc:
                failures += 1
                for row in linked_rows:
                    writer.writerow({"review_id": row["review_id"], "query": term, "match_status": f"search_error: {type(exc).__name__}"})
                continue
            matched += _write_term_results(writer, term, linked_rows, count, scenes)
            if index % 20 == 0 or index == len(terms):
                print(f"searched {index}/{len(terms)} unique title terms; rows with candidates: {matched}; errors: {failures}", flush=True)

    print(f"Done: {searched} searches, {matched} dev rows returned candidates, {skipped} rows skipped, {failures} errors; output={output}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
