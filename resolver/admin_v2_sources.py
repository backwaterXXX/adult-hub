"""Revision-checked source configuration preview and save routes."""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
from admin_v2_sync_history import safe_sync_history as _safe_sync_history

logger = logging.getLogger(__name__)


MAX_REQUEST_BYTES = 256 * 1024
_LOG = logging.getLogger("resolver.admin_v2")


class ChannelRemovalLookupError(RuntimeError):
    """Failure querying active source removal jobs."""


def is_channel_removal_active(admin, channel_id: str) -> bool:
    from common.managed_sources import canonical_channel_id, is_channel_locked
    if is_channel_locked(channel_id):
        return True
    try:
        from common.job_queue import connect_queue
        aliases = {channel_id, f"-100{canonical_channel_id(channel_id)}", canonical_channel_id(channel_id)}
        conn = connect_queue(admin.HUB_DB_URL)
        try:
            rows = conn.execute(
                "SELECT payload FROM video_duplicate_jobs "
                "WHERE action='source_remove' AND state IN ('queued', 'processing')"
            ).fetchall()
            for (raw_payload,) in rows:
                try:
                    p = json.loads(raw_payload) if isinstance(raw_payload, str) else (raw_payload or {})
                    if isinstance(p, dict):
                        p_ch = str(p.get("channel_id", "")).strip()
                        if p_ch in aliases or canonical_channel_id(p_ch) == canonical_channel_id(channel_id):
                            return True
                except Exception:
                    logger.debug("Ignored exception in is_channel_removal_active", exc_info=True)
        finally:
            conn.close()
    except Exception as exc:
        _LOG.error("Failed to query active source_remove jobs for %s: %s", channel_id, exc)
        raise ChannelRemovalLookupError(
            "Could not verify active channel operations. Retry shortly."
        ) from exc
    return False


async def parse_body(request: Request):
    length = request.headers.get("content-length")
    try:
        if length is not None and int(length) > MAX_REQUEST_BYTES:
            return None
        body = await request.json()
    except Exception:
        logger.debug("Ignored exception in parse_body", exc_info=True)
        return None
    if (not isinstance(body, dict) or set(body) != {"expected_revision", "overrides"}
            or type(body["expected_revision"]) is not int or body["expected_revision"] < 0
            or not isinstance(body["overrides"], dict)):
        return None
    try:
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return body

async def preview(admin, body):
    from common.managed_sources import preview_overrides
    return await admin.asyncio.to_thread(
        preview_overrides,
        admin.CONFIG_PATH,
        body["overrides"],
        expected_revision=body["expected_revision"],
    )

async def flush_source_audit(admin):
    """Idempotently drain the durable file outbox into PostgreSQL."""
    try:
        from common.managed_sources import clear_pending_source_audit, read_overrides, read_pending_source_audit
        current = await admin.asyncio.to_thread(read_overrides)
        pending = await admin.asyncio.to_thread(read_pending_source_audit)
        if pending is None:
            return True
        if pending["revision"] == current["revision"] + 1:
            # The writer crashed before publishing the corresponding config revision.
            cleared = await admin.asyncio.to_thread(
                clear_pending_source_audit, expected_revision=current["revision"],
                audit_revision=pending["revision"],
            )
            return cleared
        if pending["revision"] != current["revision"]:
            return False
        from common.admin_audit_events import record_source_settings_change
        recorded = await admin.asyncio.to_thread(
            record_source_settings_change, admin.HUB_DB_URL,
            revision=pending["revision"], changes=pending["changes"],
            truncated=pending["truncated"],
        )
        if not recorded:
            raise RuntimeError("Pending source audit could not be validated or replayed.")
        cleared = await admin.asyncio.to_thread(
            clear_pending_source_audit, expected_revision=current["revision"],
            audit_revision=pending["revision"],
        )
        if cleared:
            return True
        latest = await admin.asyncio.to_thread(read_pending_source_audit)
        return latest is None
    except Exception as exc:
        _LOG.warning("Admin source change audit could not be recorded (%s)", type(exc).__name__)
        return False

