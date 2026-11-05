import json
import os
import sys
import time
import signal
import subprocess
import threading
import tempfile
import logging
from common.logging_setup import configure_logging

logger = logging.getLogger(__name__)


LOGGER = configure_logging("sync.runner")

try:
    from common.config_loader import ConfigError
except ImportError:
    class ConfigError(Exception):
        pass

try:
    from common.job_queue import claim_job, finish_job, heartbeat_job, requeue_processing, pending_actions, WORKER_ACTIONS, ALL_ACTIONS
except Exception:  # pragma: no cover - the shared package ships in both images
    claim_job = finish_job = requeue_processing = None
    heartbeat_job = None
    pending_actions = None
    WORKER_ACTIONS = ()
    ALL_ACTIONS = ()

try:
    from common.worker_capabilities import publish_worker_capabilities
except Exception:  # pragma: no cover - older deployments do not publish capabilities
    publish_worker_capabilities = None

try:
    from common.admin_schedules import dispatch_due_schedules
except Exception:  # pragma: no cover - older deployments may not have migrations yet
    dispatch_due_schedules = None

try:
    from common.system_schedules import (
        claim_due_system_schedule, ensure_system_schedules, finish_system_schedule, list_system_schedules,
    )
except Exception:  # pragma: no cover - older deployments may not have migrations yet
    claim_due_system_schedule = ensure_system_schedules = finish_system_schedule = list_system_schedules = None

try:
    from db_schema import init_db
except ImportError:  # pragma: no cover - module is present in the sync image
    init_db = None

try:
    from common.app_version import __version__ as APP_VERSION
except ImportError:
    APP_VERSION = os.getenv("APP_VERSION", "0+unknown")

try:
    from common.config_loader import load_config
    cfg = load_config()
    interval = cfg.sync.interval_minutes * 60
    daily_at = getattr(cfg.sync, "daily_at", None)
    run_on_startup = getattr(cfg.sync, "run_on_startup", True)
    BASE_BACKOFF_SECONDS = cfg.sync.retry_backoff_base_seconds
    MAX_BACKOFF_SECONDS = cfg.sync.retry_backoff_max_seconds
    SHUTDOWN_GRACE_SECONDS = getattr(cfg.sync, "shutdown_grace_seconds", 180)
except ConfigError as ce:
    LOGGER.critical(f"[sync-daemon] FATAL CONFIG ERROR: {ce}")
    sys.exit(1)
except Exception as e:
    LOGGER.critical(f"[sync-daemon] FATAL: config load failed unexpectedly: {e}")
    sys.exit(1)

stop_event = threading.Event()
current_process = None
consecutive_errors = 0
requested_source = None
requested_sync_mode = "scheduled"
system_sync_due = False
SYNC_JOB_HEARTBEAT_INTERVAL_SECONDS = 15

def signal_handler(signum, frame):
    LOGGER.info("[sync-daemon] Received shutdown signal, stopping...")
    stop_event.set()
    global current_process
    if current_process and current_process.poll() is None:
        try:
            current_process.terminate()
        except Exception:
            logger.debug("Ignored exception in signal_handler", exc_info=True)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def calculate_next_sleep_seconds(daily_at_time, default_interval: int) -> float:
    if not daily_at_time:
        return float(default_interval)
    import datetime
    parts = daily_at_time.split(":")
    target_h, target_m = int(parts[0]), int(parts[1])
    now = datetime.datetime.now()
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return max(1.0, (target - now).total_seconds())

def format_duration(seconds: float) -> str:
    total_sec = int(round(seconds))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

if daily_at:
    LOGGER.info(f"[sync-daemon] Adult-Hub {APP_VERSION}; scheduled daily sync at {daily_at} (retry base {BASE_BACKOFF_SECONDS}s, max {MAX_BACKOFF_SECONDS}s)...")
else:
    LOGGER.info(f"[sync-daemon] Adult-Hub {APP_VERSION}; starting periodic sync runner (every {interval//60} min, retry base {BASE_BACKOFF_SECONDS}s, max {MAX_BACKOFF_SECONDS}s)...")

