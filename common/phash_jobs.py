"""Durable PostgreSQL queue for server-side video pHash generation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from common import db
from common.telegram_transfer_budget import (
    current_local_day_start_timestamp,
    next_daily_reset_timestamp,
)

MAX_ATTEMPTS = 3
LEASE_SECONDS = 90


def enqueue(db_url: str | None, *, video_id: str, duration_us: int,
            algorithm_version: str, decoder_profile: str, user_id: str) -> dict:
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        identity = f"{video_id}:{algorithm_version}:{decoder_profile}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("phash:" + identity,))
        video = conn.execute(
            "SELECT provider FROM videos WHERE id=%s FOR UPDATE", (video_id,),
        ).fetchone()
        if video is None or video[0] != "telegram":
            conn.commit()
            return {"outcome": "not_found"}

        row = conn.execute(
            "SELECT * FROM video_phash_jobs WHERE video_id=%s AND algorithm_version=%s "
            "AND decoder_profile=%s FOR UPDATE",
            (video_id, algorithm_version, decoder_profile),
        ).fetchone()
        current = conn.execute(
            """SELECT p.fingerprint, f.fingerprint, t.video_id
               FROM video_phash_provenance p
               JOIN video_fingerprints f ON f.video_id=p.video_id AND f.algorithm='PHASH'
               LEFT JOIN video_phash_thumbnails t ON t.video_id=p.video_id
               WHERE p.video_id=%s AND p.generator_version=%s""",
            (video_id, algorithm_version + ":" + decoder_profile),
        ).fetchone()
        complete = bool(current and current[0] == current[1] and current[2] is not None)

        if row is not None and row["state"] in {"queued", "processing"} and not complete:
            conn.commit()
            return {"outcome": "existing", "job": dict(row)}
        if complete:
            if row is None:
                job_id = "p-" + uuid.uuid4().hex
                row = conn.execute(
                    """INSERT INTO video_phash_jobs
                       (id,video_id,algorithm_version,decoder_profile,duration_us,submitted_by_user_id,
                        state,attempts,fingerprint,created_at,updated_at,finished_at)
                       VALUES (%s,%s,%s,%s,%s,%s,'done',0,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                       RETURNING *""",
                    (job_id, video_id, algorithm_version, decoder_profile, duration_us, user_id, current[0]),
                ).fetchone()
            elif row["state"] != "done":
                row = conn.execute(
                    """UPDATE video_phash_jobs SET state='done',fingerprint=%s,error_code=NULL,error_detail='',
                       processing_duration_ms=processing_duration_ms + CASE WHEN processing_started_at IS NOT NULL
                           THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-processing_started_at))*1000)::BIGINT)
                           ELSE 0 END,processing_started_at=NULL,
                       lease_token=NULL,lease_until=NULL,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s RETURNING *""", (current[0], row["id"]),
                ).fetchone()
            conn.commit()
            return {"outcome": "complete", "job": dict(row)}

        retry_after_budget_reset = False
        if row is not None and row["state"] == "failed" and row["error_code"] == "daily_transfer_budget_exhausted":
            updated_at = row["updated_at"]
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            retry_after_budget_reset = updated_at.timestamp() < current_local_day_start_timestamp()

        if row is not None and (row["state"] == "done" or retry_after_budget_reset):
            row = conn.execute(
                """UPDATE video_phash_jobs SET state='queued',duration_us=%s,attempts=0,
                   available_at=CURRENT_TIMESTAMP,lease_token=NULL,lease_until=NULL,
                   fingerprint=NULL,bytes_read=NULL,error_code=NULL,error_detail='',finished_at=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *""",
                (duration_us, row["id"]),
            ).fetchone()
        elif row is not None:
            # Failed and cancelled jobs require an explicit admin retry.
            conn.commit()
            return {"outcome": "terminal", "job": dict(row)}
        else:
            row = conn.execute(
                """INSERT INTO video_phash_jobs
                   (id,video_id,algorithm_version,decoder_profile,duration_us,submitted_by_user_id,state)
                   VALUES (%s,%s,%s,%s,%s,%s,'queued') RETURNING *""",
                ("p-" + uuid.uuid4().hex, video_id, algorithm_version, decoder_profile,
                 duration_us, user_id),
            ).fetchone()
        conn.commit()
        return {"outcome": "queued", "job": dict(row)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _public_job(conn, row) -> dict:
    item = dict(row)
    item["thumbnail_ready"] = conn.execute(
        """SELECT 1 FROM video_phash_provenance p
           JOIN video_fingerprints f ON f.video_id=p.video_id AND f.algorithm='PHASH'
           JOIN video_phash_thumbnails t ON t.video_id=p.video_id
           WHERE p.video_id=%s AND p.generator_version=%s AND p.fingerprint=f.fingerprint""",
        (item["video_id"], item["algorithm_version"] + ":" + item["decoder_profile"]),
    ).fetchone() is not None
    item.pop("submitted_by_user_id", None)
    item.pop("lease_token", None)
    item.pop("lease_until", None)
    item.pop("processing_started_at", None)
    item.pop("processing_duration_ms", None)
    return item


def _processing_time_select(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""{prefix}processing_duration_ms + CASE
        WHEN {prefix}state = 'processing' AND {prefix}processing_started_at IS NOT NULL
        THEN GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - {prefix}processing_started_at)) * 1000)::BIGINT)
        ELSE 0
    END AS processing_time_ms"""


