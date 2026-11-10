"""Top-level scheduled sync orchestration."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Set
from common.http_rate_limit import snapshot_host_rate_limit_stats

from sync import sync as _core

from common import schedule_stages

_SYNC_SCHEDULE = "library-sync"


def _skip_stage(name: str) -> bool:
    """True once a shutdown is pending: the stage waits for the next pass.

    Every enrichment and backfill stage selects its work again from scratch, so
    skipping one costs nothing but delay. Not skipping is what made a stop wait
    for a single stage longer than the container's grace period and end in SIGKILL.
    """
    if not _core.stop_requested():
        return False
    _core.logger.warning("Shutdown requested; skipping the %s stage until the next pass.", name)
    return True


def _run_timed_stage(name: str, operation):
    if _skip_stage(name):
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


def _run_duplicate_jobs_checkpoint(conn, collected=None):
    """Upkeep only: a failed checkpoint must not cost the pass its later stages or history entry."""
    try:
        _core.safe_process_duplicate_jobs_checkpoint(conn, _core.MEDIA_DIR, collected)
    except Exception as e:
        _core.logger.warning("Could not process queued duplicate decisions: %s", e)


class _StageRecorder:
    """Records each dependent stage of one pass in the schedule history.

    ``stages`` holds the operator's stage switches, filled once the pass has its
    connection. ``target`` is the database URL the records are written to.
    """

    def __init__(self) -> None:
        self.stages: dict = {}
        self.active: set = set()
        self.target: Optional[str] = None

    def enabled(self, stage_id: str) -> bool:
        return bool(self.stages.get(stage_id, {}).get("enabled", True))

    def open(self, stage_id: str) -> bool:
        """Start one dependent stage unless an operator switched it off."""
        if not self.enabled(stage_id):
            _core.logger.info("Stage %s is disabled; skipping it in this pass.", stage_id)
            return False
        self.active.add(stage_id)
        if self.target is not None:
            schedule_stages.record_stage_start(self.target, _SYNC_SCHEDULE, stage_id)
        return True

    def close(self, stage_id: str, problems=()) -> None:
        if stage_id not in self.active:
            return
        self.active.discard(stage_id)
        if self.target is None:
            return
        detail = "; ".join(str(item) for item in problems if item)[:400] or None
        schedule_stages.record_stage_finish(self.target, _SYNC_SCHEDULE, stage_id,
                                            success=detail is None, error=detail)

    def run(self, stage_id: str, metric_name: Optional[str], operation):
        """One recorded stage around a single operation; failures keep propagating."""
        if metric_name and _skip_stage(metric_name):
            return None
        if not self.open(stage_id):
            return None
        problems: list = []
        try:
            if metric_name:
                return _run_timed_stage(metric_name, operation)
            return operation()
        except Exception as error:
            problems.append(_core._safe_error_detail(error))
            raise
        finally:
            self.close(stage_id, problems)

    def run_group(self, stage_id: str, operation):
        """One recorded stage made of steps that each stay individually tolerant."""
        if not self.open(stage_id):
            return
        problems: list = []
        try:
            operation(problems)
        finally:
            self.close(stage_id, problems)


class _SyncPass:
    """State shared by the stages of one scheduled sync pass."""

    def __init__(self, conn: Any, sync_mode: str, source_filter: Optional[str],
                 skip_telegram_channels: bool) -> None:
        self.conn = conn
        self.sync_mode = sync_mode
        self.source_filter = source_filter
        self.skip_telegram_channels = skip_telegram_channels
        self.recorder = _StageRecorder()
        self.before_ids: dict = {}
        # Dictionary: composite_id -> {"video": dict, "genres": set(), "tags": set()}
        self.collected: Dict[str, Dict[str, Any]] = {}
        # ETAP B: providers (and telegram channels) that synced cleanly in THIS
        # run. Destructive upkeep (retention, orphaned purge, .strm rewrite,
        # dead/unplayable sweeps) only touches sources present in this set that
        # are also registered and enabled. A provider with any failed page/fetch
        # stays out: its historical rows, files and .strm URLs are preserved.
        self.providers_succeeded: Set[str] = set()
        self.source_warnings: Dict[str, list] = {}
        self.source_errors: Dict[str, str] = {}
        self.source_error_details: Dict[str, str] = {}
        self.source_durations: Dict[str, float] = {}
        self.availability_errors: list = []


def _log_sync_start() -> None:
    _core.logger.info("Starting Adult-Hub sync (deduplicated)...")
    _core.logger.info("Media Dir: %s", _core.MEDIA_DIR)
    _core.logger.info("Resolver API: %s", _core.RESOLVER_API_URL)
    _core.logger.info("Resolver Public URL: %s", _core.RESOLVER_PUBLIC_URL)
    _core.logger.info("Retention Days: %s", _core.RETENTION_DAYS)


def _run_performer_catalog_stage(recorder: _StageRecorder, conn) -> None:
    if recorder.open("performer-catalog"):
        catalogue_problems: list = []
        try:
            from sync.performer_catalog_sync import sync_catalog_if_due
            catalogue_result = sync_catalog_if_due(conn)
            if catalogue_result:
                _core.logger.info("Performer catalogue refreshed: %s people, %s aliases", *catalogue_result)
        except Exception as e:
            _core.logger.warning("Performer catalogue refresh failed; using last local copy: %s", e)
            catalogue_problems.append(_core._safe_error_detail(e))
        finally:
            recorder.close("performer-catalog", catalogue_problems)


def _run_startup_maintenance(conn) -> None:
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
    _run_duplicate_jobs_checkpoint(conn)


def _telegram_filter_state(source_filter: Optional[str]):
    """Return (channel filter, whether the Telegram sources pass the filter)."""
    telegram_channel_filter = source_filter.split(":", 1)[1] if source_filter and source_filter.startswith("telegram:") else source_filter
    telegram_filter_enabled = (
        not source_filter or source_filter == "telegram"
        or bool(_core.CONFIG and _core.CONFIG.get_telegram_channel(telegram_channel_filter))
    )
    return telegram_channel_filter, telegram_filter_enabled


def _run_telegram_scans(sp: _SyncPass) -> None:
    recorder = sp.recorder
    telegram_channel_filter, telegram_filter_enabled = _telegram_filter_state(sp.source_filter)
    if _core.SYNC_TELEGRAM and telegram_filter_enabled and not sp.skip_telegram_channels:
        tg_auth_fn = getattr(_core, "is_telegram_authorized", None)
        is_authed = False
        if callable(tg_auth_fn):
            try:
                is_authed = bool(tg_auth_fn())
            except Exception:
                is_authed = False
        if not is_authed:
            _core.logger.info(
                "Telegram is not authorized yet. Safely disabling Telegram channel scans until login is completed in the Admin UI."
            )
            sp.skip_telegram_channels = True

    if _core.SYNC_TELEGRAM and telegram_filter_enabled and sp.skip_telegram_channels:
        _core.logger.info("Skipping Telegram channel scans by request; continuing full maintenance pass.")
    # Source scans and the availability sweep they feed are recorded stages: an
    # operator can switch either one off without stopping the rest of the pass.
    scans_wanted = _core.SYNC_TELEGRAM and telegram_filter_enabled and not sp.skip_telegram_channels
    scans_active = scans_wanted and recorder.open("source-scans")
    availability_active = scans_active and recorder.open("availability-reconciliation")
    if scans_wanted and not scans_active:
        _core.logger.info("Configured source scans are disabled; skipping channel crawls in this pass.")
    if scans_active:
        tg_src = _core.CONFIG.get_source("telegram") if _core.CONFIG else None
        if tg_src:
            _scan_configured_telegram_channels(sp, tg_src, telegram_channel_filter, availability_active)
        else:
            _scan_legacy_telegram_channels(sp)
    recorder.close("source-scans", list(sp.source_errors.values()))
    recorder.close("availability-reconciliation", sp.availability_errors)


def _record_channel_failure(sp: _SyncPass, source_key: str, label: str, error: Exception) -> None:
    detail = _core._safe_error_detail(error)
    _core.logger.error("Telegram sync failed for %s: %s", label, detail)
    sp.source_errors[source_key] = f"Sync failed ({type(error).__name__})."
    sp.source_error_details[source_key] = detail


def _scan_configured_telegram_channels(sp: _SyncPass, tg_src, telegram_channel_filter, availability_active: bool) -> None:
    enabled_channels = [ch for ch in tg_src.channels if ch.enabled]
    _core.logger.info("Syncing Telegram (%s enabled channel(s))...", len(enabled_channels))
    for ch in enabled_channels:
        if sp.source_filter and sp.source_filter != "telegram" and telegram_channel_filter != ch.id:
            continue
        if _core.stop_requested():
            _core.logger.warning(
                "Shutdown requested before Telegram channel %s: skipping the rest of this "
                "pass so the channel already synced stays consistent.", ch.id)
            break
        source_started = _core.time.monotonic()
        try:
            _sync_configured_channel(sp, ch, availability_active)
        except Exception as e:
            _record_channel_failure(sp, f"telegram:{ch.id}", ch.id, e)
        finally:
            sp.source_durations[f"telegram:{ch.id}"] = round(_core.time.monotonic() - source_started, 3)
        _run_duplicate_jobs_checkpoint(sp.conn, sp.collected)


def _sync_configured_channel(sp: _SyncPass, ch, availability_active: bool) -> None:
    synced_ids = _core.sync_telegram_channel_pipeline(ch.id, ch, sp.conn, _core.MEDIA_DIR, sp.collected, sync_mode=sp.sync_mode)
    warnings = getattr(synced_ids, "warnings", [])
    if warnings:
        sp.source_warnings[f"telegram:{ch.id}"] = warnings
    else:
        sp.providers_succeeded.add(f"telegram:{ch.id}")
    if availability_active and sp.sync_mode == "scheduled" and not warnings:
        _reconcile_channel_availability(sp, ch, synced_ids)


def _reconcile_channel_availability(sp: _SyncPass, ch, synced_ids) -> None:
    try:
        outcome = _core.reconcile_telegram_channel(
            sp.conn, ch.id, _core.MEDIA_DIR,
            _core.DATA_DIR / "telegram-unavailable-quarantine",
            _core.RESOLVER_API_URL, _core.fetch_json_with_retry,
            _core.logger, current_video_ids=synced_ids,
            limit=int(os.getenv("TELEGRAM_AVAILABILITY_CHECKS_PER_RUN", "20")),
            now=int(_core.time.time()),
        )
        _core.logger.info("Telegram %s availability: %s", ch.id, outcome)
    except Exception as e:
        _core.logger.warning("Telegram availability reconciliation failed for %s: %s", ch.id, e)
        sp.availability_errors.append(f"{ch.id}: {_core._safe_error_detail(e)}")


def _legacy_telegram_channel(channel_name: str):
    class _SimpleCh:
        studio = channel_name
        min_duration_seconds = 420
        max_pages = _core.TELEGRAM_MAX_PAGES
        history_batch_size = 100
    return _SimpleCh()


def _scan_legacy_telegram_channels(sp: _SyncPass) -> None:
    _core.logger.info(
        "Syncing Telegram (%s channel(s), %s page(s))...",
        len(_core.TELEGRAM_CHANNELS), _core.TELEGRAM_MAX_PAGES)
    for channel in _core.TELEGRAM_CHANNELS:
        channel_name = channel.strip()
        if not channel_name:
            continue
        if _core.stop_requested():
            _core.logger.warning(
                "Shutdown requested before Telegram channel %s: skipping the rest of this "
                "pass so the channel already synced stays consistent.", channel_name)
            break
        source_started = _core.time.monotonic()
        try:
            _sync_legacy_channel(sp, channel_name)
        except Exception as e:
            _record_channel_failure(sp, f"telegram:{channel_name}", channel_name, e)
        finally:
            sp.source_durations[f"telegram:{channel_name}"] = round(_core.time.monotonic() - source_started, 3)
        _run_duplicate_jobs_checkpoint(sp.conn, sp.collected)


def _sync_legacy_channel(sp: _SyncPass, channel_name: str) -> None:
    synced_ids = _core.sync_telegram_channel_pipeline(channel_name, _legacy_telegram_channel(channel_name), sp.conn, _core.MEDIA_DIR, sp.collected,
                                   sync_mode=sp.sync_mode)
    warnings = getattr(synced_ids, "warnings", [])
    if warnings:
        sp.source_warnings[f"telegram:{channel_name}"] = warnings
    else:
        sp.providers_succeeded.add(f"telegram:{channel_name}")


def _run_dead_stream_filter(sp: _SyncPass) -> None:
    # 2. Drop catalog entries the availability worker has denylisted.
    if _core.HIDE_DEAD_STREAMS:
        try:
            _core.hide_dead_streams(sp.collected, sp.conn)
        except Exception as e:
            _core.logger.warning("Dead-stream filtering failed (keeping all): %s", e)


def _run_final_flush(sp: _SyncPass) -> None:
    # 8. Flush any remaining videos (if any)
    # Leftovers belong to channels whose own pipeline flush already failed, so a
    # failure here must be reported but must not cost the pass its maintenance
    # stages: rows that were committed stay unpublished and the retry pass above
    # publishes them on the next run.
    try:
        _core.flush_collected(sp.collected, sp.conn, _core.MEDIA_DIR, "remaining", raise_on_error=True)
    except Exception as e:
        _core.logger.error("Final catalog flush failed: %s", _core._safe_error_detail(e))
    _run_duplicate_jobs_checkpoint(sp.conn, sp.collected)


def _run_stream_rewrite(sp: _SyncPass) -> None:
    # 8b. Rewrite stale .strm files to the currently configured per-provider
    # mode (e.g. redirect -> proxy flips apply without waiting for re-sync).
    # Guarded: absent/disabled/failed sources keep their stored URLs.
    try:
        _core.rewrite_all_stream_files(sp.conn, _core.MEDIA_DIR, sp.providers_succeeded)
    except Exception as e:
        _core.logger.warning("Stream rewrite pass failed: %s", e)


def _run_scene_identification(sp: _SyncPass) -> None:
    # Scene identification for Telegram is the first metadata-enrichment step:
    # fingerprints must run before performer-profile lookups and PBC requests.
    # The scraper is optional, so an absent module leaves the stage unrecorded
    # instead of reporting a failure on every install that never asked for it.
    try:
        from sync.scene_scrapers import backfill_scenes  # type: ignore
    except ImportError:
        backfill_scenes = None
    if backfill_scenes is not None:
        try:
            sp.recorder.run(
                "scene-identification", "metadata_scenes",
                lambda: backfill_scenes(sp.conn, _core.MEDIA_DIR, limit=_core.SCENE_BACKFILL_PER_RUN),
            )
        except Exception as e:
            _core.logger.warning("Scene fingerprint backfill pass failed: %s", e)
    _run_duplicate_jobs_checkpoint(sp.conn, sp.collected)


def _run_metadata_backfills(sp: _SyncPass, problems: list) -> None:
    """One stage of enrichment steps that each stay individually tolerant."""
    # 8c. Backfill missing performers from studio title naming conventions.
    try:
        _run_timed_stage(
            "performer_title_backfill",
            lambda: _core.backfill_performers_from_titles(sp.conn, _core.MEDIA_DIR),
        )
    except Exception as e:
        _core.logger.warning("Title performer backfill pass failed: %s", e)
        problems.append(_core._safe_error_detail(e))
    _run_duplicate_jobs_checkpoint(sp.conn, sp.collected)
    # 8d. Performer enrichment: PBC aliases+bios (rate-limited).
    _run_pbc_backfill(sp, problems)
    _run_tpdb_backfill(sp, problems)
    _run_stashdb_backfill(sp, problems)


def _run_pbc_backfill(sp: _SyncPass, problems: list) -> None:
    try:
        from sync.pbc import backfill_pbc  # type: ignore
        _run_timed_stage("metadata_pbc", lambda: backfill_pbc(sp.conn, limit=_core.PBC_BACKFILL_PER_RUN))
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning("PBC backfill pass failed: %s", e)
        problems.append(_core._safe_error_detail(e))


def _run_tpdb_backfill(sp: _SyncPass, problems: list) -> None:
    try:
        from sync.theporndb import backfill_tpdb  # type: ignore
        _run_timed_stage("metadata_tpdb", lambda: backfill_tpdb(sp.conn, limit=_core.TPDB_BACKFILL_PER_RUN))
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning("ThePornDB backfill pass failed: %s", e)
        problems.append(_core._safe_error_detail(e))


def _run_stashdb_backfill(sp: _SyncPass, problems: list) -> None:
    try:
        from sync.stashdb import backfill_stashdb  # type: ignore
        _run_timed_stage("metadata_stashdb", lambda: backfill_stashdb(sp.conn, limit=_core.TPDB_BACKFILL_PER_RUN))
    except ImportError:
        pass
    except Exception as e:
        _core.logger.warning("StashDB backfill pass failed: %s", e)
        problems.append(_core._safe_error_detail(e))


def _run_telegram_title_cleanup(sp: _SyncPass) -> None:
    # Repair stored Telegram titles and move caption hashtags into the existing
    # genre/tag fields. The bounded worker resumes its cursor on the next pass.
    try:
        from sync.telegram_title_cleanup import cleanup_telegram_titles
        cleanup_result = sp.recorder.run(
            "telegram-title-cleanup", "telegram_title_cleanup",
            lambda: cleanup_telegram_titles(
                sp.conn, _core.MEDIA_DIR, stop_requested=_core.stop_requested,
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


def _library_maintenance_pass(sp: _SyncPass) -> None:
    # 9. Clean up expired videos via retention policy + orphaned records.
    # Both use the success signal: absent, disabled and failed sources keep
    # every historical row, file and .strm URL.
    try:
        _core.cleanup_orphaned_records(_core.MEDIA_DIR, sp.conn, sp.providers_succeeded)
    except Exception as e:
        _core.logger.warning("Orphaned-record cleanup failed: %s", e)
    _core.cleanup_retention(sp.conn, _core.MEDIA_DIR, _core.RETENTION_DAYS, sp.providers_succeeded)


def _run_jellyfin_reconciliation(sp: _SyncPass, jellyfin_active: bool, problems: list) -> None:
    try:
        jellyfin_result = (_run_timed_stage(
            "jellyfin_metadata_reconciliation", _core.sync_jellyfin_telegram_metadata,
        ) if jellyfin_active else None)
        if isinstance(jellyfin_result, dict):
            _core._ACTIVE_SYNC_METRICS["jellyfin_refresh_poll"] = dict(
                jellyfin_result.get("refresh_poll") or {}
            )
    except Exception as e:
        _core.logger.warning("Telegram metadata reconciliation failed: %s", e)
        problems.append(_core._safe_error_detail(e))


def _publish_jellyfin_people(sp: _SyncPass, problems: list) -> None:
    # Publish structured StashDB details to existing Jellyfin person pages.
    try:
        from sync.jellyfin_people import sync_jellyfin_people
        sync_jellyfin_people(
            sp.conn, _core.JELLYFIN_URL, _core.JELLYFIN_API_KEY,
            limit=_core.JELLYFIN_PEOPLE_PER_RUN,
        )
    except Exception as e:
        _core.logger.warning("Jellyfin person metadata pass failed: %s", e)
        problems.append(_core._safe_error_detail(e))


def _run_jellyfin_stage(sp: _SyncPass) -> None:
    # Reconciliation, the library refresh trigger, person pages and display
    # titles all belong to one recorded Jellyfin stage.
    # Locked Telegram entries require an explicit unlock/refresh/relock cycle
    # when their authoritative local NFO changes. Reconcile existing entries
    # before queueing the global scan so targeted refreshes avoid its backlog.
    # New entries import their locked NFO during the scan; the next run verifies
    # their metadata and records the durable hash state.
    jellyfin_active = sp.recorder.open("jellyfin-refresh")
    jellyfin_problems: list = []
    try:
        _run_jellyfin_reconciliation(sp, jellyfin_active, jellyfin_problems)
        _run_duplicate_jobs_checkpoint(sp.conn, sp.collected)

        # 10. Trigger Jellyfin Refresh
        if jellyfin_active:
            _core.trigger_jellyfin_refresh()

        if jellyfin_active and not _skip_stage("jellyfin_people"):
            _publish_jellyfin_people(sp, jellyfin_problems)

        # 11. Sync display titles through Jellyfin's supported item API.
        if jellyfin_active and not _skip_stage("jellyfin_titles"):
            _core.sync_jellyfin_titles()
    except Exception as error:
        sp.recorder.close("jellyfin-refresh", [*jellyfin_problems, _core._safe_error_detail(error)])
        raise
    sp.recorder.close("jellyfin-refresh", jellyfin_problems)


def _expected_sources(sp: _SyncPass) -> set:
    expected_sources = set()
    if _core.CONFIG:
        for source in _core.CONFIG.get_enabled_sources():
            if source.provider == "telegram":
                expected_sources.update(
                    f"telegram:{channel.id}" for channel in source.channels if channel.enabled
                )
            else:
                expected_sources.add(source.provider)
    if sp.skip_telegram_channels:
        expected_sources = {name for name in expected_sources
                            if name != "telegram" and not name.startswith("telegram:")}
    if sp.source_filter:
        expected_sources = _apply_source_filter(expected_sources, sp.source_filter)
    return expected_sources


def _apply_source_filter(expected_sources: set, source_filter: str) -> set:
    if source_filter == "telegram":
        return {name for name in expected_sources if name == "telegram" or name.startswith("telegram:")}
    if source_filter.startswith("telegram:"):
        return {source_filter}
    if _core.CONFIG and _core.CONFIG.get_telegram_channel(source_filter):
        return {f"telegram:{source_filter}"}
    return {source_filter}


def _source_video_sets(source: str, before_ids: dict, after_ids: dict):
    provider = "telegram" if source.startswith("telegram:") else source
    before = before_ids.get(provider, set())
    after = after_ids.get(provider, set())
    if source.startswith("telegram:"):
        channel = source.split(":", 1)[1]
        prefix = f"telegram:{channel}:"
        before = {item for item in before if item.startswith(prefix)}
        after = {item for item in after if item.startswith(prefix)}
    return before, after


def _source_label(source: str) -> str:
    source_label = source
    if source.startswith("telegram:") and _core.CONFIG:
        channel_cfg = _core.CONFIG.get_telegram_channel(source.split(":", 1)[1])
        if channel_cfg:
            channel_name = (channel_cfg.studio or channel_cfg.id).strip()
            source_label = f"tg:[{channel_name[:24].rstrip() + '…' if len(channel_name) > 25 else channel_name}]"
    return source_label


def _build_source_rows(sp: _SyncPass, expected_sources: set, after_ids: dict) -> list:
    source_rows = []
    for source in sorted(expected_sources | sp.providers_succeeded):
        before, after = _source_video_sets(source, sp.before_ids, after_ids)
        source_rows.append({
            "source": source, "label": _source_label(source), "success": source in sp.providers_succeeded,
            "error": sp.source_errors.get(source), **({"warnings": sp.source_warnings[source]} if source in sp.source_warnings else {}), "added": len(after - before),
            "detail": sp.source_error_details.get(source),
            "removed": len(before - after), "videos": len(after),
            "duration_seconds": sp.source_durations.get(source),
        })
    return source_rows


def _write_pass_history(sp: _SyncPass, started_at: int, source_rows: list, failed_sources: list) -> None:
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
        "sync_mode": sp.sync_mode, "success": not failed_sources, "source_filter": sp.source_filter,
        "interrupted": _core.stop_requested(),
        "skip_telegram_channels": sp.skip_telegram_channels,
        "sources": source_rows, "stages": stage_summary,
        "jellyfin_refresh_poll": _core._ACTIVE_SYNC_METRICS.get("jellyfin_refresh_poll", {}),
        "http": {**http_summary, "hosts": http_hosts, "rate_limit": rate_limit_summary},
    })


def _finish_sync_pass(sp: _SyncPass, started_at: int) -> bool:
    expected_sources = _expected_sources(sp)
    failed_sources = sorted(expected_sources - sp.providers_succeeded - sp.source_warnings.keys())
    after_ids = _core._video_id_snapshot(sp.conn)
    source_rows = _build_source_rows(sp, expected_sources, after_ids)
    _write_pass_history(sp, started_at, source_rows, failed_sources)
    if failed_sources:
        _core.logger.error("Sync completed with source failures: %s", ", ".join(failed_sources))
        return False
    _core.logger.info("Sync completed successfully.")
    return True


def _run_sync_locked(source_filter: Optional[str] = None, sync_mode: str = "scheduled",
                     skip_telegram_channels: bool = False):
    snapshot_host_rate_limit_stats(reset=True)
    _core._ACTIVE_SYNC_METRICS = {
        "stages": {},
        "http": {"failed_attempts": 0, "retries": 0, "rate_limited": 0},
        "http_hosts": {},
    }
    started_at = int(_core.time.time())
    _log_sync_start()

    conn = _core.init_db()
    try:
        sp = _SyncPass(conn=conn, sync_mode=sync_mode, source_filter=source_filter,
                       skip_telegram_channels=skip_telegram_channels)
        sp.recorder.target = getattr(conn, "_url", None)
        schedule_stages.mark_interrupted_stages(conn, _SYNC_SCHEDULE)
        sp.recorder.stages.update(schedule_stages.stage_states(conn, _SYNC_SCHEDULE))
        _run_performer_catalog_stage(sp.recorder, conn)
        sp.before_ids = _core._video_id_snapshot(conn)
        _run_startup_maintenance(conn)
        _run_telegram_scans(sp)
        _run_dead_stream_filter(sp)
        _run_final_flush(sp)
        _run_stream_rewrite(sp)
        _run_scene_identification(sp)
        sp.recorder.run_group("metadata-backfill", lambda problems: _run_metadata_backfills(sp, problems))
        _run_telegram_title_cleanup(sp)
        sp.recorder.run("library-maintenance", None, lambda: _library_maintenance_pass(sp))
        _run_jellyfin_stage(sp)
        return _finish_sync_pass(sp, started_at)
    finally:
        conn.close()
