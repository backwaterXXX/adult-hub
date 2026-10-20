"""Single-owner durable worker for resolver-side pHash and artwork generation."""

from __future__ import annotations

import base64
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

from common import db, phash_jobs
from common.app_version import __version__
from common.logging_setup import configure_logging
from common.worker_capabilities import publish_worker_capabilities

from admin_v2_phash import _store

LOGGER = configure_logging("resolver.phash_worker")
DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
CAPABILITIES_PATH = os.getenv("PHASH_WORKER_CAPABILITIES_PATH", "/data/cache/phash-worker-capabilities.json")
HEALTH_URL = "http://127.0.0.1:8090/admin/health/ready"
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 1200
MAX_BYTE_BUDGET = 256 * 1024 * 1024
PHASH_RANGE_CACHE_DIR = os.getenv("PHASH_RANGE_CACHE_DIR", "/data/cache/phash-ranges")
PHASH_RANGE_CACHE_TTL_SECONDS = 24 * 60 * 60
_stop = threading.Event()
_active_lock = threading.Lock()
_active_process: subprocess.Popen | None = None


def _handle_stop(_signum, _frame) -> None:
    _stop.set()
    with _active_lock:
        process = _active_process
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _publish(running: bool) -> None:
    try:
        publish_worker_capabilities(
            CAPABILITIES_PATH, version=__version__, actions=("video_phash",), running=running,
        )
    except Exception as exc:
        LOGGER.warning("Could not publish pHash worker status (%s)", type(exc).__name__)


