"""Top-level scheduled sync orchestration."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Set
from common.http_rate_limit import snapshot_host_rate_limit_stats

if __package__:
    from . import sync as _core
else:
    import sync as _core


def _run_sync_locked(source_filter: Optional[str] = None, sync_mode: str = "scheduled",
                     skip_telegram_channels: bool = False):
    snapshot_host_rate_limit_stats(reset=True)
    _core._ACTIVE_SYNC_METRICS = {
        "stages": {},
        "http": {"failed_attempts": 0, "retries": 0, "rate_limited": 0},
        "http_hosts": {},
    }

    def run_timed_stage(name: str, operation):
        started = _core.time.monotonic()
        failed = False
        try:
            return operation()
        except Exception:
            failed = True
            raise
        finally:
            _core._observe_sync_stage(name, _core.time.monotonic() - started, failed)

    started_at = int(_core.time.time())
    _core.logger.info("Starting Adult-Hub sync (deduplicated)...")
    _core.logger.info(f"Media Dir: {_core.MEDIA_DIR}")
    _core.logger.info(f"Resolver API: {_core.RESOLVER_API_URL}")
    _core.logger.info(f"Resolver Public URL: {_core.RESOLVER_PUBLIC_URL}")
    _core.logger.info(f"Retention Days: {_core.RETENTION_DAYS}")

    conn = _core.init_db()
    try:
        from performer_catalog_sync import sync_catalog_if_due
        catalogue_result = sync_catalog_if_due(conn)
        if catalogue_result:
            _core.logger.info("Performer catalogue refreshed: %s people, %s aliases", *catalogue_result)
    except Exception as e:
        _core.logger.warning("Performer catalogue refresh failed; using last local copy: %s", e)
    before_ids = _core._video_id_snapshot(conn)

    # 0. Migrate filenames, migrate categories, clean legacy folders and old transcodes.
    # NOTE (ETAP B): cleanup_orphaned_records() no longer runs here — it needs
    # the per-provider success signal collected below, so it runs at step 9
    # alongside cleanup_retention() with providers_succeeded.
    _core.migrate_synced_files(conn, _core.MEDIA_DIR)
    _core.migrate_categories(conn, _core.MEDIA_DIR)
    _core.migrate_display_titles(conn, _core.MEDIA_DIR)
    _core.migrate_redundant_performers(conn, _core.MEDIA_DIR)
    _core.cleanup_legacy_folders(_core.MEDIA_DIR)
    _core.cleanup_old_transcodes()
    try:
        _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR)
    except Exception as e:
        _core.logger.warning("Could not process queued duplicate decisions: %s", e)

    # Dictionary: composite_id -> {"video": dict, "genres": set(), "tags": set()}
    collected: Dict[str, Dict[str, Any]] = {}
    # ETAP B: providers (and telegram channels) that synced cleanly in THIS
    # run. Destructive upkeep (retention, orphaned purge, .strm rewrite,
    # dead/unplayable sweeps) only touches sources present in this set that
    # are also registered and enabled. A provider with any failed page/fetch
    # stays out: its historical rows, files and .strm URLs are preserved.
    providers_succeeded: Set[str] = set()
    source_errors: Dict[str, str] = {}
    source_error_details: Dict[str, str] = {}
    source_durations: Dict[str, float] = {}

    # 1. Fetch Telegram videos.
    telegram_channel_filter = source_filter.split(":", 1)[1] if source_filter and source_filter.startswith("telegram:") else source_filter
    telegram_filter_enabled = (
        not source_filter or source_filter == "telegram"
        or bool(_core.CONFIG and _core.CONFIG.get_telegram_channel(telegram_channel_filter))
    )
    if _core.SYNC_TELEGRAM and telegram_filter_enabled and skip_telegram_channels:
        _core.logger.info("Skipping Telegram channel scans by request; continuing full maintenance pass.")
    if _core.SYNC_TELEGRAM and telegram_filter_enabled and not skip_telegram_channels:
        tg_src = _core.CONFIG.get_source("telegram") if _core.CONFIG else None
        if tg_src:
            enabled_channels = [ch for ch in tg_src.channels if ch.enabled]
            _core.logger.info(f"Syncing Telegram ({len(enabled_channels)} enabled channel(s))...")
            for ch in enabled_channels:
                if source_filter and source_filter != "telegram" and telegram_channel_filter != ch.id:
                    continue
                source_started = _core.time.monotonic()
                try:
                    _core.sync_telegram_channel_pipeline(ch.id, ch, conn, _core.MEDIA_DIR, collected, sync_mode=sync_mode)
                    providers_succeeded.add(f"telegram:{ch.id}")
                    if sync_mode == "scheduled":
                        try:
                            current_ids = {video_id for video_id in collected
                                           if video_id.startswith(f"telegram:{ch.id}:")}
                            outcome = _core.reconcile_telegram_channel(
                                conn, ch.id, _core.MEDIA_DIR,
                                _core.DATA_DIR / "telegram-unavailable-quarantine",
                                _core.RESOLVER_API_URL, _core.fetch_json_with_retry,
                                _core.logger, current_video_ids=current_ids,
                                limit=int(os.getenv("TELEGRAM_AVAILABILITY_CHECKS_PER_RUN", "20")),
                                now=int(_core.time.time()),
                            )
                            _core.logger.info("Telegram %s availability: %s", ch.id, outcome)
                        except Exception as e:
                            _core.logger.warning("Telegram availability reconciliation failed for %s: %s", ch.id, e)
                except Exception as e:
                    detail = _core._safe_error_detail(e)
                    _core.logger.error("Telegram sync failed for %s: %s", ch.id, detail)
                    source_errors[f"telegram:{ch.id}"] = f"Sync failed ({type(e).__name__})."
                    source_error_details[f"telegram:{ch.id}"] = detail
                finally:
                    source_durations[f"telegram:{ch.id}"] = round(_core.time.monotonic() - source_started, 3)
                _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)
        else:
            _core.logger.info(f"Syncing Telegram ({len(_core.TELEGRAM_CHANNELS)} channel(s), {_core.TELEGRAM_MAX_PAGES} page(s))...")
            for channel in _core.TELEGRAM_CHANNELS:
                channel_name = channel.strip()
                if not channel_name:
                    continue
                if source_filter and source_filter != "telegram" and telegram_channel_filter != channel_name:
                    continue
                class _SimpleCh:
                    studio = channel_name
                    min_duration_seconds = 420
                    max_pages = _core.TELEGRAM_MAX_PAGES
                    history_batch_size = 100
                source_started = _core.time.monotonic()
                try:
                    _core.sync_telegram_channel_pipeline(channel_name, _SimpleCh(), conn, _core.MEDIA_DIR, collected,
                                                   sync_mode=sync_mode)
                    providers_succeeded.add(f"telegram:{channel_name}")
                except Exception as e:
                    detail = _core._safe_error_detail(e)
                    _core.logger.error("Telegram sync failed for %s: %s", channel_name, detail)
                    source_errors[f"telegram:{channel_name}"] = f"Sync failed ({type(e).__name__})."
                    source_error_details[f"telegram:{channel_name}"] = detail
                finally:
                    source_durations[f"telegram:{channel_name}"] = round(_core.time.monotonic() - source_started, 3)
                _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)

    # 2. Drop catalog entries the availability worker has denylisted.
    if _core.HIDE_DEAD_STREAMS:
        try:
            _core.hide_dead_streams(collected, conn)
        except Exception as e:
            _core.logger.warning(f"Dead-stream filtering failed (keeping all): {e}")

    # 8. Flush any remaining videos (if any)
    _core.flush_collected(collected, conn, _core.MEDIA_DIR, "remaining")
    _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)

    # 8b. Rewrite stale .strm files to the currently configured per-provider
    # mode (e.g. redirect -> proxy flips apply without waiting for re-sync).
    # Guarded: absent/disabled/failed sources keep their stored URLs.
    try:
        _core.rewrite_all_stream_files(conn, _core.MEDIA_DIR, providers_succeeded)
    except Exception as e:
        _core.logger.warning(f"Stream rewrite pass failed: {e}")

    # Scene identification for Telegram is the first metadata-enrichment step:
    # fingerprints must run before performer-profile lookups and PBC requests.
    try:
        from scene_scrapers import backfill_scenes  # type: ignore
        run_timed_stage(
            "metadata_scenes",
            lambda: backfill_scenes(conn, _core.MEDIA_DIR, limit=_core.SCENE_BACKFILL_PER_RUN),
        )
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning(f"Scene fingerprint backfill pass failed: {e}")
    _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)

    # 8c. Backfill missing performers from studio title naming conventions.
    try:
        run_timed_stage(
            "performer_title_backfill",
            lambda: _core.backfill_performers_from_titles(conn, _core.MEDIA_DIR),
        )
    except Exception as e:
        _core.logger.warning(f"Title performer backfill pass failed: {e}")
    _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)

    # 8d. Performer enrichment: PBC aliases+bios (rate-limited).
    try:
        from pbc import backfill_pbc  # type: ignore
        run_timed_stage("metadata_pbc", lambda: backfill_pbc(conn, limit=_core.PBC_BACKFILL_PER_RUN))
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning(f"PBC backfill pass failed: {e}")
    try:
        from theporndb import backfill_tpdb  # type: ignore
        run_timed_stage("metadata_tpdb", lambda: backfill_tpdb(conn, limit=_core.TPDB_BACKFILL_PER_RUN))
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning(f"ThePornDB backfill pass failed: {e}")
    try:
        from stashdb import backfill_stashdb  # type: ignore
        run_timed_stage("metadata_stashdb", lambda: backfill_stashdb(conn, limit=_core.TPDB_BACKFILL_PER_RUN))
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning(f"StashDB backfill pass failed: {e}")

    # 9. Clean up expired videos via retention policy + orphaned records.
    # Both use the success signal: absent, disabled and failed sources keep
    # every historical row, file and .strm URL.
    try:
        _core.cleanup_orphaned_records(_core.MEDIA_DIR, conn, providers_succeeded)
    except Exception as e:
        _core.logger.warning(f"Orphaned-record cleanup failed: {e}")
    _core.cleanup_retention(conn, _core.MEDIA_DIR, _core.RETENTION_DAYS, providers_succeeded)

    # Locked Telegram entries require an explicit unlock/refresh/relock cycle
    # when their authoritative local NFO changes. Reconcile existing entries
    # before queueing the global scan so targeted refreshes avoid its backlog.
    # New entries import their locked NFO during the scan; the next run verifies
    # their metadata and records the durable hash state.
    try:
        jellyfin_result = run_timed_stage(
            "jellyfin_metadata_reconciliation", _core.sync_jellyfin_telegram_metadata,
        )
        if isinstance(jellyfin_result, dict):
            _core._ACTIVE_SYNC_METRICS["jellyfin_refresh_poll"] = dict(
                jellyfin_result.get("refresh_poll") or {}
            )
    except Exception as e:
        _core.logger.warning(f"Telegram metadata reconciliation failed: {e}")
    _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)

    # 10. Trigger Jellyfin Refresh
    _core.trigger_jellyfin_refresh()

    # Publish structured StashDB details to existing Jellyfin person pages.
    try:
        from jellyfin_people import sync_jellyfin_people
        sync_jellyfin_people(
            conn, _core.JELLYFIN_URL, _core.JELLYFIN_API_KEY,
            limit=_core.JELLYFIN_PEOPLE_PER_RUN,
        )
    except Exception as e:
        _core.logger.warning(f"Jellyfin person metadata pass failed: {e}")

    # 11. Sync display titles through Jellyfin's supported item API.
    _core.sync_jellyfin_titles()
    expected_sources = set()
    if _core.CONFIG:
        for source in _core.CONFIG.get_enabled_sources():
            if source.provider == "telegram":
                expected_sources.update(
                    f"telegram:{channel.id}" for channel in source.channels if channel.enabled
                )
            else:
                expected_sources.add(source.provider)
    if skip_telegram_channels:
        expected_sources = {name for name in expected_sources
                            if name != "telegram" and not name.startswith("telegram:")}
    if source_filter:
        if source_filter == "telegram":
            expected_sources = {name for name in expected_sources if name == "telegram" or name.startswith("telegram:")}
        elif source_filter.startswith("telegram:"):
            expected_sources = {source_filter}
        elif _core.CONFIG and _core.CONFIG.get_telegram_channel(source_filter):
            expected_sources = {f"telegram:{source_filter}"}
        else:
            expected_sources = {source_filter}
    failed_sources = sorted(expected_sources - providers_succeeded)
    after_ids = _core._video_id_snapshot(conn)
    source_rows = []
    for source in sorted(expected_sources | providers_succeeded):
        if source == "telegram" and any(item.startswith("telegram:") for item in expected_sources):
            continue
        provider = "telegram" if source.startswith("telegram:") else source
        before = before_ids.get(provider, set())
        after = after_ids.get(provider, set())
        if source.startswith("telegram:"):
            channel = source.split(":", 1)[1]
            prefix = f"telegram:{channel}:"
            before = {item for item in before if item.startswith(prefix)}
            after = {item for item in after if item.startswith(prefix)}
        source_label = source
        if source.startswith("telegram:") and _core.CONFIG:
            channel_cfg = _core.CONFIG.get_telegram_channel(source.split(":", 1)[1])
            if channel_cfg:
                channel_name = (channel_cfg.studio or channel_cfg.id).strip()
                source_label = f"tg:[{channel_name[:24].rstrip() + '…' if len(channel_name) > 25 else channel_name}]"
        source_rows.append({
            "source": source, "label": source_label, "success": source in providers_succeeded,
            "error": source_errors.get(source), "added": len(after - before),
            "detail": source_error_details.get(source),
            "removed": len(before - after), "videos": len(after),
            "duration_seconds": source_durations.get(source),
        })
    ended_at = int(_core.time.time())
    http_hosts = _core._ACTIVE_SYNC_METRICS["http_hosts"]
    http_summary = dict(_core._ACTIVE_SYNC_METRICS["http"])
    stage_summary = {
        name: {**values, "total_seconds": round(values["total_seconds"], 3)}
        for name, values in _core._ACTIVE_SYNC_METRICS["stages"].items()
    }
    rate_limit_summary = snapshot_host_rate_limit_stats(reset=True)
    _core._write_sync_history({
        "started_at": started_at, "ended_at": ended_at,
        "duration_seconds": max(0, ended_at - started_at),
        "managed_config_revision": int(getattr(_core.CONFIG, "managed_revision", 0)),
        "sync_mode": sync_mode, "success": not failed_sources, "source_filter": source_filter,
        "skip_telegram_channels": skip_telegram_channels,
        "sources": source_rows, "stages": stage_summary,
        "jellyfin_refresh_poll": _core._ACTIVE_SYNC_METRICS.get("jellyfin_refresh_poll", {}),
        "http": {**http_summary, "hosts": http_hosts, "rate_limit": rate_limit_summary},
    })
    if failed_sources:
        _core.logger.error("Sync completed with source failures: %s", ", ".join(failed_sources))
        return False
    _core.logger.info("Sync completed successfully.")
    return True