TRIGGER_FILE = os.getenv("SYNC_TRIGGER_FILE", "/data/cache/sync.trigger")
STATUS_FILE = os.getenv("SYNC_STATUS_FILE", "/data/cache/sync_status.json")
DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
WORKER_CAPABILITIES_FILE = os.getenv("ADMIN_WORKER_CAPABILITIES_PATH", "/data/cache/worker-capabilities.json")
SYNC_HISTORY_FILE = os.getenv("SYNC_HISTORY_FILE", "/data/cache/sync_history.json")


def _failed_sync_sources(started_at: int, ended_at: int, source_filter: str | None) -> list[dict]:
    """Read safe per-source failures for the sync child that just finished."""
    try:
        with open(SYNC_HISTORY_FILE, encoding="utf-8") as history_file:
            history = json.load(history_file)
        runs = history.get("runs", []) if isinstance(history, dict) else []
        matching = [run for run in runs if isinstance(run, dict)
                    and type(run.get("started_at")) is int
                    and started_at - 2 <= run["started_at"] <= ended_at
                    and run.get("source_filter") == source_filter]
        if not matching:
            return []
        run = max(matching, key=lambda item: item.get("started_at", 0))
        failures = []
        for source in run.get("sources", []):
            if not isinstance(source, dict) or source.get("success") is not False:
                continue
            label = source.get("label")
            error = source.get("error")
            if isinstance(label, str) and len(label) <= 256:
                item = {"label": label}
                if isinstance(error, str) and error in {
                    "Sync failed (RuntimeError).", "Sync failed (TimeoutError).",
                    "Sync failed (ValueError).", "Sync failed (OSError).",
                }:
                    item["error"] = error
                if (str(source.get("source", "")).startswith("telegram:")
                        and error == "Sync failed (RuntimeError)."):
                    detail = source.get("detail")
                    if isinstance(detail, str) and 0 < len(detail) <= 700:
                        item["detail"] = detail
                    else:
                        item["detail"] = "Telegram fetch/history operation failed."
                failures.append(item)
        return failures[:100]
    except (OSError, ValueError, TypeError):
        return []


def publish_worker_heartbeat(running: bool = True) -> None:
    if publish_worker_capabilities is None:
        return
    try:
        publish_worker_capabilities(
            WORKER_CAPABILITIES_FILE, version=APP_VERSION, actions=ALL_ACTIONS, running=running
        )
    except Exception as e:
        LOGGER.warning(f"[sync-daemon] Could not publish worker capabilities: {e}")


def worker_heartbeat_loop() -> None:
    while not stop_event.is_set():
        publish_worker_heartbeat()
        stop_event.wait(5.0)


def update_sync_status(running: bool, exit_code: int = None, success: bool = None, source: str = None):
    try:
        import json
        from pathlib import Path
        p = Path(STATUS_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "running": running,
            "updated_at": int(time.time()),
            "updated_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if running:
            data["started_at"] = int(time.time())
            data["active_source"] = source
        else:
            data["last_exit_code"] = exit_code
            data["last_success"] = success
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp_name, 0o666)
            except OSError:
                pass
            os.replace(tmp_name, p)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as e:
        LOGGER.warning(f"[sync-daemon] Could not update status file: {e}")


def run_jobs_worker() -> int:
    """Run sync.py --jobs-only to process queued jobs without full sync."""
    global current_process
    LOGGER.info("[sync-daemon] On-demand jobs trigger detected, executing jobs-only worker...")
    try:
        proc = subprocess.Popen([sys.executable, "sync.py", "--jobs-only"])
        current_process = proc
        while proc.poll() is None:
            if stop_event.is_set():
                _stop_child(proc)
                break
            time.sleep(0.2)
        code = proc.returncode if proc.returncode is not None else 0
        if code != 0:
            LOGGER.warning(f"[sync-daemon] Jobs worker exited with code {code}")
        return code
    except Exception as e:
        LOGGER.warning(f"[sync-daemon] Jobs worker execution error: {e}")
        return -1
    finally:
        current_process = None


