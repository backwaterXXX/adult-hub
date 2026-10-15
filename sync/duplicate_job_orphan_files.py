"""Durable file quarantine and restore operations for duplicate jobs."""
from __future__ import annotations

import json
import time
from pathlib import Path

from psycopg.types.json import Jsonb

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core


def _orphan_file_operation(conn, job_id: str, payload: dict, media_dir: Path, quarantine_root: Path,
                           services, moved: list[tuple[Path, Path]]) -> dict:
    """Move an audited orphan family to or from quarantine with a durable journal."""
    from common.admin_operations import _orphan_finding_state

    mode = payload.get("mode")
    media_root = Path(media_dir).resolve()
    if mode == "quarantine":
        plan_id = str(payload.get("plan_id") or "")
        plan = conn.execute("SELECT scope,revisions FROM admin_operation_plans WHERE plan_id=%s "
                            "AND operation_type='orphan_file_quarantine'", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("Orphan quarantine plan is unavailable.")
        scope = plan[0] if isinstance(plan[0], dict) else json.loads(plan[0] or "{}")
        revisions = plan[1] if isinstance(plan[1], dict) else json.loads(plan[1] or "{}")
        audit_id, finding_ids = scope.get("audit_id"), scope.get("finding_ids")
        operation_id = plan_id
        existing = conn.execute("SELECT state,files FROM admin_orphan_quarantine WHERE operation_id=%s FOR UPDATE",
                                (operation_id,)).fetchone()
        if existing:
            files = existing[1] if isinstance(existing[1], list) else json.loads(existing[1] or "[]")
        else:
            current = _orphan_finding_state(conn, str(audit_id), finding_ids, media_root)
            if current is None or current["revisions"] != revisions:
                raise ValueError("Audit findings or library files changed before quarantine.")
            files = []
            for family in current["items"]:
                for item in family["files"]:
                    files.append({**item, "quarantine_path": (Path("orphan-files") / operation_id / item["path"]).as_posix(),
                                  "finding_id": family["finding_id"]})
            with conn:
                conn.execute("INSERT INTO admin_orphan_quarantine (operation_id,audit_id,job_id,state,files) "
                             "VALUES (%s,%s,%s,'moving',%s)",
                             (operation_id, audit_id, job_id, Jsonb(files)))
    elif mode == "restore":
        operation_id = str(payload.get("operation_id") or "")
        row = conn.execute("SELECT state,files FROM admin_orphan_quarantine WHERE operation_id=%s FOR UPDATE",
                           (operation_id,)).fetchone()
        if not row or row[0] not in {"active", "restoring"}:
            raise ValueError("File quarantine is not available to restore.")
        files = row[1] if isinstance(row[1], list) else json.loads(row[1] or "[]")
        with conn:
            conn.execute("UPDATE admin_orphan_quarantine SET state='restoring',job_id=%s,updated_at=to_timestamp(%s) "
                         "WHERE operation_id=%s AND state IN ('active','restoring')",
                         (job_id, int(time.time()), operation_id))
    else:
        raise ValueError("Orphan file operation mode is invalid.")

    if not isinstance(files, list) or not files or len(files) > 75:
        raise ValueError("Orphan file operation journal is invalid.")
    total, moved_count = len(files), 0
    stage = "moving_files" if mode == "quarantine" else "restoring_files"
    _core._record_job_progress(conn, job_id, stage, completed=0, total=total)
    for item in files:
        rel = Path(str(item.get("path") or ""))
        qrel = Path(str(item.get("quarantine_path") or ""))
        if (rel.is_absolute() or qrel.is_absolute() or ".." in rel.parts or ".." in qrel.parts
                or "\\" in str(item.get("path") or "") or "\\" in str(item.get("quarantine_path") or "")
                or rel.suffix.lower() not in {".strm", ".nfo", ".jpg"}):
            raise ValueError("Unsafe path in orphan file operation journal.")
        original = media_root.joinpath(*rel.parts)
        quarantined = quarantine_root.joinpath(*qrel.parts)
        quarantine_base = quarantine_root.resolve()
        if (not original.resolve().is_relative_to(media_root)
                or not quarantined.parent.resolve().is_relative_to(quarantine_base)):
            raise ValueError("Orphan file paths resolve outside their managed roots.")
        if mode == "quarantine":
            if original.exists() or original.is_symlink():
                stat = original.lstat()
                identity = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
                expected = (item.get("size"), item.get("mtime_ns"), item.get("dev"), item.get("ino"))
                if original.is_symlink() or not original.is_file() or identity != expected:
                    raise ValueError("An orphan file changed after the audit; nothing was overwritten.")
                if quarantined.exists() or quarantined.is_symlink():
                    if not services.duplicate_file_matches(original, quarantined):
                        raise FileExistsError("A quarantine target already exists; nothing was overwritten.")
                    original.unlink()
                else:
                    services.duplicate_move_file(original, quarantined)
                moved.append((original, quarantined))
            elif not quarantined.exists() or quarantined.is_symlink():
                raise FileNotFoundError("An audited orphan file is missing from both locations.")
            qstat = quarantined.lstat()
            if (quarantined.is_symlink() or not quarantined.is_file()
                    or qstat.st_size != item.get("size") or qstat.st_mtime_ns != item.get("mtime_ns")):
                raise ValueError("A quarantine file does not match the audited orphan; review is required.")
            item["quarantine_identity"] = {"size": int(qstat.st_size), "mtime_ns": int(qstat.st_mtime_ns),
                                           "dev": int(qstat.st_dev), "ino": int(qstat.st_ino)}
        else:
            if quarantined.exists() or quarantined.is_symlink():
                qstat = quarantined.lstat()
                if (quarantined.is_symlink() or not quarantined.is_file()
                        or qstat.st_size != item.get("size")
                        or qstat.st_mtime_ns != item.get("mtime_ns")):
                    raise ValueError("A quarantined file changed; restore was stopped safely.")
                if original.exists() or original.is_symlink():
                    raise FileExistsError("A restore target already exists; nothing was overwritten.")
                services.duplicate_move_file(quarantined, original)
                moved.append((quarantined, original))
            else:
                stat = original.lstat() if original.exists() and not original.is_symlink() else None
                if stat is None or stat.st_size != item.get("size") or stat.st_mtime_ns != item.get("mtime_ns"):
                    raise FileNotFoundError("A file is missing from both its original and quarantine locations.")
        moved_count += 1
        if mode == "quarantine":
            with conn:
                conn.execute("UPDATE admin_orphan_quarantine SET files=%s,updated_at=to_timestamp(%s) "
                             "WHERE operation_id=%s", (Jsonb(files), int(time.time()), operation_id))
        _core._record_job_progress(conn, job_id, stage, completed=moved_count, total=total)

    if mode == "quarantine":
        with conn:
            conn.execute("UPDATE admin_audit_items SET status='resolved' WHERE job_id=%s AND id=ANY(%s) "
                         "AND kind='orphan_file' AND status='open'", (audit_id, scope["finding_ids"]))
            conn.execute("UPDATE admin_orphan_quarantine SET state='active',updated_at=to_timestamp(%s) "
                         "WHERE operation_id=%s", (int(time.time()), operation_id))
        return {"operation_id": operation_id, "files_quarantined": total, "local_state": "quarantined"}
    with conn:
        conn.execute("UPDATE admin_orphan_quarantine SET state='restored',updated_at=to_timestamp(%s) "
                     "WHERE operation_id=%s", (int(time.time()), operation_id))
    return {"operation_id": operation_id, "files_restored": total, "local_state": "restored"}
