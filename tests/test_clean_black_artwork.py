import tempfile
import unittest
from pathlib import Path

from tools.clean_black_artwork import clean_nfo_thumbnail


class TestCleanBlackArtwork(unittest.TestCase):
    def test_clean_nfo_thumbnail_removes_root_thumb_preserves_actor_thumb(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            nfo = Path(tmp_dir) / "sample.nfo"
            nfo.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                "<movie>\n"
                "  <title>Sample Movie</title>\n"
                "  <thumb>https://example.com/bad_bumper.jpg</thumb>\n"
                "  <actor>\n"
                "    <name>Jane Doe</name>\n"
                "    <thumb>https://example.com/actor.jpg</thumb>\n"
                "  </actor>\n"
                "</movie>",
                encoding="utf-8",
            )

            changed = clean_nfo_thumbnail(nfo)
            self.assertTrue(changed)

            content = nfo.read_text(encoding="utf-8")
            self.assertNotIn("bad_bumper.jpg", content)
            self.assertIn("https://example.com/actor.jpg", content)
            self.assertIn("<title>Sample Movie</title>", content)

    def test_clean_nfo_thumbnail_no_root_thumb(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            nfo = Path(tmp_dir) / "sample.nfo"
            nfo.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                "<movie>\n"
                "  <title>Sample Movie</title>\n"
                "  <actor>\n"
                "    <name>Jane Doe</name>\n"
                "    <thumb>https://example.com/actor.jpg</thumb>\n"
                "  </actor>\n"
                "</movie>",
                encoding="utf-8",
            )

            changed = clean_nfo_thumbnail(nfo)
            self.assertFalse(changed)

    def test_clean_nfo_thumbnail_missing_file(self):
        self.assertFalse(clean_nfo_thumbnail(Path("/nonexistent/file.nfo")))


if __name__ == "__main__":
    unittest.main()
