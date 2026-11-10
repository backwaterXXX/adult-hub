"""Resumable Telegram title cleanup and sidecar repair contracts."""

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


from sync.telegram_title_cleanup import cleanup_telegram_titles


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

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
    def _row(self, video_id, title, caption, nfo_name, *, overrides=None, scene_source=None):
        return (
            video_id, title, "slug", caption, "", [], [], [], scene_source,
            overrides or {}, nfo_name.replace(".nfo", ".strm"),
        )

    def test_repairs_titles_tags_genres_and_nfo_while_protecting_overrides(self):
        dirty = (
            "💪⛓️    ❗️❗️Gymrat KINK 🍆 💪⛓️    ❗️❗️Gymrat KINK 🍆 "
            "#ANAL #ASIA #OF #203"
        )
        rows = [
            self._row("telegram:one", dirty, dirty, "one.nfo"),
            self._row("telegram:manual", dirty, "#OF caption", "manual.nfo",
                      overrides={"title": "Chosen by operator"}),
            self._row("telegram:catalog", dirty, "#ASIA caption", "catalog.nfo",
                      scene_source="stashdb"),
        ]
        conn = FakeConnection(rows)
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir)
            for name in ("one.nfo", "manual.nfo", "catalog.nfo"):
                (media_dir / name).write_text(
                    "<movie><title>Old title</title><originaltitle>Source caption</originaltitle></movie>",
                    encoding="utf-8",
                )
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

            root = ET.parse(media_dir / "one.nfo").getroot()
            self.assertEqual(root.findtext("title"), "#203")
            self.assertEqual(root.findtext("originaltitle"), "Source caption")
            self.assertEqual({node.text for node in root.findall("genre")}, {
                "Amateur & OnlyFans", "Anal", "Asian & International",
            })
            self.assertEqual({node.text for node in root.findall("tag")}, {
                "#ANAL", "#ASIA", "#OF",
            })
            self.assertEqual(ET.parse(media_dir / "manual.nfo").getroot().findtext("title"),
                             "Old title")
            self.assertEqual(ET.parse(media_dir / "catalog.nfo").getroot().findtext("title"),
                             "Old title")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["processed"], 3)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "complete")

    def test_removes_channel_branding_from_stored_title_and_nfo(self):
        row = self._row(
            "telegram:channel-branding", "💪 GYMRAT Kink - Bareback with Alex",
            "", "channel-branding.nfo",
        )
        conn = FakeConnection([row])
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir)
            (media_dir / "channel-branding.nfo").write_text(
                "<movie><title>Old title</title></movie>", encoding="utf-8",
            )
            result = cleanup_telegram_titles(
                conn, media_dir, status_path=media_dir / "status.json",
            )
            update = next(call for call in conn.calls if call[0].startswith("UPDATE videos"))
            self.assertEqual(update[1][0], "Bareback with Alex")
            self.assertEqual(
                ET.parse(media_dir / "channel-branding.nfo").getroot().findtext("title"),
                "Bareback with Alex",
            )
            self.assertEqual(result["status"], "complete")

    def test_stop_leaves_cursor_ready_for_the_next_sync(self):
        row = self._row("telegram:one", "💪 Dirty #ANAL", "#ANAL caption", "one.nfo")
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


if __name__ == "__main__":
    unittest.main()
