"""Top-level scheduled sync orchestration."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Set
from common.http_rate_limit import snapshot_host_rate_limit_stats

if __package__:
    from . import sync as _core
else:
    import sync as _core

from common import schedule_stages

_SYNC_SCHEDULE = "library-sync"


def _run_sync_locked(source_filter: Optional[str] = None, sync_mode: str = "scheduled",
                     skip_telegram_channels: bool = False):
    snapshot_host_rate_limit_stats(reset=True)
    _core._ACTIVE_SYNC_METRICS = {
        "stages": {},
        "http": {"failed_attempts": 0, "retries": 0, "rate_limited": 0},
        "http_hosts": {},
    }

    def skip_stage(name: str) -> bool:
        """True once a shutdown is pending: the stage waits for the next pass.

        Every enrichment and backfill stage selects its work again from scratch, so
        skipping one costs nothing but delay. Not skipping is what made a stop wait
        for a single stage longer than the container's grace period and end in SIGKILL.
        """
        if not _core.stop_requested():
            return False
        _core.logger.warning("Shutdown requested; skipping the %s stage until the next pass.", name)
        return True

    def run_timed_stage(name: str, operation):
        if skip_stage(name):
            return None
        started = _core.time.monotonic()
        failed = False
        try:
            return operation()
        except Exception:
            failed = True
            raise
        finally:
            _core._observe_sync_stage(name, _core.time.monotonic() - started, failed)

    def run_duplicate_jobs_checkpoint(conn, collected=None):
        """Upkeep only: a failed checkpoint must not cost the pass its later stages or history entry."""
        try:
            _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)
        except Exception as e:
            _core.logger.warning("Could not process queued duplicate decisions: %s", e)

    # Filled once the pass has its connection: the operator's stage switches.
    stages: dict = {}
    _ACTIVE_STAGES: set = set()
    stage_target: Optional[str] = None

    def stage_enabled(stage_id: str) -> bool:
        return bool(stages.get(stage_id, {}).get("enabled", True))

    def open_stage(stage_id: str) -> bool:
        """Start one dependent stage unless an operator switched it off."""
        if not stage_enabled(stage_id):
            _core.logger.info("Stage %s is disabled; skipping it in this pass.", stage_id)
            return False
        _ACTIVE_STAGES.add(stage_id)
        if stage_target is not None:
            schedule_stages.record_stage_start(stage_target, _SYNC_SCHEDULE, stage_id)
        return True

    def close_stage(stage_id: str, problems=()) -> None:
        if stage_id not in _ACTIVE_STAGES:
            return
        _ACTIVE_STAGES.discard(stage_id)
        if stage_target is None:
            return
        detail = "; ".join(str(item) for item in problems if item)[:400] or None
        schedule_stages.record_stage_finish(stage_target, _SYNC_SCHEDULE, stage_id,
                                            success=detail is None, error=detail)

    def run_stage(stage_id: str, metric_name: Optional[str], operation):
        """One recorded stage around a single operation; failures keep propagating."""
        if metric_name and skip_stage(metric_name):
            return None
        if not open_stage(stage_id):
            return None
        problems: list = []
        try:
            if metric_name:
                return run_timed_stage(metric_name, operation)
            return operation()
        except Exception as error:
            problems.append(_core._safe_error_detail(error))
            raise
        finally:
            close_stage(stage_id, problems)

    def run_stage_group(stage_id: str, operation):
        """One recorded stage made of steps that each stay individually tolerant."""
        if not open_stage(stage_id):
            return
        problems: list = []
        try:
            operation(problems)
        finally:
            close_stage(stage_id, problems)

    started_at = int(_core.time.time())
    _core.logger.info("Starting Adult-Hub sync (deduplicated)...")
    _core.logger.info(f"Media Dir: {_core.MEDIA_DIR}")
    _core.logger.info(f"Resolver API: {_core.RESOLVER_API_URL}")
    _core.logger.info(f"Resolver Public URL: {_core.RESOLVER_PUBLIC_URL}")
    _core.logger.info(f"Retention Days: {_core.RETENTION_DAYS}")

    conn = _core.init_db()
    stage_target = getattr(conn, "_url", None)
    schedule_stages.mark_interrupted_stages(conn, _SYNC_SCHEDULE)
    stages.update(schedule_stages.stage_states(conn, _SYNC_SCHEDULE))
    if open_stage("performer-catalog"):
        catalogue_problems: list = []
        try:
            from performer_catalog_sync import sync_catalog_if_due
            catalogue_result = sync_catalog_if_due(conn)
            if catalogue_result:
                _core.logger.info("Performer catalogue refreshed: %s people, %s aliases", *catalogue_result)
        except Exception as e:
            _core.logger.warning("Performer catalogue refresh failed; using last local copy: %s", e)
            catalogue_problems.append(_core._safe_error_detail(e))
        finally:
            close_stage("performer-catalog", catalogue_problems)
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
    # File publication is the one step that can fail after the catalog batch
    # commits, so entries can be durable in the database and still missing from
    # the library - including when the process died mid-publication. Retry them
    # before the crawlers run so this pass's Jellyfin refresh picks them up.
    try:
        _core.republish_pending_videos(conn, _core.MEDIA_DIR)
    except Exception as e:
        _core.logger.warning("Could not retry pending library publications: %s", e)
    run_duplicate_jobs_checkpoint(conn)

    # Dictionary: composite_id -> {"video": dict, "genres": set(), "tags": set()}
    collected: Dict[str, Dict[str, Any]] = {}
    # ETAP B: providers (and telegram channels) that synced cleanly in THIS
    # run. Destructive upkeep (retention, orphaned purge, .strm rewrite,
    # dead/unplayable sweeps) only touches sources present in this set that
    # are also registered and enabled. A provider with any failed page/fetch
    # stays out: its historical rows, files and .strm URLs are preserved.
    providers_succeeded: Set[str] = set()
    source_warnings: Dict[str, list] = {}
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
    # Source scans and the availability sweep they feed are recorded stages: an
    # operator can switch either one off without stopping the rest of the pass.
    scans_wanted = _core.SYNC_TELEGRAM and telegram_filter_enabled and not skip_telegram_channels
    scans_active = scans_wanted and open_stage("source-scans")
    availability_active = scans_active and open_stage("availability-reconciliation")
    availability_errors: list = []
    if scans_wanted and not scans_active:
        _core.logger.info("Configured source scans are disabled; skipping channel crawls in this pass.")
    if scans_active:
        tg_src = _core.CONFIG.get_source("telegram") if _core.CONFIG else None
        if tg_src:
            enabled_channels = [ch for ch in tg_src.channels if ch.enabled]
            _core.logger.info(f"Syncing Telegram ({len(enabled_channels)} enabled channel(s))...")
            for ch in enabled_channels:
                if source_filter and source_filter != "telegram" and telegram_channel_filter != ch.id:
                    continue
                if _core.stop_requested():
                    _core.logger.warning(
                        "Shutdown requested before Telegram channel %s: skipping the rest of this "
                        "pass so the channel already synced stays consistent.", ch.id)
                    break
                source_started = _core.time.monotonic()
                try:
                    synced_ids = _core.sync_telegram_channel_pipeline(ch.id, ch, conn, _core.MEDIA_DIR, collected, sync_mode=sync_mode)
                    warnings = getattr(synced_ids, "warnings", [])
                    if warnings:
                        source_warnings[f"telegram:{ch.id}"] = warnings
                    else:
                        providers_succeeded.add(f"telegram:{ch.id}")
                    if availability_active and sync_mode == "scheduled" and not warnings:
                        try:
                            outcome = _core.reconcile_telegram_channel(
                                conn, ch.id, _core.MEDIA_DIR,
                                _core.DATA_DIR / "telegram-unavailable-quarantine",
                                _core.RESOLVER_API_URL, _core.fetch_json_with_retry,
                                _core.logger, current_video_ids=synced_ids,
                                limit=int(os.getenv("TELEGRAM_AVAILABILITY_CHECKS_PER_RUN", "20")),
                                now=int(_core.time.time()),
                            )
                            _core.logger.info("Telegram %s availability: %s", ch.id, outcome)
                        except Exception as e:
                            _core.logger.warning("Telegram availability reconciliation failed for %s: %s", ch.id, e)
                            availability_errors.append(f"{ch.id}: {_core._safe_error_detail(e)}")
                except Exception as e:
                    detail = _core._safe_error_detail(e)
                    _core.logger.error("Telegram sync failed for %s: %s", ch.id, detail)
                    source_errors[f"telegram:{ch.id}"] = f"Sync failed ({type(e).__name__})."
                    source_error_details[f"telegram:{ch.id}"] = detail
                finally:
                    source_durations[f"telegram:{ch.id}"] = round(_core.time.monotonic() - source_started, 3)
                run_duplicate_jobs_checkpoint(conn, collected)
        else:
            _core.logger.info(f"Syncing Telegram ({len(_core.TELEGRAM_CHANNELS)} channel(s), {_core.TELEGRAM_MAX_PAGES} page(s))...")
            for channel in _core.TELEGRAM_CHANNELS:
                channel_name = channel.strip()
                if not channel_name:
                    continue
                if _core.stop_requested():
                    _core.logger.warning(
                        "Shutdown requested before Telegram channel %s: skipping the rest of this "
                        "pass so the channel already synced stays consistent.", channel_name)
                    break
                class _SimpleCh:
                    studio = channel_name
                    min_duration_seconds = 420
                    max_pages = _core.TELEGRAM_MAX_PAGES
                    history_batch_size = 100
                source_started = _core.time.monotonic()
                try:
                    synced_ids = _core.sync_telegram_channel_pipeline(channel_name, _SimpleCh(), conn, _core.MEDIA_DIR, collected,
                                                   sync_mode=sync_mode)
                    warnings = getattr(synced_ids, "warnings", [])
                    if warnings:
                        source_warnings[f"telegram:{channel_name}"] = warnings
                    else:
                        providers_succeeded.add(f"telegram:{channel_name}")
                except Exception as e:
                    detail = _core._safe_error_detail(e)
                    _core.logger.error("Telegram sync failed for %s: %s", channel_name, detail)
                    source_errors[f"telegram:{channel_name}"] = f"Sync failed ({type(e).__name__})."
                    source_error_details[f"telegram:{channel_name}"] = detail
                finally:
                    source_durations[f"telegram:{channel_name}"] = round(_core.time.monotonic() - source_started, 3)
                run_duplicate_jobs_checkpoint(conn, collected)
    close_stage("source-scans", list(source_errors.values()))
    close_stage("availability-reconciliation", availability_errors)

    # 2. Drop catalog entries the availability worker has denylisted.
    if _core.HIDE_DEAD_STREAMS:
        try:
            _core.hide_dead_streams(collected, conn)
        except Exception as e:
            _core.logger.warning(f"Dead-stream filtering failed (keeping all): {e}")

    # 8. Flush any remaining videos (if any)
    # Leftovers belong to channels whose own pipeline flush already failed, so a
    # failure here must be reported but must not cost the pass its maintenance
    # stages: rows that were committed stay unpublished and the retry pass above
    # publishes them on the next run.
    try:
        _core.flush_collected(collected, conn, _core.MEDIA_DIR, "remaining", raise_on_error=True)
    except Exception as e:
        _core.logger.error("Final catalog flush failed: %s", _core._safe_error_detail(e))
    run_duplicate_jobs_checkpoint(conn, collected)

    # 8b. Rewrite stale .strm files to the currently configured per-provider
    # mode (e.g. redirect -> proxy flips apply without waiting for re-sync).
    # Guarded: absent/disabled/failed sources keep their stored URLs.
    try:
        _core.rewrite_all_stream_files(conn, _core.MEDIA_DIR, providers_succeeded)
    except Exception as e:
        _core.logger.warning(f"Stream rewrite pass failed: {e}")

    # Scene identification for Telegram is the first metadata-enrichment step:
    # fingerprints must run before performer-profile lookups and PBC requests.
    # The scraper is optional, so an absent module leaves the stage unrecorded
    # instead of reporting a failure on every install that never asked for it.
    try:
        from scene_scrapers import backfill_scenes  # type: ignore
    except ImportError:
        backfill_scenes = None
    if backfill_scenes is not None:
        try:
            run_stage(
                "scene-identification", "metadata_scenes",
                lambda: backfill_scenes(conn, _core.MEDIA_DIR, limit=_core.SCENE_BACKFILL_PER_RUN),
            )
        except Exception as e:
            _core.logger.warning(f"Scene fingerprint backfill pass failed: {e}")
    run_duplicate_jobs_checkpoint(conn, collected)

    def performer_and_metadata_backfill(problems: list) -> None:
        """One stage of enrichment steps that each stay individually tolerant."""
        # 8c. Backfill missing performers from studio title naming conventions.
        try:
            run_timed_stage(
                "performer_title_backfill",
                lambda: _core.backfill_performers_from_titles(conn, _core.MEDIA_DIR),
            )
        except Exception as e:
            _core.logger.warning(f"Title performer backfill pass failed: {e}")
            problems.append(_core._safe_error_detail(e))
        run_duplicate_jobs_checkpoint(conn, collected)
        # 8d. Performer enrichment: PBC aliases+bios (rate-limited).
        try:
            from pbc import backfill_pbc  # type: ignore
            run_timed_stage("metadata_pbc", lambda: backfill_pbc(conn, limit=_core.PBC_BACKFILL_PER_RUN))
        except ImportError:
            pass
        except Exception as e:
            _core.logger.warning(f"PBC backfill pass failed: {e}")
            problems.append(_core._safe_error_detail(e))
        try:
            from theporndb import backfill_tpdb  # type: ignore
            run_timed_stage("metadata_tpdb", lambda: backfill_tpdb(conn, limit=_core.TPDB_BACKFILL_PER_RUN))
        except ImportError:
            pass
        except Exception as e:
            _core.logger.warning(f"ThePornDB backfill pass failed: {e}")
            problems.append(_core._safe_error_detail(e))
        try:
            from stashdb import backfill_stashdb  # type: ignore
            run_timed_stage("metadata_stashdb", lambda: backfill_stashdb(conn, limit=_core.TPDB_BACKFILL_PER_RUN))
        except ImportError:
            pass
        except Exception as e:
            _core.logger.warning(f"StashDB backfill pass failed: {e}")
            problems.append(_core._safe_error_detail(e))

    run_stage_group("metadata-backfill", performer_and_metadata_backfill)

    # Repair stored Telegram titles and move caption hashtags into the existing
    # genre/tag fields. The bounded worker resumes its cursor on the next pass.
    try:
        if __package__:
            from .telegram_title_cleanup import cleanup_telegram_titles
        else:
            from telegram_title_cleanup import cleanup_telegram_titles
        cleanup_result = run_stage(
            "telegram-title-cleanup", "telegram_title_cleanup",
            lambda: cleanup_telegram_titles(
                conn, _core.MEDIA_DIR, stop_requested=_core.stop_requested,
                logger=_core.logger,
            ),
        )
        if isinstance(cleanup_result, dict):
            _core._ACTIVE_SYNC_METRICS["telegram_title_cleanup"] = {
                key: cleanup_result.get(key) for key in (
                    "status", "processed", "total", "titles_changed", "tags_added",
                    "categories_added", "nfos_missing", "nfos_failed", "last_result",
                )
            }
    except Exception as e:
        _core.logger.warning("Telegram title cleanup failed: %s", _core._safe_error_detail(e))

    # 9. Clean up expired videos via retention policy + orphaned records.
    # Both use the success signal: absent, disabled and failed sources keep
    # every historical row, file and .strm URL.
    def library_maintenance_pass():
        try:
            _core.cleanup_orphaned_records(_core.MEDIA_DIR, conn, providers_succeeded)
        except Exception as e:
            _core.logger.warning(f"Orphaned-record cleanup failed: {e}")
        _core.cleanup_retention(conn, _core.MEDIA_DIR, _core.RETENTION_DAYS, providers_succeeded)

    run_stage("library-maintenance", None, library_maintenance_pass)

    # Locked Telegram entries require an explicit unlock/refresh/relock cycle
    # when their authoritative local NFO changes. Reconcile existing entries
    # before queueing the global scan so targeted refreshes avoid its backlog.
    # New entries import their locked NFO during the scan; the next run verifies
    # their metadata and records the durable hash state.
    # Reconciliation, the library refresh trigger, person pages and display
    # titles all belong to one recorded Jellyfin stage.
    jellyfin_active = open_stage("jellyfin-refresh")
    jellyfin_problems: list = []
    try:
        try:
            jellyfin_result = (run_timed_stage(
                "jellyfin_metadata_reconciliation", _core.sync_jellyfin_telegram_metadata,
            ) if jellyfin_active else None)
            if isinstance(jellyfin_result, dict):
                _core._ACTIVE_SYNC_METRICS["jellyfin_refresh_poll"] = dict(
                    jellyfin_result.get("refresh_poll") or {}
                )
        except Exception as e:
            _core.logger.warning(f"Telegram metadata reconciliation failed: {e}")
            jellyfin_problems.append(_core._safe_error_detail(e))
        run_duplicate_jobs_checkpoint(conn, collected)

        # 10. Trigger Jellyfin Refresh
        if jellyfin_active:
            _core.trigger_jellyfin_refresh()

        # Publish structured StashDB details to existing Jellyfin person pages.
        if jellyfin_active and not skip_stage("jellyfin_people"):
            try:
                from jellyfin_people import sync_jellyfin_people
                sync_jellyfin_people(
                    conn, _core.JELLYFIN_URL, _core.JELLYFIN_API_KEY,
                    limit=_core.JELLYFIN_PEOPLE_PER_RUN,
                )
            except Exception as e:
                _core.logger.warning(f"Jellyfin person metadata pass failed: {e}")
                jellyfin_problems.append(_core._safe_error_detail(e))

        # 11. Sync display titles through Jellyfin's supported item API.
        if jellyfin_active and not skip_stage("jellyfin_titles"):
            _core.sync_jellyfin_titles()
    except Exception as error:
        close_stage("jellyfin-refresh", [*jellyfin_problems, _core._safe_error_detail(error)])
        raise
    close_stage("jellyfin-refresh", jellyfin_problems)
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
    failed_sources = sorted(expected_sources - providers_succeeded - source_warnings.keys())
    after_ids = _core._video_id_snapshot(conn)
    source_rows = []
    for source in sorted(expected_sources | providers_succeeded):
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
            "error": source_errors.get(source), **({"warnings": source_warnings[source]} if source in source_warnings else {}), "added": len(after - before),
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
        "interrupted": _core.stop_requested(),
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
