"""Optional, bounded diagnostics for Telegram playback requests.

The playback path remains unchanged when diagnostics are disabled. While enabled,
active request counters stay in memory and one summary row is persisted when the
HTTP stream finishes (or is cancelled).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from common import db
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional


class PlaybackDiagnostics:
    RETENTION_DAYS = 7
    MAX_REPORTS = 500
    MAX_ACTIVE = 100
    MAX_SAMPLE_BUCKETS = 500
    SAMPLE_SECONDS = 5

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.getenv("DATABASE_URL") or None
        self._lock = threading.RLock()
        self._active: dict[str, PlaybackTrace] = {}
        self._enabled_until: Optional[float] = None
        try:
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(
                        "DELETE FROM diagnostic_reports WHERE ended_at < %s",
                        (time.time() - self.RETENTION_DAYS * 86400,),
                    )
                row = conn.execute(
                    "SELECT value FROM diagnostic_settings WHERE key='enabled_until'"
                ).fetchone()
            if row:
                expiry = float(row[0])
                if expiry > time.time():
                    self._enabled_until = expiry
        except Exception:
            # Diagnostics must never prevent the resolver from starting.
            self._enabled_until = None

    def _connect(self) -> db.Connection:
        return db.connect(self.db_url)

    @staticmethod
    def _human_time(timestamp: Optional[float]) -> Optional[str]:
        if timestamp is None:
            return None
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")

    def _is_enabled_locked(self) -> bool:
        if self._enabled_until is None:
            return False
        if self._enabled_until <= time.time():
            self._enabled_until = None
            try:
                with closing(self._connect()) as conn:
                    with conn:
                        conn.execute("DELETE FROM diagnostic_settings WHERE key='enabled_until'")
            except Exception:
                pass
            return False
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            enabled = self._is_enabled_locked()
            expiry = self._enabled_until if enabled else None
            active_count = len(self._active)
        saved_count = 0
        try:
            with closing(self._connect()) as conn:
                saved_count = int(conn.execute("SELECT count(*) FROM diagnostic_reports").fetchone()[0])
        except Exception:
            pass
        return {
            "enabled": enabled,
            "enabled_until": self._human_time(expiry),
            "active_count": active_count,
            "saved_count": saved_count,
            "retention_days": self.RETENTION_DAYS,
        }

    def set_enabled(self, hours: int = 24) -> dict[str, Any]:
        hours = max(1, min(int(hours), 24))
        expiry = time.time() + hours * 3600
        with self._lock:
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO diagnostic_settings(key,value) VALUES('enabled_until', %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (str(expiry),),
                    )
            self._enabled_until = expiry
        return self.status()

    def disable(self) -> dict[str, Any]:
        with self._lock:
            self._enabled_until = None
            try:
                with closing(self._connect()) as conn:
                    with conn:
                        conn.execute("DELETE FROM diagnostic_settings WHERE key='enabled_until'")
            except Exception:
                pass
        return self.status()

    def begin(self, video_id: str, method: str, range_header: Optional[str]) -> Optional["PlaybackTrace"]:
        if method.upper() == "HEAD":
            return None
        with self._lock:
            if not self._is_enabled_locked():
                return None
            # Diagnostics are optional and must not grow without bound under
            # long-lived or abandoned playback requests. Playback proceeds
            # normally when the in-memory trace budget is exhausted.
            if len(self._active) >= self.MAX_ACTIVE:
                return None
            trace = PlaybackTrace(self, video_id, method, range_header)
            self._active[trace.report_id] = trace
            return trace

    def _finish(self, trace: "PlaybackTrace", outcome: str, error_type: Optional[str]) -> None:
        report = trace.to_report(outcome, error_type)
        with self._lock:
            self._active.pop(trace.report_id, None)
        try:
            now = time.time()
            cutoff = now - self.RETENTION_DAYS * 86400
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO diagnostic_reports(report_id,ended_at,report_json) VALUES(%s,%s,%s) "
                        "ON CONFLICT (report_id) DO UPDATE SET ended_at = EXCLUDED.ended_at, "
                        "report_json = EXCLUDED.report_json",
                        (trace.report_id, now, json.dumps(report, separators=(",", ":"))),
                    )
                    conn.execute("DELETE FROM diagnostic_reports WHERE ended_at < %s", (cutoff,))
                    conn.execute("""DELETE FROM diagnostic_reports WHERE report_id IN (
                        SELECT report_id FROM diagnostic_reports ORDER BY ended_at DESC OFFSET %s
                    )""", (self.MAX_REPORTS,))
        except Exception:
            # Recording failures are intentionally isolated from HTTP playback.
            pass

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            active = [trace.to_report("active", None) for trace in self._active.values()]
        saved: list[dict[str, Any]] = []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT report_json FROM diagnostic_reports ORDER BY ended_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            saved = [json.loads(row[0]) for row in rows]
        except Exception:
            pass
        reports = active + saved
        reports.sort(key=lambda item: item.get("started_epoch", 0), reverse=True)
        return reports[:limit]

    def get_report(self, report_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            trace = self._active.get(report_id)
            if trace:
                return trace.to_report("active", None)
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT report_json FROM diagnostic_reports WHERE report_id=%s", (report_id,)
                ).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:
            return None

    def clear_reports(self) -> int:
        with closing(self._connect()) as conn:
            with conn:
                cur = conn.execute("DELETE FROM diagnostic_reports")
                return int(cur.rowcount)


class PlaybackTrace:
    def __init__(self, owner: PlaybackDiagnostics, video_id: str,
                 method: str, range_header: Optional[str]):
        self.owner = owner
        self.report_id = uuid.uuid4().hex[:16]
        self.started_epoch = time.time()
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self.provider = "telegram"
        self.video_key = hashlib.sha256(video_id.encode("utf-8", "replace")).hexdigest()[:12]
        self.method = method[:8]
        self.range = range_header[:80] if range_header else None
        self.status_code: Optional[int] = None
        self.file_size_ms: Optional[int] = None
        self.upstream_first_byte_ms: Optional[int] = None
        self.downstream_first_byte_ms: Optional[int] = None
        self.upstream_bytes = 0
        self.downstream_bytes = 0
        self.upstream_max_gap_ms = 0
        self.downstream_max_gap_ms = 0
        self.upstream_samples: dict[int, int] = {}
        self.downstream_samples: dict[int, int] = {}
        self.samples_truncated = False
        self._last_upstream_at: Optional[float] = None
        self._last_downstream_at: Optional[float] = None
        self._phase = "initializing"
        self._phase_started = time.monotonic()

    def _elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started) * 1000))

    def mark_file_size(self) -> None:
        with self._lock:
            self.file_size_ms = self._elapsed_ms()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase[:32]
            self._phase_started = time.monotonic()

    def set_status(self, status_code: int) -> None:
        with self._lock:
            self.status_code = int(status_code)

    def _record(self, count: int, upstream: bool) -> None:
        if count <= 0:
            return
        now = time.monotonic()
        elapsed = now - self.started
        with self._lock:
            if upstream:
                self._last_upstream_at = now
                if self.upstream_first_byte_ms is None:
                    self.upstream_first_byte_ms = int(elapsed * 1000)
                self.upstream_bytes += count
                samples = self.upstream_samples
            else:
                self._last_downstream_at = now
                if self.downstream_first_byte_ms is None:
                    self.downstream_first_byte_ms = int(elapsed * 1000)
                self.downstream_bytes += count
                samples = self.downstream_samples
            bucket = int(elapsed // self.owner.SAMPLE_SECONDS)
            if bucket in samples or len(samples) < self.owner.MAX_SAMPLE_BUCKETS:
                samples[bucket] = samples.get(bucket, 0) + count
            else:
                self.samples_truncated = True

    def note_upstream(self, count: int, wait_ms: int = 0) -> None:
        self._record(count, True)
        if count > 0 and wait_ms >= 0:
            with self._lock:
                self.upstream_max_gap_ms = max(self.upstream_max_gap_ms, int(wait_ms))

    def note_downstream(self, count: int, send_wait_ms: int = 0) -> None:
        self._record(count, False)
        if count > 0 and send_wait_ms >= 0:
            with self._lock:
                self.downstream_max_gap_ms = max(self.downstream_max_gap_ms, int(send_wait_ms))

    def snapshot(self, outcome: str, error_type: Optional[str]) -> dict[str, Any]:
        with self._lock:
            elapsed = self._elapsed_ms()
            now_mono = time.monotonic()
            return {
                "report_id": self.report_id,
                "started_at": self.owner._human_time(self.started_epoch),
                "started_epoch": self.started_epoch,
                "ended_at": None if outcome == "active" else self.owner._human_time(time.time()),
                "provider": self.provider,
                "video_key": self.video_key,
                "method": self.method,
                "range": self.range,
                "status_code": self.status_code,
                "outcome": outcome,
                "error_type": error_type,
                "elapsed_ms": elapsed,
                "phase": self._phase if outcome == "active" else None,
                "phase_idle_ms": (max(0, int((now_mono - self._phase_started) * 1000))
                                  if outcome == "active" else None),
                "file_size_ms": self.file_size_ms,
                "upstream_first_byte_ms": self.upstream_first_byte_ms,
                "downstream_first_byte_ms": self.downstream_first_byte_ms,
                "upstream_bytes": self.upstream_bytes,
                "downstream_bytes": self.downstream_bytes,
                "upstream_max_gap_ms": self.upstream_max_gap_ms,
                "downstream_max_gap_ms": self.downstream_max_gap_ms,
                "upstream_idle_ms": (None if self._last_upstream_at is None else
                                     max(0, int((now_mono - self._last_upstream_at) * 1000))),
                "downstream_idle_ms": (None if self._last_downstream_at is None else
                                       max(0, int((now_mono - self._last_downstream_at) * 1000))),
                "upstream_samples": [
                    {"t_s": bucket * self.owner.SAMPLE_SECONDS, "bytes": count}
                    for bucket, count in sorted(self.upstream_samples.items())
                ],
                "downstream_samples": [
                    {"t_s": bucket * self.owner.SAMPLE_SECONDS, "bytes": count}
                    for bucket, count in sorted(self.downstream_samples.items())
                ],
                "samples_truncated": self.samples_truncated,
                "active": outcome == "active",
            }

    def to_report(self, outcome: str, error_type: Optional[str]) -> dict[str, Any]:
        return self.snapshot(outcome, error_type)

    def finish(self, outcome: str, error_type: Optional[str] = None) -> None:
        self.owner._finish(self, outcome, error_type)


diagnostics = PlaybackDiagnostics()
