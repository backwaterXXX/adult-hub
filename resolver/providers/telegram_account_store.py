"""Persistent storage and discovery for multi-account Telegram configurations."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, List, Optional

from common import db
from .telegram_account import DEFAULT_ACCOUNT_ID, TelegramAccountContext, validate_account_id

logger = logging.getLogger(__name__)


@dataclass
class TelegramAccountRecord:
    account_id: str
    label: str = ""
    phone_mask: str = ""
    enabled: bool = True
    session_file: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self, include_session_file: bool = False) -> dict:
        d = {
            "account_id": self.account_id,
            "label": self.label,
            "phone_mask": self.phone_mask,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_session_file:
            d["session_file"] = self.session_file
        return d


@dataclass
class TelegramChannelAccessRecord:
    account_id: str
    channel_id: str
    access_status: str = "unknown"  # 'accessible' | 'no_access' | 'temporary_error' | 'flood_wait' | 'unknown'
    last_checked_at: Optional[datetime] = None
    error_detail: str = ""

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "channel_id": self.channel_id,
            "access_status": self.access_status,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "error_detail": self.error_detail,
        }


# In-memory fallback cache when PostgreSQL is unavailable or in offline test fixtures
_MEMORY_ACCOUNTS: dict[str, TelegramAccountRecord] = {}
_MEMORY_CHANNEL_ACCESS: dict[tuple[str, str], TelegramChannelAccessRecord] = {}
_MEMORY_ONLY_MODE: bool = False


def set_memory_only_mode(enabled: bool) -> None:
    """Explicitly enable or disable isolated memory-only mode for tests and benchmarks."""
    global _MEMORY_ONLY_MODE
    _MEMORY_ONLY_MODE = bool(enabled)


def is_memory_only_mode() -> bool:
    """Return True when operating in explicit memory-only mode."""
    return _MEMORY_ONLY_MODE or os.getenv("TELEGRAM_ACCOUNT_STORE_MEMORY_ONLY", "").lower() in ("1", "true", "yes")


def _normalize_datetime(value: Any) -> Optional[datetime]:
    """Normalize None, datetime, or int/float epoch seconds to UTC datetime or None."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    return None


def _is_db_available() -> bool:
    if is_memory_only_mode():
        return False
    try:
        with db.connection(connect_timeout=2) as conn:
            return db.table_exists(conn, "telegram_accounts")
    except Exception:
        return False


@contextmanager
def _write_connection(connect_timeout: int = 3) -> Iterator[Any]:
    """Context manager for internally opened write transactions.

    Enters the yielded Connection so __exit__ commits on success or
    rolls back on exception, while db.connection closes it in all cases.
    """
    with db.connection(connect_timeout=connect_timeout) as conn:
        with conn:
            yield conn


def list_accounts(conn=None) -> List[TelegramAccountRecord]:
    """List all configured Telegram accounts, auto-discovering legacy session if empty."""
    if is_memory_only_mode() and conn is None:
        accounts = sorted(_MEMORY_ACCOUNTS.values(), key=lambda a: a.account_id)
        if not accounts:
            ctx = TelegramAccountContext(DEFAULT_ACCOUNT_ID)
            session_path = ctx.get_session_path()
            if os.path.isfile(session_path) and os.path.getsize(session_path) > 0:
                logger.info("Auto-registering legacy default Telegram session from disk: %s", session_path)
                default_acc = upsert_account(
                    account_id=DEFAULT_ACCOUNT_ID,
                    label="Default Account",
                    phone_mask="",
                    enabled=True,
                    session_file=session_path,
                    conn=conn,
                )
                return [default_acc]
        return accounts

    def _execute(c):
        rows = c.execute(
            "SELECT account_id, label, phone_mask, enabled, session_file, created_at, updated_at "
            "FROM public.telegram_accounts ORDER BY account_id"
        ).fetchall()
        return [
            TelegramAccountRecord(
                account_id=r[0],
                label=r[1] or "",
                phone_mask=r[2] or "",
                enabled=bool(r[3]),
                session_file=r[4] or "",
                created_at=_normalize_datetime(r[5]),
                updated_at=_normalize_datetime(r[6]),
            )
            for r in rows
        ]

    accounts: List[TelegramAccountRecord] = []
    try:
        if conn is not None:
            accounts = _execute(conn)
        else:
            with db.connection(connect_timeout=3) as c:
                accounts = _execute(c)
    except Exception as exc:
        logger.debug("Database query for telegram_accounts failed (falling back to memory): %s", exc)
        accounts = sorted(_MEMORY_ACCOUNTS.values(), key=lambda a: a.account_id)

    if not accounts:
        # Check if legacy default session file exists on disk
        ctx = TelegramAccountContext(DEFAULT_ACCOUNT_ID)
        session_path = ctx.get_session_path()
        if os.path.isfile(session_path) and os.path.getsize(session_path) > 0:
            logger.info("Auto-registering legacy default Telegram session from disk: %s", session_path)
            default_acc = upsert_account(
                account_id=DEFAULT_ACCOUNT_ID,
                label="Default Account",
                phone_mask="",
                enabled=True,
                session_file=session_path,
                conn=conn,
            )
            return [default_acc]

    return accounts


