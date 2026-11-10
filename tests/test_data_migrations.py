import json
import tempfile
import unittest
from pathlib import Path


from pg_helper import PgTestCase
from sync.category_mapping import map_categories
from common.performers import prune_redundant_performers
from sync.data_migrations import (
    CATEGORY_MAPPING_VERSION,
    DISPLAY_TITLE_VERSION,
    REDUNDANT_PERFORMERS_VERSION,
    DataMigrationServices,
    _resolve_default_services,
    migrate_categories,
    migrate_display_titles,
    migrate_redundant_performers,
    migrate_synced_files,
)
from sync.media_files import resolve_media_path, stable_media_basename


class TestDataMigrations(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media = self.root / "media"
        self.media.mkdir()

        self.services = DataMigrationServices(
            logger=type("DummyLogger", (), {
                "debug": lambda *a, **k: None,
                "info": lambda *a, **k: None,
                "warning": lambda *a, **k: None,
                "error": lambda *a, **k: None,
            })(),
            video_is_disabled=lambda _vid: False,
            resolve_media_path=resolve_media_path,
            stable_media_basename=stable_media_basename,
            write_stream_file=lambda path, vid: path.write_text("http://stream", encoding="utf-8"),
            map_categories=map_categories,
            prune_redundant_performers=prune_redundant_performers,
            load_performer_thumbs=lambda conn, perfs: {},
            format_dateadded=lambda ts: "2026-01-01",
            create_nfo_file=lambda video, genres, tags, path, **_kwargs: path.write_text(f"<movie><title>{video['title']}</title></movie>", encoding="utf-8"),
        )

    def test_resolve_default_services(self):
        resolved = _resolve_default_services(None)
        self.assertIsNotNone(resolved.logger)
        self.assertTrue(callable(resolved.resolve_media_path))
        self.assertTrue(callable(resolved.stable_media_basename))
        self.assertTrue(callable(resolved.map_categories))
        self.assertTrue(callable(resolved.prune_redundant_performers))

    def test_migrate_synced_files_fast_path(self):
        stable_name = f"{stable_media_basename('test:123')}.strm"
        (self.media / stable_name).write_text("http://stream", encoding="utf-8")
        # Should return without reading DB
        migrate_synced_files(self.conn, self.media, services=self.services)

    def test_migrate_synced_files_renames_legacy_files(self):
        vid_id = "sample:video1"
        stable_base = stable_media_basename(vid_id)
        legacy_base = "Legacy Old Name"

        # Insert record into videos and synced_files
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title) VALUES (%s, %s, %s)",
                (vid_id, "sample", "Legacy Old Name"),
            )
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id, category, created_at) VALUES (%s, %s, %s, %s)",
                (f"{legacy_base}.strm", vid_id, "solo", 1700000000),
            )

        (self.media / f"{legacy_base}.strm").write_text("http://stream", encoding="utf-8")
        (self.media / f"{legacy_base}.nfo").write_text("<movie></movie>", encoding="utf-8")
        (self.media / f"{legacy_base}.jpg").write_bytes(b"image")

        migrate_synced_files(self.conn, self.media, services=self.services)

        self.assertFalse((self.media / f"{legacy_base}.strm").exists())
        self.assertFalse((self.media / f"{legacy_base}.nfo").exists())
        self.assertFalse((self.media / f"{legacy_base}.jpg").exists())

        self.assertTrue((self.media / f"{stable_base}.strm").exists())
        self.assertTrue((self.media / f"{stable_base}.nfo").exists())
        self.assertTrue((self.media / f"{stable_base}.jpg").exists())

        row = self.conn.execute("SELECT file_path FROM synced_files WHERE video_id = %s", (vid_id,)).fetchone()
        self.assertEqual(row[0], f"{stable_base}.strm")

    def test_migrate_categories_and_meta_tracking(self):
        vid_id = "sample:vid_cat"
        stable_base = stable_media_basename(vid_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, categories) VALUES (%s, %s, %s, %s)",
                (vid_id, "sample", "Muscle Stud", '["bareback", "solo"]'),
            )
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id, category, created_at) VALUES (%s, %s, %s, %s)",
                (f"{stable_base}.strm", vid_id, "bareback", 1700000000),
            )

        nfo_path = self.media / f"{stable_base}.nfo"
        nfo_path.write_text("<movie><title>Muscle Stud</title><genre>raw</genre><uniqueid>vid_cat</uniqueid></movie>", encoding="utf-8")

        migrate_categories(self.conn, self.media, services=self.services, category_mapping_version=CATEGORY_MAPPING_VERSION)

        meta_ver = self.conn.execute("SELECT value FROM meta WHERE key = 'category_mapping_version'").fetchone()
        self.assertEqual(meta_ver[0], str(CATEGORY_MAPPING_VERSION))

        # Check DB update
        v_row = self.conn.execute("SELECT categories FROM videos WHERE id = %s", (vid_id,)).fetchone()
        cats = json.loads(v_row[0])
        self.assertIn("Bareback & Breeding", cats)

        # Check junction table
        junction = [r[0] for r in self.conn.execute("SELECT category FROM video_categories WHERE video_id = %s", (vid_id,)).fetchall()]
        self.assertIn("Bareback & Breeding", junction)

        # Check NFO updated
        nfo_content = nfo_path.read_text(encoding="utf-8")
        self.assertIn("<genre>Muscle &amp; Jocks</genre>", nfo_content)

    def test_migrate_display_titles(self):
        vid_id = "sample:disp_title"
        stable_base = stable_media_basename(vid_id)
        with self.conn:
            self.conn.execute(
                """INSERT INTO videos (id, provider, title, slug, duration,
                                      performers, categories, tags, first_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (vid_id, "sample", "Raw Title", "raw-title", 600,
                 '["Performer A"]', '["Solo"]', '["tag1"]', 1700000000),
            )

        nfo_path = self.media / f"{stable_base}.nfo"
        nfo_path.write_text("<movie><title>Old Title</title></movie>", encoding="utf-8")

        updated = migrate_display_titles(self.conn, self.media, services=self.services, display_title_version=DISPLAY_TITLE_VERSION)
        self.assertEqual(updated, 1)

        # Running again should skip due to meta version
        second_run = migrate_display_titles(self.conn, self.media, services=self.services, display_title_version=DISPLAY_TITLE_VERSION)
        self.assertEqual(second_run, 0)

    def test_migrate_redundant_performers(self):
        vid_id = "sample:red_perf"
        stable_base = stable_media_basename(vid_id)
        # "Colombiano Alex Smith" is redundant when "Alex Smith" is present
        perfs = ["Colombiano Alex Smith", "Alex Smith"]
        with self.conn:
            self.conn.execute(
                """INSERT INTO videos (id, provider, title, slug, duration,
                                      performers, categories, tags, first_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (vid_id, "sample", "Alex Smith", "slug", 600,
                 json.dumps(perfs), '["Solo"]', '["tag1"]', 1700000000),
            )
            # Create performer_bios table if needed and insert stopword
            self.conn.execute("CREATE TABLE IF NOT EXISTS performer_bios (name TEXT PRIMARY KEY)")
            self.conn.execute("INSERT INTO performer_bios (name) VALUES ('fucking')")
            self.conn.execute("INSERT INTO performer_bios (name) VALUES ('black')")
            self.conn.execute("INSERT INTO performer_bios (name) VALUES ('Jordan')")

        nfo_path = self.media / f"{stable_base}.nfo"
        nfo_path.write_text("<movie><title>Test</title></movie>", encoding="utf-8")

        updated = migrate_redundant_performers(self.conn, self.media, services=self.services, redundant_performers_version=REDUNDANT_PERFORMERS_VERSION)
        self.assertEqual(updated, 1)

        # Stopword bio purged
        bio_row = self.conn.execute("SELECT 1 FROM performer_bios WHERE name = 'fucking'").fetchone()
        self.assertIsNone(bio_row)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM performer_bios WHERE name = 'black'").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM performer_bios WHERE name = 'Jordan'").fetchone())

        # Video performers pruned to just Alex Smith
        v_row = self.conn.execute("SELECT performers FROM videos WHERE id = %s", (vid_id,)).fetchone()
        self.assertEqual(json.loads(v_row[0]), ["Alex Smith"])

        # Running again should skip due to meta version
        self.assertEqual(migrate_redundant_performers(self.conn, self.media, services=self.services, redundant_performers_version=REDUNDANT_PERFORMERS_VERSION), 0)

    def test_break_alias_cycles_and_flatten(self):
        from sync.data_migrations import break_alias_cycles_and_flatten

        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS performer_aliases (alias TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
            # 2-cycle
            self.conn.execute("INSERT INTO performer_aliases (alias, canonical) VALUES ('apolo adri', 'Apolo Adrii') ON CONFLICT (alias) DO UPDATE SET canonical=EXCLUDED.canonical")
            self.conn.execute("INSERT INTO performer_aliases (alias, canonical) VALUES ('apolo adrii', 'Apolo Adri') ON CONFLICT (alias) DO UPDATE SET canonical=EXCLUDED.canonical")
            # chain: a -> b -> c
            self.conn.execute("INSERT INTO performer_aliases (alias, canonical) VALUES ('apolo a', 'Apolo Adri') ON CONFLICT (alias) DO UPDATE SET canonical=EXCLUDED.canonical")
            # self-reference
            self.conn.execute("INSERT INTO performer_aliases (alias, canonical) VALUES ('samuel', 'Samuel') ON CONFLICT (alias) DO UPDATE SET canonical=EXCLUDED.canonical")

        broken, flattened = break_alias_cycles_and_flatten(self.conn)
        self.assertGreaterEqual(broken, 1)

        # Self-reference gone
        self.assertIsNone(self.conn.execute("SELECT 1 FROM performer_aliases WHERE alias = 'samuel' AND canonical = 'Samuel'").fetchone())

        # Cycle broken: apolo adri -> Apolo Adrii exists, reverse deleted
        rows = dict(self.conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall())
        self.assertEqual(rows.get("apolo adri"), "Apolo Adrii")
        self.assertNotIn("apolo adrii", rows)
        # Chain flattened: apolo a points directly to Apolo Adrii
        self.assertEqual(rows.get("apolo a"), "Apolo Adrii")


if __name__ == "__main__":
    unittest.main()
