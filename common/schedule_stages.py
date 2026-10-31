"""Durable per-stage results for work that runs inside a system schedule pass.

A system schedule (library sync, database backup) executes several distinct
stages. The parent row only records the pass as a whole, so a failed stage was
invisible once the pass completed. Each stage keeps its own row here: the worker
writes it, the Admin API reads it, and an operator can switch a stage off without
stopping the pass around it.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from common import admin_schedules, db
from common.job_queue import enqueue_job

LOGGER = logging.getLogger(__name__)
MAX_ERROR_LENGTH = 200
_RESULTS = {"queued", "running", "done", "failed"}

# Stage failures are shown to operators and stored in the database, so the same
# redaction the sync worker applies to provider errors is used here as well.
_URL_WITH_QUERY = re.compile(r"https?://[^\s\]\[()<>\"']+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET = re.compile(r"(?i)(\b(?:token|api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;]+")
_TELEGRAM_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")

# A manually run stage is a durable job action, so the admin API can queue it and
# the jobs worker can report the result back to the same row the pass would use.
JOB_STAGES = {
    "history_cleanup": ("database-backup", "history-cleanup"),
    "performer_catalog": ("library-sync", "performer-catalog"),
    "scene_backfill": ("library-sync", "scene-identification"),
    "telegram_title_cleanup": ("library-sync", "telegram-title-cleanup"),
    "metadata_backfill": ("library-sync", "metadata-backfill"),
    "collections_refresh": ("library-sync", "jellyfin-refresh"),
}


class Stage(NamedTuple):
    id: str
    schedule_id: str
    name: str
    details: str
    run_action: str | None
    run_unavailable_reason: str | None


STAGES: tuple[Stage, ...] = (
    Stage(
        "history-cleanup", "database-backup", "Backup history cleanup",
        "Runs after a successful system backup; removes history older than one calendar month.",
        "history_cleanup", None,
    ),
    Stage(
        "performer-catalog", "library-sync", "StashDB performer catalog",
        "At sync, at most once per 24 hours; requires configured credentials.",
        "performer_catalog", None,
    ),
    Stage(
        "source-scans", "library-sync", "Configured source scans",
        "Scans enabled sources; Telegram availability checks run after successful channel scans.",
        None, "Runs only inside a locked sync pass.",
    ),
    Stage(
        "availability-reconciliation", "library-sync", "Telegram availability reconciliation",
        "Runs for eligible enabled channels after a successful channel scan.",
        None, "Needs the sync lock and the Telegram session of a running pass.",
    ),
    Stage(
        "scene-identification", "library-sync", "Scene identification",
        "Backfills scene fingerprints when the optional scraper is installed and work is available.",
        "scene_backfill", None,
    ),
    Stage(
        "metadata-backfill", "library-sync", "Performer and scene metadata backfill",
        "Title-based performer discovery and PBC, TPDB and StashDB enrichment; subject to"
        " configuration and available work.",
        "metadata_backfill", None,
    ),
    Stage(
        "telegram-title-cleanup", "library-sync", "Telegram title and hashtag cleanup",
        "Up to 500 stored Telegram videos per library sync.",
        "telegram_title_cleanup", None,
    ),
    Stage(
        "library-maintenance", "library-sync", "Library maintenance",
        "Retention, orphan cleanup and stream maintenance depend on source success and safety guards.",
        None, "Needs the per-source success signal that only a scan pass produces.",
    ),
    Stage(
        "jellyfin-refresh", "library-sync", "Jellyfin reconciliation and refresh",
        "Subject to configuration and available work.",
        "collections_refresh", None,
    ),
)

_BY_ID = {stage.id: stage for stage in STAGES}


def registry() -> tuple[Stage, ...]:
    return STAGES


def get_stage(stage_id: str) -> Stage | None:
    return _BY_ID.get(stage_id)


def stage_for_job(action: str):
    """The (schedule, stage) row a durable job action reports into, if any."""
    return JOB_STAGES.get(action)


def _clean_error(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _URL_WITH_QUERY.sub(lambda match: match.group(0).split("?", 1)[0], text)
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _SECRET.sub(r"\1[redacted]", text)
    text = _TELEGRAM_TOKEN.sub("[redacted-token]", text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return (text[:MAX_ERROR_LENGTH - 1] + "…") if len(text) > MAX_ERROR_LENGTH else (text or None)


def _connect(conn) -> db.Connection | None:
    """Borrow the caller's database identity without borrowing its transaction.

    Bookkeeping must never abort a running pass, so it always gets its own
    connection: a failed write rolls back here and cannot poison the caller.
    A handle that does not say which database it belongs to is refused, so a
    test double can never send writes to whatever DATABASE_URL happens to be set.
    """
    if isinstance(conn, str):
        return db.connect(conn)
    if conn is None:
        return db.connect(None)
    url = getattr(conn, "_url", None)
    return db.connect(url) if isinstance(url, str) and url else None


def stage_states(conn, schedule_id: str) -> dict[str, dict]:
    """Current enable flag and last result per stage; unrecorded stages are absent."""
    owned = not isinstance(conn, db.Connection)
    try:
        connection = _connect(conn) if owned else conn
    except db.Error:
        return {}
    if connection is None:
        return {}
    try:
        rows = connection.execute(
            "SELECT id,enabled,last_result,last_error FROM system_schedule_stages WHERE schedule_id=%s",
            (schedule_id,),
        ).fetchall()
    except (db.UndefinedTable, db.ProgrammingError):
        return {}
    except db.Error:
        LOGGER.warning("Could not read system schedule stages for %s.", schedule_id)
        return {}
    finally:
        if owned:
            connection.close()
    return {
        row[0]: {"enabled": bool(row[1]), "last_result": row[2], "last_error": row[3]}
        for row in rows if row[0] in _BY_ID
    }


def stage_enabled(conn, schedule_id: str, stage_id: str) -> bool:
    """True unless an operator switched this stage off; an unknown stage still runs."""
    return bool(stage_states(conn, schedule_id).get(stage_id, {}).get("enabled", True))


def mark_interrupted_stages(conn, schedule_id: str) -> int:
    """Close stages a dead process left running, so the next pass starts clean.

    Only 'running' rows are touched: a 'queued' row belongs to a durable job that
    has not started yet, and its own run will report the real result.
    """
    try:
        own = _connect(conn)
    except db.Error:
        return 0
    if own is None:
        return 0
    try:
        with own:
            return own.execute(
                "UPDATE system_schedule_stages SET last_result='failed',"
                "last_error=%s,updated_at=%s WHERE schedule_id=%s AND last_result='running'",
                (_clean_error("Interrupted before it reported a result."),
                 admin_schedules._utc().timestamp(), schedule_id),
            ).rowcount
    except (db.UndefinedTable, db.ProgrammingError):
        return 0
    except db.Error:
        LOGGER.warning("Could not close the interrupted stages of %s.", schedule_id)
        return 0
    finally:
        own.close()


def record_stage_start(conn, schedule_id: str, stage_id: str, *, now=None) -> None:
    if stage_id not in _BY_ID:
        return
    instant = admin_schedules._utc(now)
    try:
        own = _connect(conn)
    except db.Error:
        LOGGER.warning("Could not open a connection to record the %s stage start.", stage_id)
        return
    if own is None:
        LOGGER.warning("Cannot record the %s stage start: the connection has no database URL.", stage_id)
        return
    try:
        with own:
            own.execute(
                "INSERT INTO system_schedule_stages"
                " (schedule_id,id,last_result,last_run_at,last_finished_at,last_error,updated_at)"
                " VALUES (%s,%s,'running',%s,NULL,NULL,%s)"
                " ON CONFLICT(schedule_id,id) DO UPDATE SET last_result='running',"
                " last_run_at=%s,last_finished_at=NULL,last_error=NULL,updated_at=%s",
                (schedule_id, stage_id, instant.timestamp(), instant.timestamp(),
                 instant.timestamp(), instant.timestamp()),
            )
    except (db.UndefinedTable, db.ProgrammingError):
        LOGGER.debug("System schedule stages are not available yet.")
    except db.Error:
        LOGGER.warning("Could not record the %s stage start.", stage_id)
    finally:
        own.close()


def record_stage_finish(conn, schedule_id: str, stage_id: str, *, success: bool,
                        now=None, error=None) -> None:
    if stage_id not in _BY_ID:
        return
    if type(success) is not bool:
        raise ValueError("success must be boolean")
    instant = admin_schedules._utc(now)
    result = "done" if success else "failed"
    detail = None if success else _clean_error(error)
    try:
        own = _connect(conn)
    except db.Error:
        LOGGER.warning("Could not open a connection to record the %s stage result.", stage_id)
        return
    if own is None:
        LOGGER.warning("Cannot record the %s stage result: the connection has no database URL.", stage_id)
        return
    try:
        with own:
            own.execute(
                "INSERT INTO system_schedule_stages"
                " (schedule_id,id,last_result,last_run_at,last_finished_at,last_error,updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(schedule_id,id) DO UPDATE SET last_result=%s,"
                " last_finished_at=%s,last_error=%s,updated_at=%s",
                (schedule_id, stage_id, result, instant.timestamp(), instant, detail,
                 instant.timestamp(), result, instant, detail, instant.timestamp()),
            )
    except (db.UndefinedTable, db.ProgrammingError):
        LOGGER.debug("System schedule stages are not available yet.")
    except db.Error:
        LOGGER.warning("Could not record the %s stage result.", stage_id)
    finally:
        own.close()


def list_stage_runs(db_url) -> list[dict]:
    """Registry metadata merged with the recorded state of every stage."""
    rows = {}
    conn = db.connect(db_url)
    try:
        rows = {
            row[0]: row for row in conn.execute(
                "SELECT id,enabled,revision,last_result,last_error,last_run_at,last_finished_at"
                " FROM system_schedule_stages"
            ).fetchall()
        }
    except (db.UndefinedTable, db.ProgrammingError):
        rows = {}
    finally:
        conn.close()
    items = []
    for stage in STAGES:
        row = rows.get(stage.id)
        stored = row[1:] if row else None
        items.append({
            "id": stage.id,
            "schedule_id": stage.schedule_id,
            "name": stage.name,
            "details": stage.details,
            "run_action": stage.run_action,
            "run_unavailable_reason": stage.run_unavailable_reason,
            "enabled": bool(stored[0]) if stored else True,
            "revision": int(stored[1]) if stored else 1,
            "last_result": stored[2] if stored else None,
            "last_error": stored[3] if stored else None,
            "last_run_at": admin_schedules._utc(stored[4]).isoformat() if stored and stored[4] else None,
            "last_finished_at": admin_schedules._utc(stored[5]).isoformat() if stored and stored[5] else None,
        })
    return items


def set_stage_enabled(db_url, schedule_id: str, stage_id: str, *, enabled: bool,
                      revision: int, now=None) -> dict:
    stage = _BY_ID.get(stage_id)
    if stage is None or stage.schedule_id != schedule_id:
        return {"outcome": "not_found"}
    if type(enabled) is not bool or type(revision) is not int or revision < 1:
        raise ValueError("Provide an enabled state and a valid revision.")
    instant = admin_schedules._utc(now)
    conn = db.connect(db_url)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT enabled,revision FROM system_schedule_stages"
            " WHERE schedule_id=%s AND id=%s FOR UPDATE",
            (schedule_id, stage_id),
        ).fetchone()
        current = bool(row[0]) if row else True
        stored = int(row[1]) if row else 1
        if stored != revision:
            conn.rollback()
            return {"outcome": "conflict", "revision": stored}
        if row:
            conn.execute(
                "UPDATE system_schedule_stages SET enabled=%s,revision=revision+1,updated_at=%s"
                " WHERE schedule_id=%s AND id=%s",
                (enabled, instant.timestamp(), schedule_id, stage_id),
            )
        else:
            conn.execute(
                "INSERT INTO system_schedule_stages (schedule_id,id,enabled,revision,updated_at)"
                " VALUES (%s,%s,%s,2,%s)",
                (schedule_id, stage_id, enabled, instant.timestamp()),
            )
        from common.admin_audit_events import record_schedule_enabled
        if not record_schedule_enabled(
                conn, schedule_id=f"system-{schedule_id}/{stage_id}", before=current,
                after=enabled, revision_before=revision, revision_after=revision + 1):
            raise RuntimeError("Could not record the stage enabled audit event.")
        conn.commit()
        return {"outcome": "updated", "revision": revision + 1, "enabled": enabled}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_stage_run(db_url, schedule_id: str, stage_id: str, *, now=None) -> dict:
    """Queue a standalone stage run, refusing to overlap an unfinished one."""
    stage = _BY_ID.get(stage_id)
    if stage is None or stage.schedule_id != schedule_id:
        return {"outcome": "not_found"}
    if not stage.run_action:
        raise ValueError("This stage has no independent trigger.")
    instant = admin_schedules._utc(now)
    conn = db.connect(db_url)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT enabled,last_result FROM system_schedule_stages"
            " WHERE schedule_id=%s AND id=%s FOR UPDATE",
            (schedule_id, stage_id),
        ).fetchone()
        if row and row[0] is False:
            conn.rollback()
            return {"outcome": "disabled"}
        if row and row[1] in {"queued", "running"}:
            conn.rollback()
            return {"outcome": "conflict"}
        # The job is committed on its own connection before the marker is written:
        # a failed marker write leaves a runnable job whose worker records the row,
        # while the reverse order could strand the stage in 'queued' forever.
        job_id = enqueue_job(db_url, stage.run_action, {})
        conn.execute(
            "INSERT INTO system_schedule_stages (schedule_id,id,last_result,last_run_at,updated_at)"
            " VALUES (%s,%s,'queued',%s,%s)"
            " ON CONFLICT(schedule_id,id) DO UPDATE SET last_result='queued',last_run_at=%s,updated_at=%s",
            (schedule_id, stage_id, instant.timestamp(), instant.timestamp(),
             instant.timestamp(), instant.timestamp()),
        )
        conn.commit()
        return {"outcome": "queued", "action": stage.run_action, "job_id": job_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
