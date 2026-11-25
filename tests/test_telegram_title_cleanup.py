"""Resumable Telegram title cleanup and sidecar repair contracts."""

import json
import tempfile
import unittest
from pathlib import Path


from unittest.mock import MagicMock, patch

from sync.telegram_title_cleanup import cleanup_telegram_titles


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None):
        sql = " ".join(query.split())
        self.calls.append((sql, params))
        if "SELECT count(*) FROM videos" in sql:
            return Cursor([(len(self.rows),)])
        if "SELECT v.id, v.title" in sql:
            return Cursor(self.rows)
        return Cursor([])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestTelegramTitleCleanup(unittest.TestCase):
    def _row(self, video_id, title, caption, *, overrides=None, scene_source=None):
        return (
            video_id, title, "slug", caption, "", [], [], [], scene_source,
            overrides or {},
        )

    def test_repairs_titles_tags_and_genres_while_protecting_overrides(self):
        dirty = (
            "💪⛓️    ❗️❗️Gymrat KINK 🍆 💪⛓️    ❗️❗️Gymrat KINK 🍆 "
            "#ANAL #ASIA #OF #203"
        )
        rows = [
            self._row("telegram:one", dirty, dirty),
            self._row("telegram:manual", dirty, "#OF caption",
                      overrides={"title": "Chosen by operator"}),
            self._row("telegram:catalog", dirty, "#ASIA caption",
                      scene_source="stashdb"),
        ]
        conn = FakeConnection(rows)
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir)
            state_path = Path(temp_dir) / "status.json"
            result = cleanup_telegram_titles(
                conn, media_dir, status_path=state_path,
            )

            update_calls = [call for call in conn.calls if call[0].startswith("UPDATE videos")]
            by_id = {params[3]: params for _sql, params in update_calls}
            self.assertEqual(by_id["telegram:one"][0], "#203")
            self.assertEqual(by_id["telegram:manual"][0], dirty)
            self.assertEqual(by_id["telegram:catalog"][0], dirty)
            self.assertEqual(json.loads(by_id["telegram:one"][1]), [
                "Amateur & OnlyFans", "Anal", "Asian & International",
            ])
            self.assertEqual(json.loads(by_id["telegram:one"][2]), [
                "#ANAL", "#ASIA", "#OF",
            ])

            self.assertEqual(by_id["telegram:manual"][0], dirty)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["processed"], 3)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "complete")

    def test_removes_channel_branding_from_stored_title(self):
        row = self._row(
            "telegram:channel-branding", "💪 GYMRAT Kink - Bareback with Alex", "",
        )
        conn = FakeConnection([row])
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir)
            result = cleanup_telegram_titles(
                conn, media_dir, status_path=media_dir / "status.json",
            )
            update = next(call for call in conn.calls if call[0].startswith("UPDATE videos"))
            self.assertEqual(update[1][0], "Bareback with Alex")
            self.assertEqual(result["status"], "complete")

    def test_stop_leaves_cursor_ready_for_the_next_sync(self):
        row = self._row("telegram:one", "💪 Dirty #ANAL", "#ANAL caption")
        conn = FakeConnection([row])
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir)
            state_path = Path(temp_dir) / "status.json"
            paused = cleanup_telegram_titles(
                conn, media_dir, status_path=state_path, stop_requested=lambda: True,
            )
            self.assertEqual(paused["status"], "pending")
            self.assertEqual(paused["processed"], 0)
            self.assertEqual(paused["cursor"], "")

            resumed = cleanup_telegram_titles(conn, media_dir, status_path=state_path)
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["processed"], 1)

    @patch("sync.telegram_title_cleanup.cleanup_telegram_titles", return_value={"status": "complete"})
    def test_action_records_the_cleanup_stage(self, mock_cleanup):
        from common.job_stages import WORKING_STAGES
        from sync.duplicate_job_external_handlers import _action_telegram_title_cleanup
        self.assertIn("cleaning_telegram_titles", WORKING_STAGES)
        conn = FakeConnection([])
        services = MagicMock()
        services.logger = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            result, committed = _action_telegram_title_cleanup(
                conn, "job-1", {}, Path(temp_dir), services,
            )
            self.assertEqual(result["local_state"], "telegram_title_cleanup_completed")
            self.assertFalse(committed)


if __name__ == "__main__":
    unittest.main()
