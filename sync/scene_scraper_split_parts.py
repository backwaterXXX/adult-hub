"""Conservative title and combined-runtime lookup for split Telegram scenes."""

from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

if __package__:
    from . import scene_scrapers as _core
    from .scene_scraper_models import PART_MARKER_SQL
else:
    import scene_scrapers as _core
    from scene_scraper_models import PART_MARKER_SQL


_PART_MARKER = re.compile(
    r"(?i)(?<![\w])(?:part[\s_.-]*|pt\.?[\s_.-]*|s[\s_.-]*)(?P<number>\d{1,2})(?!\d)"
)
_EXTENSION = re.compile(r"\.(?:mp4|mkv|avi|mov|wmv|webm)$", re.IGNORECASE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_MENTION = re.compile(r"@[\w.]+")
_HASHTAG = re.compile(r"#[\w-]+")
_BRACKETED = re.compile(r"\[[^\]]*\]")
_MEDIA_NOISE = {
    "1080p", "720p", "2160p", "4k", "hd", "uhd", "hevc", "h264", "h265",
    "x264", "x265", "web", "rip", "xxx",
}
_TITLE_STOPWORDS = {"a", "an", "and", "at", "for", "in", "of", "the", "to"}
SPLIT_DURATION_TOLERANCE_SECONDS = 30


def normalize_scene_title(value: str) -> str:
    """Normalize a title for comparison while preserving meaningful words."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _EXTENSION.sub(" ", text)
    text = _BRACKETED.sub(" ", text)
    text = _HASHTAG.sub(" ", text)
    text = _MENTION.sub(" ", text)
    text = _URL.sub(" ", text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    words = [word for word in text.split() if word not in _MEDIA_NOISE]
    return " ".join(words)


def parse_split_part_title(value: str) -> Optional[tuple[int, str, str]]:
    """Return (part number, base title, normalized key) for explicit part markers."""
    raw = unicodedata.normalize("NFKC", str(value or ""))
    raw = _EXTENSION.sub("", raw)
    match = _PART_MARKER.search(raw)
    if not match:
        return None
    number = int(match.group("number"))
    if not 1 <= number <= 12:
        return None
    base = raw[:match.start()] + " " + raw[match.end():]
    base = _BRACKETED.sub(" ", base)
    base = _HASHTAG.sub(" ", base)
    base = _MENTION.sub(" ", base)
    base = _URL.sub(" ", base)
    base = re.sub(r"[_|]+", " ", base)
    base = re.sub(r"[^\w&'’.,:()!-]+", " ", base, flags=re.UNICODE)
    base = re.sub(r"\s+", " ", base).strip(" -–—|,.:;!_")
    key = normalize_scene_title(base)
    if len(key.replace(" ", "")) < 5:
        return None
    return number, base, key


def _scene_fields(scene: Any) -> tuple[str, int, list[str], str]:
    """Read common scene fields from a StashDB or ThePornDB response."""
    title = str(scene.get("title") or "").strip()
    try:
        duration = int(scene.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    studio_obj = scene.get("studio") if isinstance(scene.get("studio"), dict) else {}
    site_obj = scene.get("site") if isinstance(scene.get("site"), dict) else {}
    studio = str(studio_obj.get("name") or site_obj.get("name") or "").strip()
    performers = []
    for item in scene.get("performers") or []:
        if not isinstance(item, dict):
            continue
        performer = item.get("performer") if isinstance(item.get("performer"), dict) else item
        name = str(performer.get("name") or "").strip()
        if name:
            performers.append(name)
    return title, duration, performers, studio


def _title_evidence(query_title: str, candidate_title: str) -> tuple[bool, float]:
    query_key = normalize_scene_title(query_title)
    candidate_key = normalize_scene_title(candidate_title)
    if not query_key or not candidate_key:
        return False, 0.0
    if query_key == candidate_key:
        return True, 1.0
    query_words = set(query_key.split())
    candidate_words = set(candidate_key.split())
    meaningful = candidate_words - _TITLE_STOPWORDS
    coverage = len(meaningful & query_words) / len(meaningful) if meaningful else 0.0
    ratio = difflib.SequenceMatcher(None, query_key, candidate_key).ratio()
    # Many Telegram titles append studio and cast to the scene title. Accept that
    # only when the complete, reasonably specific catalogue title appears in it.
    strong_containment = len(meaningful) >= 3 and coverage >= 0.90
    return strong_containment or coverage >= 0.65, max(coverage, ratio)


def _has_independent_evidence(
    *, source_texts: Iterable[str], caption_performers: Iterable[str],
    scene_performers: Iterable[str], studio: str,
) -> bool:
    source_keys = {normalize_scene_title(text) for text in source_texts if text}
    source_keys.discard("")
    performer_keys = {normalize_scene_title(name) for name in caption_performers if name}
    catalog_performer_keys = {normalize_scene_title(name) for name in scene_performers if name}
    if performer_keys & catalog_performer_keys:
        return True
    studio_key = normalize_scene_title(studio)
    if not studio_key:
        return False
    studio_words = set(studio_key.split()) - _TITLE_STOPWORDS
    if len(studio_words) < 2:
        return False
    return any(studio_words <= set(source_key.split()) for source_key in source_keys)


def _to_scraped_scene(scene: dict[str, Any], source: str) -> _core.ScrapedScene:
    title, duration, performers, studio = _scene_fields(scene)
    if source == "stashdb":
        images = scene.get("images") if isinstance(scene.get("images"), list) else []
        urls = scene.get("urls") if isinstance(scene.get("urls"), list) else []
        poster = str((images[0] or {}).get("url") or "") if images and isinstance(images[0], dict) else ""
        url = next((str(row.get("url") or "") for row in urls
                    if isinstance(row, dict) and row.get("url")), "")
        external_id = str(scene.get("id") or "")
        url = url or (f"https://stashdb.org/scenes/{external_id}" if external_id else "")
        tags = [str(row.get("name") or "").strip() for row in scene.get("tags", [])
                if isinstance(row, dict) and row.get("name")]
        plot = str(scene.get("details") or "").strip()
        date = str(scene.get("release_date") or scene.get("date") or "")[:10]
    else:
        poster = str(scene.get("poster_image") or scene.get("image") or scene.get("poster") or "")
        url = str(scene.get("url") or "")
        external_id = str(scene.get("id") or scene.get("_id") or "")
        tags = [str(row.get("name") or "").strip() for row in scene.get("tags", [])
                if isinstance(row, dict) and row.get("name")]
        plot = str(scene.get("description") or "").strip()
        date = str(scene.get("date") or "")[:10]
    return _core.ScrapedScene(
        title=title, plot=plot, date=date, duration=duration, poster_url=poster,
        performers=performers, tags=tags, studio=studio, external_id=external_id,
        url=url, confidence=0.92, source=f"{source}:split_title_duration_v1",
    )


def _stashdb_candidates(title: str, http_client: Any) -> list[dict[str, Any]]:
    token = (_core.os.environ.get("STASHDB_API_KEY", "").strip()
             or _core.os.environ.get("STASH_API_KEY", "").strip())
    if not token:
        return []
    query = """
    query SearchSplitScene($term: String!, $limit: Int) {
      searchScenes(term: $term, limit: $limit) {
        scenes {
          id title details date release_date duration
          studio { name }
          performers { performer { name } }
          tags { name }
          images { url }
          urls { url }
        }
      }
    }
    """
    endpoint = _core.os.environ.get("STASHDB_API_URL", "https://stashdb.org/graphql")
    response = _core.request_with_host_limit(
        http_client, "post", endpoint,
        headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "AdultHubSync/1.0 (split-scene lookup)"},
        json={"query": query, "variables": {"term": title, "limit": 20}},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError("StashDB split-title lookup returned GraphQL errors")
    result = ((payload.get("data") or {}).get("searchScenes") or {})
    return result.get("scenes") or []


def _theporndb_candidates(title: str, http_client: Any) -> list[dict[str, Any]]:
    token = (_core.os.environ.get("THEPORNDB_API_TOKEN", "").strip()
             or _core.os.environ.get("THEPORNDB_API_KEY", "").strip())
    if not token:
        return []
    response = _core.request_with_host_limit(
        http_client, "get", "https://api.theporndb.net/scenes",
        params={"q": title, "per_page": 20, "page": 1},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": _core.USER_AGENT},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("data") if isinstance(payload, dict) else None
    return result if isinstance(result, list) else []


def search_split_scene(
    title: str,
    *,
    duration: int,
    source_texts: Iterable[str] = (),
    caption_performers: Iterable[str] = (),
    http_client: Optional[Any] = None,
    duration_tolerance: int = SPLIT_DURATION_TOLERANCE_SECONDS,
) -> Optional[_core.ScrapedScene]:
    """Search both scene catalogues and return only one title/runtime/evidence match."""
    query_title = str(title or "").strip()
    try:
        runtime = int(duration or 0)
        tolerance = max(0, int(duration_tolerance))
    except (TypeError, ValueError):
        return None
    client = http_client or _core.httpx
    if not query_title or runtime <= 0 or client is None:
        return None

    providers = (
        ("stashdb", _stashdb_candidates),
        ("theporndb", _theporndb_candidates),
    )
    for source, fetch in providers:
        try:
            candidates = fetch(query_title, client)
        except Exception as exc:
            _core.logger.warning("%s split-title lookup failed (%s)", source, type(exc).__name__)
            continue
        accepted = _accepted_scenes(candidates, query_title, runtime, tolerance,
                                    source_texts, caption_performers, source)
        if len(accepted) == 1:
            return next(iter(accepted.values()))
        if len(accepted) > 1:
            _core.logger.info("Rejected ambiguous %s split-title match for %r", source, query_title[:120])
    return None


def _accepted_scenes(candidates, query_title: str, runtime: int, tolerance: int,
                     source_texts, caption_performers, source: str) -> dict:
    """Return candidates that match the title and runtime, keyed by external id or title/runtime."""
    accepted: dict[str, _core.ScrapedScene] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate_title, candidate_duration, performers, studio = _scene_fields(item)
        if (not candidate_title or candidate_duration <= 0
                or abs(candidate_duration - runtime) > tolerance):
            continue
        title_ok, _score = _title_evidence(query_title, candidate_title)
        if not title_ok:
            continue
        independent = _has_independent_evidence(
            source_texts=source_texts, caption_performers=caption_performers,
            scene_performers=performers, studio=studio,
        )
        # Exact scene title + summed runtime is sufficient. If the Telegram
        # title has extra words, require corroboration from the caption's
        # explicitly labelled cast or a studio name present in the source.
        exact_title = normalize_scene_title(query_title) == normalize_scene_title(candidate_title)
        if not exact_title and not independent:
            continue
        external_id = str(item.get("id") or item.get("_id") or "")
        dedupe_key = external_id or f"{normalize_scene_title(candidate_title)}:{candidate_duration}"
        accepted[dedupe_key] = _to_scraped_scene(item, source)
    return accepted

def _fetch_split_candidate_rows(conn: Any, attempt_source: str, limit: int = 1000):
    """Telegram rows whose title or caption carries a part marker. None when the query is unavailable."""
    try:
        return conn.execute(
            f"""SELECT v.id,v.title,v.source_filename,v.source_text,v.duration,
                      v.performer_resolution,v.scene_match_external_id,
                      (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
               FROM videos v
               LEFT JOIN external_metadata_attempts a
                 ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
               WHERE v.provider='telegram'
                 AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
                 AND {PART_MARKER_SQL}
               ORDER BY v.first_seen ASC,v.id ASC
               LIMIT %s""",
            (attempt_source, max(1, int(limit))),
        ).fetchall()
    except Exception as exc:
        _core.logger.debug("Split-sequence query unavailable (%s)", type(exc).__name__)
        return None


def _import_split_helpers():
    """(extract_telegram_metadata, parse_split_part_title, search_split_scene), or None when unavailable."""
    try:
        from common.telegram_metadata import extract_telegram_metadata
    except ImportError as exc:
        _core.logger.warning("Split-sequence lookup unavailable (%s)", type(exc).__name__)
        return None
    return extract_telegram_metadata, parse_split_part_title, search_split_scene


def _split_part_entry(row, extract, parse):
    """(channel, base key, part number, part record) for one candidate row, or None when it is not a part."""
    video_id, title, filename, source_text, duration, resolution, matched_id, attempt_due = row
    caption = str(source_text or "")
    extracted = extract(caption)
    candidates = (extracted.get("title"), filename, title, caption)
    parsed = next((result for value in candidates
                   if value and (result := parse(str(value))) is not None), None)
    if not parsed:
        return None
    part_number, base_title, base_key = parsed
    identifier_parts = str(video_id or "").split(":", 2)
    if len(identifier_parts) < 3 or identifier_parts[0] != "telegram":
        return None
    channel = identifier_parts[1]
    try:
        part_duration = int(duration or 0)
    except (TypeError, ValueError):
        part_duration = 0
    if isinstance(resolution, str):
        try:
            resolution = _core.json.loads(resolution)
        except (TypeError, ValueError):
            resolution = {}
    resolution = resolution if isinstance(resolution, dict) else {}
    part = {
        "video_id": str(video_id), "title": base_title, "source_text": caption,
        "source_filename": str(filename or ""), "duration": part_duration,
        "resolution": resolution, "matched_id": str(matched_id or ""),
        "attempt_due": bool(attempt_due), "explicit_cast": (
            extracted.get("performer_names", [])
            if extracted.get("performer_source") == "labeled_cast" else []
        ),
    }
    return channel, base_key, part_number, part


def _group_split_parts(rows, extract, parse) -> dict:
    groups: dict[tuple[str, str], dict[int, list[dict[str, Any]]]] = {}
    for row in rows:
        entry = _split_part_entry(row, extract, parse)
        if entry is None:
            continue
        channel, base_key, part_number, part = entry
        groups.setdefault((channel, base_key), {}).setdefault(part_number, []).append(part)
    return groups


def _split_group_is_complete(parts_by_number: dict, part_count: int) -> bool:
    if part_count < 2 or part_count > 12:
        return False
    # Do not put an incomplete group on cooldown; a later sync may add
    # the missing part and make the sequence searchable.
    if set(parts_by_number) != set(range(1, part_count + 1)):
        return False
    return not any(len(part_rows) != 1 for part_rows in parts_by_number.values())


def _split_parts_eligible(parts: list) -> Optional[int]:
    """Total runtime when the parts may be searched, or None when the group must be skipped."""
    if (not all(part["attempt_due"] for part in parts)
            or any(part["matched_id"] for part in parts)
            or any(int(part["resolution"].get("priority") or 0) >= 70 for part in parts)):
        return None
    durations = [part["duration"] for part in parts]
    if any(duration <= 0 or duration > 8 * 60 * 60 for duration in durations):
        return None
    total_duration = sum(durations)
    if total_duration > 12 * 60 * 60:
        return None
    return total_duration


def _eligible_split_group(parts_by_number: dict):
    """(parts in order, total runtime) for a searchable group, or None."""
    part_count = max(parts_by_number)
    if not _split_group_is_complete(parts_by_number, part_count):
        return None
    parts = [parts_by_number[number][0] for number in range(1, part_count + 1)]
    total_duration = _split_parts_eligible(parts)
    if total_duration is None:
        return None
    return parts, total_duration


def _search_split_group(conn: Any, media_dir: Path, parts: list, total_duration: int, now: int,
                        http_client: Optional[Any], attempt_source: str, search) -> int:
    """Search one complete part sequence and apply the match. Returns the number of parts updated."""
    updated = 0
    query_title = parts[0]["title"]
    source_texts = [text for part in parts
                    for text in (part["source_text"], part["source_filename"])]
    explicit_cast = list(dict.fromkeys(
        name for part in parts for name in part["explicit_cast"] if str(name).strip()
    ))
    try:
        scene = search(
            query_title, duration=total_duration, source_texts=source_texts,
            caption_performers=explicit_cast, http_client=http_client,
            duration_tolerance=SPLIT_DURATION_TOLERANCE_SECONDS,
        )
        outcome = "found" if scene else "no_match"
        if scene:
            for part in parts:
                with conn:
                    applied = _core._apply_matched_scene(
                        conn, media_dir, part["video_id"], scene,
                        http_client=http_client, now=now,
                    )
                    updated += int(applied)
                    if _core.record_attempt:
                        _core.record_attempt(conn, "video", part["video_id"], attempt_source, outcome)
        elif _core.record_attempt:
            for part in parts:
                with conn:
                    _core.record_attempt(conn, "video", part["video_id"], attempt_source, outcome)
    except Exception as exc:
        if _core.record_attempt:
            for part in parts:
                try:
                    with conn:
                        _core.record_attempt(
                            conn, "video", part["video_id"], attempt_source, "error", error=exc,
                        )
                except Exception:
                    _core.logger.debug("Could not save split-title retry state for %s", part["video_id"])
        _core.logger.warning("Split-sequence lookup failed for %s (%s)",
                             parts[0]["video_id"], type(exc).__name__)
    return updated


def _backfill_split_part_matches(
    conn: Any,
    media_dir: Path,
    *,
    limit: int,
    sleep_sec: float,
    http_client: Optional[Any],
) -> Tuple[int, int]:
    """Search complete part sequences once using their combined runtime.

    Incomplete, duplicated, already matched, or higher-priority-resolved groups
    are left untouched. Attempts are tracked per member so normal retry policy
    also applies when the sync process is interrupted part way through a group.
    """
    attempt_source = "telegram_split_title_duration_v1"
    candidate_limit = max(100, int(limit) * 12)
    rows = _fetch_split_candidate_rows(conn, attempt_source, limit=candidate_limit)
    if not rows:
        return 0, 0

    helpers = _import_split_helpers()
    if helpers is None:
        return 0, 0
    extract, parse, search = helpers

    groups = _group_split_parts(rows, extract, parse)

    checked = updated = searched_groups = 0
    now = int(_core.time.time())
    for (_channel, _base_key), parts_by_number in groups.items():
        eligible = _eligible_split_group(parts_by_number)
        if eligible is None:
            continue
        parts, total_duration = eligible
        if searched_groups >= max(1, int(limit)):
            break
        if searched_groups and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        searched_groups += 1
        checked += len(parts)
        updated += _search_split_group(conn, media_dir, parts, total_duration, now, http_client,
                                       attempt_source, search)
    return checked, updated
