"""Telegram metadata must remain ingestible when its preview service is down."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nfo = load_module("nfo_ingestion", ROOT / "sync/nfo.py")
migration = load_module("telegram_nfo_migration", ROOT / "tools/migrate_telegram_nfo.py")


class TelegramNfoIngestionTests(unittest.TestCase):
    def generate(self, directory, provider="telegram", nfo_tags=None, **overrides):
        dest = Path(directory) / "film.nfo"
        nfo.create_nfo_file(
            {"provider": provider, "title": "Correct title", "provider_video_id": "42",
             "thumbnail": "https://example.test/admin/api/thumb?provider=telegram&ref=42",
             "performers": ["Example Actor"],
             "performer_thumbs": {"Example Actor": "https://example.test/actor.jpg"}, **overrides},
            set(), nfo_tags or set(), dest,
            get_provider_info_fn=lambda _: {"name": "Site", "domain": "example.test"},
            clean_display_title_fn=lambda title, **_: title,
            normalize_performers_fn=lambda names: names,
            map_categories_fn=lambda genres, **_: genres,
        )
        return ET.parse(dest).getroot()

    def test_telegram_has_no_remote_movie_thumb_and_preserves_local_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "film.jpg"
            image.write_bytes(b"local preview")
            root = self.generate(directory)
            self.assertIsNone(root.find("thumb"))
            self.assertEqual(root.findtext("lockdata"), "true")
            self.assertEqual(root.findtext("title"), "Correct title")
            self.assertEqual(root.findtext("actor/thumb"), "https://example.test/actor.jpg")
            self.assertEqual(image.read_bytes(), b"local preview")

    def test_other_provider_keeps_movie_thumb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.generate(directory, "other")
            self.assertIsNotNone(root.find("thumb"))
            self.assertIsNone(root.find("lockdata"))

    def test_dry_run_apply_backup_and_idempotence(self):
        original = b'''<?xml version="1.0"?><movie><title>Correct title</title>
          <uniqueid type="telegram">42</uniqueid>
          <thumb>https://example.test/admin/api/thumb?provider=telegram&amp;ref=42</thumb>
          <thumb>local.jpg</thumb><actor><thumb>https://example.test/actor.jpg</thumb></actor>
          <plot>Original description</plot></movie>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "film.nfo"
            path.write_bytes(original)
            with patch("builtins.print"):
                self.assertEqual(migration.migrate_directory(Path(directory)), (1, 0))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(Path(directory).glob("*.bak")), [])
                self.assertEqual(migration.migrate_directory(Path(directory), apply=True), (1, 0))
                self.assertEqual(migration.migrate_directory(Path(directory), apply=True), (0, 0))
            root = ET.parse(path).getroot()
            self.assertEqual(root.findtext("lockdata"), "true")
            self.assertEqual([node.text for node in root.findall("thumb")], ["local.jpg"])
            self.assertEqual(root.findtext("actor/thumb"), "https://example.test/actor.jpg")
            self.assertEqual(root.findtext("plot"), "Original description")
            self.assertEqual(path.with_suffix(".nfo.before-telegram-thumb.bak").read_bytes(), original)

    def test_migration_preserves_non_telegram_nfo(self):
        original = b'<movie><uniqueid type="other">42</uniqueid><thumb>https://example.test/admin/api/thumb?provider=telegram</thumb></movie>'
        self.assertIsNone(migration.migrated_xml(original))

    def test_migration_locks_telegram_without_remote_thumb(self):
        original = b'<movie><uniqueid type="telegram">42</uniqueid><lockdata>false</lockdata></movie>'
        replacement = migration.migrated_xml(original)
        self.assertEqual(ET.fromstring(replacement).findtext("lockdata"), "true")
        self.assertIsNone(migration.migrated_xml(replacement))

    def test_generator_preserves_migrated_bytes_and_mtime_for_same_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.generate(directory)
            root.remove(root.find("lockdata"))
            ET.SubElement(root, "thumb").text = "https://example.test/admin/api/thumb?provider=telegram&ref=42"
            path = Path(directory) / "film.nfo"
            path.write_bytes(migration.migrated_xml(ET.tostring(root)))
            before = path.read_bytes()
            before_mtime = path.stat().st_mtime_ns
            self.generate(directory)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_generator_writes_real_metadata_changes_after_migration(self):
        changes = (
            {"title": "Changed title"},
            {"nfo_tags": {"Changed tag"}},
            {"performers": ["Different Actor"]},
            {"performer_thumbs": {"Example Actor": "https://example.test/new-actor.jpg"}},
        )
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = self.generate(directory)
                root.remove(root.find("lockdata"))
                path = Path(directory) / "film.nfo"
                path.write_bytes(migration.migrated_xml(ET.tostring(root)))
                before = path.read_bytes()
                self.generate(directory, **change)
                self.assertNotEqual(path.read_bytes(), before)

    def test_metadata_comparison_preserves_repeated_element_order(self):
        first = ET.fromstring('<movie><tag>A</tag><tag>B</tag><actor><name>One</name></actor><actor><name>Two</name></actor></movie>')
        reordered_tags = ET.fromstring('<movie><tag>B</tag><tag>A</tag><actor><name>One</name></actor><actor><name>Two</name></actor></movie>')
        reordered_actors = ET.fromstring('<movie><tag>A</tag><tag>B</tag><actor><name>Two</name></actor><actor><name>One</name></actor></movie>')
        self.assertNotEqual(nfo._metadata_key(first, movie_root=True),
                            nfo._metadata_key(reordered_tags, movie_root=True))
        self.assertNotEqual(nfo._metadata_key(first, movie_root=True),
                            nfo._metadata_key(reordered_actors, movie_root=True))


if __name__ == "__main__":
    unittest.main()
