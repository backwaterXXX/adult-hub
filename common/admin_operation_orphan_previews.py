"""Preview and revision checks for orphan file quarantine and restore."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path, PurePosixPath

from psycopg.types.json import Jsonb

from common import db
from common.admin_operations import MAX_ORPHAN_QUARANTINE_BATCH, PLAN_TTL_SECONDS, _canonical


def _orphan_relative_posix(relative) -> PurePosixPath | None:
    if not isinstance(relative, str) or not relative or len(relative) > 2048 or "\x00" in relative:
        return None
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or "" in posix.parts or "\\" in relative:
        return None
    return posix


def _orphan_identity(item: dict) -> dict | None:
    identity = {key: item.get(key) for key in ("size", "mtime_ns", "dev", "ino")}
    if any(type(value) is not int or value < 0 for value in identity.values()):
        return None
    return identity


def _orphan_file_is_unchanged(original: Path, root: Path, identity: dict) -> bool:
    """Return True when the file is still a regular file under root with the recorded identity."""
    try:
        stat = original.lstat()
    except OSError:
        return False
    return not (original.is_symlink() or not original.is_file() or not original.resolve().is_relative_to(root)
                or any(getattr(stat, key) != identity[stored] for key, stored in
                       (("st_size", "size"), ("st_mtime_ns", "mtime_ns"), ("st_dev", "dev"), ("st_ino", "ino"))))


def _safe_orphan_files(raw, media_root: Path) -> list[dict] | None:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 25:
        return None
    files, seen, family = [], set(), None
    root = media_root.resolve()
    for item in raw:
        if not isinstance(item, dict):
            return None
        relative = item.get("path")
        posix = _orphan_relative_posix(relative)
        if posix is None:
            return None
        suffix = Path(relative).suffix.lower()
        if suffix not in {".strm", ".nfo", ".jpg"} or item.get("suffix") != suffix:
            return None
        family_key = (posix.parent.as_posix(), posix.stem)
        if family is None:
            family = family_key
        if family != family_key or relative in seen:
            return None
        seen.add(relative)
        identity = _orphan_identity(item)
        if identity is None:
            return None
        original = root / Path(*posix.parts)
        if not _orphan_file_is_unchanged(original, root, identity):
            return None
        files.append({"path": relative, "suffix": suffix, **identity})
    files.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    return files

def _orphan_finding_state(conn, audit_id: str, finding_ids: list[int], media_root: Path) -> dict | None:
    job = conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s AND action='consistency_audit'",
                       (audit_id,)).fetchone()
    if job is None or str(job[0]) != "done":
        return None
    result = job[1] if isinstance(job[1], dict) else json.loads(job[1] or "{}")
    if result.get("library_inventory_status") != "complete":
        return None
    rows = conn.execute(
        "SELECT id,kind,status,details FROM admin_audit_items WHERE job_id=%s AND id=ANY(%s) ORDER BY id",
        (audit_id, finding_ids),
    ).fetchall()
    if len(rows) != len(finding_ids):
        return None
    items, revisions = [], {}
    for finding_id, kind, status, details in rows:
        if kind != "orphan_file" or status != "open":
            return None
        details = details if isinstance(details, dict) else json.loads(details or "{}")
        files = _safe_orphan_files(details.get("files"), media_root)
        if files is None:
            return None
        revision = hashlib.sha256(_canonical(files).encode("utf-8")).hexdigest()
        revisions[str(finding_id)] = revision
        items.append({"finding_id": int(finding_id), "files": files,
                      "total_bytes": sum(file["size"] for file in files)})
    return {"items": items, "revisions": revisions}

def preview_orphan_file_quarantine(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    if (not isinstance(audit_id, str) or not audit_id or len(audit_id) > 100
            or not isinstance(finding_ids, list) or not 1 <= len(finding_ids) <= MAX_ORPHAN_QUARANTINE_BATCH
            or any(type(value) is not int or value <= 0 for value in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)):
        return {"outcome": "invalid", "message": f"Select between 1 and {MAX_ORPHAN_QUARANTINE_BATCH} valid orphan families."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        state = _orphan_finding_state(conn, audit_id, finding_ids, Path(os.getenv("MEDIA_DIR", "/media/adult")))
        if state is None:
            conn.rollback()
            return {"outcome": "conflict", "message": "The audit is partial or selected files changed. Run a new complete audit."}
        plan_id, now = uuid.uuid4().hex, int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans (plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'orphan_file_quarantine','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"audit_id": audit_id, "finding_ids": finding_ids}),
             Jsonb(state["revisions"]), Jsonb({"items": state["items"]}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "orphan_file_quarantine", "items": state["items"], "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def preview_orphan_file_restore(db_url: str | None, operation_id: str) -> dict:
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 100:
        return {"outcome": "invalid", "message": "Specify a valid file quarantine operation."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        row = conn.execute("SELECT state,files FROM admin_orphan_quarantine WHERE operation_id=%s", (operation_id,)).fetchone()
        if not row or row[0] != "active":
            conn.rollback()
            return {"outcome": "conflict", "message": "This file quarantine is not available to restore."}
        files = row[1] if isinstance(row[1], list) else json.loads(row[1] or "[]")
        media_root = Path(os.getenv("MEDIA_DIR", "/media/adult")).resolve()
        data_default = "/data" if Path("/data").exists() else "./data"
        quarantine_root = Path(os.getenv("DATA_DIR", data_default)).resolve() / "duplicate-quarantine"
        for item in files:
            relative = PurePosixPath(item.get("path", ""))
            quarantine_relative = PurePosixPath(item.get("quarantine_path", ""))
            if (relative.is_absolute() or ".." in relative.parts or not relative.parts or "\\" in item.get("path", "")
                    or quarantine_relative.is_absolute() or ".." in quarantine_relative.parts
                    or not quarantine_relative.parts or "\\" in item.get("quarantine_path", "")):
                return {"outcome": "conflict", "message": "The quarantine record is invalid."}
            target = media_root.joinpath(*relative.parts)
            if target.exists() or target.is_symlink():
                return {"outcome": "conflict", "message": "A restore target already exists. No files were changed."}
            stored = quarantine_root.joinpath(*quarantine_relative.parts)
            expected = item.get("quarantine_identity") if isinstance(item.get("quarantine_identity"), dict) else {}
            try:
                stat = stored.lstat()
            except OSError:
                return {"outcome": "conflict", "message": "A quarantined file is missing. No files were changed."}
            if (stored.is_symlink() or not stored.is_file()
                    or stat.st_size != expected.get("size") or stat.st_mtime_ns != expected.get("mtime_ns")):
                return {"outcome": "conflict", "message": "A quarantined file changed. No files were changed."}
        digest = hashlib.sha256(_canonical(files).encode("utf-8")).hexdigest()
        plan_id, now = uuid.uuid4().hex, int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        items = [{"path": item["path"], "size": item["size"]} for item in files]
        conn.execute(
            "INSERT INTO admin_operation_plans (plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'orphan_file_restore','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"operation_id": operation_id}), Jsonb({"operation_id": digest}),
             Jsonb({"items": items}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "orphan_file_restore", "items": items, "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