def safe_error(admin, exc):
    from common.managed_sources import ManagedAuditPending, ManagedConfigConflict, ManagedConfigLockTimeout
    if isinstance(exc, ManagedConfigConflict):
        return admin.JSONResponse(
            {"error": "Source configuration changed. Reload it before saving."}, status_code=409
        )
    if isinstance(exc, ManagedAuditPending):
        return admin.JSONResponse(
            {"error": "A previous source change is saved, but its audit record is still syncing. Retry after the database recovers."},
            status_code=503,
        )
    if isinstance(exc, ManagedConfigLockTimeout):
        return admin.JSONResponse(
            {"error": "Source configuration is temporarily locked by another operation. Retry shortly."},
            status_code=503,
        )
    if isinstance(exc, ChannelRemovalLookupError):
        return admin.JSONResponse(
            {"error": "Could not verify active channel operations. Retry shortly."},
            status_code=503,
        )
    return admin.JSONResponse({"error": "Source configuration is invalid or unavailable."}, status_code=400)


def _route_list_sources(router, admin):
    @router.get("/api/v2/sources/list")
    async def list_sources(request: Request):
        """List validated source configuration and the last applied revision."""
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            data = await admin.asyncio.to_thread(
                admin.admin_data.configured_sources, admin.CONFIG_PATH)
            try:
                history = await admin.asyncio.to_thread(admin._sync_history, 1)
            except Exception:
                history = None
            runs = history.get("runs", []) if isinstance(history, dict) else []
            latest_revision = None
            if runs and isinstance(runs[0], dict):
                value = runs[0].get("managed_config_revision")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    latest_revision = value
            return {"ok": True, **data,
                    "desired_revision": data.get("managed_revision", 0),
                    "applied_revision": latest_revision,
                    "applied_revision_available": (isinstance(history, dict)
                                                    and not history.get("error")
                                                    and isinstance(history.get("runs"), list))}
        except Exception:
            logger.warning("Request failed in list_sources", exc_info=True)
            return admin.JSONResponse({"error": "Could not load configured sources."}, status_code=500)


def _route_source_sync_history(router, admin):
    @router.get("/api/v2/sources/sync-history")
    async def source_sync_history(request: Request, limit: int = 30, offset: int = 0):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            safe_limit = max(1, min(limit, 100))
            safe_offset = max(0, offset)
            report = await admin.asyncio.to_thread(admin._sync_history, safe_limit, safe_offset) if safe_offset else await admin.asyncio.to_thread(admin._sync_history, safe_limit)
            return {"ok": True, **_safe_sync_history(report), "limit": safe_limit, "offset": safe_offset}
        except Exception:
            logger.warning("Request failed in source_sync_history", exc_info=True)
            return admin.JSONResponse({"error": "Sync history is unavailable."}, status_code=503)


def _route_get_source_settings(router, admin):
    @router.get("/api/v2/sources/settings")
    async def get_source_settings(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            import yaml
            from pathlib import Path
            from common.managed_sources import apply_overrides, read_overrides

            def read_base():
                with Path(admin.CONFIG_PATH).open(encoding="utf-8") as stream:
                    return yaml.safe_load(stream)

            base = await admin.asyncio.to_thread(read_base)
            if base is None:
                base = {}
            audit_pending = not await flush_source_audit(admin)
            overlay = await admin.asyncio.to_thread(read_overrides)
            await admin.asyncio.to_thread(apply_overrides, base, overlay)
            return {"ok": True, "revision": overlay["revision"], "overrides": overlay["overrides"],
                    "audit_pending": audit_pending}
        except Exception:
            logger.warning("Request failed in get_source_settings", exc_info=True)
            return admin.JSONResponse({"error": "Source settings are unavailable."}, status_code=503)


_CHANNEL_BODY_LIMIT = 4096


async def _read_channel_body(admin, request):
    """Read the small JSON body of a channel route. Returns (body, error_response)."""
    length = request.headers.get("content-length")
    try:
        if length is not None and int(length) > _CHANNEL_BODY_LIMIT:
            return None, admin.JSONResponse({"error": "Request is too large."}, status_code=413)
    except ValueError:
        return None, admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
    raw_body = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw_body) + len(chunk) > _CHANNEL_BODY_LIMIT:
                return None, admin.JSONResponse({"error": "Request is too large."}, status_code=413)
            raw_body.extend(chunk)
        body = json.loads(raw_body)
    except Exception:
        body = None
    return body, None