def _get_memory_account(valid_id: str) -> Optional[TelegramAccountRecord]:
    if valid_id in _MEMORY_ACCOUNTS:
        return _MEMORY_ACCOUNTS[valid_id]
    if valid_id == DEFAULT_ACCOUNT_ID:
        ctx = TelegramAccountContext(DEFAULT_ACCOUNT_ID)
        path = ctx.get_session_path()
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return TelegramAccountRecord(
                account_id=DEFAULT_ACCOUNT_ID,
                label="Default Account",
                enabled=True,
                session_file=path,
            )
    return None


def get_account(account_id: str, conn=None) -> Optional[TelegramAccountRecord]:
    valid_id = validate_account_id(account_id)
    if is_memory_only_mode() and conn is None:
        return _get_memory_account(valid_id)

    def _execute(c):
        row = c.execute(
            "SELECT account_id, label, phone_mask, enabled, session_file, created_at, updated_at "
            "FROM public.telegram_accounts WHERE account_id = %s",
            (valid_id,),
        ).fetchone()
        if not row:
            return None
        return TelegramAccountRecord(
            account_id=row[0],
            label=row[1] or "",
            phone_mask=row[2] or "",
            enabled=bool(row[3]),
            session_file=row[4] or "",
            created_at=_normalize_datetime(row[5]),
            updated_at=_normalize_datetime(row[6]),
        )

    try:
        if conn is not None:
            return _execute(conn)
        with db.connection(connect_timeout=3) as c:
            return _execute(c)
    except Exception as exc:
        logger.debug("Database get_account for %s failed (falling back to memory): %s", valid_id, exc)
        return _get_memory_account(valid_id)


def create_account(
    account_id: str,
    *,
    label: str = "",
    enabled: bool = True,
    session_file: str = "",
    conn=None,
) -> Optional[TelegramAccountRecord]:
    """Atomically create a new Telegram account.

    Uses atomic INSERT ... ON CONFLICT DO NOTHING.
    Returns None if an account with this account_id already exists.
    """
    valid_id = validate_account_id(account_id)
    now = datetime.now(timezone.utc)
    if not session_file:
        session_file = TelegramAccountContext(valid_id).get_session_path()

    effective_label = label.strip() or valid_id

    # In-memory store when explicit memory-only mode is active
    if is_memory_only_mode() and conn is None:
        if valid_id in _MEMORY_ACCOUNTS:
            return None
        rec = TelegramAccountRecord(
            account_id=valid_id,
            label=effective_label,
            phone_mask="",
            enabled=enabled,
            session_file=session_file,
            created_at=now,
            updated_at=now,
        )
        _MEMORY_ACCOUNTS[valid_id] = rec
        return rec

    if conn is None and not _is_db_available():
        raise RuntimeError(f"Database is unavailable for create_account '{valid_id}'")

    def _execute(c):
        cursor = c.execute(
            "INSERT INTO public.telegram_accounts "
            "(account_id, label, phone_mask, enabled, session_file, created_at, updated_at) "
            "VALUES (%s, %s, '', %s, %s, to_timestamp(%s), to_timestamp(%s)) "
            "ON CONFLICT (account_id) DO NOTHING "
            "RETURNING account_id",
            (valid_id, effective_label, enabled, session_file, now.timestamp(), now.timestamp()),
        )
        row = cursor.fetchone()
        return bool(row)

    try:
        if conn is not None:
            inserted = _execute(conn)
        else:
            with _write_connection(connect_timeout=3) as c:
                inserted = _execute(c)
    except Exception as exc:
        logger.error("Database create_account failed for %s: %s", valid_id, exc)
        raise

    if not inserted:
        return None

    record = TelegramAccountRecord(
        account_id=valid_id,
        label=effective_label,
        phone_mask="",
        enabled=enabled,
        session_file=session_file,
        created_at=now,
        updated_at=now,
    )
    _MEMORY_ACCOUNTS[valid_id] = record
    return record


