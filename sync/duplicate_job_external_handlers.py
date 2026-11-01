"""Handlers for duplicate-job actions backed by external services or tools."""
from __future__ import annotations

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core

from common import schedule_stages


def execute_external_operation(action, conn, job_id, payload, media_dir, services):
    """Run the action, reporting scheduled-stage results for the stages it owns."""
    stage = schedule_stages.stage_for_job(action)
    if stage is None:
        return _execute(action, conn, job_id, payload, media_dir, services)
    db_url = getattr(conn, "_url", None) or _core.os.getenv("DATABASE_URL")
    schedule_id, stage_id = stage
    schedule_stages.record_stage_start(db_url, schedule_id, stage_id)
    try:
        result = _execute(action, conn, job_id, payload, media_dir, services)
    except Exception as error:
        schedule_stages.record_stage_finish(db_url, schedule_id, stage_id,
                                            success=False, error=str(error))
        raise
    schedule_stages.record_stage_finish(db_url, schedule_id, stage_id, success=True)
    return result


def _execute(action, conn, job_id, payload, media_dir, services):
    """Execute a maintenance or integration action and report commit status."""
    if action == "source_remove":
        if not isinstance(payload.get("channel_id"), str):
            raise ValueError("Channel removal requires a valid Telegram channel ID.")
        if __package__:
            from .source_removal import remove_telegram_channel
        else:
            from source_removal import remove_telegram_channel
        result = remove_telegram_channel(
            conn, media_dir, payload["channel_id"], job_id, services,
            _core._record_job_progress,
        )
        return result, True

    if action == "backup_create":
        _core._record_job_progress(conn, job_id, "creating_backup")
        backup_path = _core.db.pg_backup(
            getattr(conn, "_url", None) or _core.os.getenv("DATABASE_URL"),
            _core.os.getenv("BACKUP_DIR", "/data/backups"),
            job_id=job_id,
        )
        return {"backup_created": True, "backup_name": _core.Path(backup_path).name}, True

    if action == "backup_restore_test":
        _core._record_job_progress(conn, job_id, "testing_backup_restore")
        filename = payload.get("filename")
        backup_root = _core.Path(_core.os.getenv("BACKUP_DIR", "/data/backups")).resolve()
        candidate_path = backup_root / str(filename or "")
        backup_path = candidate_path.resolve()
        if (
            not isinstance(filename, str)
            or _core.Path(filename).name != filename
            or not filename.startswith("adulthub-")
            or not filename.endswith(".dump.gz")
            or candidate_path.is_symlink()
            or backup_path.parent != backup_root
        ):
            raise ValueError("Backup restore test request is invalid.")
        verification = _core.db.pg_restore_test(
            backup_path,
            _core.os.getenv("BACKUP_RESTORE_DATABASE_URL"),
            primary_db_url=getattr(conn, "_url", None) or _core.os.getenv("DATABASE_URL"),
            job_id=job_id,
        )
        return {
            "restore_test": verification.get("outcome") == "passed",
            "catalog_rows": int(verification.get("catalog_rows") or 0),
        }, True

    if action == "duplicate_reconcile":
        try:
            from .video_duplicate_automation import reconcile
        except ImportError:
            from video_duplicate_automation import reconcile
        # Automatic merges hide library entries, so a scheduled pass only reports
        # candidates until the operator opts in.
        execute_merges = _core.os.getenv("DUPLICATE_AUTO_MERGE_ENABLED", "false").strip().lower() == "true"
        result = reconcile(getattr(conn, "_url", None) or _core.os.getenv("DATABASE_URL"),
                           execute_merges=execute_merges)
        result["local_state"] = "duplicate_reconciliation_completed"
        return result, True

    if action == "metadata_backfill":
        if services.external_metadata_backfill is None:
            raise RuntimeError("External metadata scanning is unavailable in this worker.")

        def report_metadata_progress(
            completed: int,
            total: int,
            stage: str = "scanning_metadata",
            _job_id: str = job_id,
        ) -> None:
            _core._record_job_progress(conn, _job_id, stage, completed=completed, total=total)

        result = services.external_metadata_backfill(conn, media_dir, report_metadata_progress)
        result["local_state"] = "metadata_scan_completed"
        return result, True

    if action == "telegram_fingerprint_scan":
        video_ids = payload.get("video_ids")
        if (not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 100
                or any(not isinstance(video_id, str) or not video_id for video_id in video_ids)
                or len(set(video_ids)) != len(video_ids)):
            raise ValueError("Telegram fingerprint scan requires 1–100 unique video IDs.")
        eligible = {
            row[0] for row in conn.execute(
                """SELECT id FROM videos WHERE id=ANY(%s) AND provider='telegram'
                   AND duration>0 AND duration<10800
                   AND NOT (left(lower(coalesce(scene_match_source,'')),7)='stashdb'
                            AND nullif(scene_match_external_id,'') IS NOT NULL)""",
                (video_ids,),
            ).fetchall()
        }
        if eligible != set(video_ids):
            raise ValueError("One or more videos no longer meet the Telegram fingerprint scan criteria.")
        if services.telegram_fingerprint_scan is None:
            raise RuntimeError("Telegram fingerprint scanning is unavailable in this worker.")

        def report_scan_progress(completed, total, _job_id=job_id):
            _core._record_job_progress(
                conn, _job_id, "metadata_scenes", completed=completed, total=total,
            )

        checked, enriched = services.telegram_fingerprint_scan(
            conn, media_dir, video_ids, report_scan_progress,
        )
        return {
            "videos_requested": len(video_ids),
            "videos_checked": int(checked or 0),
            "metadata_enriched": int(enriched or 0),
            "local_state": "telegram_fingerprint_scan_completed",
        }, True

    if action == "artwork_generate":
        from urllib.parse import quote
        from common.image_utils import extract_video_frame
        if __package__:
            from .media_files import resolve_media_path
        else:
            from media_files import resolve_media_path

        video_id = payload.get("video_id")
        offset = payload.get("offset_seconds", 60)
        if not isinstance(video_id, str) or not video_id or len(video_id) > 1000:
            raise ValueError("Artwork generation requires one valid video ID.")
        if type(offset) is not int or offset <= 0:
            raise ValueError("Artwork generation requires a positive offset.")
        _core._record_job_progress(conn, job_id, "generating_artwork")
        rows = conn.execute(
            "SELECT sf.file_path FROM synced_files sf JOIN videos v ON v.id=sf.video_id "
            "WHERE sf.video_id=%s ORDER BY sf.file_path", (video_id,),
        ).fetchall()
        if not rows:
            raise ValueError("Target media file not found in library.")
        requested_path = payload.get("file_path")
        if requested_path is not None:
            if not isinstance(requested_path, str) or requested_path not in {str(row[0]) for row in rows}:
                raise ValueError("Artwork generation file is no longer registered for this item.")
            selected_path = requested_path
        else:
            selected_path = str(rows[0][0])
        dest = resolve_media_path(selected_path, media_dir).with_suffix(".jpg")
        if dest.is_symlink():
            raise ValueError("Artwork destination must not be a symlink.")
        if payload.get("only_if_artwork_invalid") is True:
            try:
                from .artwork_schedule import artwork_needs_generation
            except ImportError:
                from artwork_schedule import artwork_needs_generation
            if not artwork_needs_generation(dest):
                return {"local_state": "artwork_already_valid", "skipped": True}, True
        base = _core.os.getenv("RESOLVER_API_URL", "http://resolver:8090").rstrip("/")
        # The resolver only serves /play/* under /admin; bare /play/* answers 307,
        # which httpx does not follow while ffmpeg does.
        api_base = base if base.endswith("/admin") else base + "/admin"
        variant = payload.get("variant")
        if not variant or variant == "auto":
            from common.video_variants import fetch_and_select_best_variant
            variant = fetch_and_select_best_variant(api_base, video_id, purpose="artwork")
        elif str(variant).lower() == "original":
            variant = None

        stream_query = f"?variant={quote(str(variant), safe='')}" if variant else ""
        stream_url = f"{api_base}/play/{quote(video_id, safe='')}/stream{stream_query}"
        success, error = extract_video_frame(
            stream_url, dest, offset_seconds=offset,
        )
        if not success:
            raise RuntimeError(f"Failed to generate artwork: {error}")
        refreshed = _core._refresh_jellyfin_with_progress(
            conn, job_id, "artwork_generated", services,
        )
        return {"local_state": "artwork_generated",
                "jellyfin_refresh": "updated" if refreshed else "failed",
                "variant": str(variant or "original")}, True

    if action == "artwork_download":
        video_id = payload.get("video_id")
        if not isinstance(video_id, str) or not video_id or len(video_id) > 1000:
            raise ValueError("Artwork download requires one valid video ID.")
        _core._record_job_progress(conn, job_id, "downloading_artwork")
        try:
            from .scene_artwork_download import download_artwork_for_video
        except ImportError:
            from scene_artwork_download import download_artwork_for_video
        result = download_artwork_for_video(conn, media_dir, video_id)
        if result.get("downloaded") is True:
            result["local_state"] = "artwork_downloaded"
            refreshed = _core._refresh_jellyfin_with_progress(
                conn, job_id, "artwork_downloaded", services)
            result["jellyfin_refresh"] = "updated" if refreshed else "failed"
        return result, True

    if action == "collections_refresh":
        _core._record_job_progress(conn, job_id, "refreshing_collections")
        from common.managed_collections import refresh_managed_collections
        import httpx

        jf_url = _core.os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
        jf_key = _core.os.getenv("JELLYFIN_API_KEY", "").strip()
        if not jf_url or not jf_key:
            raise ValueError("Jellyfin configuration is required for collection refresh.")
        db_url = getattr(conn, "_url", None) or _core.os.getenv("DATABASE_URL")

        async def refresh_collections(_jf_url=jf_url, _jf_key=jf_key, _db_url=db_url):
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await refresh_managed_collections(client, _jf_url, _jf_key, _db_url)

        result = _core.asyncio.run(refresh_collections())
        result["message"] = f"Updated {len(result['collections'])} Adult Hub collections."
        return result, False

    if action == "performer_merge":
        merges = payload.get("merges")
        if not isinstance(merges, list) or not merges:
            raise ValueError("No performer pairs to merge.")
        pairs = []
        for pair in merges:
            if not isinstance(pair, dict):
                raise ValueError("Invalid performer pair.")
            alias, canonical = pair.get("alias"), pair.get("canonical")
            if (
                not isinstance(alias, str)
                or not isinstance(canonical, str)
                or not alias.strip()
                or not canonical.strip()
                or alias.strip() == canonical.strip()
            ):
                raise ValueError("A distinct alias and canonical name are required.")
            pairs.append((alias.strip(), canonical.strip()))
        import fuzzy_performers

        db_url = getattr(conn, "_url", None) or _core.os.getenv("DATABASE_URL")
        expected_revision = payload.get("expected_revision")
        resume = payload.get("_resume") if isinstance(payload.get("_resume"), dict) else {}
        if expected_revision is not None:
            if len(pairs) != 1 or not isinstance(expected_revision, str):
                raise ValueError("Invalid revision-bound performer merge plan.")
            if not resume.get("merge_started"):
                current_revision = fuzzy_performers.performer_merge_revision(conn, *pairs[0], legacy=not expected_revision.startswith("v2:"))
                if current_revision != expected_revision:
                    raise ValueError("Performer records changed after this preview. Create a new preview.")
        _core._record_job_progress(conn, job_id, "merging_performers")
        result = fuzzy_performers.execute_performer_merges(db_url, media_dir, pairs)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Performer merge failed."))
        return result, False

    if action == "history_cleanup":
        _core._record_job_progress(conn, job_id, "pruning_history")
        from common.history_retention import prune_history
        counts = prune_history(conn)
        return {"history_rows_removed": counts, "local_state": "history_cleanup_completed"}, False

    if action == "performer_catalog":
        _core._record_job_progress(conn, job_id, "syncing_performer_catalog")
        try:
            from .performer_catalog_sync import sync_catalog
        except ImportError:
            from performer_catalog_sync import sync_catalog
        performers, aliases = sync_catalog(conn)
        return {
            "catalogue_performers": performers,
            "catalogue_aliases": aliases,
            "local_state": "performer_catalog_completed",
        }, False

    if action == "scene_backfill":
        try:
            from scene_scrapers import backfill_scenes  # type: ignore
        except ImportError:
            raise RuntimeError("Scene identification is not installed in this worker.")
        limit = payload.get("limit")
        if not isinstance(limit, int) or limit < 1:
            limit = int(_core.os.getenv("SCENE_BACKFILL_PER_RUN", "500"))
        _core._record_job_progress(conn, job_id, "metadata_scenes", total=limit)
        matched, enriched = backfill_scenes(conn, media_dir, limit=limit)
        return {
            "scene_identified": matched,
            "metadata_enriched": enriched,
            "local_state": "scene_backfill_completed",
        }, False

    if action == "telegram_title_cleanup":
        try:
            from .telegram_title_cleanup import cleanup_telegram_titles
        except ImportError:
            from telegram_title_cleanup import cleanup_telegram_titles
        _core._record_job_progress(conn, job_id, "cleaning_telegram_titles")
        result = cleanup_telegram_titles(conn, media_dir, logger=services.logger)
        reconcile = getattr(services, "sync_jellyfin_telegram_metadata", None)
        if callable(reconcile):
            _core._record_job_progress(conn, job_id, "reconciling_jellyfin_metadata")
            reconciliation = reconcile(limit=200)
            if not isinstance(reconciliation, dict):
                raise RuntimeError("Jellyfin metadata reconciliation returned an invalid result.")
            result["jellyfin_metadata_reconciliation"] = {
                key: value for key, value in reconciliation.items()
                if not str(key).startswith("_")
            }
            if int(reconciliation.get("failed") or 0):
                raise RuntimeError(
                    "Jellyfin metadata reconciliation failed for "
                    f"{int(reconciliation['failed'])} item(s)."
                )
        result["local_state"] = "telegram_title_cleanup_completed"
        return result, False

    raise ValueError("Unsupported external job type.")