def _create_channel_body(body):
    """Return the stripped (channel_id, studio) of a valid create body, else None."""
    if (not isinstance(body, dict) or set(body) != {"expected_revision", "channel"}
            or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
            or not isinstance(body.get("channel"), dict)
            or set(body["channel"]) != {"id", "studio"}
            or not isinstance(body["channel"].get("id"), str)
            or not isinstance(body["channel"].get("studio"), str)):
        return None
    return body["channel"]["id"].strip(), body["channel"]["studio"].strip()


def _valid_channel_fields(channel_id, studio):
    """Check the length and control-character rules for a channel ID and studio label."""
    return bool(channel_id and len(channel_id) <= 256
                and studio and len(studio) <= 160
                and not any(ord(char) < 32 for char in channel_id + studio))


def _remove_channel_body(body):
    """Return the stripped channel ID of a valid remove body, else None."""
    if (not isinstance(body, dict) or set(body) != {"expected_revision", "channel_id"}
            or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
            or not isinstance(body.get("channel_id"), str)):
        return None
    return body["channel_id"].strip()


def _valid_channel_id(channel_id):
    """Check the length and control-character rules for a channel ID."""
    return bool(channel_id and len(channel_id) <= 256
                and not any(ord(char) < 32 for char in channel_id))


async def _checked_current_overrides(admin, expected_revision):
    """Return the overrides at the expected revision after the audit outbox is drained."""
    from common.managed_sources import ManagedAuditPending, ManagedConfigConflict, read_overrides

    observed = await admin.asyncio.to_thread(read_overrides)
    if observed["revision"] != expected_revision:
        raise ManagedConfigConflict("Stale source revision.")
    if not await flush_source_audit(admin):
        raise ManagedAuditPending("A prior source audit is still pending.")
    current = await admin.asyncio.to_thread(read_overrides)
    if current["revision"] != expected_revision:
        raise ManagedConfigConflict("Stale source revision.")
    return current


async def _existing_channel_ids(admin, current):
    """Return the channel IDs of the first effective Telegram source."""
    import yaml
    from pathlib import Path
    from common.managed_sources import apply_overrides

    def read_ids():
        base = yaml.safe_load(Path(admin.CONFIG_PATH).read_text(encoding="utf-8")) or {}
        effective = apply_overrides(base, current)
        for source in effective.get("sources", []):
            if source.get("provider") == "telegram":
                return {str(item.get("id", "")).strip() for item in source.get("channels", [])}
        return set()

    return await admin.asyncio.to_thread(read_ids)


def _stage_channel_create(overrides, channel_id, studio, restoring):
    """Write the new channel entry into the candidate overrides, in place."""
    from common.managed_sources import canonical_channel_id

    ch_canon = canonical_channel_id(channel_id)
    telegram = overrides.setdefault("telegram", {})
    channels = telegram.setdefault("channels", {})
    if restoring:
        telegram["removed_channels"] = [
            item for item in telegram.get("removed_channels", []) if canonical_channel_id(item) != ch_canon
        ]
        if not telegram["removed_channels"]:
            telegram.pop("removed_channels")
    previous = next((v for k, v in channels.items() if canonical_channel_id(k) == ch_canon), {})
    for k in list(channels.keys()):
        if k != channel_id and canonical_channel_id(k) == ch_canon:
            channels.pop(k, None)
    channels[channel_id] = {**previous, "studio": studio, "enabled": True}
    return overrides