def read_job(db_url: str | None, job_id: str) -> dict | None:
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        row = conn.execute(
            f"SELECT *, {_processing_time_select()} FROM video_phash_jobs WHERE id=%s", (job_id,),
        ).fetchone()
        result = _public_job(conn, row) if row else None
        conn.commit()
        return result
    finally:
        conn.close()


def list_jobs(db_url: str | None, *, state: str = "", limit: int = 50,
              offset: int = 0) -> dict:
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        clauses, params = [], []
        if state == "active":
            clauses.append("state IN ('queued','processing')")
        elif state:
            clauses.append("state=%s")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = conn.execute("SELECT COUNT(*) FROM video_phash_jobs" + where, params).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM video_phash_jobs WHERE state IN ('queued','processing')"
        ).fetchone()[0]
        jobs = [_public_job(conn, row) for row in conn.execute(
            f"SELECT j.*,v.title,{_processing_time_select('j')} "
            "FROM video_phash_jobs j JOIN videos v ON v.id=j.video_id" + where +
            " ORDER BY j.available_at,j.created_at,j.id LIMIT %s OFFSET %s",
            (*params, max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()]
        conn.commit()
        return {"available": True, "jobs": jobs, "total": int(total),
                "active_total": int(active), "offset": max(0, int(offset)),
                "limit": max(1, min(int(limit), 100)),
                "truncated": max(0, int(offset)) + len(jobs) < total}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_job(db_url: str | None, job_id: str) -> str:
    conn = db.connect(db_url)
    try:
        row = conn.execute(
            "SELECT state FROM video_phash_jobs WHERE id=%s FOR UPDATE", (job_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return "not_found"
        if row[0] not in {"queued", "processing"}:
            conn.commit()
            return "conflict"
        conn.execute(
            """UPDATE video_phash_jobs SET state='cancelled',error_code=NULL,error_detail='',
               processing_duration_ms=processing_duration_ms + CASE
                   WHEN state='processing' AND processing_started_at IS NOT NULL
                   THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-processing_started_at))*1000)::BIGINT)
                   ELSE 0 END,processing_started_at=NULL,lease_token=NULL,lease_until=NULL,
               finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE id=%s AND state IN ('queued','processing')""",
            (job_id,),
        )
        conn.commit()
        return "cancelled"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def retry_job(db_url: str | None, job_id: str) -> bool:
    conn = db.connect(db_url)
    try:
        changed = conn.execute(
            """UPDATE video_phash_jobs SET state='queued',attempts=0,available_at=CURRENT_TIMESTAMP,
               lease_token=NULL,lease_until=NULL,fingerprint=NULL,bytes_read=NULL,error_code=NULL,error_detail='',
               finished_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND state IN ('failed','cancelled')""",
            (job_id,),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_expired(db_url: str | None) -> int:
    conn = db.connect(db_url)
    try:
        conn.execute(
            """UPDATE video_phash_jobs SET state='failed',lease_token=NULL,lease_until=NULL,
               processing_duration_ms=processing_duration_ms + CASE WHEN processing_started_at IS NOT NULL
                   THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (updated_at-processing_started_at))*1000)::BIGINT)
                   ELSE 0 END,processing_started_at=NULL,
               error_code='attempts_exhausted',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE state='processing' AND lease_until < CURRENT_TIMESTAMP AND attempts >= %s""",
            (MAX_ATTEMPTS,),
        )
        changed = conn.execute(
            """UPDATE video_phash_jobs SET state='queued',lease_token=NULL,lease_until=NULL,
               processing_duration_ms=processing_duration_ms + CASE WHEN processing_started_at IS NOT NULL
                   THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (updated_at-processing_started_at))*1000)::BIGINT)
                   ELSE 0 END,processing_started_at=NULL,
               error_code='worker_restarted',available_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE state='processing' AND lease_until < CURRENT_TIMESTAMP AND attempts < %s""",
            (MAX_ATTEMPTS,),
        ).rowcount
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next(db_url: str | None) -> dict | None:
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        row = conn.execute(
            """SELECT * FROM video_phash_jobs WHERE state='queued' AND available_at <= CURRENT_TIMESTAMP
               ORDER BY available_at,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"""
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        token = uuid.uuid4().hex
        result = conn.execute(
            """UPDATE video_phash_jobs SET state='processing',attempts=attempts+1,
               lease_token=%s,lease_until=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
               processing_started_at=CURRENT_TIMESTAMP,error_code=NULL,error_detail='',
               updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *""",
            (token, LEASE_SECONDS, row["id"]),
        ).fetchone()
        conn.commit()
        return dict(result)
    except db.IntegrityError:
        conn.rollback()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def heartbeat(db_url: str | None, job_id: str, token: str) -> bool:
    conn = db.connect(db_url)
    try:
        changed = conn.execute(
            """UPDATE video_phash_jobs SET lease_until=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
               updated_at=CURRENT_TIMESTAMP WHERE id=%s AND state='processing' AND lease_token=%s""",
            (LEASE_SECONDS, job_id, token),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_failure(db_url: str | None, job: dict, error_code: str, *, retryable: bool,
                   error_detail: str = "") -> bool:
    error_detail = str(error_detail or "")[:2000]
    conn = db.connect(db_url)
    try:
        if retryable and int(job["attempts"]) < MAX_ATTEMPTS:
            delay = 30 if int(job["attempts"]) == 1 else 120
            changed = conn.execute(
                """UPDATE video_phash_jobs SET state='queued',lease_token=NULL,lease_until=NULL,
                   processing_duration_ms=processing_duration_ms + CASE WHEN processing_started_at IS NOT NULL
                       THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-processing_started_at))*1000)::BIGINT)
                       ELSE 0 END,processing_started_at=NULL,
                   error_code=%s,error_detail=%s,available_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                   updated_at=CURRENT_TIMESTAMP WHERE id=%s AND state='processing' AND lease_token=%s""",
                (error_code, error_detail, delay, job["id"], job["lease_token"]),
            ).rowcount
        else:
            changed = conn.execute(
                """UPDATE video_phash_jobs SET state='failed',lease_token=NULL,lease_until=NULL,
                   processing_duration_ms=processing_duration_ms + CASE WHEN processing_started_at IS NOT NULL
                       THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-processing_started_at))*1000)::BIGINT)
                       ELSE 0 END,processing_started_at=NULL,
                   error_code=%s,error_detail=%s,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s AND state='processing' AND lease_token=%s""",
                (error_code, error_detail, job["id"], job["lease_token"]),
            ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def defer_until_daily_reset(db_url: str | None, job: dict) -> bool:
    """Keep daily-budget-limited work queued until the transfer budget resets."""
    reset_at = next_daily_reset_timestamp()
    conn = db.connect(db_url)
    try:
        changed = conn.execute(
            """UPDATE video_phash_jobs SET state='queued',attempts=0,available_at=to_timestamp(%s),
               lease_token=NULL,lease_until=NULL,processing_duration_ms=processing_duration_ms + CASE
                   WHEN processing_started_at IS NOT NULL
                   THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-processing_started_at))*1000)::BIGINT)
                   ELSE 0 END,processing_started_at=NULL,error_code='daily_transfer_budget_exhausted',
               error_detail='Waiting for the daily Telegram transfer budget to reset.',finished_at=NULL,
               updated_at=CURRENT_TIMESTAMP WHERE id=%s AND state='processing' AND lease_token=%s""",
            (reset_at, job["id"], job["lease_token"]),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_success(conn, job: dict, fingerprint: str, bytes_read: int) -> bool:
    """Mark complete in the same transaction as fingerprint/provenance/thumbnail storage."""
    changed = conn.execute(
        """UPDATE video_phash_jobs SET state='done',fingerprint=%s,bytes_read=%s,error_code=NULL,error_detail='',
           processing_duration_ms=processing_duration_ms + CASE WHEN processing_started_at IS NOT NULL
               THEN GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-processing_started_at))*1000)::BIGINT)
               ELSE 0 END,processing_started_at=NULL,
           lease_token=NULL,lease_until=NULL,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
           WHERE id=%s AND state='processing' AND lease_token=%s
             AND lease_until >= CURRENT_TIMESTAMP""",
        (fingerprint, bytes_read, job["id"], job["lease_token"]),
    ).rowcount
    return changed == 1


def worker_lock(db_url: str | None):
    """Keep one PostgreSQL session advisory lock for the worker process lifetime."""
    conn = db.connect(db_url)
    try:
        conn.autocommit = True
        acquired = conn.execute("SELECT pg_try_advisory_lock(%s)", (0x504841534851,)).fetchone()[0]
        if acquired:
            return conn
        conn.close()
        return None
    except Exception:
        conn.close()
        raise
