"""Shared daily transfer cap and rate limiter for Telegram fingerprints."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path


DEFAULT_DAILY_BYTE_BUDGET = 10_000_000_000
DEFAULT_MAX_BYTES_PER_SECOND = 2 * 1024 * 1024
DEFAULT_LEDGER_PATH = "/data/cache/telegram-fingerprint-daily-usage.json"


class DailyTelegramTransferBudgetExceeded(RuntimeError):
    """The fingerprinting pipeline reached its shared daily Telegram budget."""


class DailyTelegramTransferBudgetUnavailable(RuntimeError):
    """The shared transfer ledger could not be read or safely updated."""


def _resolve_ledger_path() -> Path:
    configured = os.environ.get("TELEGRAM_FINGERPRINT_USAGE_PATH")
    if configured:
        return Path(configured)
    default = Path(DEFAULT_LEDGER_PATH)
    try:
        default.parent.mkdir(parents=True, exist_ok=True)
        return default
    except OSError:
        return Path(tempfile.gettempdir()) / "adult-hub" / "cache" / "telegram-fingerprint-daily-usage.json"


def reserve_telegram_fingerprint_bytes(
    byte_count: int,
    *,
    max_bytes_per_second: int | None = None,
    allow_rate_above_configured: bool = False,
) -> float:
    """Reserve fingerprint traffic under a shared cap and global rate limit.

    Reservations are charged before network I/O. If a process exits or an
    upstream request fails, the reservation remains charged for safety.
    Concurrent sync and resolver workers share the same locked ledger file.
    """
    try:
        byte_count = int(byte_count)
        daily_limit = int(os.environ.get(
            "TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET",
            str(DEFAULT_DAILY_BYTE_BUDGET),
        ))
        configured_rate = int(os.environ.get(
            "TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND",
            str(DEFAULT_MAX_BYTES_PER_SECOND),
        ))
        override_rate = int(max_bytes_per_second) if max_bytes_per_second is not None else configured_rate
    except (TypeError, ValueError) as exc:
        raise DailyTelegramTransferBudgetUnavailable("Invalid fingerprint transfer budget configuration") from exc
    if byte_count <= 0 or daily_limit <= 0 or configured_rate <= 0 or override_rate <= 0:
        raise DailyTelegramTransferBudgetUnavailable("Fingerprint transfer budgets must be positive")
    rate = (override_rate if allow_rate_above_configured
            else min(configured_rate, override_rate))
    ledger_path = _resolve_ledger_path()
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")

    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o660)
    except OSError as exc:
        raise DailyTelegramTransferBudgetUnavailable("Cannot open shared fingerprint transfer ledger") from exc

    wait_seconds = 0.0
    try:
        with os.fdopen(lock_fd, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            now = datetime.now().astimezone()
            now_seconds = now.timestamp()
            day_key = now.date().isoformat()
            next_midnight = datetime.combine(
                now.date() + timedelta(days=1), datetime_time.min, tzinfo=now.tzinfo,
            )
            day_end = next_midnight.timestamp()

            try:
                with ledger_path.open("r", encoding="utf-8") as handle:
                    state = json.load(handle)
                if not isinstance(state, dict):
                    raise ValueError("ledger root is not an object")
                previous_date = state.get("date")
                previous_bytes = state.get("bytes_reserved", 0)
                next_allowed_at = state.get("next_allowed_at", now_seconds)
                if (not isinstance(previous_date, str)
                        or type(previous_bytes) is not int or previous_bytes < 0
                        or type(next_allowed_at) not in (int, float)):
                    raise ValueError("ledger fields are invalid")
            except FileNotFoundError:
                previous_date, previous_bytes, next_allowed_at = day_key, 0, now_seconds
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise DailyTelegramTransferBudgetUnavailable("Shared fingerprint transfer ledger is invalid") from exc

            used_today = previous_bytes if previous_date == day_key else 0
            scheduled_at = max(now_seconds, float(next_allowed_at))
            transfer_seconds = byte_count / rate
            if scheduled_at + transfer_seconds > day_end:
                raise DailyTelegramTransferBudgetExceeded(
                    "Telegram fingerprint daily transfer window is exhausted"
                )
            if used_today + byte_count > daily_limit:
                raise DailyTelegramTransferBudgetExceeded(
                    f"Telegram fingerprint daily transfer budget exhausted ({used_today}/{daily_limit} bytes)"
                )

            state = {
                "date": day_key,
                "bytes_reserved": used_today + byte_count,
                "next_allowed_at": scheduled_at + transfer_seconds,
            }
            try:
                fd, temporary = tempfile.mkstemp(prefix="telegram-transfer-", dir=ledger_path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(state, handle, separators=(",", ":"))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, ledger_path)
                    directory_fd = os.open(ledger_path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except OSError as exc:
                raise DailyTelegramTransferBudgetUnavailable("Cannot update shared fingerprint transfer ledger") from exc
            wait_seconds = max(0.0, scheduled_at - now_seconds)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise DailyTelegramTransferBudgetUnavailable("Cannot lock shared fingerprint transfer ledger") from exc

    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return wait_seconds