def log_next_sync_schedule():
    try:
        schedule = next(item for item in list_system_schedules(DB_URL) if item["id"] == "library-sync")
        if schedule.get("enabled") and schedule.get("next_run_at"):
            from datetime import datetime
            from zoneinfo import ZoneInfo
            zone = schedule.get("timezone", "UTC")
            upcoming = datetime.fromisoformat(schedule["next_run_at"]).astimezone(ZoneInfo(zone))
            LOGGER.info(f"[sync-daemon] Next scheduled sync at {upcoming.isoformat()} ({zone}).")
        else:
            LOGGER.info("[sync-daemon] Library sync schedule is disabled or has no next run.")
    except Exception as exc:
        LOGGER.warning(f"[sync-daemon] Could not read next sync schedule: {exc}")


def wait_with_trigger(duration: float, *, allow_schedule: bool = True):
    global requested_source, requested_sync_mode, system_sync_due
    from pathlib import Path
    trig = Path(TRIGGER_FILE)
    step = 1.0
    deadline = time.monotonic() + duration
    next_worker_attempt = 0.0
    next_queue_error_log = 0.0
    next_schedule_dispatch = 0.0
    next_schedule_error_log = 0.0
    next_system_schedule_check = 0.0
    next_system_schedule_error_log = 0.0
    while not stop_event.is_set():
        actions = set()
        worker_wake = False
        if allow_schedule and claim_due_system_schedule and time.monotonic() >= next_system_schedule_check:
            try:
                if claim_due_system_schedule(DB_URL, "library-sync"):
                    system_sync_due = True
                    return False
            except Exception as e:
                if time.monotonic() >= next_system_schedule_error_log:
                    LOGGER.warning(f"[sync-daemon] Could not check the Library sync cron schedule: {e}")
                    next_system_schedule_error_log = time.monotonic() + 60.0
            next_system_schedule_check = time.monotonic() + 10.0
        if dispatch_due_schedules and time.monotonic() >= next_schedule_dispatch:
            try:
                worker_wake = dispatch_due_schedules(DB_URL) > 0
            except Exception as e:
                if time.monotonic() >= next_schedule_error_log:
                    LOGGER.warning(f"[sync-daemon] Could not dispatch Admin schedules: {e}")
                    next_schedule_error_log = time.monotonic() + 60.0
            next_schedule_dispatch = time.monotonic() + 30.0
        if pending_actions:
            try:
                actions = pending_actions(DB_URL)
            except Exception as e:
                if time.monotonic() >= next_queue_error_log:
                    LOGGER.warning(f"[sync-daemon] Could not inspect durable queue: {e}")
                    next_queue_error_log = time.monotonic() + 30.0
        if "sync" in actions:
            # A wake file may be missing or overwritten by another action.
            # Claim through the runner; a failed claim must not fall through
            # to an unrelated scheduled full sync.
            if run_pending_sync_jobs():
                continue
        if trig.exists():
            try:
                import json
                payload = json.loads(trig.read_text()) if trig.stat().st_size else {}
            except Exception:
                payload = {}

            if isinstance(payload, dict) and payload.get("action") == "jobs":
                trig.unlink(missing_ok=True)
                worker_wake = True
            elif isinstance(payload, dict) and payload.get("action") == "sync":
                # Durable signals carry no request themselves. The next poll
                # finds the persisted job, even if this signal was stale.
                trig.unlink(missing_ok=True)
            else:
                # Retain compatibility with manually written legacy triggers.
                requested_source = payload.get("source") if isinstance(payload, dict) else None
                requested_mode = payload.get("sync_mode", "scheduled") if isinstance(payload, dict) else "scheduled"
                trig.unlink(missing_ok=True)
                label = f" for {requested_source}" if requested_source else ""
                LOGGER.info(f"[sync-daemon] On-demand sync trigger detected{label}, executing sync immediately!")
                requested_sync_mode = requested_mode if requested_mode in {"scheduled", "new", "backfill"} else "scheduled"
                return True
        if (worker_wake or actions.intersection(WORKER_ACTIONS)) and time.monotonic() >= next_worker_attempt:
            run_jobs_worker()
            # Avoid respawning rapidly when an external full sync owns the
            # lock or the child fails. It alone owns recovery of processing.
            next_worker_attempt = time.monotonic() + 5.0
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        stop_event.wait(timeout=min(step, remaining))
    return False