def _build_upsert_update_clause(
    label: Optional[str],
    phone_mask: Optional[str],
    enabled: Optional[bool],
    session_file: Optional[str],
) -> str:
    updates = ["updated_at = EXCLUDED.updated_at"]
    if label is not None and label != "":
        updates.append("label = EXCLUDED.label")
    if phone_mask is not None and phone_mask != "":
        updates.append("phone_mask = EXCLUDED.phone_mask")
    if enabled is not None:
        updates.append("enabled = EXCLUDED.enabled")
    if session_file is not None and session_file != "":
        updates.append("session_file = EXCLUDED.session_file")
    return ", ".join(updates)


def _resolve_effective_account_fields(
    valid_id: str,
    label: Optional[str],
    phone_mask: Optional[str],
    enabled: Optional[bool],
    conn=None,
) -> tuple[str, str, bool, Optional[datetime]]:
    existing = _MEMORY_ACCOUNTS.get(valid_id) or get_account(valid_id, conn=conn)
    eff_label = label if (label is not None and label != "") else (existing.label if existing else valid_id)
    eff_phone = phone_mask if phone_mask is not None else (existing.phone_mask if existing else "")
    eff_enabled = enabled if enabled is not None else (existing.enabled if existing else True)
    created_at = existing.created_at if existing else None
    return eff_label, eff_phone, eff_enabled, created_at


def upsert_account(
    account_id: str,
    *,
    label: Optional[str] = None,
    phone_mask: Optional[str] = None,
    enabled: Optional[bool] = None,
    session_file: Optional[str] = None,
    conn=None,
) -> TelegramAccountRecord:
    valid_id = validate_account_id(account_id)
    now = datetime.now(timezone.utc)
    if not session_file:
        session_file = TelegramAccountContext(valid_id).get_session_path()

    eff_label, eff_phone, eff_enabled, orig_created_at = _resolve_effective_account_fields(
        valid_id, label, phone_mask, enabled, conn=conn
    )

    if is_memory_only_mode() and conn is None:
        record = TelegramAccountRecord(
            account_id=valid_id,
            label=eff_label,
            phone_mask=eff_phone,
            enabled=eff_enabled,
            session_file=session_file,
            created_at=orig_created_at or now,
            updated_at=now,
        )
        _MEMORY_ACCOUNTS[valid_id] = record
        return record

    if conn is None and not _is_db_available():
        raise RuntimeError(f"Database is unavailable for upsert_account '{valid_id}'")

    clause = _build_upsert_update_clause(label, phone_mask, enabled, session_file)
    sql = (
        "INSERT INTO public.telegram_accounts "
        "(account_id, label, phone_mask, enabled, session_file, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s)) "
        "ON CONFLICT (account_id) DO UPDATE SET " + clause
    )

    def _execute(c):
        c.execute(sql, (valid_id, eff_label, eff_phone, eff_enabled, session_file, now.timestamp(), now.timestamp()))

    try:
        if conn is not None:
            _execute(conn)
        else:
            with _write_connection(connect_timeout=3) as c:
                _execute(c)
    except Exception as exc:
        logger.error("Database upsert_account failed for %s: %s", valid_id, exc)
        raise

    record = TelegramAccountRecord(
        account_id=valid_id,
        label=eff_label,
        phone_mask=eff_phone,
        enabled=eff_enabled,
        session_file=session_file,
        created_at=orig_created_at or now,
        updated_at=now,
    )
    _MEMORY_ACCOUNTS[valid_id] = record
    return record


