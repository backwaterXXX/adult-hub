"""Run the verified PostgreSQL backup once per local calendar day."""

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
from common.history_retention import prune_history


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("backup.scheduler")
STOP = threading.Event()


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


def _run_backup() -> int:
    lock_path = Path(os.getenv("BACKUP_LOCK_PATH", "/data/cache/backup.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.warning("Another backup is already running; scheduled backup skipped")
            return 1

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
        else:
            LOGGER.info("Scheduled PostgreSQL backup completed")
            try:
                counts = prune_history()
                LOGGER.info("Scheduled monthly history cleanup completed counts=%s", counts)
            except Exception as error:
                LOGGER.error(
                    "Scheduled monthly history cleanup failed error=%s detail=%s",
                    type(error).__name__,
                    error,
                )
                return 1
    return 0


def _stop(signum, _frame) -> None:
    LOGGER.info("Stopping backup scheduler signal=%s", signum)
    STOP.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one backup and monthly history cleanup, then exit",
    )
    args = parser.parse_args()
    if args.once:
        return _run_backup()

    hour, minute = _parse_daily_at(os.getenv("BACKUP_DAILY_AT", "04:30"))
    LOGGER.info("Daily PostgreSQL backup scheduled at %02d:%02d local time", hour, minute)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not STOP.is_set():
        now = dt.datetime.now().astimezone()
        next_run = _next_run(now, hour, minute)
        wait_seconds = max(1.0, (next_run - now).total_seconds())
        if STOP.wait(min(wait_seconds, 60.0)):
            break
        if dt.datetime.now().astimezone() >= next_run:
            _run_backup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
