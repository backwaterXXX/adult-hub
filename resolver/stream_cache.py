"""Persistent stream URL cache with an in-memory LRU hot set."""
import os
import time
import threading
import logging
from collections import OrderedDict
from typing import Optional, Tuple

from common import db

logger = logging.getLogger("resolver")

# Persistent LRU Stream Cache
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))  # 60 minutes (matches config.DEFAULT_CACHE_TTL_SECONDS)
MAX_CACHE_ENTRIES = int(os.getenv("MAX_CACHE_ENTRIES", "2000"))

def cache_ttl_for_provider(provider_name: str) -> int:
    """Generic per-provider stream-cache TTL (seconds).

    Neutral: no hardcoded provider names. Reads ``CACHE_TTL_<PROVIDER>_SECONDS``
    dynamically (e.g. CACHE_TTL_TELEGRAM_SECONDS) and falls back to the global
    CACHE_TTL_SECONDS default. Invalid values fall back to the default too.
    """
    try:
        env_key = f"CACHE_TTL_{str(provider_name).upper()}_SECONDS"
        return int(os.getenv(env_key, str(CACHE_TTL_SECONDS)))
    except Exception:
        logger.debug("Ignored exception in cache_ttl_for_provider", exc_info=True)
        return CACHE_TTL_SECONDS

_CACHE_LEN_TTL = 30.0  # seconds between DB count refreshes for __len__