def _apply_mem_updates(
    mem: TelegramAccountRecord,
    label: Optional[str],
    enabled: Optional[bool],
    phone_mask: Optional[str],
    now: datetime,
) -> None:
    if label is not None:
        mem.label = label
    if enabled is not None:
        mem.enabled = enabled
    if phone_mask is not None:
        mem.phone_mask = phone_mask
    mem.updated_at = now


def _execute_update_account(
    valid_id: str,
    label: Optional[str],
    enabled: Optional[bool],
    phone_mask: Optional[str],
    now: datetime,
    conn=None,
) -> bool:
    """Execute UPDATE query for an account.

    Exceptions raised during execution or during _write_connection exit/commit
    propagate directly to the caller (update_account), which catches, logs, and
    re-raises them so no in-memory cache or caller state is modified on DB failure.
    """
    updates = ["updated_at = to_timestamp(%s)"]
    params: list[Any] = [now.timestamp()]
    for col, val in [("label", label), ("enabled", enabled), ("phone_mask", phone_mask)]:
        if val is not None:
            updates.append(f"{col} = %s")
            params.append(val)
    params.append(valid_id)
    sql = f"UPDATE public.telegram_accounts SET {', '.join(updates)} WHERE account_id = %s RETURNING account_id"

    def _execute(c):
        row = c.execute(sql, tuple(params)).fetchone()
        return bool(row)

    if conn is not None:
        return _execute(conn)
    with _write_connection(connect_timeout=3) as c:
        return _execute(c)


def update_account(
    account_id: str,
    *,
    label: Optional[str] = None,
    enabled: Optional[bool] = None,
    phone_mask: Optional[str] = None,
    conn=None,
) -> Optional[TelegramAccountRecord]:
    valid_id = validate_account_id(account_id)
    now = datetime.now(timezone.utc)

    if is_memory_only_mode() and conn is None:
        mem = _MEMORY_ACCOUNTS.get(valid_id)
        if not mem:
            return None
        _apply_mem_updates(mem, label, enabled, phone_mask, now)
        return mem

    if conn is None and not _is_db_available():
        raise RuntimeError(f"Database is unavailable for update_account '{valid_id}'")

    try:
        updated_in_db = _execute_update_account(valid_id, label, enabled, phone_mask, now, conn=conn)
    except Exception as exc:
        logger.error("Database update_account failed for %s: %s", valid_id, exc)
        raise

    if not updated_in_db:
        return None

    mem = _MEMORY_ACCOUNTS.get(valid_id)
    if mem:
        _apply_mem_updates(mem, label, enabled, phone_mask, now)

    return get_account(valid_id, conn=conn) or mem


def _remove_account_session_files(valid_id: str, acc: Optional[TelegramAccountRecord]) -> None:
    session_path = (
        acc.session_file
        if acc and acc.session_file
        else TelegramAccountContext(valid_id).get_session_path()
    )
    pending_path = TelegramAccountContext(valid_id).get_pending_session_path()

    safe_paths = [
        session_path,
        pending_path,
        f"{session_path}-journal",
        f"{session_path}-wal",
        f"{session_path}-shm",
    ]
    for p in safe_paths:
        norm = os.path.abspath(p)
        base = os.path.basename(norm)
        if not (base.endswith(".session") or ".session-" in base):
            continue
        if os.path.isfile(norm):
            try:
                os.remove(norm)
                logger.info("Removed session file for account %s: %s", valid_id, norm)
            except OSError as err:
                logger.warning("Could not delete session file %s: %s", norm, err)


