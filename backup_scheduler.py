"""Run the verified PostgreSQL backup from its saved Europe/Warsaw cron setting."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import backup_db
from common import schedule_stages
from common.history_retention import prune_history
from common.system_schedules import (
    claim_due_system_schedule, ensure_system_schedules, finish_system_schedule,
)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("backup.scheduler")
STOP = threading.Event()
BACKUP_OVERLAP = 75
BACKUP_RETRY_SECONDS = 300
ARTWORK_RETRY_SECONDS = 300


def _parse_daily_at(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError):
        raise ValueError("BACKUP_DAILY_AT must use HH:MM in the configured local timezone") from None


def _next_run(now: dt.datetime, hour: int, minute: int) -> dt.datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def _run_backup(db_url: str | None = None) -> int:
    lock_path = Path(os.getenv("BACKUP_LOCK_PATH", "/data/cache/backup.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.warning("Another backup is already running; scheduled backup skipped")
            return BACKUP_OVERLAP

        keep_days = str(max(1, int(os.getenv("BACKUP_KEEP_DAYS", "7"))))
        dest_dir = os.getenv("BACKUP_DIR", "/data/backups")
        saved_argv = sys.argv
        try:
            sys.argv = ["backup_db.py", "--dest-dir", dest_dir, "--keep", keep_days]
            try:
                exit_code = backup_db.main()
            except Exception as error:
                LOGGER.error("Scheduled PostgreSQL backup crashed error=%s", type(error).__name__)
                exit_code = 1
        finally:
            sys.argv = saved_argv
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        if exit_code:
            LOGGER.error("Scheduled PostgreSQL backup failed exit_code=%d", exit_code)
            return exit_code
        LOGGER.info("Scheduled PostgreSQL backup completed")
        schedule_stages.mark_interrupted_stages(db_url, "database-backup")
        if not schedule_stages.stage_enabled(db_url, "database-backup", "history-cleanup"):
            LOGGER.info("Backup history cleanup is disabled; keeping history as it is")
            return 0
        schedule_stages.record_stage_start(db_url, "database-backup", "history-cleanup")
        try:
            counts = prune_history()
            LOGGER.info("Scheduled monthly history cleanup completed counts=%s", counts)
        except Exception as error:
            LOGGER.error(
                "Scheduled monthly history cleanup failed error=%s detail=%s",
                type(error).__name__,
                error,
            )
            schedule_stages.record_stage_finish(db_url, "database-backup", "history-cleanup",
                                                success=False, error=str(error))
            return 1
        schedule_stages.record_stage_finish(db_url, "database-backup", "history-cleanup", success=True)
    return 0


def _run_artwork_generation_schedule(db_url: str | None) -> int:
    """Queue up to 100 due artwork jobs without waiting for the backup run."""
    try:
        due = claim_due_system_schedule(db_url, "artwork-generation")
    except Exception:
        LOGGER.exception("Could not check the daily artwork generation schedule")
        return 1
    if not due:
        return 0

    success = False
    try:
        try:
            # The scheduler image copies sync/ modules into /app, where sync.py
            # is a facade that loads config.yaml (not mounted in this service).
            # Import the standalone schedule module first to avoid that facade.
            from artwork_schedule import queue_artwork_generation_batch
        except ImportError:
            from sync.artwork_schedule import queue_artwork_generation_batch
        result = queue_artwork_generation_batch(db_url, Path(os.getenv("MEDIA_DIR", "/media/adult")))
        queued = len(result.get("job_ids") or [])
        LOGGER.info(
            "Daily artwork generation queued %s job(s) from %s candidate(s).",
            queued, result.get("candidates", 0),
        )
        success = True
    except Exception:
        LOGGER.exception("Daily artwork generation batch failed")
    try:
        finish_system_schedule(db_url, "artwork-generation", success=success)
    except Exception:
        LOGGER.exception("Could not record the artwork generation schedule result")
        return 1
    return 0 if success else 1


def _artwork_scheduler_loop(db_url: str | None) -> None:
    """Poll artwork independently so a long database backup cannot delay 06:00."""
    while not STOP.is_set():
        result = _run_artwork_generation_schedule(db_url)
        STOP.wait(ARTWORK_RETRY_SECONDS if result else 30.0)


def _start_artwork_scheduler(db_url: str | None):
    thread = threading.Thread(
        target=_artwork_scheduler_loop, args=(db_url,),
        name="daily-artwork-scheduler", daemon=True,
    )
    thread.start()
    return thread


def _stop(signum, _frame) -> None:
    LOGGER.info("Stopping backup scheduler signal=%s", signum)
    STOP.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="check the saved schedule once and run if due, then exit",
    )
    parser.add_argument("--force", action="store_true", help="run a backup immediately")
    args = parser.parse_args()
    db_url = (os.getenv("DATABASE_URL") or "").strip() or None
    if args.force:
        code = _run_backup(db_url)
        return 0 if code == BACKUP_OVERLAP else code
    if not db_url:
        LOGGER.error("DATABASE_URL is required to read the saved backup cron schedule")
        return 1
    try:
        ensure_system_schedules(db_url, backup_daily_at=os.getenv("BACKUP_DAILY_AT", "04:30"),
                                include_sync=False, include_artwork=True)
    except Exception:
        LOGGER.exception("Could not initialize system schedules")
        return 1
    if args.once:
        try:
            due = claim_due_system_schedule(db_url, "database-backup")
        except Exception:
            LOGGER.exception("Could not check the database backup schedule")
            return 1
        if not due:
            LOGGER.info("Saved database backup schedule is not due; skipping")
            return 0
        code = 1
        try:
            code = _run_backup(db_url)
        except Exception:
            LOGGER.exception("The scheduled database backup crashed")
        try:
            finish_system_schedule(db_url, "database-backup", success=code == 0,
                                   skipped_overlap=code == BACKUP_OVERLAP)
        except Exception:
            LOGGER.exception("Could not record the database backup result")
        return 0 if code == BACKUP_OVERLAP else code

    LOGGER.info("Database backup and artwork schedulers are polling the saved cron schedules")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    artwork_thread = _start_artwork_scheduler(db_url)

    try:
        while not STOP.is_set():
            code = 0
            try:
                due = claim_due_system_schedule(db_url, "database-backup")
            except Exception:
                LOGGER.exception("Could not check the database backup schedule")
                due = False
                code = 1
            if due:
                code = 1
                try:
                    code = _run_backup(db_url)
                except Exception:
                    LOGGER.exception("The scheduled database backup crashed")
                try:
                    finish_system_schedule(db_url, "database-backup", success=code == 0,
                                           skipped_overlap=code == BACKUP_OVERLAP)
                except Exception:
                    LOGGER.exception("Could not record the database backup result")
            STOP.wait(BACKUP_RETRY_SECONDS if code else 30.0)
    finally:
        STOP.set()
        artwork_thread.join(timeout=5.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