async def _queue_channel_readd(admin, channel_id, studio, revision):
    """Queue re-adding a channel whose deletion is still running. Returns the response."""
    try:
        from common.job_queue import enqueue_job
        job_id = await admin.asyncio.to_thread(
            enqueue_job, admin.HUB_DB_URL, "source_readd", {
                "channel_id": channel_id,
                "studio": studio,
                "action_type": "create",
            }
        )
        admin._wake_worker("jobs")
        return admin.JSONResponse(
            {
                "ok": True,
                "queued": True,
                "job_id": job_id,
                "state": "queued",
                "source_id": channel_id,
                "revision": revision,
                "message": f"Channel deletion is in progress for '{channel_id}'. Re-adding is queued and will execute after deletion.",
            },
            status_code=202,
        )
    except Exception:
        logger.debug("Ignored exception in create_source", exc_info=True)
        return admin.JSONResponse(
            {"error": "Channel deletion is in progress; wait for it to complete before re-adding."},
            status_code=409,
        )


async def _prepare_channel_create(admin, current, channel_id, studio):
    """Stage the new channel in the candidate overrides.

    Returns (candidate, None) when the save should proceed, or (None, response)
    when the request ends early with a conflict or a queued re-add.
    """
    from common.managed_sources import canonical_channel_id, is_channel_locked

    ch_canon = canonical_channel_id(channel_id)
    existing_ids = await _existing_channel_ids(admin, current)
    duplicate = {"error": "This channel ID already exists."}
    if any(canonical_channel_id(cid) == ch_canon for cid in existing_ids):
        return None, admin.JSONResponse(duplicate, status_code=409)
    candidate = current["overrides"]
    telegram = candidate.setdefault("telegram", {})
    channels = telegram.setdefault("channels", {})
    restoring = any(canonical_channel_id(item) == ch_canon
                    for item in telegram.get("removed_channels", []))
    if not restoring and any(canonical_channel_id(cid) == ch_canon for cid in channels):
        return None, admin.JSONResponse(duplicate, status_code=409)
    locked = is_channel_locked(channel_id)
    removing = restoring and await admin.asyncio.to_thread(is_channel_removal_active, admin, channel_id)
    if locked or removing:
        return None, await _queue_channel_readd(admin, channel_id, studio, current["revision"])
    return _stage_channel_create(candidate, channel_id, studio, restoring), None


def _route_create_source(router, admin):
    @router.post("/api/v2/sources")
    async def create_source(request: Request):
        """Add one Telegram channel to the shared managed configuration."""
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body, error = await _read_channel_body(admin, request)
        if error is not None:
            return error
        fields = _create_channel_body(body)
        if fields is None:
            return admin.JSONResponse({"error": "Specify a channel ID, studio label, and current revision."}, status_code=400)
        channel_id, studio = fields
        if not _valid_channel_fields(channel_id, studio):
            return admin.JSONResponse({"error": "Enter a valid channel ID and studio label."}, status_code=400)
        try:
            from common.managed_sources import preview_overrides, update_overrides

            current = await _checked_current_overrides(admin, body["expected_revision"])
            candidate, early = await _prepare_channel_create(admin, current, channel_id, studio)
            if early is not None:
                return early
            preview_result = await admin.asyncio.to_thread(
                preview_overrides, admin.CONFIG_PATH, candidate,
                expected_revision=body["expected_revision"],
            )
            saved = await admin.asyncio.to_thread(
                update_overrides, admin.CONFIG_PATH, candidate,
                expected_revision=body["expected_revision"],
                audit_event={"changes": preview_result["changes"], "truncated": preview_result["truncated"]},
            )
        except Exception as exc:
            logger.debug("Ignored exception in create_source", exc_info=True)
            return safe_error(admin, exc)
        audit_pending = not await flush_source_audit(admin)
        return {"ok": True, "revision": saved["revision"], "source_id": channel_id,
                "changes": preview_result["changes"],
                "application": "next sync or resolver config reload", "audit_pending": audit_pending}


