"""Telegram channel filtering and resumable history synchronization."""

import json
import re
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Set
from urllib.parse import quote

from common.duration import MIN_VIDEO_DURATION_SECONDS, passes_minimum_video_duration


def safe_error_detail(error: BaseException, limit: int = 600) -> str:
    """Return a useful, bounded error summary without leaking credentials."""
    message = str(error).strip() or "No error message provided."
    # URLs may include query parameters; retain the endpoint path and drop them.
    message = re.sub(
        r"https?://[^\s\]\[()<>\"']+",
        lambda match: match.group(0).split("?", 1)[0].split("#", 1)[0],
        message,
    )
    message = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", message)
    message = re.sub(r"(?i)(\b(?:token|api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", message)
    message = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[redacted-token]", message)
    message = re.sub(r"\s+", " ", message)
    if len(message) > limit:
        message = message[:limit - 1].rstrip() + "…"
    return f"{type(error).__name__}: {message}"


@dataclass(frozen=True)
class TelegramSyncServices:
    """Runtime dependencies supplied by the sync facade.

    Keeping these callbacks explicit lets callers and tests replace the same
    operations they historically patched on ``sync.py``.
    """

    resolver_public_url: str
    resolver_api_url: str
    map_category: Callable
    merge_video_fields: Callable
    purge_videos: Callable
    fetch_json_with_retry: Callable
    flush_collected: Callable
    logger: Any
    time: Any = _time
    observe_stage: Callable | None = None
    category_fetch_delay_seconds: float = 0.0


def telegram_tag_set(values: Any) -> Set[str]:
    return {str(value).strip().lstrip("#").casefold() for value in (values or [])
            if str(value).strip().lstrip("#")}


def telegram_passes_tags(tags: Any, whitelist: Set[str], blacklist: Set[str]) -> bool:
    found = telegram_tag_set(tags)
    return not (found & blacklist) and (not whitelist or bool(found & whitelist))


def telegram_video_tags(video: Dict[str, Any]) -> Set[str]:
    return telegram_tag_set(c for c in video.get("categories", [])
                            if isinstance(c, str) and c.startswith("#"))


def collect_single_telegram_video(
    vid: Dict[str, Any], channel_id: str, studio_name: str,
    collected: Dict[str, Dict[str, Any]], *, services: TelegramSyncServices,
) -> str:
    vid_id = vid.get("provider_video_id") or vid.get("id", "").split(":", 1)[-1]
    comp_id = f"telegram:{vid_id}"
    categories = vid.get("categories", []) or []
    display = studio_name or (
        categories[0].strip() if categories and isinstance(categories[0], str)
        and categories[0].strip() else channel_id
    )
    vid["studio"] = display
    thumbnail = (vid.get("thumbnail") or "").strip()
    if thumbnail.startswith("/"):
        vid["thumbnail"] = f"{services.resolver_public_url.rstrip('/')}{thumbnail}"
    if comp_id not in collected:
        collected[comp_id] = {"video": vid, "genres": set(), "tags": set()}
    else:
        services.merge_video_fields(collected[comp_id]["video"], vid)
        collected[comp_id]["video"]["studio"] = display
    for category in vid.get("categories", []):
        mapped = services.map_category(category)
        if mapped:
            collected[comp_id]["genres"].add(mapped)
        if isinstance(category, str):
            normalized = category.strip()
            if normalized.startswith("#") and len(normalized) > 1:
                hashtag_count = sum(
                    1 for tag in collected[comp_id]["tags"]
                    if isinstance(tag, str) and tag.startswith("#")
                )
                if hashtag_count < 15:
                    collected[comp_id]["tags"].add(normalized)
    collected[comp_id]["tags"].add(display)
    return comp_id


