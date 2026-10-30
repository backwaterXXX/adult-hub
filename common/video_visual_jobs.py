"""Durable bounded queue for frame-signature generation and pair verification."""

from __future__ import annotations

import hashlib
import time
import uuid

from common import db
from psycopg.types.json import Jsonb

ALGORITHM_VERSION = "visual-sequence-v1"
CONFIRMATION_POLICY_VERSION = "strict-5-of-8-v2"
MAX_ATTEMPTS = 3
LEASE_SECONDS = 1800
# The worker renews its lease every 15 seconds, so a processing job whose row has
# not been touched for this long belongs to a process that is gone.
LEASE_STALE_SECONDS = 90


def _identity(kind: str, values: tuple[str, ...], algorithm_version: str) -> str:
    raw = "\0".join((kind, *values, algorithm_version))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enqueue_signature(db_url: str | None, video_id: str, *, priority: int = 30) -> str | None:
    conn = db.connect(db_url)
    try:
        row = conn.execute("""SELECT v.provider,v.duration,v.file_size_bytes,f.fingerprint
                              FROM videos v LEFT JOIN video_fingerprints f ON f.video_id=v.id AND f.algorithm='OSHASH'
                              WHERE v.id=%s""", (video_id,)).fetchone()
        if not row or row[0] != "telegram" or not row[1] or float(row[1]) <= 0:
            conn.commit()
            return None
        key = _identity("signature", (video_id,), ALGORITHM_VERSION)
        source_key = hashlib.sha256("\0".join(map(str, (video_id, row[1], row[2] or "", row[3] or ""))).encode()).hexdigest()
        job_id = "vs-" + key[:32]
        changed = conn.execute(
            """INSERT INTO video_visual_jobs
               (id,dedupe_key,kind,video_id,algorithm_version,state,priority,source_key)
               VALUES (%s,%s,'signature',%s,%s,'queued',%s,%s)
               ON CONFLICT (dedupe_key) DO UPDATE SET state='queued',attempts=0,
                 available_at=EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT,
                 lease_token=NULL,lease_until=NULL,error_code=NULL,error_detail='',
                 source_key=EXCLUDED.source_key,
                 priority=LEAST(video_visual_jobs.priority,EXCLUDED.priority),
                 updated_at=CURRENT_TIMESTAMP
               WHERE video_visual_jobs.state IN ('failed','cancelled') OR video_visual_jobs.source_key<>EXCLUDED.source_key""",
            (job_id, key, video_id, ALGORITHM_VERSION, max(0, min(int(priority), 100)), source_key),
        )
        conn.commit()
        return job_id if changed.rowcount else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def enqueue_pair(db_url: str | None, left_id: str, right_id: str, *, priority: int = 20) -> str | None:
    left_id, right_id = sorted((str(left_id), str(right_id)))
    if not left_id or left_id == right_id:
        return None
    conn = db.connect(db_url)
    try:
        rows = conn.execute(
            """SELECT v.id,v.provider,v.duration,v.file_size_bytes,f.fingerprint
               FROM videos v LEFT JOIN video_fingerprints f ON f.video_id=v.id AND f.algorithm='OSHASH'
               WHERE v.id=ANY(%s) ORDER BY v.id""",
            ([left_id, right_id],),
        ).fetchall()
        if len(rows) != 2 or any(row[1] != "telegram" or not row[2] or float(row[2]) <= 0 for row in rows):
            conn.commit()
            return None
        duration_left, duration_right = float(rows[0][2]), float(rows[1][2])
        if abs(duration_left - duration_right) > min(15, max(5, .01 * max(duration_left, duration_right))):
            conn.commit()
            return None
        key = _identity("verify_pair", (left_id, right_id), ALGORITHM_VERSION)
        job_id = "vp-" + key[:32]
        left_sig = conn.execute(
            "SELECT source_key FROM video_visual_signatures WHERE video_id=%s AND algorithm_version=%s",
            (left_id, ALGORITHM_VERSION),
        ).fetchone()
        right_sig = conn.execute(
            "SELECT source_key FROM video_visual_signatures WHERE video_id=%s AND algorithm_version=%s",
            (right_id, ALGORITHM_VERSION),
        ).fetchone()
        source_left = hashlib.sha256("\0".join(map(str, (rows[0][0], rows[0][2], rows[0][3] or "", rows[0][4] or ""))).encode()).hexdigest()
        source_right = hashlib.sha256("\0".join(map(str, (rows[1][0], rows[1][2], rows[1][3] or "", rows[1][4] or ""))).encode()).hexdigest()
        if not left_sig or not right_sig or left_sig[0] != source_left or right_sig[0] != source_right:
            conn.commit()
            return None
        comparison = conn.execute(
            """INSERT INTO video_visual_comparisons
               (id,left_id,right_id,algorithm_version,left_source_key,right_source_key,state)
               VALUES (%s,%s,%s,%s,%s,%s,'queued')
               ON CONFLICT (id) DO UPDATE SET state='queued',evidence='{}'::jsonb,
                 frame_sheet=NULL,left_source_key=EXCLUDED.left_source_key,
                 right_source_key=EXCLUDED.right_source_key,
                 updated_at=CURRENT_TIMESTAMP,
                 expires_at=NULL
               WHERE video_visual_comparisons.left_source_key<>EXCLUDED.left_source_key
                  OR video_visual_comparisons.right_source_key<>EXCLUDED.right_source_key
                  OR video_visual_comparisons.state='failed'
                  OR (video_visual_comparisons.state='queued' AND EXISTS (SELECT 1 FROM video_visual_jobs j WHERE j.id=video_visual_comparisons.id AND j.state IN ('failed','cancelled')))
                  OR (video_visual_comparisons.state IN ('confirmed','review','different','inconclusive')
                      AND video_visual_comparisons.evidence->>'confirmation_policy' IS DISTINCT FROM %s)""",
            (job_id, left_id, right_id, ALGORITHM_VERSION, source_left, source_right, CONFIRMATION_POLICY_VERSION),
        )
        if not comparison.rowcount:
            conn.commit()
            return None
        changed = conn.execute(
            """INSERT INTO video_visual_jobs
               (id,dedupe_key,kind,left_id,right_id,algorithm_version,state,priority,source_key,right_source_key)
               VALUES (%s,%s,'verify_pair',%s,%s,%s,'queued',%s,%s,%s)
               ON CONFLICT (dedupe_key) DO UPDATE SET state='queued',attempts=0,
                 available_at=EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT,
                 lease_token=NULL,lease_until=NULL,error_code=NULL,error_detail='',
                 source_key=EXCLUDED.source_key,right_source_key=EXCLUDED.right_source_key,
                 priority=LEAST(video_visual_jobs.priority,EXCLUDED.priority),
                 updated_at=CURRENT_TIMESTAMP
               WHERE video_visual_jobs.state IN ('failed','cancelled','done')
                  OR video_visual_jobs.source_key<>EXCLUDED.source_key
                  OR video_visual_jobs.right_source_key<>EXCLUDED.right_source_key""",
            (job_id, key, left_id, right_id, ALGORITHM_VERSION, max(0, min(int(priority), 100)), source_left, source_right),
        )
        conn.commit()
        return job_id if changed.rowcount else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next(db_url: str | None) -> dict | None:
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        conn.execute("BEGIN")
        row = conn.execute(
            """SELECT * FROM video_visual_jobs WHERE state='queued'
               AND available_at <= %s AND attempts < %s
               ORDER BY priority,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (int(time.time()), MAX_ATTEMPTS),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        token = uuid.uuid4().hex
        claimed = conn.execute(
            """UPDATE video_visual_jobs SET state='processing',attempts=attempts+1,
               lease_token=%s,lease_until=%s,updated_at=%s WHERE id=%s RETURNING *""",
            (token, int(time.time()) + LEASE_SECONDS, int(time.time()), row["id"]),
        ).fetchone()
        if claimed["kind"] == "verify_pair":
            conn.execute(
                "UPDATE video_visual_comparisons SET state='processing',updated_at=%s WHERE id=%s",
                (int(time.time()), row["id"]),
            )
        conn.commit()
        return dict(claimed)
    except db.IntegrityError:
        # video_visual_jobs_single_active_idx keeps the whole hub on one active
        # job, so a peer worker that committed its claim first leaves nothing
        # here: skip this cycle instead of failing the worker.
        conn.rollback()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def heartbeat(db_url: str | None, job: dict) -> bool:
    conn = db.connect(db_url)
    try:
        changed = conn.execute(
            """UPDATE video_visual_jobs SET lease_until=%s,updated_at=%s
               WHERE id=%s AND state='processing' AND lease_token=%s""",
            (int(time.time()) + LEASE_SECONDS, int(time.time()), job["id"], job["lease_token"]),
        ).rowcount
        conn.commit()
        return changed == 1
    finally:
        conn.close()


def finish_signature(db_url: str | None, job: dict, *, source_key: str,
                     duration: float, signature: dict, contact_sheet: bytes | None = None) -> bool:
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        row = conn.execute(
            """SELECT 1 FROM video_visual_jobs WHERE id=%s AND state='processing'
               AND lease_token=%s AND lease_until >= %s FOR UPDATE""",
            (job["id"], job["lease_token"], int(time.time())),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        conn.execute(
            """INSERT INTO video_visual_signatures
               (video_id,algorithm_version,source_key,duration_seconds,signature,contact_sheet)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (video_id) DO UPDATE SET algorithm_version=EXCLUDED.algorithm_version,
                 source_key=EXCLUDED.source_key,duration_seconds=EXCLUDED.duration_seconds,
                 signature=EXCLUDED.signature,contact_sheet=EXCLUDED.contact_sheet,updated_at=%s""",
            (job["video_id"], job["algorithm_version"], source_key, float(duration),
             Jsonb(signature), contact_sheet, int(time.time())),
        )
        conn.execute(
            """UPDATE video_visual_jobs SET state='done',lease_token=NULL,lease_until=NULL,
               error_code=NULL,error_detail='',finished_at=%s,updated_at=%s WHERE id=%s""",
            (int(time.time()), int(time.time()), job["id"]),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_comparison(db_url: str | None, job: dict, *, state: str, evidence: dict,
                      frame_sheet: bytes | None) -> bool:
    conn = db.connect(db_url)
    try:
        row = conn.execute(
            """SELECT 1 FROM video_visual_jobs WHERE id=%s AND state='processing'
               AND lease_token=%s AND lease_until >= %s FOR UPDATE""",
            (job["id"], job["lease_token"], int(time.time())),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        now = int(time.time())
        conn.execute(
            """UPDATE video_visual_comparisons SET state=%s,evidence=%s,frame_sheet=%s,
               updated_at=%s,expires_at=%s WHERE id=%s""",
            (state, Jsonb(evidence), frame_sheet, now, None, job["id"]),
        )
        conn.execute(
            """UPDATE video_visual_jobs SET state='done',lease_token=NULL,lease_until=NULL,
               error_code=NULL,error_detail='',finished_at=%s,updated_at=%s WHERE id=%s""",
            (now, now, job["id"]),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_failure(db_url: str | None, job: dict, error_code: str, *, retryable: bool) -> None:
    conn = db.connect(db_url)
    try:
        now = int(time.time())
        attempts = int(job.get("attempts") or 1)
        state = "queued" if retryable and attempts < MAX_ATTEMPTS else "failed"
        conn.execute(
            """UPDATE video_visual_jobs SET state=%s,available_at=%s,lease_token=NULL,lease_until=NULL,
               error_code=%s,updated_at=%s,finished_at=CASE WHEN %s='failed' THEN %s ELSE NULL END
               WHERE id=%s AND state='processing' AND lease_token=%s""",
            (state, now + min(3600, 30 * (2 ** max(0, attempts - 1))), error_code[:80], now,
             state, now, job["id"], job["lease_token"]),
        )
        if job.get("kind") == "verify_pair" and state == "failed":
            conn.execute(
                "UPDATE video_visual_comparisons SET state='failed',updated_at=%s WHERE id=%s",
                (now, job["id"]),
            )
        conn.commit()
    finally:
        conn.close()


def recover_expired(db_url: str | None, *, force: bool = False) -> int:
    """Return abandoned jobs to the queue, failing the ones out of attempts.

    ``force`` is what a restarting worker uses to reclaim the job it died with.
    It may not reset a lease another worker is still renewing: that would hand
    the same work to two workers, waste the in-flight run and void the first
    worker's lease token. A crashed worker is identified instead by its heartbeat
    going stale, which is far sooner than the lease itself expires.

    A job with no attempts left is marked failed instead of requeued, because
    ``claim_next`` never takes ``attempts >= MAX_ATTEMPTS``; requeueing it would
    strand it in ``queued`` forever. Rows already stuck that way are healed too.
    """
    conn = db.connect(db_url)
    try:
        now = int(time.time())
        if force:
            expired_sql, expired_at = "updated_at < %s", now - LEASE_STALE_SECONDS
        else:
            expired_sql, expired_at = "lease_until < %s", now
        exhausted = conn.execute(
            f"""UPDATE video_visual_jobs SET state='failed',lease_token=NULL,lease_until=NULL,
               error_code='attempts_exhausted',finished_at=%s,updated_at=%s
               WHERE state='processing' AND attempts >= %s AND {expired_sql}
               RETURNING id""",
            (now, now, MAX_ATTEMPTS, expired_at),
        ).fetchall()
        stuck = conn.execute(
            """UPDATE video_visual_jobs SET state='failed',lease_token=NULL,lease_until=NULL,
               error_code='attempts_exhausted',finished_at=%s,updated_at=%s
               WHERE state='queued' AND attempts >= %s
               RETURNING id""",
            (now, now, MAX_ATTEMPTS),
        ).fetchall()
        failed_ids = [str(row[0]) for row in exhausted] + [str(row[0]) for row in stuck]
        if failed_ids:
            conn.execute(
                """UPDATE video_visual_comparisons SET state='failed',updated_at=%s
                   WHERE id = ANY(%s)""",
                (now, failed_ids),
            )
        recovered = conn.execute(
            f"""UPDATE video_visual_jobs SET state='queued',lease_token=NULL,lease_until=NULL,
               available_at=%s,updated_at=%s
               WHERE state='processing' AND attempts < %s AND {expired_sql}
               RETURNING id""",
            (now, now, MAX_ATTEMPTS, expired_at),
        ).fetchall()
        ids = [str(row[0]) for row in recovered]
        if ids:
            conn.execute(
                """UPDATE video_visual_comparisons SET state='queued',updated_at=%s
                   WHERE id = ANY(%s) AND state='processing'""",
                (now, ids),
            )
        conn.commit()
        return len(ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
