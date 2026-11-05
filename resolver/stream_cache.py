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
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "1500")) # 25 minutes
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
        self._init_db()

    def _pg(self):
        return db.connect(self.db_url) if self._pg_ok else None

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
            logger.info(f"Initialized persistent stream cache in PostgreSQL ({len(self._memory)} loaded)")
        except Exception as e:
            logger.warning(f"Failed to initialize PostgreSQL persistent cache: {e}. Falling back to memory LRU.")
            self._pg_ok = False

    def _pg_write(self, sql: str, params=()):
        if not self._pg_ok:
            return False
        try:
            conn = db.connect(self.db_url)
        except Exception as e:
            logger.debug(f"PostgreSQL cache write skipped (no connection): {e}")
            return False
        try:
            with conn:
                conn.execute(sql, params)
            return True
        except Exception as e:
            logger.debug(f"PostgreSQL cache write error: {e}")
            return False
        finally:
            conn.close()

    def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            if key in self._memory:
                return self._get_from_memory(key, now)
            if self._pg_ok:
                return self._get_from_postgres(key, now)
            return None

    def _get_from_memory(self, key: str, now: float) -> Optional[str]:
        url, exp, last_persisted_access = self._memory[key]
        if now < exp:
            self._memory.move_to_end(key)
            if now - last_persisted_access >= self._access_persist_interval_seconds:
                if self._pg_write(
                        "UPDATE stream_cache SET last_accessed = %s WHERE cache_key = %s",
                        (now, key)):
                    self._memory[key] = (url, exp, now)
            return url
        del self._memory[key]
        self._pg_write("DELETE FROM stream_cache WHERE cache_key = %s", (key,))
        return None

    def _get_from_postgres(self, key: str, now: float) -> Optional[str]:
        try:
            conn = db.connect(self.db_url)
        except Exception as e:
            logger.debug(f"PostgreSQL cache get error: {e}")
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
                    self._memory[key] = (url, exp, now)
                    if len(self._memory) > self.max_entries:
                        self._memory.popitem(last=False)
                    conn.execute("UPDATE stream_cache SET last_accessed = %s WHERE cache_key = %s", (now, key))
                    conn.commit()
                    return url
                else:
                    conn.execute("DELETE FROM stream_cache WHERE cache_key = %s", (key,))
                    conn.commit()
        except Exception as e:
            logger.debug(f"PostgreSQL cache get error: {e}")
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

            if not self._pg_ok:
                return
            try:
                conn = db.connect(self.db_url)
            except Exception as e:
                logger.debug(f"PostgreSQL cache set error: {e}")
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
            except Exception as e:
                logger.debug(f"PostgreSQL cache set error: {e}")
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
            if self._pg_ok:
                try:
                    conn = db.connect(self.db_url)
                    try:
                        cur = conn.execute("SELECT count(*) FROM stream_cache WHERE expire_at > %s", (now,))
                        return cur.fetchone()[0]
                    finally:
                        conn.close()
                except Exception:
                    logger.debug("Ignored exception in __len__", exc_info=True)
            return sum(1 for _, exp, _ in self._memory.values() if exp > now)

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
                finally:
                    conn.close()
            self._memory.clear()
            return len(live_keys)

    def close(self):
        with self._lock:
            self._memory.clear()
