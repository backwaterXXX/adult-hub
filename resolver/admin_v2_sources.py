"""Revision-checked source configuration preview and save routes."""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
from admin_v2_sync_history import safe_sync_history as _safe_sync_history


MAX_REQUEST_BYTES = 256 * 1024
MAX_CHECK_REQUEST_BYTES = 4096
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
                    pass
        finally:
            conn.close()
    except Exception as exc:
        _LOG.error("Failed to query active source_remove jobs for %s: %s", channel_id, exc)
        raise ChannelRemovalLookupError(
            "Could not verify active channel operations. Retry shortly."
        ) from exc
    return False


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    def _is_channel_removal_active(channel_id: str) -> bool:
        return is_channel_removal_active(admin, channel_id)

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
            return admin.JSONResponse({"error": "Could not load configured sources."}, status_code=500)

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
            return admin.JSONResponse({"error": "Sync history is unavailable."}, status_code=503)

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
            audit_pending = not await flush_source_audit()
            overlay = await admin.asyncio.to_thread(read_overrides)
            await admin.asyncio.to_thread(apply_overrides, base, overlay)
            return {"ok": True, "revision": overlay["revision"], "overrides": overlay["overrides"],
                    "audit_pending": audit_pending}
        except Exception:
            return admin.JSONResponse({"error": "Source settings are unavailable."}, status_code=503)

    async def parse_body(request: Request):
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > MAX_REQUEST_BYTES:
                return None
            body = await request.json()
        except Exception:
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

    async def preview(body):
        from common.managed_sources import preview_overrides
        return await admin.asyncio.to_thread(
            preview_overrides,
            admin.CONFIG_PATH,
            body["overrides"],
            expected_revision=body["expected_revision"],
        )

    async def flush_source_audit():
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

    @router.post("/api/v2/sources")
    async def create_source(request: Request):
        """Add one Telegram channel to the shared managed configuration."""
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > 4096:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw_body) + len(chunk) > 4096:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw_body.extend(chunk)
            body = json.loads(raw_body)
        except Exception:
            body = None
        if (not isinstance(body, dict) or set(body) != {"expected_revision", "channel"}
                or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
                or not isinstance(body.get("channel"), dict)
                or set(body["channel"]) != {"id", "studio"}
                or not isinstance(body["channel"].get("id"), str)
                or not isinstance(body["channel"].get("studio"), str)):
            return admin.JSONResponse({"error": "Specify a channel ID, studio label, and current revision."}, status_code=400)
        channel_id = body["channel"]["id"].strip()
        studio = body["channel"]["studio"].strip()
        if (not channel_id or len(channel_id) > 256 or not studio or len(studio) > 160
                or any(ord(char) < 32 for char in channel_id + studio)):
            return admin.JSONResponse({"error": "Enter a valid channel ID and studio label."}, status_code=400)
        try:
            from common.managed_sources import (
                ManagedConfigConflict, canonical_channel_id, is_channel_locked,
                preview_overrides, read_overrides, update_overrides,
            )

            observed = await admin.asyncio.to_thread(read_overrides)
            if observed["revision"] != body["expected_revision"]:
                raise ManagedConfigConflict("Stale source revision.")
            if not await flush_source_audit():
                from common.managed_sources import ManagedAuditPending
                raise ManagedAuditPending("A prior source audit is still pending.")
            current = await admin.asyncio.to_thread(read_overrides)
            if current["revision"] != body["expected_revision"]:
                raise ManagedConfigConflict("Stale source revision.")
            import yaml
            from pathlib import Path
            from common.managed_sources import apply_overrides
            def existing_channel_ids():
                base = yaml.safe_load(Path(admin.CONFIG_PATH).read_text(encoding="utf-8")) or {}
                effective = apply_overrides(base, current)
                for source in effective.get("sources", []):
                    if source.get("provider") == "telegram":
                        return {str(item.get("id", "")).strip() for item in source.get("channels", [])}
                return set()
            existing_ids = await admin.asyncio.to_thread(existing_channel_ids)
            ch_canon = canonical_channel_id(channel_id)
            if any(canonical_channel_id(cid) == ch_canon for cid in existing_ids):
                return admin.JSONResponse({"error": "This channel ID already exists."}, status_code=409)
            if is_channel_locked(channel_id):
                return admin.JSONResponse(
                    {"error": "Channel deletion is in progress; wait for it to complete before re-adding."},
                    status_code=409,
                )
            candidate = current["overrides"]
            telegram = candidate.setdefault("telegram", {})
            channels = telegram.setdefault("channels", {})
            removed = telegram.get("removed_channels", [])
            restoring = any(canonical_channel_id(item) == ch_canon for item in removed)
            if any(canonical_channel_id(cid) == ch_canon for cid in channels) and not restoring:
                return admin.JSONResponse({"error": "This channel ID already exists."}, status_code=409)
            if restoring:
                if await admin.asyncio.to_thread(_is_channel_removal_active, channel_id):
                    return admin.JSONResponse(
                        {"error": "Channel deletion is in progress; wait for it to complete before re-adding."},
                        status_code=409,
                    )
                telegram["removed_channels"] = [
                    item for item in removed if canonical_channel_id(item) != ch_canon
                ]
                if not telegram["removed_channels"]:
                    telegram.pop("removed_channels")
            previous = next((v for k, v in channels.items() if canonical_channel_id(k) == ch_canon), {})
            for k in list(channels.keys()):
                if k != channel_id and canonical_channel_id(k) == ch_canon:
                    channels.pop(k, None)
            channels[channel_id] = {**previous, "studio": studio, "enabled": True}
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
            return safe_error(exc)
        audit_pending = not await flush_source_audit()
        return {"ok": True, "revision": saved["revision"], "source_id": channel_id,
                "changes": preview_result["changes"],
                "application": "next sync or resolver config reload", "audit_pending": audit_pending}

    def safe_error(exc):
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

    @router.post("/api/v2/sources/remove")
    async def remove_source(request: Request):
        """Remove a Telegram channel and queue deletion of its catalog data."""
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > 4096:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw_body) + len(chunk) > 4096:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw_body.extend(chunk)
            body = json.loads(raw_body)
        except Exception:
            body = None
        if (not isinstance(body, dict) or set(body) != {"expected_revision", "channel_id"}
                or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
                or not isinstance(body.get("channel_id"), str)):
            return admin.JSONResponse({"error": "Specify a channel ID and current revision."}, status_code=400)
        channel_id = body["channel_id"].strip()
        if (not channel_id or len(channel_id) > 256
                or any(ord(char) < 32 for char in channel_id)):
            return admin.JSONResponse({"error": "Enter a valid channel ID."}, status_code=400)
        try:
            from common.managed_sources import (
                ManagedAuditPending, ManagedConfigConflict, apply_overrides,
                preview_overrides, read_overrides, update_overrides,
            )

            observed = await admin.asyncio.to_thread(read_overrides)
            if observed["revision"] != body["expected_revision"]:
                raise ManagedConfigConflict("Stale source revision.")
            if not await flush_source_audit():
                raise ManagedAuditPending("A prior source audit is still pending.")
            current = await admin.asyncio.to_thread(read_overrides)
            if current["revision"] != body["expected_revision"]:
                raise ManagedConfigConflict("Stale source revision.")

            import yaml
            from pathlib import Path

            def read_base():
                with Path(admin.CONFIG_PATH).open(encoding="utf-8") as stream:
                    return yaml.safe_load(stream)

            base = await admin.asyncio.to_thread(read_base) or {}
            effective = await admin.asyncio.to_thread(apply_overrides, base, current)
            telegram = next((source for source in effective.get("sources", [])
                             if source.get("provider") == "telegram"), None)
            configured_ids = {
                str(channel.get("id", "")).strip()
                for channel in (telegram or {}).get("channels", [])
                if isinstance(channel, dict)
            }
            if channel_id not in configured_ids:
                return admin.JSONResponse({"error": "This channel is not configured."}, status_code=404)
            if await admin.asyncio.to_thread(_is_channel_removal_active, channel_id):
                return admin.JSONResponse(
                    {"error": "Channel deletion is already queued or in progress."},
                    status_code=409,
                )

            candidate = current["overrides"]
            telegram_overrides = candidate.setdefault("telegram", {})
            removed_channels = set(telegram_overrides.get("removed_channels", []))
            removed_channels.add(channel_id)
            telegram_overrides["removed_channels"] = sorted(removed_channels)
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
            try:
                from common.job_queue import enqueue_job
                job_id = await admin.asyncio.to_thread(
                    enqueue_job, admin.HUB_DB_URL, "source_remove", {"channel_id": channel_id},
                )
            except Exception as enqueue_exc:
                _LOG.error("Failed to enqueue source_remove job: %s", enqueue_exc)
                try:
                    await flush_source_audit()
                except Exception:
                    pass
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
                        await flush_source_audit()
                    except Exception:
                        pass
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
        except Exception as exc:
            return safe_error(exc)
        admin._wake_worker("jobs")
        audit_pending = not await flush_source_audit()
        return {"ok": True, "revision": saved["revision"], "source_id": channel_id,
                "job_id": job_id, "state": "queued",
                "changes": preview_result["changes"],
                "application": "next sync or resolver config reload",
                "audit_pending": audit_pending,
                "message": "Channel removed from future syncs. Its channel-only videos and channel sync history are queued for deletion."}

    @router.post("/api/v2/sources/preview")
    async def preview_sources(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        body = await parse_body(request)
        if body is None:
            return admin.JSONResponse({"error": "Specify the current revision and source settings."}, status_code=400)
        try:
            result = await preview(body)
        except Exception as exc:
            return safe_error(exc)
        return {"ok": True, **result}

    @router.post("/api/v2/sources/save")
    async def save_sources(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body = await parse_body(request)
        if body is None:
            return admin.JSONResponse({"error": "Specify the current revision and source settings."}, status_code=400)
        try:
            from common.managed_sources import (
                ManagedConfigConflict, canonical_channel_id, is_channel_locked,
                read_overrides, update_overrides,
            )
            observed = await admin.asyncio.to_thread(read_overrides)
            if observed["revision"] != body["expected_revision"]:
                raise ManagedConfigConflict("Stale source revision.")
            if not await flush_source_audit():
                from common.managed_sources import ManagedAuditPending
                raise ManagedAuditPending("A prior source audit is still pending.")
            tg_candidate = body.get("overrides", {}).get("telegram", {})
            if isinstance(tg_candidate, dict):
                current_removed = list(observed.get("overrides", {}).get("telegram", {}).get("removed_channels", []))
                current_removed_canon = {canonical_channel_id(cid): cid for cid in current_removed}

                new_removed = list(tg_candidate.get("removed_channels", [])) if "removed_channels" in tg_candidate else []
                new_removed_canon = {canonical_channel_id(cid) for cid in new_removed}

                new_channels = tg_candidate.get("channels", {})
                if isinstance(new_channels, dict):
                    for ch_id, ch_conf in new_channels.items():
                        ch_str = str(ch_id).strip()
                        canon = canonical_channel_id(ch_str)
                        is_enabled = isinstance(ch_conf, dict) and ch_conf.get("enabled", True) is not False
                        if is_enabled:
                            if is_channel_locked(ch_str):
                                return admin.JSONResponse(
                                    {"error": f"Channel deletion is in progress for '{ch_str}'; wait for it to complete before re-adding."},
                                    status_code=409,
                                )
                            if canon in current_removed_canon or ch_str in current_removed:
                                if await admin.asyncio.to_thread(_is_channel_removal_active, ch_str):
                                    return admin.JSONResponse(
                                        {"error": f"Channel deletion is in progress for '{ch_str}'; wait for it to complete before re-adding."},
                                        status_code=409,
                                    )

                if "removed_channels" in tg_candidate:
                    for canon, orig_id in current_removed_canon.items():
                        if canon not in new_removed_canon:
                            if is_channel_locked(orig_id):
                                return admin.JSONResponse(
                                    {"error": f"Channel deletion is in progress for '{orig_id}'; wait for it to complete before re-adding."},
                                    status_code=409,
                                )
                            if await admin.asyncio.to_thread(_is_channel_removal_active, orig_id):
                                return admin.JSONResponse(
                                    {"error": f"Channel deletion is in progress for '{orig_id}'; wait for it to complete before re-adding."},
                                    status_code=409,
                                )
            preview_result = await preview(body)
            saved = await admin.asyncio.to_thread(
                update_overrides,
                admin.CONFIG_PATH,
                body["overrides"],
                expected_revision=body["expected_revision"],
                audit_event={"changes": preview_result["changes"], "truncated": preview_result["truncated"]},
            )
        except Exception as exc:
            return safe_error(exc)
        audit_pending = not await flush_source_audit()
        return {"ok": True, "revision": saved["revision"], "changes": preview_result["changes"],
                "truncated": preview_result["truncated"],
                "application": "next sync or resolver config reload", "audit_pending": audit_pending}

    @router.post("/api/v2/sources/check")
    async def check_source_access(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        declared_length = request.headers.get("content-length")
        try:
            if declared_length is not None and int(declared_length) > MAX_CHECK_REQUEST_BYTES:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw_body) + len(chunk) > MAX_CHECK_REQUEST_BYTES:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw_body.extend(chunk)
            body = json.loads(raw_body)
        except Exception:
            body = None
        if (not isinstance(body, dict) or set(body) != {"source_id"}
                or not isinstance(body["source_id"], str)
                or not body["source_id"].strip() or len(body["source_id"]) > 256):
            return admin.JSONResponse({"error": "Choose one configured Telegram channel."}, status_code=400)
        channel_id = body["source_id"].strip()
        try:
            from common.config_loader import load_config
            cfg = await admin.asyncio.to_thread(load_config, str(admin.CONFIG_PATH))
            telegram = cfg.get_source("telegram")
            configured = telegram and any(channel.id == channel_id for channel in telegram.channels)
            if not configured:
                return admin.JSONResponse({"error": "Channel is not configured."}, status_code=400)
        except Exception:
            return admin.JSONResponse({"error": "Source configuration is unavailable."}, status_code=503)

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
            return {
                "ok": True,
                "source_id": channel_id,
                "status": "inaccessible" if inaccessible else "unknown",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "message": ("Telegram did not grant access to this channel."
                            if inaccessible else "Telegram could not confirm read access to this channel."),
            }
        return {
            "ok": True,
            "source_id": channel_id,
            "status": "accessible",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "message": "Telegram read access confirmed.",
        }

    return router