def _check_account_disconnected(valid_id: str) -> None:
    try:
        from .telegram_router import get_telegram_router
        router = get_telegram_router()
        client = router._clients.get(valid_id)
        if client is not None and hasattr(client, "is_connected") and client.is_connected():
            raise RuntimeError(f"Cannot delete account '{valid_id}': client is still connected in router pool")
    except ImportError:
        pass


def _delete_account_from_memory(valid_id: str) -> Optional[TelegramAccountRecord]:
    acc = _MEMORY_ACCOUNTS.pop(valid_id, None)
    for key in list(_MEMORY_CHANNEL_ACCESS.keys()):
        if key[0] == valid_id:
            _MEMORY_CHANNEL_ACCESS.pop(key, None)
    return acc


def _execute_delete_account(valid_id: str, conn=None) -> bool:
    """Execute DELETE query for an account.

    Exceptions raised during execution or during _write_connection exit/commit
    propagate directly to the caller (delete_account), which catches, logs, and
    re-raises them so neither memory cache nor on-disk session files are removed on DB failure.
    """
    def _execute(c):
        cursor = c.execute(
            "DELETE FROM public.telegram_accounts WHERE account_id = %s RETURNING account_id",
            (valid_id,),
        )
        return bool(cursor.fetchone())

    if conn is not None:
        return _execute(conn)
    with _write_connection(connect_timeout=3) as c:
        return _execute(c)


def delete_account(account_id: str, conn=None) -> bool:
    """Delete account record and its associated SQLite session file.

    Guarantees that no channels, videos, or Jellyfin catalog files are touched.
    Propagates database errors and ensures session files are only removed after
    successful row deletion and after disconnected validation.
    """
    valid_id = validate_account_id(account_id)
    _check_account_disconnected(valid_id)

    if is_memory_only_mode() and conn is None:
        if valid_id not in _MEMORY_ACCOUNTS:
            return False
        acc = _delete_account_from_memory(valid_id)
        _remove_account_session_files(valid_id, acc)
        return True

    if conn is None and not _is_db_available():
        raise RuntimeError(f"Database is unavailable for delete_account '{valid_id}'")

    acc = get_account(valid_id, conn=conn)
    if not acc and valid_id not in _MEMORY_ACCOUNTS:
        return False

    try:
        deleted_from_db = _execute_delete_account(valid_id, conn=conn)
    except Exception as exc:
        logger.error("Database delete_account failed for %s: %s", valid_id, exc)
        raise

    if not deleted_from_db and valid_id not in _MEMORY_ACCOUNTS:
        return False

    _delete_account_from_memory(valid_id)
    _remove_account_session_files(valid_id, acc)
    return True


def _list_memory_channel_access(
    account_id: Optional[str],
    channel_id: Optional[str],
) -> List[TelegramChannelAccessRecord]:
    results = []
    for (a_id, c_id), rec in _MEMORY_CHANNEL_ACCESS.items():
        if (account_id is None or a_id == account_id) and (channel_id is None or c_id == channel_id):
            results.append(rec)
    results.sort(key=lambda r: (r.channel_id, r.account_id))
    return results


def list_channel_access(
    account_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    conn=None,
) -> List[TelegramChannelAccessRecord]:
    if is_memory_only_mode() and conn is None:
        return _list_memory_channel_access(account_id, channel_id)

    def _execute(c):
        conditions = []
        params = []
        if account_id is not None:
            conditions.append("account_id = %s")
            params.append(validate_account_id(account_id))
        if channel_id is not None:
            conditions.append("channel_id = %s")
            params.append(str(channel_id).strip())

        sql = "SELECT account_id, channel_id, access_status, last_checked_at, error_detail FROM public.telegram_account_channels"
        if conditions:
            sql += f" WHERE {' AND '.join(conditions)}"
        sql += " ORDER BY channel_id, account_id"

        rows = c.execute(sql, tuple(params)).fetchall()
        return [
            TelegramChannelAccessRecord(
                account_id=r[0],
                channel_id=r[1],
                access_status=r[2] or "unknown",
                last_checked_at=_normalize_datetime(r[3]),
                error_detail=r[4] or "",
            )
            for r in rows
        ]

    try:
        if conn is not None:
            return _execute(conn)
        with db.connection(connect_timeout=3) as c:
            return _execute(c)
    except Exception as exc:
        logger.debug("Database list_channel_access failed: %s", exc)
        return _list_memory_channel_access(account_id, channel_id)


