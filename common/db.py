"""Shared PostgreSQL access layer for Adult Hub (psycopg 3)."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import set_json_loads

__all__ = [
    "Record", "Connection", "Cursor", "connect", "connection", "database_url",
    "table_exists", "table_names", "table_columns", "pg_backup", "pg_restore",
    "prune_old_backups",
    "restore_test_target_available",
    "Error", "DatabaseError", "OperationalError", "IntegrityError",
    "ProgrammingError", "UndefinedTable",
]

Error = psycopg.Error
DatabaseError = psycopg.DatabaseError
OperationalError = psycopg.OperationalError
IntegrityError = psycopg.IntegrityError
ProgrammingError = psycopg.ProgrammingError
UndefinedTable = pg_errors.UndefinedTable


def _postgres_database_identity(value: str | None):
    """Return the effective simple host/database identity for a PostgreSQL URI."""
    from urllib.parse import parse_qs, unquote, urlsplit

    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"postgres", "postgresql"}:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    # Service/hostaddr options can redirect libpq independently of the URI host;
    # fail closed rather than accidentally treating the primary as a safe target.
    if query.get("service") or query.get("hostaddr"):
        return None
    hosts = query.get("host", [])
    ports = query.get("port", [])
    users = query.get("user", [])
    passwords = query.get("password", [])
    if (len(hosts) > 1 or len(ports) > 1 or len(users) > 1 or len(passwords) > 1
            or (hosts and parsed.hostname) or (ports and parsed.port is not None)
            or (users and parsed.username) or (passwords and parsed.password)
            or any(len(query.get(key, [])) > 1 for key in
                   ("sslmode", "sslrootcert", "sslcert", "sslkey", "target_session_attrs"))):
        return None
    hostname = hosts[0] if hosts else unquote(parsed.hostname) if parsed.hostname else None
    if not hostname or "," in hostname:
        return None

    if hostname.startswith("/"):
        endpoint = ("socket", hostname)
    else:
        port = int(ports[0]) if ports else (parsed.port or 5432)
        if not 1 <= port <= 65535:
            return None
        endpoint = ("tcp", hostname.casefold(), port)

    dbnames = query.get("dbname", [])
    databases = query.get("database", [])
    if len(dbnames) > 1 or len(databases) > 1 or (dbnames and databases):
        return None
    database_values = dbnames or databases
    database = database_values[0] if database_values else unquote(parsed.path.lstrip("/"))
    if not database:
        return None
    return (*endpoint, database)


def restore_test_target_available(restore_admin_db_url: str | None,
                                  primary_db_url: str | None) -> bool:
    """Check a restore-test URI is syntactically usable and distinct from primary.

    This is a configuration check only; database reachability and CREATEDB
    permissions are verified when a restore test job runs. Never return URI
    details to callers.
    """

    try:
        target = _postgres_database_identity(restore_admin_db_url)
        primary = _postgres_database_identity(primary_db_url)
        return target is not None and primary is not None and target != primary
    except (AttributeError, TypeError, ValueError):
        return False


_EPOCH_TIME_COLUMNS = frozenset({
    "performers_checked_at", "performers_audited_at",
    "scene_matched_at", "first_seen", "last_seen", "detected_at",
    "created_at", "updated_at", "backfill_completed_at", "pbc_updated_at",
    "expire_at", "last_accessed", "ended_at", "last_run_at",
})
_JSONB_COLUMNS = frozenset({"performers", "categories", "tags", "aliases"})
_FLOAT_TIME_COLUMNS = frozenset({"expire_at", "last_accessed", "ended_at"})
_NANO_TIME_COLUMNS = frozenset({"performers_audited_at"})


def _split_sql_list(value: str) -> list[str]:
    """Split a SQL expression list without splitting nested expressions."""
    parts: list[str] = []
    start = depth = 0
    quote: str | None = None
    i = 0
    while i < len(value):
        char = value[i]
        if quote:
            if char == quote:
                if i + 1 < len(value) and value[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:i].strip())
            start = i + 1
        i += 1
    parts.append(value[start:].strip())
    return parts


def _adapt_no_column_insert(sql: str, connection=None) -> str:
    no_columns = re.search(
        r'(?is)(INSERT\s+INTO\s+([\w"]+)(?:\.[\w"]+)?\s+)(VALUES\s*\([^()]*\))', sql
    )
    if no_columns and connection is not None:
        table = no_columns.group(2).split(".")[-1].strip('"')
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        if columns:
            column_list = ", ".join('"' + row[0].replace('"', '""') + '"' for row in columns)
            replacement = no_columns.group(1) + "(" + column_list + ") " + no_columns.group(3)
            sql = sql[:no_columns.start()] + replacement + sql[no_columns.end():]
    return sql


def _adapt_insert_value(column: str, value: str) -> str | None:
    """Return the adapted expression for one INSERT value, or None when it stays unchanged."""
    if column in _EPOCH_TIME_COLUMNS:
        epoch = "%s / 1000000000.0" if column in _NANO_TIME_COLUMNS else "%s"
        if value == "%s":
            return f"to_timestamp({epoch})"
        if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            return (f"to_timestamp({value} / 1000000000.0)"
                    if column in _NANO_TIME_COLUMNS else f"to_timestamp({value})")
        return None
    if column in _JSONB_COLUMNS and value == "%s":
        return "%s::jsonb"
    return None


def _adapt_insert_values(sql: str) -> str:
    insert = re.search(
        r"(?is)(INSERT\s+INTO\s+(?:[\w\"]+\.)?[\w\"]+\s*)"
        r"\(([^()]*)\)\s*VALUES\s*\(([^()]*)\)", sql,
    )
    if insert:
        columns = _split_sql_list(insert.group(2))
        values = _split_sql_list(insert.group(3))
        if len(columns) == len(values):
            changed = False
            for index, (column, value) in enumerate(zip(columns, values)):
                column = column.rsplit(".", 1)[-1].strip().strip('"').lower()
                adapted = _adapt_insert_value(column, value)
                if adapted is not None:
                    values[index] = adapted
                    changed = True
            if changed:
                replacement = (insert.group(1) + "(" + insert.group(2) + ") VALUES ("
                               + ", ".join(values) + ")")
                sql = sql[:insert.start()] + replacement + sql[insert.end():]
    return sql


def _adapt_epoch_comparisons(sql: str) -> str:
    names = "|".join(sorted(map(re.escape, _EPOCH_TIME_COLUMNS), key=len, reverse=True))
    nanos = "|".join(sorted(map(re.escape, _NANO_TIME_COLUMNS), key=len, reverse=True))
    sql = re.sub(
        rf"(?i)(\b(?:[\w\"]+\.)?(?:{nanos})\s*=\s*)(%s)",
        r"\1to_timestamp(\2 / 1000000000.0)", sql,
    )
    sql = re.sub(
        rf"(?i)(\b(?:[\w\"]+\.)?(?:{nanos})\s*(?:<=|>=|<>|!=|=|<|>)\s*)(%s)",
        r"\1to_timestamp(\2 / 1000000000.0)", sql,
    )
    sql = re.sub(
        rf"(?i)(\b(?:[\w\"]+\.)?(?:{names})\s*=\s*)(%s)",
        r"\1to_timestamp(\2)", sql,
    )
    sql = re.sub(
        rf"(?i)(\b(?:[\w\"]+\.)?(?:{names})\s*(?:<=|>=|<>|!=|=|<|>)\s*)(%s)",
        r"\1to_timestamp(\2)", sql,
    )
    json_names = "|".join(sorted(map(re.escape, _JSONB_COLUMNS), key=len, reverse=True))
    sql = re.sub(
        rf"(?i)(\b(?:[\w\"]+\.)?(?:{json_names})\s*=\s*)(%s)",
        r"\1\2::jsonb", sql,
    )
    sql = re.sub(r"(?i)(THEN\s+)(%s)(\s+ELSE\s+aliases\b)", r"\1\2::jsonb\3", sql)
    return sql


def _adapt_epoch_sql(sql: str, connection=None) -> str:
    """Keep the existing epoch-seconds API working with timestamptz columns."""
    sql = _adapt_no_column_insert(sql, connection)
    sql = _adapt_insert_values(sql)
    return _adapt_epoch_comparisons(sql)


def _values_close_index(sql: str, start: int) -> int:
    """Return the index just past the parenthesis closing the VALUES list opened before start."""
    depth = 1
    quote: str | None = None
    value_close = start
    while value_close < len(sql) and depth:
        char = sql[value_close]
        if quote:
            if char == quote:
                if value_close + 1 < len(sql) and sql[value_close + 1] == quote:
                    value_close += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        value_close += 1
    return value_close


def _normalize_empty_json_lists(sql: str, params: Sequence[Any]) -> tuple[Any, ...]:
    """Map legacy empty TEXT list values to valid empty JSON arrays."""
    values = list(params)
    insert = re.search(
        r"(?is)INSERT\s+INTO\s+(?:[\w\"]+\.)?[\w\"]+\s*"
        r"\(([^()]*)\)\s*VALUES\s*\(", sql,
    )
    if not insert:
        return tuple(values)
    value_open = insert.end() - 1
    value_close = _values_close_index(sql, value_open + 1)
    expressions = _split_sql_list(sql[value_open + 1:value_close - 1])
    offset = sql[:insert.start()].count("%s")
    columns = _split_sql_list(insert.group(1))
    if len(columns) != len(expressions):
        return tuple(values)
    for column, expression in zip(columns, expressions):
        name = column.rsplit(".", 1)[-1].strip().strip('"').lower()
        if name not in _JSONB_COLUMNS:
            offset += expression.count("%s")
            continue
        for _ in range(expression.count("%s")):
            if offset < len(values) and values[offset] == "":
                values[offset] = "[]"
            offset += 1
    return tuple(values)


class Record(tuple):
    """Tuple row with column-name access for query results."""

    def __new__(cls, values: Sequence[Any], columns: Sequence[str]):
        instance = super().__new__(cls, tuple(values))
        instance._columns = list(columns)
        return instance

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self._columns.index(key)
            except ValueError:
                raise KeyError(key) from None
        return super().__getitem__(key)

    def keys(self):
        return list(self._columns)

    def __contains__(self, key):
        if isinstance(key, str):
            return key in self._columns
        return super().__contains__(key)


def database_url(*, test: bool = False, allow_test_fallback: bool = False) -> str:
    """Return the configured PostgreSQL URL or raise a clear error."""
    from common.config import get_database_url
    return get_database_url(test=test, allow_test_fallback=allow_test_fallback)


def _wrap(row, columns: Sequence[str], row_factory):
    if row is None:
        return None
    values = tuple(
        (value.timestamp() if name.lower() in _FLOAT_TIME_COLUMNS else int(value.timestamp()))
        if isinstance(value, datetime) and name.lower() != "applied_at"
        else value
        for value, name in zip(row, columns)
    )
    return Record(values, columns) if row_factory is Record else values


class Cursor:
    """Small cursor adapter that supports the shared Record row format."""

    def __init__(self, conn: "Connection", raw: psycopg.Cursor):
        self._conn = conn
        self._raw = raw

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> "Cursor":
        sql = _adapt_epoch_sql(sql, self._raw.connection)
        if params is not None:
            params = _normalize_empty_json_lists(sql, params)
        self._raw.execute(sql, tuple(params) if params is not None else None)
        return self

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> "Cursor":
        self._raw.executemany(_adapt_epoch_sql(sql, self._raw.connection), [tuple(params) for params in seq])
        return self

    def fetchone(self):
        return _wrap(self._raw.fetchone(), self.columns, self._conn._row_factory)

    def fetchall(self):
        return [_wrap(row, self.columns, self._conn._row_factory)
                for row in self._raw.fetchall()]

    def fetchmany(self, size: int = 1):
        return [_wrap(row, self.columns, self._conn._row_factory)
                for row in self._raw.fetchmany(size)]

    @property
    def columns(self) -> list[str]:
        return [column.name for column in self._raw.description or ()]

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    @property
    def description(self):
        return self._raw.description

    def close(self) -> None:
        self._raw.close()

    def __iter__(self) -> Iterator:
        while (row := self.fetchone()) is not None:
            yield row


class Connection:
    """Connection facade with explicit commit/rollback context semantics."""

    def __init__(self, raw: psycopg.Connection, url: str | None = None):
        self._raw = raw
        self._url = url
        self._row_factory: Any = None

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        if value not in (None, Record):
            raise ValueError("row_factory must be None or db.Record")
        self._row_factory = value

    @property
    def in_transaction(self) -> bool:
        return self._raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    @property
    def autocommit(self) -> bool:
        return self._raw.autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._raw.autocommit = bool(value)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Cursor:
        return Cursor(self, self._raw.cursor()).execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> Cursor:
        return Cursor(self, self._raw.cursor()).executemany(sql, seq)

    def cursor(self) -> Cursor:
        return Cursor(self, self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()


def connect(url: str | None = None, *, connect_timeout: int = 10) -> Connection:
    """Open a PostgreSQL connection; the caller owns and closes it."""
    conninfo = (url or "").strip() or database_url()
    if not conninfo.startswith(("postgresql://", "postgres://")):
        raise ValueError("A PostgreSQL connection URL is required")
    raw = psycopg.connect(conninfo, connect_timeout=connect_timeout)
    # Preserve the database layer's long-standing JSON-as-text interface while
    # JSON/JSONB columns are introduced. Callers can continue using json.loads.
    set_json_loads(lambda value: value.decode("utf-8") if isinstance(value, bytes) else value, raw)
    raw.execute("SET TIME ZONE 'UTC'")
    raw.commit()
    return Connection(raw, url=conninfo)


@contextmanager
def connection(url: str | None = None, *, connect_timeout: int = 10) -> Iterator[Connection]:
    conn = connect(url, connect_timeout=connect_timeout)
    try:
        yield conn
    finally:
        conn.close()


def table_names(conn: Connection) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_catalog.pg_tables "
        "WHERE schemaname = current_schema()"
    ).fetchall()
    return {row[0] for row in rows}


def table_exists(conn: Connection, table: str) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables "
        "WHERE schemaname = current_schema() AND tablename = %s)",
        (table,),
    ).fetchone()[0]


def table_columns(conn: Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def pg_backup(db_url: str | None = None, backup_dir=None, prefix: str = "adulthub", *, job_id: str | None = None) -> "object":
    """Compatibility wrapper; implementation lives in ``common.backup``."""
    from common.backup import pg_backup as backup
    return backup(db_url, backup_dir, prefix, job_id=job_id)


def prune_old_backups(dest_dir, current_dest=None, keep_days: int | None = None) -> int:
    """Compatibility wrapper; implementation lives in ``common.backup``."""
    from common.backup import prune_old_backups as prune
    return prune(dest_dir, current_dest, keep_days=keep_days)


def pg_restore(dump_path: "object", db_url: str | None = None, *,
               clean: bool = True, run_migrations: bool = True) -> bool:
    """Compatibility wrapper; implementation lives in ``common.backup``."""
    from common.backup import pg_restore as restore
    return restore(dump_path, db_url, clean=clean, run_migrations=run_migrations)


def pg_restore_test(dump_path: "object", restore_admin_db_url: str | None = None, *,
                    primary_db_url: str | None, job_id: str,
                    max_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024) -> dict:
    """Compatibility wrapper; implementation lives in ``common.backup``."""
    from common.backup import pg_restore_test as restore_test
    return restore_test(
        dump_path,
        restore_admin_db_url,
        primary_db_url=primary_db_url,
        job_id=job_id,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