async def _configured_telegram_channel_ids(admin, current):
    """Return the channel IDs of the first effective Telegram source that are configured."""
    import yaml
    from pathlib import Path
    from common.managed_sources import apply_overrides

    def read_base():
        with Path(admin.CONFIG_PATH).open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)

    base = await admin.asyncio.to_thread(read_base) or {}
    effective = await admin.asyncio.to_thread(apply_overrides, base, current)
    telegram = next((source for source in effective.get("sources", [])
                     if source.get("provider") == "telegram"), None)
    return {
        str(channel.get("id", "")).strip()
        for channel in (telegram or {}).get("channels", [])
        if isinstance(channel, dict)
    }


def _stage_channel_removal(overrides, channel_id):
    """Mark the channel as removed and drop its entries from the candidate, in place."""
    from common.managed_sources import canonical_channel_id

    telegram_overrides = overrides.setdefault("telegram", {})
    removed_channels = set(telegram_overrides.get("removed_channels", []))
    removed_channels.add(channel_id)
    telegram_overrides["removed_channels"] = sorted(removed_channels)
    channels = telegram_overrides.get("channels", {})
    for k in list(channels.keys()):
        if canonical_channel_id(k) == canonical_channel_id(channel_id):
            channels.pop(k, None)
    return overrides


async def _rollback_channel_removal(admin, candidate, channel_id, saved):
    """Undo a removal whose deletion job could not be queued. Returns the error response."""
    from common.managed_sources import preview_overrides, update_overrides

    try:
        await flush_source_audit(admin)
    except Exception:
        logger.debug("Ignored exception in remove_source", exc_info=True)
    try:
        rollback_candidate = copy.deepcopy(candidate)
        rollback_tg = rollback_candidate.get("telegram", {})
        rem = [ch for ch in rollback_tg.get("removed_channels", []) if ch != channel_id]
        if rem:
            rollback_tg["removed_channels"] = rem
        else:
            rollback_tg.pop("removed_channels", None)
        rollback_preview = await admin.asyncio.to_thread(
            preview_overrides, admin.CONFIG_PATH, rollback_candidate,
            expected_revision=saved["revision"],
        )
        await admin.asyncio.to_thread(
            update_overrides, admin.CONFIG_PATH, rollback_candidate,
            expected_revision=saved["revision"],
            audit_event={"changes": rollback_preview["changes"],
                         "truncated": rollback_preview["truncated"]},
            allow_compensation=True,
        )
        try:
            await flush_source_audit(admin)
        except Exception:
            logger.debug("Ignored exception in remove_source", exc_info=True)
    except Exception as rollback_exc:
        _LOG.error("Compensating config rollback failed: %s", rollback_exc)
        return admin.JSONResponse(
            {"error": "Could not queue channel deletion and compensating configuration rollback failed. Configuration may require manual reconciliation."},
            status_code=500,
        )
    return admin.JSONResponse(
        {"error": "Could not queue channel deletion. The source and its data were left unchanged."},
        status_code=503,
    )


async def _enqueue_channel_removal(admin, candidate, channel_id, saved):
    """Queue the source_remove job. Returns (job_id, None), or (None, response) after a rollback."""
    try:
        from common.job_queue import enqueue_job
        job_id = await admin.asyncio.to_thread(
            enqueue_job, admin.HUB_DB_URL, "source_remove", {"channel_id": channel_id},
        )
    except Exception as enqueue_exc:
        _LOG.error("Failed to enqueue source_remove job: %s", enqueue_exc)
        return None, await _rollback_channel_removal(admin, candidate, channel_id, saved)
    return job_id, None