def get_channel_access_status(account_id: str, channel_id: str) -> str:
    """Return the access status ('accessible', 'inaccessible', 'unknown') for an account and channel."""
    valid_id = validate_account_id(account_id)
    clean_channel = str(channel_id).strip()
    rec = _MEMORY_CHANNEL_ACCESS.get((valid_id, clean_channel))
    if rec:
        return rec.access_status
    access_list = list_channel_access(account_id=valid_id, channel_id=clean_channel)
    if access_list:
        return access_list[0].access_status
    return "unknown"


VALID_ACCESS_STATUSES = {
    "accessible",
    "inaccessible",
    "no_access",
    "temporary_error",
    "flood_wait",
    "unknown",
}


def set_channel_access(
    account_id: str,
    channel_id: str,
    access_status: str,
    error_detail: str = "",
    conn=None,
) -> None:
    valid_id = validate_account_id(account_id)
    clean_channel = str(channel_id).strip()
    status = access_status.lower()
    if status not in VALID_ACCESS_STATUSES:
        status = "unknown"
    now = datetime.now(timezone.utc)

    rec = TelegramChannelAccessRecord(
        account_id=valid_id,
        channel_id=clean_channel,
        access_status=status,
        last_checked_at=now,
        error_detail=error_detail[:500] if error_detail else "",
    )
    _MEMORY_CHANNEL_ACCESS[(valid_id, clean_channel)] = rec

    if is_memory_only_mode() and conn is None:
        return

    def _execute(c):
        c.execute(
            "INSERT INTO public.telegram_account_channels "
            "(account_id, channel_id, access_status, last_checked_at, error_detail) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (account_id, channel_id) DO UPDATE SET "
            "access_status = EXCLUDED.access_status, "
            "last_checked_at = EXCLUDED.last_checked_at, "
            "error_detail = EXCLUDED.error_detail",
            (valid_id, clean_channel, status, now, rec.error_detail),
        )

    try:
        if conn is not None:
            _execute(conn)
        else:
            with _write_connection(connect_timeout=3) as c:
                _execute(c)
    except Exception as exc:
        logger.debug("Database set_channel_access failed for (%s, %s): %s", valid_id, clean_channel, exc)


def _get_memory_accessible_accounts(clean_channel: str) -> List[str]:
    accessible = []
    for (a_id, c_id), rec in _MEMORY_CHANNEL_ACCESS.items():
        if c_id == clean_channel and rec.access_status == "accessible":
            acc = _MEMORY_ACCOUNTS.get(a_id)
            if acc and acc.enabled:
                accessible.append(a_id)
    accessible.sort()
    return accessible


def get_accessible_accounts_for_channel(channel_id: str, conn=None) -> List[str]:
    """Return account IDs with verified 'accessible' status for channel_id."""
    clean_channel = str(channel_id).strip()
    if is_memory_only_mode() and conn is None:
        return _get_memory_accessible_accounts(clean_channel)

    def _execute(c):
        rows = c.execute(
            "SELECT a.account_id FROM public.telegram_accounts a "
            "JOIN public.telegram_account_channels ac ON a.account_id = ac.account_id "
            "WHERE ac.channel_id = %s AND ac.access_status = 'accessible' AND a.enabled = true "
            "ORDER BY a.account_id",
            (clean_channel,),
        ).fetchall()
        return [r[0] for r in rows]

    try:
        if conn is not None:
            return _execute(conn)
        with db.connection(connect_timeout=3) as c:
            return _execute(c)
    except Exception as exc:
        logger.debug("Database query for accessible accounts failed: %s", exc)
        return _get_memory_accessible_accounts(clean_channel)
