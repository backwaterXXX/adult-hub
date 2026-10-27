"""Worker for bounded Telegram frame signatures and visual pair screening."""
from __future__ import annotations

import hashlib
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request

from common import db
from common.video_visual_jobs import (claim_next, finish_comparison, finish_failure,
                                     finish_signature, heartbeat, recover_expired)
from resolver_video_visual import compare_pair
from resolver_video_fingerprints import telegram_phash

LOG = logging.getLogger("resolver.video_visual_worker")
DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True


def _resolver_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/admin/health/ready", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _source(video: dict) -> str:
    fields = (str(video["id"]), str(video["duration"]), str(video.get("file_size_bytes") or ""),
              str(video.get("oshash") or ""))
    return hashlib.sha256("\0".join(fields).encode()).hexdigest()


def _aspect(probe) -> float | None:
    if isinstance(probe, str):
        import json
        try:
            probe = json.loads(probe)
        except (TypeError, ValueError):
            return None
    if not isinstance(probe, dict):
        return None
    streams = [stream for stream in probe.get("streams", [])
               if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    ratios = [float(stream["width"]) / float(stream["height"])
              for stream in streams if stream.get("width") and stream.get("height")]
    return ratios[0] if ratios else None


def _signature(job: dict) -> None:
    conn = db.connect(DB_URL)
    try:
        conn.row_factory = db.Record
        video = conn.execute("""SELECT v.id,v.duration,v.file_size_bytes,f.fingerprint AS oshash
                              FROM videos v LEFT JOIN video_fingerprints f ON f.video_id=v.id AND f.algorithm='OSHASH'
                              WHERE v.id=%s AND v.provider='telegram'""", (job["video_id"],)).fetchone()
    finally:
        conn.close()
    if not video or not video["duration"]:
        finish_failure(DB_URL, job, "source_missing", retryable=False)
        return
    video = dict(video)
    if _source(video) != job["source_key"]:
        finish_failure(DB_URL, job, "source_changed", retryable=False)
        return
    result = telegram_phash(video["id"], float(video["duration"]), byte_budget=256 * 1024 * 1024,
                            timeout_seconds=900, resolver_url="http://127.0.0.1:8090/admin",
                            cache_dir=os.getenv("PHASH_RANGE_CACHE_DIR", "/data/cache/phash-ranges"),
                            cache_id=job["id"], include_visual_signature=True, include_thumbnail=False)
    finish_signature(DB_URL, job, source_key=job["source_key"], duration=float(video["duration"]),
                     signature=result[2], contact_sheet=result[3])


def _comparison(job: dict) -> None:
    conn = db.connect(DB_URL)
    try:
        conn.row_factory = db.Record
        rows = conn.execute("""SELECT video_id,source_key,duration_seconds
                              FROM video_visual_signatures WHERE video_id=ANY(%s)
                              ORDER BY video_id""", ([job["left_id"], job["right_id"]],)).fetchall()
        videos = conn.execute("""SELECT v.id,v.duration,v.file_size_bytes,f.fingerprint AS oshash,
                              f.media_probe_data FROM videos v LEFT JOIN video_fingerprints f
                              ON f.video_id=v.id AND f.algorithm='OSHASH'
                              WHERE v.id=ANY(%s) ORDER BY v.id""",
                              ([job["left_id"], job["right_id"]],)).fetchall()
    finally:
        conn.close()
    if len(rows) != 2:
        finish_failure(DB_URL, job, "signature_missing", retryable=True)
        return
    left, right = dict(rows[0]), dict(rows[1])
    if (left["source_key"], right["source_key"]) != (job["source_key"], job["right_source_key"]):
        finish_failure(DB_URL, job, "source_changed", retryable=True)
        return
    if len(videos) != 2 or any(_source(dict(video)) != expected for video, expected in
                               zip(videos, (job["source_key"], job["right_source_key"]))):
        finish_failure(DB_URL, job, "source_changed", retryable=True)
        return
    state, evidence, sheet = compare_pair(
        job["left_id"], job["right_id"], float(left["duration_seconds"]), float(right["duration_seconds"]),
        left_aspect=_aspect(videos[0]["media_probe_data"]),
        right_aspect=_aspect(videos[1]["media_probe_data"]),
        cache_dir=os.getenv("PHASH_RANGE_CACHE_DIR", "/data/cache/phash-ranges"),
    )
    finish_comparison(DB_URL, job, state=state, evidence=evidence, frame_sheet=sheet)


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    first_cycle = True
    while not STOP:
        try:
            recover_expired(DB_URL, force=first_cycle)
            first_cycle = False
            if not _resolver_ready():
                time.sleep(2)
                continue
            job = claim_next(DB_URL)
            if not job:
                time.sleep(2)
                continue
            heartbeat_stop = threading.Event()
            def keep_lease() -> None:
                while not heartbeat_stop.wait(15):
                    try:
                        if not heartbeat(DB_URL, job):
                            LOG.error("Visual job lease was lost: %s", job["id"])
                            return
                    except Exception:
                        LOG.exception("Could not extend visual job lease %s", job["id"])
                        return
            heartbeat_thread = threading.Thread(target=keep_lease, daemon=True)
            heartbeat_thread.start()
            try:
                (_signature if job["kind"] == "signature" else _comparison)(job)
            except Exception as exc:
                LOG.exception("Visual job %s failed", job["id"])
                finish_failure(DB_URL, job, type(exc).__name__[:80], retryable=True)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2)
        except Exception:
            LOG.exception("Visual worker cycle failed")
            time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
