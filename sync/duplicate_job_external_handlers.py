"""Handlers for duplicate-job actions backed by external services or tools."""
from __future__ import annotations

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core


def execute_external_operation(action, conn, job_id, payload, media_dir, services):
    """Execute a maintenance or integration action and report commit status."""
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
                current_revision = fuzzy_performers.performer_merge_revision(conn, *pairs[0])
                if current_revision != expected_revision:
                    raise ValueError("Performer records changed after this preview. Create a new preview.")
        _core._record_job_progress(conn, job_id, "merging_performers")
        result = fuzzy_performers.execute_performer_merges(db_url, media_dir, pairs, skip_backup=True)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Performer merge failed."))
        return result, False

    raise ValueError("Unsupported external job type.")