def _stop_child(process, grace_seconds: float | None = None):
    """Ask the sync child to stop, then force and reap it after a grace period.

    The full-sync child turns SIGTERM into a stop request it honours at its next
    channel boundary, so the default wait is that drain: ending it sooner would
    kill it with a batch half-published, which is the failure this exists to
    avoid. A child that never reaches its checkpoint is forced once the window
    expires.
    """
    if grace_seconds is None:
        grace_seconds = SHUTDOWN_GRACE_SECONDS
    if process.poll() is not None:
        return process.wait()
    try:
        process.terminate()
    except ProcessLookupError:
        return process.wait()
    try:
        return process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def run_full_sync(source_filter=None, sync_mode="scheduled"):
    """Execute one full sync child and publish its status. Returns (success, exit_code)."""
    global current_process
    source_arg = ["--source", source_filter] if source_filter else []
    # Preserve the legacy child invocation for the default mode; only explicit
    # administrative modes need an additional CLI argument.
    mode_arg = ["--sync-mode", sync_mode] if sync_mode != "scheduled" else []
    LOGGER.info(f"[sync-daemon] Executing sync{f' for {source_filter}' if source_filter else ''}...")
    update_sync_status(running=True, source=source_filter)
    success = False
    exit_code = -1
    interrupted = False
    try:
        current_process = subprocess.Popen([sys.executable, "sync.py", *source_arg, *mode_arg])
        while current_process.poll() is None:
            if stop_event.is_set():
                _stop_child(current_process)
                interrupted = True
                break
            time.sleep(0.5)

        exit_code = current_process.returncode if current_process.returncode is not None else -1
        if exit_code == 0:
            success = True
        elif interrupted:
            if exit_code < 0:
                LOGGER.warning(
                    f"[sync-daemon] Sync child was killed by signal {-exit_code} instead of "
                    f"reaching its shutdown checkpoint within {SHUTDOWN_GRACE_SECONDS}s."
                )
            else:
                LOGGER.info(f"[sync-daemon] Sync child stopped during shutdown (exit {exit_code}).")
        elif consecutive_errors < 3:
            LOGGER.warning(f"[sync-daemon] Sync exited with code {exit_code}")
    except Exception as e:
        if consecutive_errors < 3:
            LOGGER.warning(f"[sync-daemon] Sync execution error: {e}")
    finally:
        if current_process is not None:
            try:
                if current_process.poll() is None:
                    _stop_child(current_process)
                current_process.wait()
            except Exception as e:
                LOGGER.warning(f"[sync-daemon] Could not reap sync process: {e}")
        current_process = None
        update_sync_status(running=False, exit_code=exit_code, success=success)
    return success, exit_code


def _clear_sync_trigger() -> None:
    """Drop a satisfied wake signal so it cannot start a redundant full sync."""
    from pathlib import Path
    try:
        trig = Path(TRIGGER_FILE)
        if not trig.exists():
            return
        payload = json.loads(trig.read_text()) if trig.stat().st_size else {}
        if isinstance(payload, dict) and payload.get("action") == "sync":
            trig.unlink(missing_ok=True)
    except Exception:
        logger.debug("Ignored exception in _clear_sync_trigger", exc_info=True)


def _latest_scheduled_sync_start() -> int | None:
    """Start time of the last successful whole-library scheduled pass, if recorded."""
    try:
        with open(SYNC_HISTORY_FILE, encoding="utf-8") as history_file:
            runs = json.load(history_file).get("runs", [])
    except (OSError, ValueError, AttributeError):
        return None
    candidates = [run["started_at"] for run in runs if isinstance(run, dict)
                  and run.get("sync_mode") == "scheduled"
                  and run.get("source_filter") in (None, "")
                  and run.get("success") is True
                  and type(run.get("started_at")) is int]
    return max(candidates) if candidates else None


def _scheduled_sync_was_missed(seconds_to_next_slot: float) -> bool:
    """True when the daily slot has already passed without a completed pass.

    Restarting after the scheduled time used to wait for the next slot, so one
    deploy cost the catalogue a whole day of syncing.
    """
    if not daily_at:
        return False
    last_success = _latest_scheduled_sync_start()
    if last_success is None:
        # Nothing completed to compare against (fresh or never-successful daemon):
        # honour run_on_startup and wait for the slot.
        return False
    return last_success < int(time.time()) + int(seconds_to_next_slot) - 86400


