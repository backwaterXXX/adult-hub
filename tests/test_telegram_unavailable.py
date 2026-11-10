import json
import tempfile
import unittest
from pathlib import Path
import logging
import time

from sync.telegram_unavailable import _quarantine_files, _ref, _restore_files, reconcile_telegram_channel
from pg_helper import PgTestCase


class _FilesConn:
    def __init__(self, names):
        self.names = names

    def execute(self, _sql, _params):
        return self

    def fetchall(self):
        return [(name,) for name in self.names]


class TelegramUnavailableTests(unittest.TestCase):
    def test_parses_stable_telegram_ids(self):
        self.assertEqual(_ref("telegram:-100123:456:doc-id"), ("-100123", 456, "doc-id"))
        self.assertIsNone(_ref("telegram:bad:id"))

    def test_quarantine_moves_full_sidecar_family_and_restores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            media.mkdir()
            quarantine = root / "private-quarantine"
            for suffix in (".strm", ".nfo", ".jpg", ".thumb_fail"):
                (media / f"stable{suffix}").write_text(suffix, encoding="utf-8")

            moved = _quarantine_files(
                _FilesConn(["stable.strm"]), "telegram:-100123:456:doc", media, quarantine
            )

            self.assertEqual(len(moved), 4)
            self.assertFalse((media / "stable.strm").exists())
            self.assertTrue(all(Path(item["quarantine"]).exists() for item in moved))
            _restore_files(json.dumps(moved))
            self.assertEqual({p.suffix for p in media.iterdir()},
                             {".strm", ".nfo", ".jpg", ".thumb_fail"})
            self.assertTrue(all(not Path(item["quarantine"]).exists() for item in moved))

    def test_restore_keeps_regenerated_media_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "media.strm"
            archived = root / "quarantine.strm"
            source.write_text("new", encoding="utf-8")
            archived.write_text("old", encoding="utf-8")

            _restore_files([{"source": str(source), "quarantine": str(archived)}])

            self.assertEqual(source.read_text(encoding="utf-8"), "new")
            self.assertFalse(archived.exists())


class TelegramAvailabilityIntegrationTests(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name) / "media"
        self.media.mkdir()
        self.quarantine = Path(self.temp.name) / "quarantine"
        self.now = int(time.time())
        self.channel = "-100123"

    def seed(self, number, age):
        video = f"telegram:{self.channel}:{number}:doc{number}"
        name = f"{number}.strm"
        (self.media / name).write_text("stream")
        with self.conn:
            self.conn.execute("INSERT INTO videos(id,provider,last_seen,first_seen) VALUES (%s,'telegram',%s,%s)", (video, self.now-age, self.now-age))
            self.conn.execute("INSERT INTO synced_files(file_path,video_id) VALUES (%s,%s)", (name, video))
        return video

    def check(self, fetch, *, now=None, limit=20, current=None):
        return reconcile_telegram_channel(self.conn, self.channel, self.media, self.quarantine,
            "http://resolver", fetch, logging.getLogger(__name__), current_video_ids=current,
            limit=limit, now=self.now if now is None else now)

    def test_oldest_unseen_items_progress_across_bounded_runs(self):
        old = self.seed(1, 40 * 86400)
        next_old = self.seed(2, 39 * 86400)
        current = self.seed(3, 50 * 86400)
        calls = []
        def available(url, **kwargs):
            calls.append(url)
            return {"status": "available"}
        self.assertEqual(self.check(available, limit=1, current={current})["available"], 1)
        self.assertIn("/1?", calls[0])
        self.assertGreater(self.conn.execute("SELECT last_seen FROM videos WHERE id=%s", (old,)).fetchone()[0], self.now-1)
        self.check(available, limit=1, current={current})
        self.assertIn("/2?", calls[1])
        self.assertIsNotNone(next_old)

    def test_inconclusive_then_two_misses_quarantine_and_restore(self):
        video = self.seed(1, 40 * 86400)
        def fail(*args, **kwargs):
            raise TimeoutError("temporary")
        self.assertEqual(self.check(fail)["errors"], 1)
        self.assertEqual(self.conn.execute("SELECT miss_count FROM telegram_unavailable_quarantine WHERE video_id=%s", (video,)).fetchone()[0], 0)
        def missing(*args, **kwargs):
            return {"status": "missing"}
        self.assertEqual(self.check(missing, now=self.now+86400)["suspect"], 1)
        self.assertTrue((self.media / "1.strm").exists())
        self.assertEqual(self.check(missing, now=self.now+2*86400)["quarantined"], 1)
        self.assertFalse((self.media / "1.strm").exists())
        def available(*args, **kwargs):
            return {"status": "available"}
        self.assertEqual(self.check(available, now=self.now+3*86400)["restored"], 1)
        self.assertTrue((self.media / "1.strm").exists())
        self.assertIsNone(self.conn.execute("SELECT status FROM telegram_unavailable_quarantine WHERE video_id=%s", (video,)).fetchone())


if __name__ == "__main__":
    unittest.main()