def _route_remove_source(router, admin):
    @router.post("/api/v2/sources/remove")
    async def remove_source(request: Request):
        """Remove a Telegram channel and queue deletion of its catalog data."""
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body, error = await _read_channel_body(admin, request)
        if error is not None:
            return error
        channel_id = _remove_channel_body(body)
        if channel_id is None:
            return admin.JSONResponse({"error": "Specify a channel ID and current revision."}, status_code=400)
        if not _valid_channel_id(channel_id):
            return admin.JSONResponse({"error": "Enter a valid channel ID."}, status_code=400)
        try:
            from common.managed_sources import preview_overrides, update_overrides

            current = await _checked_current_overrides(admin, body["expected_revision"])
            configured_ids = await _configured_telegram_channel_ids(admin, current)
            if channel_id not in configured_ids:
                return admin.JSONResponse({"error": "This channel is not configured."}, status_code=404)
            if await admin.asyncio.to_thread(is_channel_removal_active, admin, channel_id):
                return admin.JSONResponse(
                    {"error": "Channel deletion is already queued or in progress."},
                    status_code=409,
                )
            candidate = _stage_channel_removal(current["overrides"], channel_id)
            preview_result = await admin.asyncio.to_thread(
                preview_overrides, admin.CONFIG_PATH, candidate,
                expected_revision=body["expected_revision"],
            )
            saved = await admin.asyncio.to_thread(
                update_overrides, admin.CONFIG_PATH, candidate,
                expected_revision=body["expected_revision"],
                audit_event={"changes": preview_result["changes"],
                             "truncated": preview_result["truncated"]},
            )
            job_id, failure = await _enqueue_channel_removal(admin, candidate, channel_id, saved)
            if failure is not None:
                return failure
        except Exception as exc:
            logger.debug("Ignored exception in remove_source", exc_info=True)
            return safe_error(admin, exc)
        admin._wake_worker("jobs")
        audit_pending = not await flush_source_audit(admin)
        return {"ok": True, "revision": saved["revision"], "source_id": channel_id,
                "job_id": job_id, "state": "queued",
                "changes": preview_result["changes"],
                "application": "next sync or resolver config reload",
                "audit_pending": audit_pending,
                "message": "Channel removed from future syncs. Its channel-only videos and channel sync history are queued for deletion."}