class PersistentLRUCache:
    """PostgreSQL-backed persistent LRU stream cache (plus in-memory hot set)."""

    def __init__(self, db_url: Optional[str] = None, max_entries: int = MAX_CACHE_ENTRIES, ttl_seconds: int = CACHE_TTL_SECONDS, db_path: Optional[str] = None):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._memory: OrderedDict[str, Tuple[str, float, float]] = OrderedDict()
        # Persist database recency periodically instead of on every hot hit.
        self._access_persist_interval_seconds = 60.0

        self.db_url = db_url or db_path or os.getenv("DATABASE_URL") or None
        self._pg_ok = bool(self.db_url)
        self._consecutive_errors = 0
        self._pg_recovery_at = 0.0
        self._cached_len: int = 0
        self._cached_len_at: float = 0.0
        self._init_db()

    def _pg(self, now: Optional[float] = None):
        if not self.db_url:
            return None
        current_time = time.time() if now is None else now
        if not self._pg_ok:
            if current_time < self._pg_recovery_at:
                return None
        try:
            conn = db.connect(self.db_url)
            if not self._pg_ok:
                self._pg_ok = True
                self._consecutive_errors = 0
                logger.info("PostgreSQL stream cache recovered.")
            return conn
        except Exception as e:
            self._handle_pg_error(e, current_time)
            raise

    def _handle_pg_error(self, exc: Exception, now: float) -> None:
        self._consecutive_errors += 1
        logger.debug("PostgreSQL cache error (count=%d): %s", self._consecutive_errors, exc)
        if self._consecutive_errors >= 3:
            if self._pg_ok:
                logger.warning(
                    "PostgreSQL cache failing (%s); degrading to in-memory mode for 30s.", exc
                )
            self._pg_ok = False
            self._pg_recovery_at = now + 30.0

    def _init_db(self):
        if not self._pg_ok:
            logger.warning("DATABASE_URL is not set; stream cache is memory-only.")
            return
        try:
            conn = db.connect(self.db_url)
            try:
                now = time.time()
                with conn:
                    conn.execute("DELETE FROM stream_cache WHERE expire_at < %s", (now,))
                    # Load the most recently used entries so a restart keeps hot streams.
                    rows = conn.execute(
                        "SELECT cache_key, stream_url, expire_at, last_accessed "
                        "FROM stream_cache ORDER BY last_accessed DESC LIMIT %s",
                        (self.max_entries,)
                    ).fetchall()
                    for key, url, exp, accessed in rows:
                        self._memory[key] = (url, exp, accessed)
            finally:
                conn.close()
            logger.info("Initialized persistent stream cache in PostgreSQL (%d loaded)", len(self._memory))
        except Exception as e:
            logger.warning("Failed to initialize PostgreSQL persistent cache: %s. Falling back to memory LRU.", e)
            self._pg_ok = False

    def _pg_write(self, sql: str, params=()):
        try:
            conn = self._pg()
        except Exception:
            return False
        if not conn:
            return False
        try:
            with conn:
                conn.execute(sql, params)
            self._consecutive_errors = 0
            return True
        except Exception as e:
            self._handle_pg_error(e, time.time())
            return False
        finally:
            conn.close()

    def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            if key in self._memory:
                url, persist = self._get_from_memory(key, now)
            else:
                url, persist = None, False
        if persist:
            if url is None:
                self._pg_write("DELETE FROM stream_cache WHERE cache_key = %s", (key,))
            else:
                if self._pg_write(
                        "UPDATE stream_cache SET last_accessed = %s WHERE cache_key = %s",
                        (now, key)):
                    with self._lock:
                        current = self._memory.get(key)
                        if current is not None and current[0] == url:
                            self._memory[key] = (url, current[1], now)
        if url is not None:
            return url
        if self._pg_ok or (self.db_url and now >= self._pg_recovery_at):
            return self._get_from_postgres(key, now)
        return None

    def _get_from_memory(self, key: str, now: float) -> tuple[Optional[str], bool]:
        """Read one hot entry; returns (url, needs_db_write) without doing I/O."""
        url, exp, last_persisted_access = self._memory[key]
        if now < exp:
            self._memory.move_to_end(key)
            return url, now - last_persisted_access >= self._access_persist_interval_seconds
        del self._memory[key]
        return None, True

    def _get_from_postgres(self, key: str, now: float) -> Optional[str]:
        try:
            conn = self._pg(now)
        except Exception:
            return None
        if not conn:
            return None
        try:
            cur = conn.execute(
                "SELECT stream_url, expire_at FROM stream_cache WHERE cache_key = %s",
                (key,)
            )
            row = cur.fetchone()
            if row:
                url, exp = row
                if now < exp:
                    with self._lock:
                        self._memory[key] = (url, exp, now)
                        if len(self._memory) > self.max_entries:
                            self._memory.popitem(last=False)
                    conn.execute("UPDATE stream_cache SET last_accessed = %s WHERE cache_key = %s", (now, key))
                    conn.commit()
                    self._consecutive_errors = 0
                    return url
                else:
                    conn.execute("DELETE FROM stream_cache WHERE cache_key = %s", (key,))
                    conn.commit()
            self._consecutive_errors = 0
        except Exception as e:
            self._handle_pg_error(e, now)
            try:
                conn.rollback()
            except Exception:
                logger.debug("Ignored exception in _get_from_postgres", exc_info=True)
        finally:
            conn.close()

        return None

    def set(self, key: str, url: str, ttl_seconds: Optional[int] = None):
        now = time.time()
        exp = now + (ttl_seconds if ttl_seconds else self.ttl_seconds)
        with self._lock:
            self._memory[key] = (url, exp, now)
            self._memory.move_to_end(key)
            if len(self._memory) > self.max_entries:
                self._memory.popitem(last=False)
            # Snapshot the DB-availability state while still under lock so we
            # don't hold the lock during the (potentially slow) connect call.
            should_persist = self._pg_ok or (self.db_url and now >= self._pg_recovery_at)

        # DB operations are intentionally outside the lock (C-2): a slow
        # connect (up to 10 s) must not block concurrent get()/invalidate().
        if not should_persist:
            return
        try:
            conn = self._pg(now)
        except Exception:
            return
        if not conn:
            return
        try:
            with conn:
                conn.execute("""
                    INSERT INTO stream_cache (cache_key, stream_url, expire_at, last_accessed)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        stream_url = EXCLUDED.stream_url,
                        expire_at = EXCLUDED.expire_at,
                        last_accessed = EXCLUDED.last_accessed
                """, (key, url, exp, now))

                cur = conn.execute("SELECT count(*) FROM stream_cache")
                count = cur.fetchone()[0]
                if count > self.max_entries:
                    overflow = count - self.max_entries
                    conn.execute("""
                        DELETE FROM stream_cache WHERE cache_key IN (
                            SELECT cache_key FROM stream_cache ORDER BY last_accessed ASC LIMIT %s
                        )
                    """, (overflow,))
            self._consecutive_errors = 0
        except Exception as e:
            self._handle_pg_error(e, now)
        finally:
            conn.close()

    def invalidate(self, key: str):
        """Drop one entry (used when a cached URL is found dead)."""
        with self._lock:
            self._memory.pop(key, None)
        self._pg_write("DELETE FROM stream_cache WHERE cache_key = %s", (key,))

    def __len__(self) -> int:
        now = time.time()
        with self._lock:
            if now - self._cached_len_at < _CACHE_LEN_TTL:
                return self._cached_len
            should_query = self._pg_ok
        if should_query:
            conn = None
            try:
                conn = self._pg(now)
                if conn:
                    cur = conn.execute(
                        "SELECT count(*) FROM stream_cache WHERE expire_at > %s", (now,)
                    )
                    count = cur.fetchone()[0]
                    self._consecutive_errors = 0
                    with self._lock:
                        self._cached_len = count
                        self._cached_len_at = now
                    return count
            except Exception as e:
                self._handle_pg_error(e, now)
            finally:
                if conn is not None:
                    conn.close()
        with self._lock:
            count = sum(1 for _, exp, _ in self._memory.values() if exp > now)
            self._cached_len = count
            self._cached_len_at = now
            return count

    def clear(self) -> int:
        """Clear persisted and in-memory entries and return the unique live count.

        If persistent deletion fails, preserve the hot set and propagate the
        error so the admin endpoint cannot report a successful clear while
        database entries remain available to repopulate it.
        """
        now = time.time()
        with self._lock:
            live_keys = {key for key, (_, expires, _) in self._memory.items() if expires > now}
            if self._pg_ok:
                conn = self._pg()
                if conn is None:
                    raise RuntimeError("Persistent cache database is unavailable.")
                try:
                    with conn:
                        rows = conn.execute(
                            "SELECT cache_key FROM stream_cache WHERE expire_at > %s", (now,)
                        ).fetchall()
                        live_keys.update(str(row[0]) for row in rows)
                        conn.execute("DELETE FROM stream_cache")
                    self._consecutive_errors = 0
                except Exception as e:
                    self._handle_pg_error(e, now)
                    raise
                finally:
                    conn.close()
            self._memory.clear()
            self._cached_len = 0
            self._cached_len_at = 0.0
            return len(live_keys)

    def close(self):
        with self._lock:
            self._memory.clear()
