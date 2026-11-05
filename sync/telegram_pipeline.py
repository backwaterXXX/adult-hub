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


class TelegramSyncResult(set):
    """Synced IDs and resumable warnings; incomplete sources are excluded from cleanup."""
    def __init__(self, values, warnings=()):
        super().__init__(values)
        self.warnings = list(warnings)


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


class _ChannelSync:
    """One channel's pass: filter reevaluation, latest-page scan, history backfill and persistence."""

    def __init__(self, channel_id: str, channel_cfg: Any, conn, media_dir: Path,
                 collected: Dict[str, Dict[str, Any]], services: TelegramSyncServices, sync_mode: str):
        self.channel_id = channel_id
        self.conn = conn
        self.media_dir = media_dir
        self.collected = collected
        self.services = services
        self.sync_mode = sync_mode
        self.monotonic = getattr(services.time, "monotonic", _time.monotonic)
        self.studio = getattr(channel_cfg, "studio", None) or channel_id
        self.min_dur = max(
            MIN_VIDEO_DURATION_SECONDS,
            int(getattr(channel_cfg, "min_duration_seconds", MIN_VIDEO_DURATION_SECONDS) or 0),
        )
        self.max_pages = getattr(channel_cfg, "max_pages", 1)
        self.batch_size = getattr(channel_cfg, "history_batch_size", 100)
        self.whitelist = telegram_tag_set(getattr(channel_cfg, "tags_whitelist", []))
        self.blacklist = telegram_tag_set(getattr(channel_cfg, "tags_blacklist", []))
        self.failures: list = []
        self.resumable_warnings: list = []
        self.wl_json, self.bl_json = json.dumps(sorted(self.whitelist)), json.dumps(sorted(self.blacklist))
        self.backfill = 0
        self.cursor = None
        self.watermark = None
        self.latest_cursor = None
        self.latest_head = None
        self.filter_changed = False
        self.to_purge: list = []
        self.kept = 0
        self.synced_ids: set[str] = set()

    def run(self) -> TelegramSyncResult:
        self._load_channel_state()
        if self.sync_mode == "backfill" and not self.backfill:
            self._force_backfill()
        if self.sync_mode != "backfill" and self.watermark is None and not self.backfill:
            # A previously empty channel may receive more than one batch before
            # its first watermark exists; history must cover everything below it.
            self._restart_empty_history()

        # Keep initialization and a scheduled history restart durable even if a
        # later network request or batch write fails and rolls back its transaction.
        self.conn.commit()

        self._scan_latest_pages()

        if self.sync_mode != "backfill" and self.latest_cursor is not None and not self.failures:
            self.services.logger.info("Telegram %s latest scan incomplete; will resume at message %s", self.channel_id, self.latest_cursor)

        if self.backfill and self.sync_mode != "new":
            self._run_history_batch()

        # A publication or persistence failure must fail the channel: it is the
        # only signal that keeps the source out of providers_succeeded, so
        # cleanup cannot treat the unpublished entry as an ordinary orphan.
        self._persist_collected(set(self.collected))
        if self.failures:
            raise RuntimeError(
                "One or more Telegram fetch/history operations failed: "
                + "; ".join(self.failures[:8])
            )
        self._apply_filter_outcome()
        return TelegramSyncResult(self.synced_ids, self.resumable_warnings)

    def _load_channel_state(self) -> None:
        row = self.conn.execute("""SELECT last_min_duration, last_tags_whitelist,
            last_tags_blacklist, backfill_in_progress, backfill_cursor,
            latest_watermark, latest_cursor, latest_head
            FROM telegram_channel_state WHERE channel_id = %s""", (self.channel_id,)).fetchone()
        if row is None:
            self._start_initial_history()
        else:
            self._reconcile_channel_state(row)

    def _start_initial_history(self) -> None:
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist,
             backfill_in_progress, updated_at) VALUES (%s, %s, %s, %s, 1, %s)""",
            (self.channel_id, self.min_dur, self.wl_json, self.bl_json, int(self.services.time.time())))
        self.backfill, self.cursor = 1, None
        self.watermark, self.latest_cursor, self.latest_head = None, None, None
        self.services.logger.info("Telegram %s: scheduled initial history scan", self.channel_id)

    def _reconcile_channel_state(self, row) -> None:
        old_min, old_wl_raw, old_bl_raw, backfill, cursor, watermark, latest_cursor, latest_head = row
        self.backfill, self.cursor, self.watermark = backfill, cursor, watermark
        self.latest_cursor, self.latest_head = latest_cursor, latest_head
        old_min = old_min if old_min is not None else 0
        old_wl = set(json.loads(old_wl_raw or "[]"))
        old_bl = set(json.loads(old_bl_raw or "[]"))
        changed = (self.min_dur, self.whitelist, self.blacklist) != (old_min, old_wl, old_bl)
        self.filter_changed = changed
        relaxed = self.min_dur < old_min or bool(old_wl - self.whitelist) or bool(old_bl - self.blacklist)
        if changed:
            self._reevaluate_stored_videos(relaxed)

    def _reevaluate_stored_videos(self, relaxed: bool) -> None:
        if relaxed:
            self.backfill, self.cursor = 1, None
            self.services.logger.info(
                "Telegram %s: filters relaxed; scheduled full history scan", self.channel_id
            )
        # Keep the previous filter values until fetching completes. If the
        # fetch fails, the next run must still detect this filter change
        # and retry the corresponding purge.
        self.conn.execute("""UPDATE telegram_channel_state SET
            backfill_in_progress = %s, backfill_cursor = %s, updated_at = %s
            WHERE channel_id = %s""",
            (self.backfill, self.cursor, int(self.services.time.time()), self.channel_id))
        prefixes = (
            f"telegram:{self.channel_id}:%",
            f"telegram:{self.channel_id[4:] if self.channel_id.startswith('-100') else '-100' + self.channel_id.lstrip('-')}:%",
        )
        rows = self.conn.execute("""SELECT id, duration, tags FROM videos
            WHERE provider = 'telegram' AND (id LIKE %s OR id LIKE %s)""", prefixes).fetchall()
        for vid_id, duration, tags_raw in rows:
            self._classify_stored_video(vid_id, duration, tags_raw)

    def _classify_stored_video(self, vid_id: str, duration: Any, tags_raw: Any) -> None:
        try:
            stored_tags = json.loads(tags_raw or "[]")
        except (ValueError, TypeError):
            stored_tags = []
        # Unknown duration or missing tags cannot prove a filter failure.
        if duration is not None and duration > 0 and duration < self.min_dur:
            self.to_purge.append(vid_id)
        elif (self.whitelist or self.blacklist) and stored_tags and not telegram_passes_tags(
            stored_tags, self.whitelist, self.blacklist
        ):
            self.to_purge.append(vid_id)
        else:
            self.kept += 1

    def _force_backfill(self) -> None:
        self.backfill, self.cursor = 1, None
        self.conn.execute(
            "UPDATE telegram_channel_state SET backfill_in_progress=1,backfill_cursor=NULL,updated_at=%s "
            "WHERE channel_id=%s",
            (int(self.services.time.time()), self.channel_id),
        )
        self.conn.commit()

    def _restart_empty_history(self) -> None:
        self.backfill, self.cursor = 1, None
        self.conn.execute("UPDATE telegram_channel_state SET backfill_in_progress=1, backfill_cursor=NULL WHERE channel_id=%s", (self.channel_id,))

    def _collect(self, videos):
        started = self.monotonic()
        accepted = 0
        collected_ids: list[str] = []
        failed = False
        try:
            for video in videos:
                if (passes_minimum_video_duration(video.get("duration"))
                        and telegram_passes_tags(telegram_video_tags(video), self.whitelist, self.blacklist)):
                    collected_ids.append(collect_single_telegram_video(
                        video, self.channel_id, self.studio, self.collected, services=self.services
                    ))
                    accepted += 1
            return accepted, collected_ids
        except Exception:
            failed = True
            raise
        finally:
            if self.services.observe_stage:
                self.services.observe_stage("telegram_parse_filter", self.monotonic() - started, failed)

    def _scan_latest_pages(self) -> None:
        for page in range(1, self.max_pages + 1) if self.sync_mode != "backfill" else ():
            if self._scan_latest_page(page):
                break
            # Space requests even after failures so a throttled channel does not
            # immediately trigger another request on the next page.
            delay = max(0.0, float(self.services.category_fetch_delay_seconds))
            if delay:
                self.services.time.sleep(delay)

    def _scan_latest_page(self, page: int) -> bool:
        """Fetch one latest-history page. True when the scan should stop (failed or complete)."""
        fetch_started = self.monotonic()
        url = (f"{self.services.resolver_api_url.rstrip('/')}/admin/api/telegram/history/{quote(self.channel_id)}"
               "?limit=50")
        if self.latest_cursor is not None:
            url += f"&offset_id={self.latest_cursor}"
        try:
            batch = self.services.fetch_json_with_retry(
                url, timeout=20.0, desc=f"category/telegram/{self.channel_id}/p.{page}"
            )
            count = batch["message_count"]
            next_id = batch["next_offset_id"]
            head = batch["highest_id"]
            if count and (head is None or next_id is None or head < next_id or
                          (self.latest_cursor is not None and (head >= self.latest_cursor or next_id >= self.latest_cursor))):
                raise ValueError("Telegram latest cursor did not advance")
            if self.latest_head is None and count:
                self.latest_head = head
            accepted, page_ids = self._collect(batch["videos"])
            self.services.flush_collected(self.collected, self.conn, self.media_dir, f"Telegram: {self.studio}", raise_on_error=True)
            self.synced_ids.update(page_ids)
            complete = not count or not batch["has_more"] or self.watermark is None or next_id <= self.watermark
            if complete:
                self.watermark = max(self.watermark or 0, self.latest_head or 0) or None
                self.latest_cursor = self.latest_head = None
            else:
                self.latest_cursor = next_id
            self.conn.execute("""UPDATE telegram_channel_state SET latest_watermark=%s,
                latest_cursor=%s, latest_head=%s, updated_at=%s WHERE channel_id=%s""",
                (self.watermark, self.latest_cursor, self.latest_head, int(self.services.time.time()), self.channel_id))
            self.conn.commit()
            self.services.logger.info("Telegram %s latest batch %d: accepted=%d cursor=%s complete=%s",
                                     self.channel_id, page, accepted, self.latest_cursor, complete)
        except Exception as exc:
            self.conn.rollback()
            if self.services.observe_stage:
                self.services.observe_stage("telegram_category_fetch", self.monotonic() - fetch_started, True)
            detail = safe_error_detail(exc)
            self.failures.append(f"latest page {page}: {detail}")
            self.services.logger.error(
                "Telegram latest fetch failed for %s page %d: %s", self.channel_id, page, detail
            )
            return True
        else:
            if self.services.observe_stage:
                self.services.observe_stage("telegram_category_fetch", self.monotonic() - fetch_started)
            return complete

    def _run_history_batch(self) -> None:
        url = (f"{self.services.resolver_api_url.rstrip('/')}/admin/api/telegram/history/{quote(self.channel_id)}"
               f"?limit={self.batch_size}")
        if self.cursor is not None:
            url += f"&offset_id={self.cursor}"
        fetching_history = True
        try:
            history_started = self.monotonic()
            batch = self._request_history_batch(url, history_started)
            fetching_history = False
            if self.services.observe_stage:
                self.services.observe_stage("telegram_history_fetch", self.monotonic() - history_started)
            count = batch["message_count"]
            next_cursor = batch["next_offset_id"]
            added, batch_ids = self._collect(batch["videos"])
            # Persist accepted entries before advancing cursor. A failed write retries this batch.
            self._persist_collected(batch_ids)
            if count and (next_cursor is None or (self.cursor is not None and next_cursor >= self.cursor)):
                raise ValueError("Telegram history cursor did not advance")
            completed = not batch["has_more"]
            self.conn.execute("""UPDATE telegram_channel_state SET backfill_in_progress = %s,
                backfill_cursor = %s, backfill_completed_at = %s, updated_at = %s
                WHERE channel_id = %s""", (0 if completed else 1, next_cursor,
                int(self.services.time.time()) if completed else None,
                int(self.services.time.time()), self.channel_id))
            self.conn.commit()
            self.services.logger.info(
                "Telegram %s history: messages=%d accepted=%d cursor=%s complete=%s",
                self.channel_id, count, added, next_cursor, completed,
            )
        except Exception as exc:
            detail = safe_error_detail(exc)
            (self.resumable_warnings if fetching_history else self.failures).append(f"history batch: {detail}")
            self.services.logger.warning(
                "Telegram history batch failed for %s: %s; will resume", self.channel_id, detail
            )

    def _request_history_batch(self, url: str, history_started: float):
        try:
            return self.services.fetch_json_with_retry(
                url, timeout=120.0, desc=f"backfill/telegram/{self.channel_id}"
            )
        except Exception:
            if self.services.observe_stage:
                self.services.observe_stage("telegram_history_fetch", self.monotonic() - history_started, True)
            raise

    def _persist_collected(self, ids) -> None:
        """Flush the collected entries to the catalogue and count ``ids`` as synced."""
        persist_started = self.monotonic()
        try:
            self.services.flush_collected(
                self.collected, self.conn, self.media_dir, f"Telegram: {self.studio}",
                raise_on_error=True,
            )
        except Exception:
            if self.services.observe_stage:
                self.services.observe_stage("telegram_persist", self.monotonic() - persist_started, True)
            raise
        else:
            self.synced_ids.update(ids)
            if self.services.observe_stage:
                self.services.observe_stage("telegram_persist", self.monotonic() - persist_started)

    def _apply_filter_outcome(self) -> None:
        removed = None
        if self.filter_changed and self.to_purge and not self.resumable_warnings:
            removed = self.services.purge_videos(
                self.conn, self.media_dir, self.to_purge, "Telegram filter changed",
                providers_succeeded={f"telegram:{self.channel_id}"},
            )
        if self.filter_changed and not self.resumable_warnings:
            self.conn.execute("""UPDATE telegram_channel_state SET last_min_duration = %s,
                last_tags_whitelist = %s, last_tags_blacklist = %s, updated_at = %s
                WHERE channel_id = %s""",
                (self.min_dur, self.wl_json, self.bl_json, int(self.services.time.time()), self.channel_id))
            self.conn.commit()
        if self.filter_changed and self.to_purge and not self.resumable_warnings:
            self.services.logger.info(
                "Telegram %s: reevaluated stored entries: kept=%d removed=%d",
                self.channel_id, self.kept, removed,
            )


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
    return _ChannelSync(channel_id, channel_cfg, conn, media_dir, collected, services, sync_mode).run()