def sync_telegram_channel_pipeline(
    channel_id: str, channel_cfg: Any, conn, media_dir: Path,
    collected: Dict[str, Dict[str, Any]], *, services: TelegramSyncServices,
    sync_mode: str = "scheduled",
):
    """Run latest-only, history-only, or scheduled combined Telegram work.

    Returns the video IDs this channel persisted during the run.
    """
    if sync_mode not in {"scheduled", "new", "backfill"}:
        raise ValueError("Unsupported Telegram sync mode.")
    monotonic = getattr(services.time, "monotonic", _time.monotonic)
    studio = getattr(channel_cfg, "studio", None) or channel_id
    min_dur = max(
        MIN_VIDEO_DURATION_SECONDS,
        int(getattr(channel_cfg, "min_duration_seconds", MIN_VIDEO_DURATION_SECONDS) or 0),
    )
    max_pages = getattr(channel_cfg, "max_pages", 1)
    batch_size = getattr(channel_cfg, "history_batch_size", 100)
    whitelist = telegram_tag_set(getattr(channel_cfg, "tags_whitelist", []))
    blacklist = telegram_tag_set(getattr(channel_cfg, "tags_blacklist", []))
    failures = []
    wl_json, bl_json = json.dumps(sorted(whitelist)), json.dumps(sorted(blacklist))
    row = conn.execute("""SELECT last_min_duration, last_tags_whitelist,
        last_tags_blacklist, backfill_in_progress, backfill_cursor,
        latest_watermark, latest_cursor, latest_head
        FROM telegram_channel_state WHERE channel_id = %s""", (channel_id,)).fetchone()
    filter_changed = False
    to_purge = []
    kept = 0
    if row is None:
        conn.execute("""INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist,
             backfill_in_progress, updated_at) VALUES (%s, %s, %s, %s, 1, %s)""",
            (channel_id, min_dur, wl_json, bl_json, int(services.time.time())))
        backfill, cursor = 1, None
        watermark, latest_cursor, latest_head = None, None, None
        services.logger.info("Telegram %s: scheduled initial history scan", channel_id)
    else:
        old_min, old_wl_raw, old_bl_raw, backfill, cursor, watermark, latest_cursor, latest_head = row
        old_min = old_min if old_min is not None else 0
        old_wl = set(json.loads(old_wl_raw or "[]"))
        old_bl = set(json.loads(old_bl_raw or "[]"))
        changed = (min_dur, whitelist, blacklist) != (old_min, old_wl, old_bl)
        filter_changed = changed
        relaxed = min_dur < old_min or bool(old_wl - whitelist) or bool(old_bl - blacklist)
        if changed:
            if relaxed:
                backfill, cursor = 1, None
                services.logger.info(
                    "Telegram %s: filters relaxed; scheduled full history scan", channel_id
                )
            # Keep the previous filter values until fetching completes. If the
            # fetch fails, the next run must still detect this filter change
            # and retry the corresponding purge.
            conn.execute("""UPDATE telegram_channel_state SET
                backfill_in_progress = %s, backfill_cursor = %s, updated_at = %s
                WHERE channel_id = %s""",
                (backfill, cursor, int(services.time.time()), channel_id))
            prefixes = (
                f"telegram:{channel_id}:%",
                f"telegram:{channel_id[4:] if channel_id.startswith('-100') else '-100' + channel_id.lstrip('-')}:%",
            )
            rows = conn.execute("""SELECT id, duration, tags FROM videos
                WHERE provider = 'telegram' AND (id LIKE %s OR id LIKE %s)""", prefixes).fetchall()
            for vid_id, duration, tags_raw in rows:
                try:
                    stored_tags = json.loads(tags_raw or "[]")
                except (ValueError, TypeError):
                    stored_tags = []
                # Unknown duration or missing tags cannot prove a filter failure.
                if duration is not None and duration > 0 and duration < min_dur:
                    to_purge.append(vid_id)
                elif (whitelist or blacklist) and stored_tags and not telegram_passes_tags(
                    stored_tags, whitelist, blacklist
                ):
                    to_purge.append(vid_id)
                else:
                    kept += 1

    if sync_mode == "backfill" and not backfill:
        backfill, cursor = 1, None
        conn.execute(
            "UPDATE telegram_channel_state SET backfill_in_progress=1,backfill_cursor=NULL,updated_at=%s "
            "WHERE channel_id=%s",
            (int(services.time.time()), channel_id),
        )
        conn.commit()

    if sync_mode != "backfill" and watermark is None and not backfill:
        # A previously empty channel may receive more than one batch before
        # its first watermark exists; history must cover everything below it.
        backfill, cursor = 1, None
        conn.execute("UPDATE telegram_channel_state SET backfill_in_progress=1, backfill_cursor=NULL WHERE channel_id=%s", (channel_id,))

    # Keep initialization and a scheduled history restart durable even if a
    # later network request or batch write fails and rolls back its transaction.
    conn.commit()

    synced_ids: set[str] = set()

    def collect(videos):
        started = monotonic()
        accepted = 0
        collected_ids: list[str] = []
        failed = False
        try:
            for video in videos:
                if (passes_minimum_video_duration(video.get("duration"))
                        and telegram_passes_tags(telegram_video_tags(video), whitelist, blacklist)):
                    collected_ids.append(collect_single_telegram_video(
                        video, channel_id, studio, collected, services=services
                    ))
                    accepted += 1
            return accepted, collected_ids
        except Exception:
            failed = True
            raise
        finally:
            if services.observe_stage:
                services.observe_stage("telegram_parse_filter", monotonic() - started, failed)

    for page in range(1, max_pages + 1) if sync_mode != "backfill" else ():
        fetch_started = monotonic()
        url = (f"{services.resolver_api_url.rstrip('/')}/admin/api/telegram/history/{quote(channel_id)}"
               "?limit=50")
        if latest_cursor is not None:
            url += f"&offset_id={latest_cursor}"
        try:
            batch = services.fetch_json_with_retry(
                url, timeout=20.0, desc=f"category/telegram/{channel_id}/p.{page}"
            )
            count = batch["message_count"]
            next_id = batch["next_offset_id"]
            head = batch["highest_id"]
            if count and (head is None or next_id is None or head < next_id or
                          (latest_cursor is not None and (head >= latest_cursor or next_id >= latest_cursor))):
                raise ValueError("Telegram latest cursor did not advance")
            if latest_head is None and count:
                latest_head = head
            accepted, page_ids = collect(batch["videos"])
            services.flush_collected(collected, conn, media_dir, f"Telegram: {studio}", raise_on_error=True)
            synced_ids.update(page_ids)
            complete = not count or not batch["has_more"] or watermark is None or next_id <= watermark
            if complete:
                watermark = max(watermark or 0, latest_head or 0) or None
                latest_cursor = latest_head = None
            else:
                latest_cursor = next_id
            conn.execute("""UPDATE telegram_channel_state SET latest_watermark=%s,
                latest_cursor=%s, latest_head=%s, updated_at=%s WHERE channel_id=%s""",
                (watermark, latest_cursor, latest_head, int(services.time.time()), channel_id))
            conn.commit()
            services.logger.info("Telegram %s latest batch %d: accepted=%d cursor=%s complete=%s",
                                 channel_id, page, accepted, latest_cursor, complete)
        except Exception as exc:
            conn.rollback()
            if services.observe_stage:
                services.observe_stage("telegram_category_fetch", monotonic() - fetch_started, True)
            detail = safe_error_detail(exc)
            failures.append(f"latest page {page}: {detail}")
            services.logger.error(
                "Telegram latest fetch failed for %s page %d: %s", channel_id, page, detail
            )
            break
        else:
            if services.observe_stage:
                services.observe_stage("telegram_category_fetch", monotonic() - fetch_started)
            if complete:
                break
        # Space requests even after failures so a throttled channel does not
        # immediately trigger another request on the next page.
        delay = max(0.0, float(services.category_fetch_delay_seconds))
        if delay:
            services.time.sleep(delay)

    if sync_mode != "backfill" and latest_cursor is not None and not failures:
        services.logger.info("Telegram %s latest scan incomplete; will resume at message %s", channel_id, latest_cursor)

    if backfill and sync_mode != "new":
        url = (f"{services.resolver_api_url.rstrip('/')}/admin/api/telegram/history/{quote(channel_id)}"
               f"?limit={batch_size}")
        if cursor is not None:
            url += f"&offset_id={cursor}"
        try:
            history_started = monotonic()
            try:
                batch = services.fetch_json_with_retry(
                    url, timeout=30.0, desc=f"backfill/telegram/{channel_id}"
                )
            except Exception:
                if services.observe_stage:
                    services.observe_stage("telegram_history_fetch", monotonic() - history_started, True)
                raise
            if services.observe_stage:
                services.observe_stage("telegram_history_fetch", monotonic() - history_started)
            count = batch["message_count"]
            next_cursor = batch["next_offset_id"]
            added, batch_ids = collect(batch["videos"])
            # Persist accepted entries before advancing cursor. A failed write retries this batch.
            persist_started = monotonic()
            try:
                services.flush_collected(
                    collected, conn, media_dir, f"Telegram: {studio}",
                    raise_on_error=True,
                )
            except Exception:
                if services.observe_stage:
                    services.observe_stage("telegram_persist", monotonic() - persist_started, True)
                raise
            else:
                synced_ids.update(batch_ids)
                if services.observe_stage:
                    services.observe_stage("telegram_persist", monotonic() - persist_started)
            if count and (next_cursor is None or (cursor is not None and next_cursor >= cursor)):
                raise ValueError("Telegram history cursor did not advance")
            completed = not batch["has_more"]
            conn.execute("""UPDATE telegram_channel_state SET backfill_in_progress = %s,
                backfill_cursor = %s, backfill_completed_at = %s, updated_at = %s
                WHERE channel_id = %s""", (0 if completed else 1, next_cursor,
                int(services.time.time()) if completed else None,
                int(services.time.time()), channel_id))
            conn.commit()
            services.logger.info(
                "Telegram %s history: messages=%d accepted=%d cursor=%s complete=%s",
                channel_id, count, added, next_cursor, completed,
            )
        except Exception as exc:
            detail = safe_error_detail(exc)
            failures.append(f"history batch: {detail}")
            services.logger.warning(
                "Telegram history batch failed for %s: %s; will resume", channel_id, detail
            )
    persist_started = monotonic()
    pending_ids = set(collected)
    try:
        # A publication or persistence failure must fail the channel: it is the
        # only signal that keeps the source out of providers_succeeded, so
        # cleanup cannot treat the unpublished entry as an ordinary orphan.
        services.flush_collected(collected, conn, media_dir, f"Telegram: {studio}",
                                 raise_on_error=True)
    except Exception:
        if services.observe_stage:
            services.observe_stage("telegram_persist", monotonic() - persist_started, True)
        raise
    else:
        synced_ids.update(pending_ids)
        if services.observe_stage:
            services.observe_stage("telegram_persist", monotonic() - persist_started)
    if failures:
        raise RuntimeError(
            "One or more Telegram fetch/history operations failed: "
            + "; ".join(failures[:8])
        )
    if filter_changed and to_purge:
        removed = services.purge_videos(
            conn, media_dir, to_purge, "Telegram filter changed",
            providers_succeeded={f"telegram:{channel_id}"},
        )
    if filter_changed:
        conn.execute("""UPDATE telegram_channel_state SET last_min_duration = %s,
            last_tags_whitelist = %s, last_tags_blacklist = %s, updated_at = %s
            WHERE channel_id = %s""",
            (min_dur, wl_json, bl_json, int(services.time.time()), channel_id))
        conn.commit()
    if filter_changed and to_purge:
        services.logger.info(
            "Telegram %s: reevaluated stored entries: kept=%d removed=%d",
            channel_id, kept, removed,
        )
    return synced_ids