def _route_preview_sources(router, admin):
    @router.post("/api/v2/sources/preview")
    async def preview_sources(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        body = await parse_body(request)
        if body is None:
            return admin.JSONResponse({"error": "Specify the current revision and source settings."}, status_code=400)
        try:
            result = await preview(admin, body)
        except Exception as exc:
            logger.debug("Ignored exception in preview_sources", exc_info=True)
            return safe_error(admin, exc)
        return {"ok": True, **result}


async def _check_save_revision(admin, expected_revision):
    """Check the revision, drain the audit outbox, and return the observed overrides."""
    from common.managed_sources import ManagedConfigConflict, read_overrides

    observed = await admin.asyncio.to_thread(read_overrides)
    if observed["revision"] != expected_revision:
        raise ManagedConfigConflict("Stale source revision.")
    if not await flush_source_audit(admin):
        from common.managed_sources import ManagedAuditPending
        raise ManagedAuditPending("A prior source audit is still pending.")
    return observed


async def _queue_channel_update(admin, body, observed, ch_str, ch_conf):
    """Queue a channel update that must wait for a running deletion. Returns the response."""
    try:
        from common.job_queue import enqueue_job
        ch_studio = ch_conf.get("studio") if isinstance(ch_conf, dict) else None
        job_id = await admin.asyncio.to_thread(
            enqueue_job, admin.HUB_DB_URL, "source_readd", {
                "channel_id": ch_str,
                "studio": ch_studio,
                "channel_config": ch_conf if isinstance(ch_conf, dict) else None,
                "overrides": body["overrides"],
                "action_type": "save",
            }
        )
        admin._wake_worker("jobs")
        return admin.JSONResponse(
            {
                "ok": True,
                "queued": True,
                "job_id": job_id,
                "state": "queued",
                "source_id": ch_str,
                "revision": observed["revision"],
                "message": f"Channel deletion is in progress for '{ch_str}'. Channel updates are queued and will execute after deletion.",
            },
            status_code=202,
        )
    except Exception:
        logger.debug("Ignored exception in save_sources", exc_info=True)
        return admin.JSONResponse(
            {"error": f"Channel deletion is in progress for '{ch_str}'; wait for it to complete before re-adding."},
            status_code=409,
        )


async def _queue_channel_restore(admin, body, observed, orig_id):
    """Queue re-adding a removed channel whose deletion is still running. Returns the response."""
    try:
        from common.job_queue import enqueue_job
        job_id = await admin.asyncio.to_thread(
            enqueue_job, admin.HUB_DB_URL, "source_readd", {
                "channel_id": orig_id,
                "overrides": body["overrides"],
                "action_type": "restore",
            }
        )
        admin._wake_worker("jobs")
        return admin.JSONResponse(
            {
                "ok": True,
                "queued": True,
                "job_id": job_id,
                "state": "queued",
                "source_id": orig_id,
                "revision": observed["revision"],
                "message": f"Channel deletion is in progress for '{orig_id}'. Re-adding is queued and will execute after deletion.",
            },
            status_code=202,
        )
    except Exception:
        logger.debug("Ignored exception in save_sources", exc_info=True)
        return admin.JSONResponse(
            {"error": f"Channel deletion is in progress for '{orig_id}'; wait for it to complete before re-adding."},
            status_code=409,
        )


async def _pending_channel_update_response(admin, body, observed, tg_candidate):
    """Return a queued response when an enabled channel is still being deleted, else None."""
    from common.managed_sources import canonical_channel_id, is_channel_locked

    current_removed = list(observed.get("overrides", {}).get("telegram", {}).get("removed_channels", []))
    current_removed_canon = {canonical_channel_id(cid): cid for cid in current_removed}
    new_removed = list(tg_candidate.get("removed_channels", [])) if "removed_channels" in tg_candidate else []
    new_removed_canon = {canonical_channel_id(cid) for cid in new_removed}

    new_channels = tg_candidate.get("channels", {})
    if not isinstance(new_channels, dict):
        return None
    for ch_id, ch_conf in new_channels.items():
        ch_str = str(ch_id).strip()
        canon = canonical_channel_id(ch_str)
        is_enabled = isinstance(ch_conf, dict) and ch_conf.get("enabled", True) is not False
        if not is_enabled:
            continue
        if canon in new_removed_canon or ch_str in new_removed:
            continue
        if is_channel_locked(ch_str) or (
            (canon in current_removed_canon or ch_str in current_removed)
            and await admin.asyncio.to_thread(is_channel_removal_active, admin, ch_str)
        ):
            return await _queue_channel_update(admin, body, observed, ch_str, ch_conf)
    return None


async def _pending_channel_restore_response(admin, body, observed, tg_candidate):
    """Return a queued response when a removed channel is being restored during deletion, else None."""
    from common.managed_sources import canonical_channel_id, is_channel_locked

    if "removed_channels" not in tg_candidate:
        return None
    current_removed = list(observed.get("overrides", {}).get("telegram", {}).get("removed_channels", []))
    current_removed_canon = {canonical_channel_id(cid): cid for cid in current_removed}
    new_removed_canon = {canonical_channel_id(cid) for cid in tg_candidate.get("removed_channels", [])}
    for canon, orig_id in current_removed_canon.items():
        if canon in new_removed_canon:
            continue
        if is_channel_locked(orig_id) or await admin.asyncio.to_thread(is_channel_removal_active, admin, orig_id):
            return await _queue_channel_restore(admin, body, observed, orig_id)
    return None


async def _pending_channel_response(admin, body, observed):
    """Return a queued or conflict response when channel changes must wait, else None."""
    tg_candidate = body.get("overrides", {}).get("telegram", {})
    if not isinstance(tg_candidate, dict):
        return None
    response = await _pending_channel_update_response(admin, body, observed, tg_candidate)
    if response is not None:
        return response
    return await _pending_channel_restore_response(admin, body, observed, tg_candidate)


def _route_save_sources(router, admin):
    @router.post("/api/v2/sources/save")
    async def save_sources(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body = await parse_body(request)
        if body is None:
            return admin.JSONResponse({"error": "Specify the current revision and source settings."}, status_code=400)
        try:
            from common.managed_sources import update_overrides
            observed = await _check_save_revision(admin, body["expected_revision"])
            pending = await _pending_channel_response(admin, body, observed)
            if pending is not None:
                return pending
            preview_result = await preview(admin, body)
            saved = await admin.asyncio.to_thread(
                update_overrides,
                admin.CONFIG_PATH,
                body["overrides"],
                expected_revision=body["expected_revision"],
                audit_event={"changes": preview_result["changes"], "truncated": preview_result["truncated"]},
            )
        except Exception as exc:
            logger.debug("Ignored exception in save_sources", exc_info=True)
            return safe_error(admin, exc)
        audit_pending = not await flush_source_audit(admin)
        return {"ok": True, "revision": saved["revision"], "changes": preview_result["changes"],
                "truncated": preview_result["truncated"],
                "application": "next sync or resolver config reload", "audit_pending": audit_pending}


def _source_access_channel_id(body):
    """Return the stripped channel ID of a valid check body, else None."""
    if (not isinstance(body, dict) or set(body) != {"source_id"}
            or not isinstance(body["source_id"], str)
            or not body["source_id"].strip() or len(body["source_id"]) > 256):
        return None
    return body["source_id"].strip()


async def _telegram_channel_configured(admin, channel_id):
    """Return (is_configured, error_response). The response is set when the config cannot load."""
    try:
        from common.config_loader import load_config
        cfg = await admin.asyncio.to_thread(load_config, str(admin.CONFIG_PATH))
        telegram = cfg.get_source("telegram")
        return bool(telegram and any(channel.id == channel_id for channel in telegram.channels)), None
    except Exception:
        logger.warning("Request failed in check_source_access", exc_info=True)
        return False, admin.JSONResponse({"error": "Source configuration is unavailable."}, status_code=503)


async def _probe_channel_access(admin, channel_id):
    """Return 'accessible', 'inaccessible', or 'unknown' for a configured Telegram channel."""
    try:
        from providers import get_provider
        provider = get_provider("telegram")
        await admin.asyncio.wait_for(
            provider.check_configured_channel_access(channel_id), timeout=15.0
        )
    except Exception as exc:
        inaccessible = type(exc).__name__ in {
            "ChannelPrivateError", "ChannelInvalidError", "UsernameInvalidError",
            "PeerIdInvalidError",
        }
        return "inaccessible" if inaccessible else "unknown"
    return "accessible"


_ACCESS_MESSAGES = {
    "accessible": "Telegram read access confirmed.",
    "inaccessible": "Telegram did not grant access to this channel.",
    "unknown": "Telegram could not confirm read access to this channel.",
}


def _route_check_source_access(router, admin):
    @router.post("/api/v2/sources/check")
    async def check_source_access(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body, error = await _read_channel_body(admin, request)
        if error is not None:
            return error
        channel_id = _source_access_channel_id(body)
        if channel_id is None:
            return admin.JSONResponse({"error": "Choose one configured Telegram channel."}, status_code=400)
        configured, error = await _telegram_channel_configured(admin, channel_id)
        if error is not None:
            return error
        if not configured:
            return admin.JSONResponse({"error": "Channel is not configured."}, status_code=400)
        status = await _probe_channel_access(admin, channel_id)
        return {
            "ok": True,
            "source_id": channel_id,
            "status": status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "message": _ACCESS_MESSAGES[status],
        }


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)
    _route_list_sources(router, admin)
    _route_source_sync_history(router, admin)
    _route_get_source_settings(router, admin)
    _route_create_source(router, admin)
    _route_remove_source(router, admin)
    _route_preview_sources(router, admin)
    _route_save_sources(router, admin)
    _route_check_source_access(router, admin)
    return router