def run_pending_sync_jobs() -> bool:
    """Claim and run one durable on-demand sync job. Returns True if one ran.

    Requests survive a worker restart because they are persisted in the shared
    jobs table; the signal file only wakes the runner.
    """
    if not (claim_job and finish_job):
        return False
    try:
        job = claim_job(DB_URL, "sync")
    except Exception as e:
        LOGGER.warning(f"[sync-daemon] Could not claim sync job: {e}")
        return False
    if not job:
        return False
    try:
        payload = json.loads(job.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    source = payload.get("source") if isinstance(payload, dict) else None
    sync_mode = payload.get("sync_mode", "scheduled") if isinstance(payload, dict) else "scheduled"
    if sync_mode not in {"scheduled", "new", "backfill"}:
        sync_mode = "scheduled"
    label = f" for {source}" if source else ""
    LOGGER.info(f"[sync-daemon] Running durable sync job {job['id']}{label}...")
    heartbeat_stop = threading.Event()

    def heartbeat_loop():
        while not heartbeat_stop.wait(SYNC_JOB_HEARTBEAT_INTERVAL_SECONDS):
            try:
                if heartbeat_job:
                    heartbeat_job(DB_URL, job["id"], lease_token=job["lease_token"])
            except Exception as exc:
                LOGGER.warning(f"[sync-daemon] Could not update sync-job heartbeat ({type(exc).__name__}).")

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        name=f"sync-job-heartbeat-{str(job['id'])[:8]}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        sync_started_at = int(time.time())
        success, exit_code = run_full_sync(source, sync_mode)
        sync_ended_at = int(time.time())
        result = {
            "source": source,
            "sync_mode": sync_mode,
            "local_state": "sync completed" if success else "sync failed",
            "exit_code": exit_code,
            "message": f"On-demand sync{' for ' + source if source else ''} {'completed' if success else 'failed'}.",
        }
        if not success:
            source_failures = _failed_sync_sources(sync_started_at, sync_ended_at, source)
            if source_failures:
                result["source_failures"] = source_failures
        if stop_event.is_set():
            # A pass cut short by this shutdown is not the requester's failure:
            # leaving the job in `processing` hands it to requeue_processing() on
            # the next start, which is the designed recovery for a lost worker.
            LOGGER.info(f"[sync-daemon] Leaving sync job {job['id']} for the next start to requeue: "
                        "this pass was stopped by a shutdown.")
        else:
            try:
                finalized = finish_job(DB_URL, job["id"], "done" if success else "failed", result,
                                       lease_token=job["lease_token"])
                if not finalized:
                    LOGGER.warning(f"[sync-daemon] Sync job {job['id']} was no longer pending when its result arrived.")
            except Exception as e:
                LOGGER.warning(f"[sync-daemon] Could not record sync job result: {e}")
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
    _clear_sync_trigger()
    return True


def run_loop():
    global current_process, consecutive_errors, requested_source, requested_sync_mode, system_sync_due
    if not ensure_system_schedules or not claim_due_system_schedule or not finish_system_schedule:
        raise RuntimeError("System schedule support is unavailable; refusing to start sync runner.")
    try:
        ensure_system_schedules(DB_URL, sync_daily_at=daily_at, sync_interval_minutes=interval // 60,
                                backup_daily_at=os.getenv("BACKUP_DAILY_AT", "04:30"),
                                include_artwork=False)
    except Exception as e:
        LOGGER.error(f"[sync-daemon] Could not initialize system cron settings: {e}")
        raise RuntimeError("Could not initialize system cron settings.") from e
    if requeue_processing:
        try:
            recovered = requeue_processing(DB_URL, "sync")
            if recovered:
                LOGGER.info(f"[sync-daemon] Recovered {recovered} interrupted sync job(s).")
        except Exception as e:
            LOGGER.warning(f"[sync-daemon] Could not recover sync jobs: {e}")
    first_run = True
    while not stop_event.is_set():
        # Durable on-demand requests take precedence over a scheduled run and are
        # restart-safe because they live in the shared jobs table. Drain them
        # before any startup wait so a restart cannot strand a request.
        ran_jobs = False
        while run_pending_sync_jobs():
            ran_jobs = True
            if stop_event.is_set():
                break
        if stop_event.is_set():
            break

        # Dispatch schedules before a potentially long initial full sync. The
        # periodic wait loop is not reached until that sync exits; persisting
        # due work here lets the full-sync worker process it at safe checkpoints.
        if dispatch_due_schedules:
            try:
                scheduled = dispatch_due_schedules(DB_URL)
                if scheduled:
                    LOGGER.info(f"[sync-daemon] Queued {scheduled} due Admin schedule(s) before sync.")
            except Exception as e:
                LOGGER.warning(f"[sync-daemon] Could not dispatch Admin schedules at startup: {e}")

        if ran_jobs:
            first_run = False
        if first_run and not run_on_startup:
            first_run = False
            LOGGER.info("[sync-daemon] Skipping initial run on startup (run_on_startup=false); waiting for the configured cron schedule.")
            log_next_sync_schedule()
            triggered = wait_with_trigger(float("inf"))
            if not triggered and stop_event.is_set():
                break
            if triggered:
                # Re-check durable requests before falling back to the schedule.
                continue

        if ran_jobs and not system_sync_due:
            success, exit_code = True, 0
            requested_source = None
        else:
            if first_run and not system_sync_due:
                try:
                    if claim_due_system_schedule(DB_URL, "library-sync"):
                        system_sync_due = True
                except Exception as e:
                    LOGGER.warning(f"[sync-daemon] Could not claim a due Library sync slot: {e}")
            source = None if system_sync_due else requested_source
            mode = "scheduled" if system_sync_due else requested_sync_mode
            success, exit_code = run_full_sync(source, mode)
            requested_source = None
            requested_sync_mode = "scheduled"
        was_schedule_run = system_sync_due
        if system_sync_due and finish_system_schedule:
            try:
                finish_system_schedule(DB_URL, "library-sync", success=success and not stop_event.is_set(),
                                       interrupted=stop_event.is_set())
            except Exception as e:
                LOGGER.warning(f"[sync-daemon] Could not record Library sync result: {e}")
            system_sync_due = False

        first_run = False
        if stop_event.is_set():
            break

        if success:
            if consecutive_errors > 0:
                LOGGER.info(f"[sync-daemon] Sync succeeded after {consecutive_errors} failed attempts.")
            consecutive_errors = 0
            LOGGER.info("[sync-daemon] Waiting for the next cron run or an on-demand request...")
            log_next_sync_schedule()
            wait_with_trigger(float("inf"))
        else:
            consecutive_errors += 1
            backoff = min(BASE_BACKOFF_SECONDS * (2 ** (consecutive_errors - 1)), MAX_BACKOFF_SECONDS)
            if consecutive_errors <= 3:
                LOGGER.warning(f"[sync-daemon] Sync failed (attempt {consecutive_errors}). Retrying with backoff in {backoff}s...")
            else:
                # Suppress noisy repetitive logs on persistent errors
                LOGGER.warning(f"[sync-daemon] Consecutive sync failures ({consecutive_errors}). Suppressed repetitive logs. Retrying with backoff in {backoff}s...")
            triggered = wait_with_trigger(backoff, allow_schedule=False)
            if was_schedule_run and not triggered and not stop_event.is_set():
                wait_with_trigger(float("inf"))

    LOGGER.info("[sync-daemon] Sync runner exited.")


if __name__ == "__main__":
    if init_db is not None:
        try:
            init_db(DB_URL)
        except Exception:
            LOGGER.exception("[sync-daemon] Could not apply database migrations; refusing to start.")
            sys.exit(1)
    heartbeat = threading.Thread(target=worker_heartbeat_loop, name="worker-capabilities", daemon=True)
    heartbeat.start()
    try:
        run_loop()
    finally:
        stop_event.set()
        publish_worker_heartbeat(running=False)
        heartbeat.join(timeout=2.0)
