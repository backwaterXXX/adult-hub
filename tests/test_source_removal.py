"""Behavior-focused unit tests for Telegram channel removal (W1, W5, W6, W7)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from sync import source_removal
except (ImportError, ModuleNotFoundError):
    import sync.source_removal as source_removal

_channel_ids = source_removal._channel_ids
_remove_sync_history = source_removal._remove_sync_history
_source_match_clause = source_removal._source_match_clause
remove_telegram_channel = source_removal.remove_telegram_channel


def _sql_like_match(value: str, pattern: str, escape: str = "\\") -> bool:
    import re
    regex_parts = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == escape and i + 1 < n:
            i += 1
            regex_parts.append(re.escape(pattern[i]))
        elif c == "%":
            regex_parts.append(".*")
        elif c == "_":
            regex_parts.append(".")
        else:
            regex_parts.append(re.escape(c))
        i += 1
    regex_parts.append("$")
    return bool(re.match("".join(regex_parts), value, re.DOTALL))


class FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, data=None):
        self.data = data or {}
        # tables:
        # 'videos': set of video_id
        # 'synced_files': list of (video_id, file_path)
        # 'video_sources': list of (video_id, source_id, provider, channel_id, is_active)
        # 'dead_streams': set of video_id
        # 'video_duplicate_decisions': list of (loser_id, keeper_id)
        # 'video_duplicate_dismissals': list of (left_id, right_id)
        # 'admin_quality_acknowledgements': set of video_id
        # 'telegram_channel_state': set of channel_id
        self.sql_log: list[tuple[str, tuple]] = []
        self.deleted: dict[str, list] = {
            "videos": [],
            "synced_files": [],
            "video_sources": [],
            "dead_streams": [],
            "video_duplicate_decisions": [],
            "video_duplicate_dismissals": [],
            "admin_quality_acknowledgements": [],
            "telegram_channel_state": [],
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute(self, sql: str, params: tuple | list = ()):
        sql_compact = " ".join(sql.strip().split())
        self.sql_log.append((sql_compact, tuple(params) if params else ()))

        cursor = self._select(sql, sql_compact, params)
        if cursor is None:
            cursor = self._delete(sql_compact, params)
        return cursor if cursor is not None else FakeCursor()

    def _select(self, sql: str, sql_compact: str, params):
        if "FROM pg_catalog.pg_tables" in sql:
            tables = [
                ("videos",),
                ("synced_files",),
                ("video_sources",),
                ("dead_streams",),
                ("video_duplicate_decisions",),
                ("video_duplicate_dismissals",),
                ("admin_quality_acknowledgements",),
                ("telegram_channel_state",),
            ]
            return FakeCursor(tables)

        if "SELECT id FROM videos WHERE provider='telegram'" in sql_compact:
            return self._select_telegram_videos(params)

        if "SELECT video_id, source_id FROM video_sources WHERE provider='telegram'" in sql_compact:
            return self._select_telegram_sources(params)

        return self._select_by_video_ids(sql_compact, params)

    def _select_telegram_videos(self, params):
        matching = []
        for vid in sorted(self.data.get("videos", set())):
            for pattern in params:
                if _sql_like_match(vid, pattern):
                    matching.append((vid,))
                    break
        return FakeCursor(matching)

    def _select_telegram_sources(self, params):
        aliases = {str(a).strip() for a in params[0]} if params else set()
        prefixes = params[1:]
        matching = []
        for row in self.data.get("video_sources", []):
            vid, sid, prov, cid, is_act = row[:5]
            if prov == "telegram":
                if str(cid or "").strip() in aliases or any(_sql_like_match(sid, p) for p in prefixes):
                    matching.append((vid, sid))
        return FakeCursor(matching)

    def _select_by_video_ids(self, sql_compact: str, params):
        if "SELECT id FROM videos WHERE id = ANY" in sql_compact:
            cands = set(params[0])
            matching = [(vid,) for vid in sorted(self.data.get("videos", set())) if vid in cands]
            return FakeCursor(matching)

        if "SELECT video_id, source_id, provider, channel_id, is_active FROM video_sources WHERE video_id = ANY" in sql_compact:
            vids = set(params[0])
            matching = [row for row in self.data.get("video_sources", []) if row[0] in vids]
            return FakeCursor(matching)

        if "SELECT video_id, file_path FROM synced_files WHERE video_id = ANY" in sql_compact:
            vids = set(params[0])
            matching = [row for row in self.data.get("synced_files", []) if row[0] in vids]
            return FakeCursor(matching)

        return None

    def _delete(self, sql_compact: str, params):
        cursor = self._delete_video_rows(sql_compact, params)
        if cursor is not None:
            return cursor

        if "DELETE FROM video_sources WHERE provider='telegram'" in sql_compact:
            return self._delete_telegram_sources(params)

        if "DELETE FROM telegram_channel_state WHERE channel_id = ANY" in sql_compact:
            return self._delete_channel_state(params)

        return None

    def _delete_video_rows(self, sql_compact: str, params):
        # Deletions:
        if "DELETE FROM synced_files WHERE video_id=%s" in sql_compact:
            vid = params[0]
            initial_count = len(self.data.get("synced_files", []))
            self.data["synced_files"] = [r for r in self.data.get("synced_files", []) if r[0] != vid]
            deleted_count = initial_count - len(self.data["synced_files"])
            self.deleted["synced_files"].append(vid)
            return FakeCursor(rowcount=deleted_count)

        if "DELETE FROM video_sources WHERE video_id=%s" in sql_compact:
            vid = params[0]
            self.data["video_sources"] = [r for r in self.data.get("video_sources", []) if r[0] != vid]
            self.deleted["video_sources"].append(vid)
            return FakeCursor(rowcount=1)

        if "DELETE FROM dead_streams WHERE video_id=%s" in sql_compact:
            vid = params[0]
            self.data.get("dead_streams", set()).discard(vid)
            self.deleted["dead_streams"].append(vid)
            return FakeCursor(rowcount=1)

        if "DELETE FROM video_duplicate_decisions WHERE loser_id=%s OR keeper_id=%s" in sql_compact:
            vid = params[0]
            self.data["video_duplicate_decisions"] = [
                r for r in self.data.get("video_duplicate_decisions", [])
                if r[0] != vid and r[1] != vid
            ]
            self.deleted["video_duplicate_decisions"].append(vid)
            return FakeCursor(rowcount=1)

        if "DELETE FROM video_duplicate_dismissals WHERE left_id=%s OR right_id=%s" in sql_compact:
            vid = params[0]
            self.data["video_duplicate_dismissals"] = [
                r for r in self.data.get("video_duplicate_dismissals", [])
                if r[0] != vid and r[1] != vid
            ]
            self.deleted["video_duplicate_dismissals"].append(vid)
            return FakeCursor(rowcount=1)

        if "DELETE FROM admin_quality_acknowledgements WHERE video_id=%s" in sql_compact:
            vid = params[0]
            self.data.get("admin_quality_acknowledgements", set()).discard(vid)
            self.deleted["admin_quality_acknowledgements"].append(vid)
            return FakeCursor(rowcount=1)

        if "DELETE FROM videos WHERE id=%s" in sql_compact:
            vid = params[0]
            self.data.get("videos", set()).discard(vid)
            self.deleted["videos"].append(vid)
            return FakeCursor(rowcount=1)

        return None

    def _delete_telegram_sources(self, params):
        aliases = {str(a).strip() for a in params[0]} if params else set()
        prefixes = params[1:]
        self.data["video_sources"] = [
            r for r in self.data.get("video_sources", [])
            if not (r[2] == "telegram" and (str(r[3] or "").strip() in aliases or any(_sql_like_match(r[1], p) for p in prefixes)))
        ]
        return FakeCursor(rowcount=1)

    def _delete_channel_state(self, params):
        aliases = {str(a).strip() for a in params[0]} if params else set()
        self.data["telegram_channel_state"] = [
            c for c in self.data.get("telegram_channel_state", []) if str(c).strip() not in aliases
        ]
        return FakeCursor(rowcount=1)


class FakeLogger:
    def __init__(self):
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)


class FakeServices:
    def __init__(self, unsafe_paths: set[str] | None = None):
        self.logger = FakeLogger()
        self.unsafe_paths = unsafe_paths or set()

    def resolve_media_path(self, stored_path: str, media_dir: Path) -> Path:
        if stored_path in self.unsafe_paths or ".." in stored_path:
            raise ValueError(f"Unsafe path: {stored_path}")
        return media_dir / stored_path


class TestSourceRemoval(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.media_dir = self.root / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.progress_calls: list[tuple] = []
        self.addCleanup(self.temp_dir.cleanup)

    def _record_progress(self, conn, job_id, stage, completed=0, total=0):
        self.progress_calls.append((job_id, stage, completed, total))

    def test_channel_ids_aliases(self):
        self.assertEqual(_channel_ids("-10012345"), {"-10012345", "12345"})
        self.assertEqual(_channel_ids("12345"), {"-10012345", "12345"})
        self.assertEqual(_channel_ids("-12345"), {"-10012345", "-12345"})
        self.assertEqual(_channel_ids(" -12345 "), {"-10012345", "-12345"})
        self.assertIn("-10012345", _channel_ids(" -12345 "))

    def test_source_match_clause_sql_generation_and_parameters(self):
        """Prove the production PostgreSQL SQL normalization clause and parameters are exact."""
        aliases = ["-100123", "123"]
        prefixes = ["telegram:-100123:", "telegram:123:"]
        sql_clause, params = _source_match_clause(aliases, prefixes)

        # 1. SQL condition must use TRIM(channel_id) = ANY(%s) for PostgreSQL
        self.assertIn("TRIM(channel_id) = ANY(%s)", sql_clause)
        self.assertIn("source_id LIKE %s ESCAPE '\\'", sql_clause)
        self.assertEqual(
            sql_clause,
            "TRIM(channel_id) = ANY(%s) OR source_id LIKE %s ESCAPE '\\' OR source_id LIKE %s ESCAPE '\\'",
        )

        # 2. Parameters must bind normalized aliases list first, then escaped LIKE patterns
        self.assertEqual(params[0], ["-100123", "123"])
        self.assertEqual(params[1], "telegram:-100123:%")
        self.assertEqual(params[2], "telegram:123:%")

        # 3. Wildcard escaping in prefixes: _, %, \
        wildcard_prefixes = ["telegram:@chan_name%with\\slash:"]
        sql_clause_wc, params_wc = _source_match_clause(
            ["@chan_name%with\\slash"], wildcard_prefixes
        )
        self.assertEqual(
            sql_clause_wc,
            "TRIM(channel_id) = ANY(%s) OR source_id LIKE %s ESCAPE '\\'",
        )
        self.assertEqual(params_wc[0], ["@chan_name%with\\slash"])
        self.assertEqual(params_wc[1], "telegram:@chan\\_name\\%with\\\\slash:%")

    def test_remove_telegram_channel_rejects_none_and_empty_and_whitespace_ids(self):
        """Validate at public entry before acquiring locks or mutating anything."""
        conn = FakeConnection({})
        services = FakeServices()
        invalid_ids = [None, "", "   ", "\t\n", " " * 50, 12345, [], {}]
        for bad_id in invalid_ids:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError) as ctx:
                    remove_telegram_channel(
                        conn, self.media_dir, bad_id, "job-fail", services, self._record_progress  # type: ignore
                    )
                self.assertIn("Channel removal requires a valid Telegram channel ID.", str(ctx.exception))
                # Ensure no SQL queries executed, no progress recorded
                self.assertEqual(conn.sql_log, [])
                self.assertEqual(self.progress_calls, [])

    def test_channel_ids_rejects_none_and_empty_and_whitespace_ids(self):
        """Prevent empty, None, or whitespace-only channel IDs from widening queries."""
        invalid_ids = [None, "", "   ", "\t\n", 12345, [], {}]
        for bad_id in invalid_ids:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError) as ctx:
                    _channel_ids(bad_id)  # type: ignore
                self.assertIn("Channel removal requires a valid Telegram channel ID.", str(ctx.exception))

    def test_remove_sync_history_rejects_none_and_empty_and_whitespace_ids(self):
        """Prevent empty, None, or whitespace-only channel IDs in history removal."""
        invalid_ids = [None, "", "   ", "\t\n", 12345]
        for bad_id in invalid_ids:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError) as ctx:
                    _remove_sync_history(bad_id)  # type: ignore
                self.assertIn("Channel removal requires a valid Telegram channel ID.", str(ctx.exception))

    def test_source_matches_whitespace_normalization_regression(self):
        """Whitespace normalization regression coverage for _source_matches.

        Tests sync history source filter / source entry strings and channel aliases,
        including leading whitespace before 'telegram:' and padded alias candidates.
        Note: database video_sources.source_id is an internal synthetic canonical
        identifier (telegram:<clean_id>:<msg>) and is never padded in the database model.
        """
        # Leading whitespace before 'telegram:'
        self.assertTrue(source_removal._source_matches("   telegram:-100123", {"-100123"}))
        self.assertTrue(source_removal._source_matches("   telegram:-100123   ", {"-100123"}))
        self.assertTrue(source_removal._source_matches(" \t telegram: -100123 \n", {"-100123"}))

        # Padded alias candidate in channel_ids
        self.assertTrue(source_removal._source_matches("telegram:-100123", {"   -100123   "}))
        self.assertTrue(source_removal._source_matches("   telegram:-100123   ", {"   -100123   "}))
        self.assertTrue(source_removal._source_matches("  12345  ", {"   12345   "}))

        # Padded alias candidate matching supergroup alias
        self.assertTrue(source_removal._source_matches("  telegram:12345  ", {" 12345 "}))

        # Non-matching cases
        self.assertFalse(source_removal._source_matches("   telegram:-100999   ", {"-100123"}))
        self.assertFalse(source_removal._source_matches("not-a-source", {"-100123"}))
        self.assertFalse(source_removal._source_matches(None, {"-100123"}))

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_regression_deleting_supergroup_cannot_delete_different_chat_id_catalog_or_media(
        self, mock_history, mock_verify, mock_lock
    ):
        """Deleting channel -10012345 must not delete catalog entries or media for channel -12345."""
        v_target = "telegram:-10012345:1"
        v_other = "telegram:-12345:1"

        target_strm = self.media_dir / "target" / "video_1.strm"
        target_strm.parent.mkdir(parents=True, exist_ok=True)
        target_strm.write_text("http://stream-target", encoding="utf-8")

        other_strm = self.media_dir / "other" / "video_1.strm"
        other_strm.parent.mkdir(parents=True, exist_ok=True)
        other_strm.write_text("http://stream-other", encoding="utf-8")

        data = {
            "videos": {v_target, v_other},
            "synced_files": [(v_target, "target/video_1.strm"), (v_other, "other/video_1.strm")],
            "video_sources": [
                (v_target, f"{v_target}:src", "telegram", "-10012345", 1),
                (v_other, f"{v_other}:src", "telegram", "-12345", 1),
            ],
            "telegram_channel_state": ["-10012345", "-12345"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-10012345", "job-reg", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 1)
        self.assertFalse(target_strm.exists())
        self.assertNotIn(v_target, conn.data["videos"])

        # Crucial regression assertions: other channel's media and catalog MUST NOT be deleted
        self.assertTrue(other_strm.exists(), "Deleting -10012345 must not delete -12345 media")
        self.assertIn(v_other, conn.data["videos"], "Deleting -10012345 must not delete -12345 video")
        self.assertEqual(len(conn.data["synced_files"]), 1)
        self.assertEqual(conn.data["synced_files"][0], (v_other, "other/video_1.strm"))
        self.assertTrue(any(row[0] == v_other and row[3] == "-12345" for row in conn.data["video_sources"]))
        self.assertIn("-12345", conn.data["telegram_channel_state"])

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(1, 2))
    def test_w1_w5_w7_deletes_synced_files_and_sidecars_and_returns_counts(
        self, mock_history, mock_verify, mock_lock
    ):
        video_id = "telegram:-100123:1"
        rel_path = "channel_123/video_1.strm"
        full_strm = self.media_dir / rel_path
        full_strm.parent.mkdir(parents=True, exist_ok=True)
        full_strm.write_text("http://stream", encoding="utf-8")
        full_nfo = full_strm.with_suffix(".nfo")
        full_nfo.write_text("<movie/>", encoding="utf-8")
        full_jpg = full_strm.with_suffix(".jpg")
        full_jpg.write_bytes(b"\xff\xd8\xff")
        full_thumb_fail = full_strm.with_suffix(".thumb_fail")
        full_thumb_fail.write_text("fail", encoding="utf-8")

        data = {
            "videos": {video_id},
            "synced_files": [(video_id, rel_path)],
            "video_sources": [(video_id, f"{video_id}:src", "telegram", "-100123", 1)],
            "dead_streams": {video_id},
            "video_duplicate_decisions": [(video_id, "other_video"), ("other_loser", video_id)],
            "video_duplicate_dismissals": [(video_id, "right_v"), ("left_v", video_id)],
            "admin_quality_acknowledgements": {video_id},
            "telegram_channel_state": ["-100123"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-1", services, self._record_progress
        )

        self.assertEqual(res["local_state"], "source_removed")
        self.assertEqual(res["channel_id"], "-100123")
        self.assertEqual(res["videos_deleted"], 1)
        self.assertEqual(res["files_deleted"], 4)
        self.assertEqual(res["synced_files_deleted"], 1)
        self.assertEqual(res["shared_videos_preserved"], 0)
        self.assertEqual(res["history_runs_removed"], 1)
        self.assertEqual(res["history_source_rows_removed"], 2)

        # Assert disk files unlinked
        self.assertFalse(full_strm.exists())
        self.assertFalse(full_nfo.exists())
        self.assertFalse(full_jpg.exists())
        self.assertFalse(full_thumb_fail.exists())

        # Assert DB deletes
        self.assertNotIn(video_id, conn.data["videos"])
        self.assertEqual(conn.data["synced_files"], [])
        self.assertEqual(conn.data["dead_streams"], set())
        self.assertEqual(conn.data["video_duplicate_decisions"], [])
        self.assertEqual(conn.data["video_duplicate_dismissals"], [])
        self.assertEqual(conn.data["admin_quality_acknowledgements"], set())

        # Progress tracking
        self.assertTrue(any(p[1] == "removing_source_data" and p[2] == 1 for p in self.progress_calls))

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_shared_videos_and_synced_files_preserved(
        self, mock_history, mock_verify, mock_lock
    ):
        video_id = "telegram:-100123:shared"
        rel_path = "shared/video.strm"
        full_strm = self.media_dir / rel_path
        full_strm.parent.mkdir(parents=True, exist_ok=True)
        full_strm.write_text("http://stream", encoding="utf-8")

        data = {
            "videos": {video_id},
            "synced_files": [(video_id, rel_path)],
            "video_sources": [
                (video_id, "telegram:-100123:shared:src", "telegram", "-100123", 1),
                (video_id, "telegram:-100999:shared:src", "telegram", "-100999", 1),
            ],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-2", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 0)
        self.assertEqual(res["files_deleted"], 0)
        self.assertEqual(res["synced_files_deleted"], 0)
        self.assertEqual(res["shared_videos_preserved"], 1)

        # File and DB rows remain intact
        self.assertTrue(full_strm.exists())
        self.assertIn(video_id, conn.data["videos"])
        self.assertEqual(len(conn.data["synced_files"]), 1)

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_w6_unsafe_path_fails_upfront_before_any_destructive_action(
        self, mock_history, mock_verify, mock_lock
    ):
        v1 = "telegram:-100123:1"
        v2 = "telegram:-100123:2"
        p1 = "chan/safe.strm"
        p2 = "../unsafe/escape.strm"

        full_p1 = self.media_dir / p1
        full_p1.parent.mkdir(parents=True, exist_ok=True)
        full_p1.write_text("safe", encoding="utf-8")

        data = {
            "videos": {v1, v2},
            "synced_files": [(v1, p1), (v2, p2)],
            "video_sources": [
                (v1, f"{v1}:src", "telegram", "-100123", 1),
                (v2, f"{v2}:src", "telegram", "-100123", 1),
            ],
        }
        conn = FakeConnection(data)
        services = FakeServices(unsafe_paths={p2})

        with self.assertRaises(RuntimeError) as ctx:
            remove_telegram_channel(
                conn, self.media_dir, "-100123", "job-3", services, self._record_progress
            )

        self.assertIn("A catalog file path failed safety checks; no catalog row was deleted.", str(ctx.exception))
        # Ensure absolutely NO files or DB rows were deleted
        self.assertTrue(full_p1.exists())
        self.assertEqual(conn.data["videos"], {v1, v2})
        self.assertEqual(len(conn.data["synced_files"]), 2)
        self.assertEqual(conn.deleted["videos"], [])
        self.assertEqual(conn.deleted["synced_files"], [])

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_w7_query_boundedness_no_n_plus_one(
        self, mock_history, mock_verify, mock_lock
    ):
        # 50 videos
        videos = {f"telegram:-100123:{i}" for i in range(50)}
        synced_files = [(vid, f"chan/video_{i}.strm") for i, vid in enumerate(sorted(videos))]
        video_sources = [(vid, f"{vid}:src", "telegram", "-100123", 1) for vid in sorted(videos)]

        data = {
            "videos": videos,
            "synced_files": synced_files,
            "video_sources": video_sources,
        }
        conn = FakeConnection(data)
        services = FakeServices()

        remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-batch", services, self._record_progress
        )

        select_queries = [sql for sql, _ in conn.sql_log if sql.startswith("SELECT")]
        # Discovery should have bounded count of SELECT queries:
        # 1: pg_tables
        # 2: direct_rows (videos)
        # 3: target_edges (video_sources)
        # 4: existing_rows (SELECT id FROM videos WHERE id = ANY)
        # 5: source_rows (SELECT video_id, source_id, ... FROM video_sources WHERE video_id = ANY)
        # 6: file_rows (SELECT video_id, file_path FROM synced_files WHERE video_id = ANY)
        # Total SELECT queries should be <= 10 (certainly NOT 50 or 100 queries!)
        self.assertLessEqual(len(select_queries), 10, f"Expected bounded SELECT queries, got {len(select_queries)}")

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_w6_partial_failure_reports_accurate_counts(
        self, mock_history, mock_verify, mock_lock
    ):
        v1 = "telegram:-100123:1"
        v2 = "telegram:-100123:2"
        p1 = "chan/vid1.strm"
        p2 = "chan/vid2.strm"

        full_p1 = self.media_dir / p1
        full_p1.parent.mkdir(parents=True, exist_ok=True)
        full_p1.write_text("v1", encoding="utf-8")

        data = {
            "videos": {v1, v2},
            "synced_files": [(v1, p1), (v2, p2)],
            "video_sources": [
                (v1, f"{v1}:src", "telegram", "-100123", 1),
                (v2, f"{v2}:src", "telegram", "-100123", 1),
            ],
        }
        conn = FakeConnection(data)

        # Service where p2 fails during unlinking with OSError
        class FailingServices(FakeServices):
            def resolve_media_path(self, stored_path: str, media_dir: Path) -> Path:
                path = super().resolve_media_path(stored_path, media_dir)
                if stored_path == p2:
                    # Mock candidate to raise OSError on unlink
                    class FailingPath:
                        def with_suffix(self, sfx):
                            return self
                        def exists(self):
                            return True
                        def is_symlink(self):
                            return False
                        def unlink(self):
                            raise OSError("Disk I/O failure")
                    return FailingPath()
                return path

        services = FailingServices()

        with self.assertRaises(RuntimeError) as ctx:
            remove_telegram_channel(
                conn, self.media_dir, "-100123", "job-partial", services, self._record_progress
            )

        err_msg = str(ctx.exception)
        self.assertIn("catalog cleanup stopped after deleting 1 videos and 1 files", err_msg)
        # v1 was deleted, v2 was not
        self.assertNotIn(v1, conn.data["videos"])
        self.assertIn(v2, conn.data["videos"])

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_idempotent_removal_when_empty(
        self, mock_history, mock_verify, mock_lock
    ):
        conn = FakeConnection({})
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-empty", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 0)
        self.assertEqual(res["files_deleted"], 0)
        self.assertEqual(res["synced_files_deleted"], 0)
        self.assertEqual(res["shared_videos_preserved"], 0)
        self.assertEqual(res["local_state"], "source_removed")

    def test_remove_sync_history_filtering(self):
        history_file = self.root / "sync_history.json"
        content = {
            "runs": [
                {
                    "run_id": 1,
                    "source_filter": "telegram:-100123",
                    "sources": [{"source": "telegram:-100123", "imported": 5}],
                },
                {
                    "run_id": 2,
                    "source_filter": None,
                    "sources": [
                        {"source": "telegram:-100123", "imported": 2},
                        {"source": "telegram:-100999", "imported": 10},
                    ],
                },
                {
                    "run_id": 3,
                    "source_filter": "telegram:-100999",
                    "sources": [{"source": "telegram:-100999", "imported": 12}],
                },
            ]
        }
        history_file.write_text(json.dumps(content), encoding="utf-8")

        with patch.dict(os.environ, {"SYNC_HISTORY_FILE": str(history_file)}):
            runs_removed, rows_removed = _remove_sync_history("123")  # Alias without -100

        self.assertEqual(runs_removed, 1)  # run 1 removed
        self.assertEqual(rows_removed, 1)  # row in run 2 removed

        updated = json.loads(history_file.read_text(encoding="utf-8"))
        self.assertEqual(len(updated["runs"]), 2)
        self.assertEqual(updated["runs"][0]["run_id"], 2)
        self.assertEqual(len(updated["runs"][0]["sources"]), 1)
        self.assertEqual(updated["runs"][0]["sources"][0]["source"], "telegram:-100999")
        self.assertEqual(updated["runs"][1]["run_id"], 3)

    def test_remove_sync_history_with_padded_filters_and_sources(self):
        """Verify counting semantics: runs_removed vs history_source_rows_removed.

        Counting semantics in _remove_sync_history:
        - When an entire run matches the channel (via source_filter), runs_removed is
          incremented and the run is pruned. Its internal sources are not counted in
          rows_removed.
        - When a multi-source run is retained, rows_removed counts the matching source
          rows pruned from that run.
        Therefore, in this test:
        - Run 1 (source_filter matching): 1 run removed (runs_removed=1).
        - Run 2 (source_filter None, 1 matching source): 1 source row removed (rows_removed=1).
        Total expected: runs_removed=1, rows_removed=1.
        """
        history_file = self.root / "sync_history_padded.json"
        content = {
            "runs": [
                {
                    "run_id": 1,
                    "source_filter": "   telegram:-100123   ",
                    "sources": [{"source": "   telegram: -100123   ", "imported": 5}],
                },
                {
                    "run_id": 2,
                    "source_filter": None,
                    "sources": [
                        {"source": " \t telegram:-100123 \n", "imported": 2},
                        {"source": "telegram:-100999", "imported": 10},
                    ],
                },
            ]
        }
        history_file.write_text(json.dumps(content), encoding="utf-8")

        with patch.dict(os.environ, {"SYNC_HISTORY_FILE": str(history_file)}):
            runs_removed, rows_removed = _remove_sync_history("  123  ")

        self.assertEqual(runs_removed, 1, "Run 1 must be counted as 1 removed run")
        self.assertEqual(
            rows_removed, 1,
            "Only the pruned source row from retained Run 2 is counted in rows_removed",
        )
        updated = json.loads(history_file.read_text(encoding="utf-8"))
        self.assertEqual(len(updated["runs"]), 1)
        self.assertEqual(updated["runs"][0]["run_id"], 2)
        self.assertEqual(len(updated["runs"][0]["sources"]), 1)
        self.assertEqual(updated["runs"][0]["sources"][0]["source"], "telegram:-100999")

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_release_channel_removal_lock")
    @patch.object(source_removal, "_verify_channel_not_configured")
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_channel_removal_escapes_sql_wildcards_and_preserves_unrelated_channels(
        self, mock_history, mock_verify, mock_rel_lock, mock_acq_lock
    ):
        """Wildcards _, %, and \\ in channel IDs must be escaped so other channels are not deleted."""
        target_cid = "@example_channel_contract"
        similar_cid = "@exampleXchannel_contract"
        percent_cid = "@example%channel_contract"
        slash_cid = "@example\\channel_contract"
        longer_cid = "@example_channel_contract_other"

        v_target = f"telegram:{target_cid}:1"
        v_similar = f"telegram:{similar_cid}:1"
        v_percent = f"telegram:{percent_cid}:1"
        v_slash = f"telegram:{slash_cid}:1"
        v_longer = f"telegram:{longer_cid}:1"

        data = {
            "videos": {v_target, v_similar, v_percent, v_slash, v_longer},
            "video_sources": [
                (v_target, f"telegram:{target_cid}:1", "telegram", target_cid, True),
                (v_similar, f"telegram:{similar_cid}:1", "telegram", similar_cid, True),
                (v_percent, f"telegram:{percent_cid}:1", "telegram", percent_cid, True),
                (v_slash, f"telegram:{slash_cid}:1", "telegram", slash_cid, True),
                (v_longer, f"telegram:{longer_cid}:1", "telegram", longer_cid, True),
            ],
            "synced_files": [],
        }
        conn = FakeConnection(data)
        services = FakeServices(self.root)
        progress = []

        def record_prog(*args, **kwargs):
            progress.append((args, kwargs))

        res = remove_telegram_channel(
            conn, self.media_dir, target_cid, "job-like-test", services, record_prog
        )

        self.assertEqual(res["videos_deleted"], 1)
        self.assertIn(v_target, conn.deleted["videos"])
        self.assertNotIn(v_similar, conn.deleted["videos"])
        self.assertNotIn(v_percent, conn.deleted["videos"])
        self.assertNotIn(v_slash, conn.deleted["videos"])
        self.assertNotIn(v_longer, conn.deleted["videos"])
        # All non-target videos remain in videos set
        self.assertEqual(
            conn.data["videos"],
            {v_similar, v_percent, v_slash, v_longer},
        )

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_release_channel_removal_lock")
    @patch.object(source_removal, "_verify_channel_not_configured")
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_channel_removal_removes_percent_and_backslash_channels_accurately(
        self, mock_history, mock_verify, mock_rel_lock, mock_acq_lock
    ):
        percent_cid = "@example%channel_contract"
        slash_cid = "@example\\channel_contract"
        v_percent = f"telegram:{percent_cid}:1"
        v_slash = f"telegram:{slash_cid}:1"

        data = {
            "videos": {v_percent, v_slash},
            "video_sources": [
                (v_percent, f"telegram:{percent_cid}:1", "telegram", percent_cid, True),
                (v_slash, f"telegram:{slash_cid}:1", "telegram", slash_cid, True),
            ],
            "synced_files": [],
        }
        conn = FakeConnection(data)
        services = FakeServices(self.root)

        res = remove_telegram_channel(
            conn, self.media_dir, percent_cid, "job-pct", services, lambda *a, **k: None
        )

        self.assertEqual(res["videos_deleted"], 1)
        self.assertIn(v_percent, conn.deleted["videos"])
        self.assertNotIn(v_slash, conn.deleted["videos"])
        self.assertEqual(conn.data["videos"], {v_slash})

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_multiple_source_rows_and_aliases_preserved_when_alternate_active_remains(
        self, mock_history, mock_verify, mock_lock
    ):
        """Canonical video and alternate active/inactive sources remain when target channel is removed."""
        v_id = "telegram:-100123:video_shared"
        rel_path = "target/video_shared.strm"
        strm_file = self.media_dir / rel_path
        strm_file.parent.mkdir(parents=True, exist_ok=True)
        strm_file.write_text("http://stream-shared", encoding="utf-8")
        nfo_file = strm_file.with_suffix(".nfo")
        nfo_file.write_text("<movie/>", encoding="utf-8")
        jpg_file = strm_file.with_suffix(".jpg")
        jpg_file.write_bytes(b"\xff\xd8\xff")

        data = {
            "videos": {v_id},
            "synced_files": [(v_id, rel_path)],
            "video_sources": [
                (v_id, f"{v_id}:src1", "telegram", "-100123", 1),
                (v_id, "telegram:123:video_shared:src2", "telegram", "123", 0),
                (v_id, f"{v_id}:src3", "telegram", "-100123", 1),
                (v_id, "telegram:-100999:video_shared:alt_act", "telegram", "-100999", 1),
                (v_id, "telegram:-100999:video_shared:alt_inact", "telegram", "-100999", 0),
                (v_id, "exampleweb:shared_1", "exampleweb", None, 1),
            ],
            "telegram_channel_state": ["-100123", "-100999"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-multi-src", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 0)
        self.assertEqual(res["files_deleted"], 0)
        self.assertEqual(res["synced_files_deleted"], 0)
        self.assertEqual(res["shared_videos_preserved"], 1)

        # Canonical video and files remain intact
        self.assertIn(v_id, conn.data["videos"])
        self.assertTrue(strm_file.exists())
        self.assertTrue(nfo_file.exists())
        self.assertTrue(jpg_file.exists())
        self.assertEqual(conn.data["synced_files"], [(v_id, rel_path)])

        # Target channel source rows removed; alternate active, inactive, and other providers kept
        remaining_sids = {r[1] for r in conn.data["video_sources"]}
        self.assertNotIn(f"{v_id}:src1", remaining_sids)
        self.assertNotIn("telegram:123:video_shared:src2", remaining_sids)
        self.assertNotIn(f"{v_id}:src3", remaining_sids)
        self.assertIn("telegram:-100999:video_shared:alt_act", remaining_sids)
        self.assertIn("telegram:-100999:video_shared:alt_inact", remaining_sids)
        self.assertIn("exampleweb:shared_1", remaining_sids)

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_removal_by_owner_alias_preserves_canonical_video_and_alternate_sources(
        self, mock_history, mock_verify, mock_lock
    ):
        """Invoking removal with channel alias ('123') removes all aliases and keeps alternate sources."""
        v_id = "telegram:-100123:alias_shared"
        rel_path = "target/alias_shared.strm"
        strm_file = self.media_dir / rel_path
        strm_file.parent.mkdir(parents=True, exist_ok=True)
        strm_file.write_text("http://stream-alias", encoding="utf-8")

        data = {
            "videos": {v_id},
            "synced_files": [(v_id, rel_path)],
            "video_sources": [
                (v_id, f"{v_id}:1", "telegram", "-100123", 1),
                (v_id, "telegram:123:alias_shared:2", "telegram", "123", 1),
                (v_id, "telegram:-100888:alias_shared:alt_act", "telegram", "-100888", 1),
                (v_id, "telegram:-100888:alias_shared:alt_inact", "telegram", "-100888", 0),
            ],
            "telegram_channel_state": ["-100123", "-100888"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "123", "job-alias-rem", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 0)
        self.assertEqual(res["shared_videos_preserved"], 1)
        self.assertIn(v_id, conn.data["videos"])
        self.assertTrue(strm_file.exists())

        remaining_sids = {r[1] for r in conn.data["video_sources"]}
        self.assertNotIn(f"{v_id}:1", remaining_sids)
        self.assertNotIn("telegram:123:alias_shared:2", remaining_sids)
        self.assertIn("telegram:-100888:alias_shared:alt_act", remaining_sids)
        self.assertIn("telegram:-100888:alias_shared:alt_inact", remaining_sids)

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_canonical_video_deleted_when_final_playable_source_removed(
        self, mock_history, mock_verify, mock_lock
    ):
        """When only inactive alternate sources remain, final-source condition is met and video is deleted."""
        v_id = "telegram:-100123:final_v"
        rel_path = "target/final_v.strm"
        strm_file = self.media_dir / rel_path
        strm_file.parent.mkdir(parents=True, exist_ok=True)
        strm_file.write_text("http://stream-final", encoding="utf-8")
        nfo_file = strm_file.with_suffix(".nfo")
        nfo_file.write_text("<movie/>", encoding="utf-8")
        jpg_file = strm_file.with_suffix(".jpg")
        jpg_file.write_bytes(b"\xff\xd8\xff")
        fail_file = strm_file.with_suffix(".thumb_fail")
        fail_file.write_text("fail", encoding="utf-8")

        data = {
            "videos": {v_id},
            "synced_files": [(v_id, rel_path)],
            "video_sources": [
                (v_id, f"{v_id}:src", "telegram", "-100123", 1),
                (v_id, "telegram:123:final_v:old", "telegram", "123", 0),
                (v_id, "telegram:-100999:final_v:dead", "telegram", "-100999", 0),
            ],
            "telegram_channel_state": ["-100123"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-final-src", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 1)
        self.assertEqual(res["files_deleted"], 4)
        self.assertEqual(res["synced_files_deleted"], 1)
        self.assertEqual(res["shared_videos_preserved"], 0)

        # Video and managed files deleted
        self.assertNotIn(v_id, conn.data["videos"])
        self.assertFalse(strm_file.exists())
        self.assertFalse(nfo_file.exists())
        self.assertFalse(jpg_file.exists())
        self.assertFalse(fail_file.exists())
        self.assertEqual(conn.data["synced_files"], [])
        self.assertEqual([r for r in conn.data["video_sources"] if r[0] == v_id], [])

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_canonical_owned_by_alternate_channel_retained_and_target_sources_removed(
        self, mock_history, mock_verify, mock_lock
    ):
        """Canonical video belonging to another channel retains its canonical video while target sources are deleted."""
        v_keeper = "telegram:-100999:keeper"
        rel_path = "target/keeper.strm"
        strm_file = self.media_dir / rel_path
        strm_file.parent.mkdir(parents=True, exist_ok=True)
        strm_file.write_text("http://stream-keeper", encoding="utf-8")

        data = {
            "videos": {v_keeper},
            "synced_files": [(v_keeper, rel_path)],
            "video_sources": [
                (v_keeper, "telegram:-100999:keeper", "telegram", "-100999", 1),
                (v_keeper, "telegram:-100123:loser_1", "telegram", "-100123", 1),
                (v_keeper, "telegram:123:loser_2", "telegram", "123", 0),
            ],
            "telegram_channel_state": ["-100123", "-100999"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, "-100123", "job-keeper-test", services, self._record_progress
        )

        self.assertEqual(res["videos_deleted"], 0)
        self.assertEqual(res["shared_videos_preserved"], 1)
        self.assertIn(v_keeper, conn.data["videos"])
        self.assertTrue(strm_file.exists())

        remaining_sids = {r[1] for r in conn.data["video_sources"]}
        self.assertIn("telegram:-100999:keeper", remaining_sids)
        self.assertNotIn("telegram:-100123:loser_1", remaining_sids)
        self.assertNotIn("telegram:123:loser_2", remaining_sids)

    @patch.object(source_removal, "_acquire_channel_removal_lock", return_value=None)
    @patch.object(source_removal, "_verify_channel_not_configured", return_value=None)
    @patch.object(source_removal, "_remove_sync_history", return_value=(0, 0))
    def test_removal_with_padded_channel_id_and_padded_source_channel_id(
        self, mock_history, mock_verify, mock_lock
    ):
        """Padded raw channel ID is normalized consistently before all operations.

        Verifies:
        - Lock acquisition receives normalized ID.
        - Config verification receives normalized ID.
        - History removal receives normalized ID.
        - Source matching matches target despite padded channel_id in video_sources.
        - Channel-state cleanup receives normalized aliases and is executed.
        - Result dictionary contains normalized channel_id.
        """
        raw_input = "   -100123   "
        norm_cid = "-100123"
        v_target = f"telegram:{norm_cid}:1"
        rel_path = "chan/vid.strm"
        full_strm = self.media_dir / rel_path
        full_strm.parent.mkdir(parents=True, exist_ok=True)
        full_strm.write_text("stream", encoding="utf-8")

        data = {
            "videos": {v_target},
            "synced_files": [(v_target, rel_path)],
            "video_sources": [
                (v_target, f"{v_target}:src", "telegram", "  -100123  ", 1),
            ],
            "telegram_channel_state": ["-100123"],
        }
        conn = FakeConnection(data)
        services = FakeServices()

        res = remove_telegram_channel(
            conn, self.media_dir, raw_input, "job-padded", services, self._record_progress
        )

        # 1. Calls must observe normalized channel ID, not raw padded string
        mock_lock.assert_called_once_with(norm_cid)
        mock_verify.assert_called_once_with(norm_cid)
        mock_history.assert_called_once_with(norm_cid)

        # 2. Deletions and result verification
        self.assertEqual(res["channel_id"], norm_cid)
        self.assertEqual(res["videos_deleted"], 1)
        self.assertNotIn(v_target, conn.data["videos"])
        self.assertEqual(conn.data["video_sources"], [])

        # 3. Channel state cleanup assertion
        self.assertEqual(conn.data["telegram_channel_state"], [])
        state_deletes = [
            (sql, params) for sql, params in conn.sql_log
            if "DELETE FROM telegram_channel_state" in sql
        ]
        self.assertEqual(len(state_deletes), 1)
        self.assertEqual(state_deletes[0][1], (sorted(_channel_ids(norm_cid)),))

    def test_sql_like_escaping_against_real_sqlite_engine(self):
        """Execute the exact generated SQL query against a real SQL database."""
        import sqlite3
        sconn = sqlite3.connect(":memory:")
        try:
            sconn.execute("CREATE TABLE videos (id TEXT, provider TEXT)")
            rows = [
                ("telegram:@example_channel_contract:100", "telegram"),
                ("telegram:@exampleXchannel_contract:100", "telegram"),
                ("telegram:@example%channel_contract:100", "telegram"),
                ("telegram:@example\\channel_contract:100", "telegram"),
                ("telegram:@example_channel_contract_extra:100", "telegram"),
            ]
            sconn.executemany("INSERT INTO videos VALUES (?, ?)", rows)

            channel_id = "@example_channel_contract"
            aliases = sorted(source_removal._channel_ids(channel_id))
            prefixes = [f"telegram:{value}:" for value in aliases]
            conditions = ["id LIKE ? ESCAPE '\\'" for _ in prefixes]
            params = [source_removal._escape_like_prefix(p) for p in prefixes]

            query = "SELECT id FROM videos WHERE provider='telegram' AND (" + " OR ".join(conditions) + ")"
            matched = [r[0] for r in sconn.execute(query, params).fetchall()]

            self.assertEqual(matched, ["telegram:@example_channel_contract:100"])

            # Regression assertion against real SQLite test path for padded channel_id values (Finding 2)
            padded_channel_id = "   @example_channel_contract   "
            padded_aliases = sorted(source_removal._channel_ids(padded_channel_id))
            padded_prefixes = [f"telegram:{value}:" for value in padded_aliases]
            padded_conditions = ["id LIKE ? ESCAPE '\\'" for _ in padded_prefixes]
            padded_params = [source_removal._escape_like_prefix(p) for p in padded_prefixes]

            padded_query = "SELECT id FROM videos WHERE provider='telegram' AND (" + " OR ".join(padded_conditions) + ")"
            padded_matched = [r[0] for r in sconn.execute(padded_query, padded_params).fetchall()]

            self.assertEqual(padded_matched, ["telegram:@example_channel_contract:100"])
        finally:
            sconn.close()


if __name__ == "__main__":
    unittest.main()