def _resolver_ready() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _compute(job: dict) -> dict:
    script = r"""
import base64, json, os, sys
from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
)
from common.telegram_playback_activity import has_active_playback
from resolver_video_fingerprints import FingerprintBudgetExceeded, telegram_phash
payload = json.load(sys.stdin)

def phash_transfer_rate():
    # Read the shared lease before each bounded range request. If the ledger
    # cannot be read, has_active_playback() deliberately selects the slow rate.
    return (payload['playback_bytes_per_second'] if has_active_playback()
            else payload['idle_bytes_per_second'])

try:
    fingerprint, bytes_read, thumbnail = telegram_phash(
        payload['video_id'], payload['duration_us'] / 1000000,
        byte_budget=payload['byte_budget'], timeout_seconds=payload['timeout_seconds'],
        resolver_url='http://127.0.0.1:8090/admin',
        max_bytes_per_second=phash_transfer_rate,
        allow_rate_above_configured=True,
        cache_dir=payload['cache_dir'], cache_id=payload['cache_id'], include_thumbnail=True)
    if not thumbnail:
        raise ValueError('thumbnail_missing')
    print(json.dumps({'ok': True, 'fingerprint': fingerprint, 'bytes_read': bytes_read,
                      'thumbnail': base64.b64encode(thumbnail).decode('ascii')}))
except DailyTelegramTransferBudgetExceeded:
    print(json.dumps({'ok': False, 'error_code': 'daily_transfer_budget_exhausted', 'retryable': False}))
except DailyTelegramTransferBudgetUnavailable:
    print(json.dumps({'ok': False, 'error_code': 'transfer_budget_unavailable', 'retryable': False}))
except FingerprintBudgetExceeded:
    print(json.dumps({'ok': False, 'error_code': 'byte_budget_exhausted', 'retryable': False}))
except TimeoutError:
    print(json.dumps({'ok': False, 'error_code': 'timeout', 'retryable': True}))
except OSError:
    print(json.dumps({'ok': False, 'error_code': 'source_unavailable', 'retryable': True}))
except ValueError:
    print(json.dumps({'ok': False, 'error_code': 'invalid_video', 'retryable': False}))
except Exception:
    print(json.dumps({'ok': False, 'error_code': 'generation_failed', 'retryable': True}))
"""
    payload = {
        "video_id": job["video_id"], "duration_us": int(job["duration_us"]),
        "byte_budget": max(1, min(MAX_BYTE_BUDGET,
                                  int(os.getenv("PHASH_SERVER_BYTE_BUDGET", str(MAX_BYTE_BUDGET))))),
        "timeout_seconds": max(30, min(MAX_TIMEOUT_SECONDS,
                                       int(os.getenv("PHASH_SERVER_TIMEOUT_SECONDS", "600")))),
        "playback_bytes_per_second": max(64 * 1024, min(10_000_000,
            int(os.getenv("PHASH_SERVER_BYTES_PER_SECOND", str(2 * 1024 * 1024))))),
        "idle_bytes_per_second": max(64 * 1024, min(10_000_000,
            int(os.getenv("PHASH_SERVER_IDLE_BYTES_PER_SECOND", "10000000")))),
        "cache_dir": PHASH_RANGE_CACHE_DIR,
        "cache_id": job["id"],
    }
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    with _active_lock:
        global _active_process
        _active_process = process
    try:
        raw, _ = process.communicate(json.dumps(payload), timeout=payload["timeout_seconds"] + 30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return {"ok": False, "error_code": "timeout", "retryable": True}
    finally:
        with _active_lock:
            _active_process = None
    if process.returncode != 0:
        return {"ok": False, "error_code": "generation_failed", "retryable": True}
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        if result.get("ok"):
            result["thumbnail"] = base64.b64decode(result["thumbnail"], validate=True)
        return result
    except Exception:
        return {"ok": False, "error_code": "generation_failed", "retryable": True}


def _purge_expired_range_caches(now: float | None = None) -> int:
    """Remove only pHash cache directories whose persisted ledger expired."""
    now = time.time() if now is None else now
    try:
        entries = os.scandir(PHASH_RANGE_CACHE_DIR)
    except FileNotFoundError:
        return 0
    removed = 0
    with entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or len(entry.name) != 64:
                continue
            usage_path = os.path.join(entry.path, "usage.json")
            try:
                with open(usage_path, "r", encoding="utf-8") as handle:
                    updated_at = float(json.load(handle)["updated_at"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if now - updated_at >= PHASH_RANGE_CACHE_TTL_SECONDS:
                shutil.rmtree(entry.path)
                removed += 1
    return removed


def _process(job: dict, leader) -> None:
    heartbeat_stop = threading.Event()
    lease_lost = threading.Event()

    def maintain_lease() -> None:
        while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
            try:
                leader.execute("SELECT 1")
                if not phash_jobs.heartbeat(DB_URL, job["id"], job["lease_token"]):
                    lease_lost.set()
                    _handle_stop(signal.SIGTERM, None)
                    return
                _publish(True)
            except Exception:
                lease_lost.set()
                _handle_stop(signal.SIGTERM, None)
                return

    heartbeat_thread = threading.Thread(target=maintain_lease, daemon=True)
    heartbeat_thread.start()
    try:
        result = _compute(job)
        if lease_lost.is_set() or _stop.is_set():
            return
        if not result.get("ok"):
            phash_jobs.finish_failure(
                DB_URL, job, result.get("error_code", "generation_failed"),
                retryable=bool(result.get("retryable")),
            )
            return
        conn = db.connect(DB_URL)
        try:
            conn.row_factory = db.Record
            locked = conn.execute(
                """SELECT 1 FROM video_phash_jobs WHERE id=%s AND state='processing'
                   AND lease_token=%s AND lease_until >= CURRENT_TIMESTAMP FOR UPDATE""",
                (job["id"], job["lease_token"]),
            ).fetchone()
            if not locked:
                conn.rollback()
                return
            outcome = _store(
                SimpleNamespace(HUB_DB_URL=DB_URL), job["video_id"], result["fingerprint"],
                job["decoder_profile"], job["submitted_by_user_id"], result["thumbnail"],
                connection=conn,
            )
            if outcome == "conflict":
                conn.rollback()
                phash_jobs.finish_failure(DB_URL, job, "fingerprint_conflict", retryable=False)
                return
            if not phash_jobs.finish_success(conn, job, result["fingerprint"], int(result["bytes_read"])):
                conn.rollback()
                return
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        LOGGER.info("pHash job %s completed (thumbnail saved)", job["id"])
    except Exception as exc:
        LOGGER.error("pHash job %s failed (%s)", job["id"], type(exc).__name__)
        if not lease_lost.is_set() and not _stop.is_set():
            try:
                phash_jobs.finish_failure(DB_URL, job, "generation_failed", retryable=True)
            except Exception:
                LOGGER.exception("Could not record pHash job failure %s", job["id"])
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=3)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    leader = None
    last_publish = 0.0
    last_recovery = 0.0
    last_cache_purge = 0.0
    while not _stop.is_set():
        if time.monotonic() - last_publish >= 5:
            _publish(True)
            last_publish = time.monotonic()
        if not os.getenv("PHASH_UPLOAD_ENABLED", "false").lower() == "true":
            _stop.wait(POLL_SECONDS)
            continue
        if time.monotonic() - last_cache_purge >= 3600:
            try:
                removed = _purge_expired_range_caches()
                if removed:
                    LOGGER.info("Removed %d expired pHash range caches", removed)
            except Exception as exc:
                LOGGER.warning("Could not purge expired pHash range caches (%s)", type(exc).__name__)
            last_cache_purge = time.monotonic()
        if leader is None:
            try:
                leader = phash_jobs.worker_lock(DB_URL)
                if leader is not None:
                    recovered = phash_jobs.recover_expired(DB_URL)
                    if recovered:
                        LOGGER.warning("Re-queued %d pHash jobs after expired leases", recovered)
                    last_recovery = time.monotonic()
            except Exception as exc:
                LOGGER.warning("pHash queue is unavailable (%s)", type(exc).__name__)
                if leader is not None:
                    try:
                        leader.close()
                    except Exception:
                        pass
                leader = None
            if leader is None:
                _stop.wait(POLL_SECONDS)
                continue
        try:
            leader.execute("SELECT 1")
            if time.monotonic() - last_recovery >= POLL_SECONDS:
                recovered = phash_jobs.recover_expired(DB_URL)
                if recovered:
                    LOGGER.warning("Re-queued %d pHash jobs after expired leases", recovered)
                last_recovery = time.monotonic()
            if not _resolver_ready():
                _stop.wait(POLL_SECONDS)
                continue
            job = phash_jobs.claim_next(DB_URL)
            if job is None:
                _stop.wait(POLL_SECONDS)
                continue
            LOGGER.info("Claimed pHash job %s (attempt %s)", job["id"], job["attempts"])
            _process(job, leader)
        except Exception as exc:
            LOGGER.error("pHash worker cycle failed (%s)", type(exc).__name__)
            try:
                leader.close()
            except Exception:
                pass
            leader = None
            _stop.wait(POLL_SECONDS)
    if leader is not None:
        leader.close()
    _publish(False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
