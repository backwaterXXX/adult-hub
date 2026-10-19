import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sync.telegram_unavailable import _quarantine_files, _ref, _restore_files


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


if __name__ == "__main__":
    unittest.main()
