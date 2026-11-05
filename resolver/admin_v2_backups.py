"""Backup inventory and non-destructive integrity verification."""

from __future__ import annotations

import gzip
import hashlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
import logging
from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
from common import db

logger = logging.getLogger(__name__)


_SHA256_LINE = re.compile(r"^([0-9a-f]{64})\s+(.+)$")


def _allowed_backup(root: Path, filename: str) -> Path | None:
    if not filename or Path(filename).name != filename or filename.startswith("."):
        return None
    if not (filename.startswith("adulthub-") and filename.endswith(".dump.gz")
            or filename.startswith("adult_hub-") and filename.endswith(".db")):
        return None
    path = root / filename
    try:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            return None
        return path
    except OSError:
        return None


def _verify(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    sidecar = path.with_name(path.name + ".sha256")
    checksum_status = "not_recorded"
    if sidecar.is_file() and not sidecar.is_symlink():
        with sidecar.open("rb") as checksum_file:
            line = checksum_file.read(256).decode("ascii", errors="strict").strip()
        match = _SHA256_LINE.fullmatch(line)
        if match:
            recorded_name = match.group(2)
            name_matches = recorded_name == path.name or Path(recorded_name).name == path.name
            checksum_status = "valid" if name_matches and match.group(1) == actual else "mismatch"
        else:
            checksum_status = "mismatch"

    if path.name.endswith(".dump.gz"):
        pg_restore = shutil.which("pg_restore")
        if not pg_restore:
            return {"checksum": actual, "checksum_status": checksum_status, "archive_status": "tool_unavailable"}
        with tempfile.NamedTemporaryFile(suffix=".dump") as raw:
            with gzip.open(path, "rb") as compressed:
                total = 0
                while chunk := compressed.read(1024 * 1024):
                    total += len(chunk)
                    if total > 8 * 1024 * 1024 * 1024:
                        return {"checksum": actual, "checksum_status": checksum_status, "archive_status": "too_large_to_verify"}
                    raw.write(chunk)
            raw.flush()
            result = subprocess.run([pg_restore, "--list", raw.name], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=30, check=False)
        archive_status = "valid" if result.returncode == 0 else "invalid"
    else:
        conn = None
        try:
            uri = f"file:{path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            archive_status = "valid" if conn.execute("PRAGMA quick_check").fetchone()[0] == "ok" else "invalid"
        except (sqlite3.Error, OSError):
            archive_status = "invalid"
        finally:
            if conn is not None:
                conn.close()
    return {"checksum": actual, "checksum_status": checksum_status, "archive_status": archive_status}


def _restore_test_target_ready(admin) -> bool:
    return db.restore_test_target_available(
        getattr(admin, "BACKUP_RESTORE_DATABASE_URL", ""),
        getattr(admin, "HUB_DB_URL", ""),
    )


def _backup_entry(item: dict) -> dict:
    return {"name": item["name"], "backup_type": item.get("backup_type", "unknown"),
            "size_bytes": int(item["size_bytes"]), "modified_at": item["modified_at"],
            "checksum_status": item.get("checksum_status", "not_recorded"),
            "scope": "PostgreSQL database only" if item.get("backup_type") == "postgres_custom_dump"
            else "Legacy database snapshot; media and settings are not included"}


def _route_backups(router, admin):
    @router.get("/api/v2/backups")
    async def backups(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            status = await admin.asyncio.to_thread(admin._backup_status)
            files = [_backup_entry(item) for item in status.get("files", [])]
            reported_count = status.get("count")
            count = reported_count if type(reported_count) is int and reported_count >= 0 else len(files)
            total_bytes = status.get("total_bytes")
            total_bytes = total_bytes if type(total_bytes) is int and total_bytes >= 0 else None
            return {"ok": True, "items": files, "count": count, "total_bytes": total_bytes,
                    "latest": files[0] if files else None, "available": bool(status.get("available")),
                    "inventory_available": status.get("inventory_available") is True,
                    "restore_test_available": _restore_test_target_ready(admin)}
        except Exception:
            logger.warning("Request failed in backups", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Backup inventory is unavailable."}, status_code=503)

    return backups


def _route_verify(router, admin):
    @router.get("/api/v2/backups/verify")
    async def verify(request: Request, filename: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        path = _allowed_backup(Path(admin.BACKUP_DIR), filename)
        if path is None:
            return admin.JSONResponse({"ok": False, "error": "Backup was not found."}, status_code=404)
        try:
            result = await admin.asyncio.to_thread(_verify, path)
            return {"ok": True, "name": path.name, **result}
        except Exception:
            logger.warning("Request failed in verify", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Backup verification failed."}, status_code=503)

    return verify


def _route_create_backup(router, admin):
    @router.post("/api/v2/backups/create")
    async def create_backup(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.job_queue import enqueue_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "backup_create", {}
            )
            admin._wake_worker("jobs")
            return admin.JSONResponse({"ok": True, "job_id": job_id, "state": "queued"}, status_code=202)
        except Exception:
            logger.warning("Request failed in create_backup", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not queue the database backup."}, status_code=503)

    return create_backup


def _restore_test_filename(body):
    """Return the requested filename, or None when the body is not exactly {"filename": str}."""
    if not isinstance(body, dict) or set(body) != {"filename"} or not isinstance(body["filename"], str):
        return None
    return body["filename"]


def _route_restore_test(router, admin):
    @router.post("/api/v2/backups/restore-test")
    async def restore_test(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            filename = _restore_test_filename(body)
            if filename is None:
                return admin.JSONResponse({"ok": False, "error": "Provide one backup filename."}, status_code=400)
            path = _allowed_backup(Path(admin.BACKUP_DIR), filename)
            if path is None or not path.name.endswith(".dump.gz"):
                return admin.JSONResponse({"ok": False, "error": "PostgreSQL backup was not found."}, status_code=404)
            if not _restore_test_target_ready(admin):
                return admin.JSONResponse(
                    {"ok": False, "error": "A valid dedicated restore-test database connection is not configured."},
                    status_code=503,
                )
            from common.job_queue import enqueue_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "backup_restore_test", {"filename": path.name}
            )
            admin._wake_worker("jobs")
            return admin.JSONResponse({"ok": True, "job_id": job_id, "state": "queued"}, status_code=202)
        except Exception:
            logger.warning("Request failed in restore_test", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not queue the backup restore test."}, status_code=503)

    return restore_test


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)
    _route_backups(router, admin)
    _route_verify(router, admin)
    _route_create_backup(router, admin)
    _route_restore_test(router, admin)
    return router
