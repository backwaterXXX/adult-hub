"""Unit tests for fuzzy_performers matching, scoring, reporting and merging."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pg_helper import PgTestCase, TEST_URL

from common.fuzzy_performers import (
    strip_accents,
    normalize_clean_key,
    extract_suffix_stem,
    score_canonical,
    find_fuzzy_performer_candidates,
    execute_performer_merges,
    preview_performer_merge,
)


class TestFuzzyPerformersNormalization(unittest.TestCase):
    def test_strip_accents(self):
        self.assertEqual(strip_accents("Artyóm Ivanov"), "Artyom Ivanov")
        self.assertEqual(strip_accents("Exequiel Rodríguez Balut"), "Exequiel Rodriguez Balut")
        self.assertEqual(strip_accents("René"), "Rene")
        self.assertEqual(strip_accents("François"), "Francois")
        self.assertEqual(strip_accents(""), "")

    def test_normalize_clean_key(self):
        self.assertEqual(normalize_clean_key("Artyóm Ivanov"), "artyomivanov")
        self.assertEqual(normalize_clean_key("Artyom Ivanov"), "artyomivanov")
        self.assertEqual(normalize_clean_key("Boston Jock"), "bostonjock")
        self.assertEqual(normalize_clean_key("BostonJock"), "bostonjock")
        self.assertEqual(normalize_clean_key("adam tyme"), "adamtyme")
        self.assertEqual(normalize_clean_key("Adam Tyme"), "adamtyme")

    def test_extract_suffix_stem(self):
        stem, sfx = extract_suffix_stem("Dillon Diaz VIP")
        self.assertEqual(stem, "dillondiaz")
        self.assertEqual(sfx, "VIP")

        stem, sfx = extract_suffix_stem("AfroBlackXXX")
        self.assertEqual(stem, "afroblack")
        self.assertEqual(sfx, "XXX")

        stem, sfx = extract_suffix_stem("Briankinney7")
        self.assertEqual(stem, "briankinney")
        self.assertEqual(sfx, "7")

        stem, sfx = extract_suffix_stem("HeightsHoundsXX")
        self.assertEqual(stem, "heightshounds")
        self.assertEqual(sfx, "XX")

        stem, sfx = extract_suffix_stem("John Doe")
        self.assertIsNone(stem)

    def test_score_canonical(self):
        # Human readable name with space vs handle
        s_space = score_canonical("Boston Jock", has_photo=False, has_bio=False, video_count=2)
        s_nospace = score_canonical("BostonJock", has_photo=False, has_bio=False, video_count=2)
        self.assertGreater(s_space, s_nospace)

        # Clean name vs name with trailing digits
        s_clean = score_canonical("Apollon", has_photo=False, has_bio=False, video_count=2)
        s_digits = score_canonical("Apollon31", has_photo=False, has_bio=False, video_count=5)
        self.assertGreater(s_clean, s_digits)

        # Clean name vs noise suffix XXX
        s_norm = score_canonical("Afro Black", has_photo=False, has_bio=False, video_count=1)
        s_xxx = score_canonical("AfroBlackXXX", has_photo=False, has_bio=False, video_count=2)
        self.assertGreater(s_norm, s_xxx)


class TestFuzzyPerformersDatabase(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        from common.job_queue import ALL_ACTIONS
        from common.worker_capabilities import publish_worker_capabilities
        try:
            from resolver import admin
        except ImportError:
            import resolver.admin as admin
        worker_path = Path(self.temp_dir.name) / "worker-capabilities.json"
        publish_worker_capabilities(worker_path, version="test-worker", actions=ALL_ACTIONS)
        capability_patch = patch.object(admin, "WORKER_CAPABILITIES_FILE", worker_path, create=True)
        capability_patch.start()
        self.addCleanup(capability_patch.stop)
        self.media_dir = Path(self.temp_dir.name) / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def test_find_fuzzy_candidates(self):
        # 1. Diacritics pair: Artyóm Ivanov vs Artyom Ivanov
        # 2. Spacing pair: Boston Jock vs BostonJock
        # 3. Suffix pair: Dillon Diaz VIP vs Dillon Diaz
        # 4. Similarity pair: Julian Cesar vs Jullian Cesar
        vids = [
            ("v1", ["Artyóm Ivanov", "Oliver Marks"]),
            ("v2", ["Artyom Ivanov"]),
            ("v3", ["Boston Jock"]),
            ("v4", ["BostonJock"]),
            ("v5", ["Dillon Diaz"]),
            ("v6", ["Dillon Diaz VIP"]),
            ("v7", ["Julian Cesar"]),
            ("v8", ["Jullian Cesar"]),
        ]
        with self.conn:
            for vid_id, perfs in vids:
                self.conn.execute(
                    "INSERT INTO videos (id, provider, title, performers) VALUES (%s, 'test', %s, %s)",
                    (vid_id, vid_id, json.dumps(perfs)),
                )

            # Add a photo for canonical Boston Jock
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES ('Boston Jock', 'http://img/bj.jpg', 123)"
            )

        report = find_fuzzy_performer_candidates(TEST_URL, similarity_threshold=0.88)
        self.assertTrue(report["ok"])
        groups = report["groups"]

        cat_map = {g["category"]: g for g in groups}
        self.assertIn("diacritics", cat_map)
        self.assertIn("spacing_case_punct", cat_map)
        self.assertIn("suffix_nickname", cat_map)
        self.assertIn("similarity", cat_map)

        # Check diacritics
        diag = cat_map["diacritics"]
        self.assertEqual(set([diag["canonical"]] + [v["name"] for v in diag["variants"]]), {"Artyóm Ivanov", "Artyom Ivanov"})
        self.assertEqual(diag["confidence_level"], "high")
        self.assertLess(diag["confidence"], 1.0)
        self.assertEqual(diag["canonical_video_examples"][0]["provider"], "test")
        self.assertEqual(diag["variants"][0]["video_examples"][0]["id"], "v2")

        # Check spacing
        spg = cat_map["spacing_case_punct"]
        self.assertEqual(spg["canonical"], "Boston Jock")
        self.assertEqual(spg["variants"][0]["name"], "BostonJock")

        # Check suffix
        suf = cat_map["suffix_nickname"]
        self.assertEqual(suf["canonical"], "Dillon Diaz")
        self.assertEqual(suf["variants"][0]["name"], "Dillon Diaz VIP")

        # Check similarity
        sim = cat_map["similarity"]
        names = {sim["canonical"], sim["variants"][0]["name"]}
        self.assertEqual(names, {"Julian Cesar", "Jullian Cesar"})

    def test_preview_performer_merge_reports_effect(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES ('v1', 'test', 'V1', %s)",
                (json.dumps(["BostonJock", "Someone Else"]),),
            )
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES ('v2', 'test', 'V2', %s)",
                (json.dumps(["Boston Jock"]),),
            )
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES ('BostonJock', 'http://img/a.jpg', 1)")
        preview = preview_performer_merge(TEST_URL, "BostonJock", "Boston Jock")
        self.assertTrue(preview["valid_canonical"])
        self.assertEqual(preview["alias_videos"], 1)
        self.assertEqual(preview["canonical_videos"], 1)
        self.assertEqual(preview["resolved_canonical"], "Boston Jock")
        self.assertTrue(preview["has_photo"])
        self.assertFalse(preview["canonical_has_photo"])
        self.assertTrue(any("photo" in warning.lower() for warning in preview["warnings"]))

    def test_preview_performer_merge_guards_and_unused_alias(self):
        identical = preview_performer_merge(TEST_URL, "Same Name", "Same Name")
        self.assertTrue(any("distinct" in warning for warning in identical["warnings"]))
        unused = preview_performer_merge(TEST_URL, "Nobody Here", "Canonical Name")
        self.assertEqual(unused["alias_videos"], 0)
        self.assertTrue(any("No videos" in warning for warning in unused["warnings"]))

    def test_execute_performer_merges(self):
        # Insert video with alias BostonJock and photo under BostonJock
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES ('test:v1', 'test', 'Test Vid', %s)",
                (json.dumps(["BostonJock", "Another Guy"]),),
            )
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES ('BostonJock', 'http://img/bj.jpg', 123)"
            )
            self.conn.execute(
                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES ('BostonJock', 'A bio', '1990', '[]', 123)"
            )
            self.conn.execute(
                "INSERT INTO jellyfin_person_sync (name, pbc_updated_at, format_version) VALUES ('bostonjock', 100, 1)"
            )

        merges = [("BostonJock", "Boston Jock")]
        try:
            res = execute_performer_merges(TEST_URL, self.media_dir, merges, skip_backup=True)
        except ImportError as exc:
            try:
                from test_support import optional_import_failed
            except ImportError:
                from tests.test_support import optional_import_failed
            optional_import_failed(exc)
            self.skipTest("sync module not available in this environment")
            return

        self.assertTrue(res["ok"])
        self.assertEqual(res["videos_updated"], 1)
        self.assertEqual(res["photos_migrated"], 1)
        self.assertEqual(res["bios_migrated"], 1)
        self.assertEqual(res["aliases_added"], 1)

        # Check updated videos table
        row = self.conn.execute("SELECT performers FROM videos WHERE id = 'test:v1'").fetchone()
        updated_perfs = json.loads(row[0])
        self.assertEqual(updated_perfs, ["Boston Jock", "Another Guy"])

        # Check performer_photos
        self.assertIsNone(self.conn.execute("SELECT thumb FROM performer_photos WHERE name = 'BostonJock'").fetchone())
        canon_photo = self.conn.execute("SELECT thumb FROM performer_photos WHERE name = 'Boston Jock'").fetchone()
        self.assertEqual(canon_photo[0], "http://img/bj.jpg")

        # Check performer_aliases
        alias_row = self.conn.execute("SELECT canonical FROM performer_aliases WHERE alias = 'bostonjock'").fetchone()
        self.assertEqual(alias_row[0], "Boston Jock")

        # Check jellyfin_person_sync
        self.assertIsNone(self.conn.execute("SELECT name FROM jellyfin_person_sync WHERE name = 'bostonjock'").fetchone())

        # A replay after a crash following the DB commit must still repair an NFO.
        nfo_path = self.media_dir / "test-v1.nfo"
        if not nfo_path.exists():
            nfo_path = next(self.media_dir.glob("*.nfo"), None)
        self.assertIsNotNone(nfo_path)
        nfo_path.unlink()
        replay = execute_performer_merges(TEST_URL, self.media_dir, merges, skip_backup=True)
        self.assertTrue(replay["ok"])
        self.assertEqual(replay["videos_updated"], 0)
        self.assertGreaterEqual(replay["nfos_updated"], 1)
        self.assertTrue(nfo_path.exists())

    def test_failed_merge_rolls_back_every_database_change(self):
        with self.conn:
            for vid, name in (("test:a1", "Alias One"), ("test:a2", "Alias Two")):
                self.conn.execute(
                    "INSERT INTO videos (id, provider, title, performers) VALUES (%s, 'test', %s, %s)",
                    (vid, vid, json.dumps([name])),
                )
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb, updated_at) "
                "VALUES ('Alias One', 'http://img/a.jpg', 1)")
            self.conn.execute(
                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) "
                "VALUES ('Alias One', 'A bio', '1990', '[]', 1)")

        merges = [("Alias One", "Canon Alpha"), ("Alias Two", "Canon Beta")]
        # Fail while the second alias mapping is written, after videos, photos
        # and bios for both merges were already changed in the transaction.
        with patch("common.fuzzy_performers.is_safe_alias",
                   side_effect=[True, RuntimeError("simulated crash")]):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                execute_performer_merges(TEST_URL, self.media_dir, merges, skip_backup=True)

        perfs = {
            row[0]: json.loads(row[1])
            for row in self.conn.execute("SELECT id, performers FROM videos ORDER BY id")
        }
        self.assertEqual(perfs, {"test:a1": ["Alias One"], "test:a2": ["Alias Two"]})
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM performer_aliases").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT thumb FROM performer_photos WHERE name = 'Alias One'").fetchone()[0],
            "http://img/a.jpg",
        )
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM performer_photos WHERE name = 'Canon Alpha'").fetchone())
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT name FROM performer_bios")],
            ["Alias One"],
        )

    def test_merge_keeps_canonical_bio_aliases_when_filling_missing_fields(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) "
                "VALUES ('Canon Name', '', '', %s, 1)",
                (json.dumps(["Canon Alias"]),),
            )
            self.conn.execute(
                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) "
                "VALUES ('Alias Name', 'Filled bio', '1980', %s, 2)",
                (json.dumps(["First Alias", "canon alias"]),),
            )

        res = execute_performer_merges(
            TEST_URL, self.media_dir, [("Alias Name", "Canon Name")], skip_backup=True)

        self.assertTrue(res["ok"])
        row = self.conn.execute(
            "SELECT bio, birth, aliases FROM performer_bios WHERE name = 'Canon Name'").fetchone()
        self.assertEqual(row[0], "Filled bio")
        self.assertEqual(row[1], "1980")
        aliases = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        self.assertEqual(aliases, ["Canon Alias", "First Alias"])
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM performer_bios WHERE name = 'Alias Name'").fetchone())



    def test_find_fuzzy_performer_candidates_ignores_editors_cut_and_fucked(self):
        # Insert records containing Editor's Cut.mp4, Editor‘s Cut.mp4, fucked, and Fucked
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES ('test:v_edit', 'test', 'Test Title', %s)",
                (json.dumps(["Editor's Cut.mp4", "Editor‘s Cut.mp4", "Fucked", "fucked", "Alex Fox"]),),
            )
            self.conn.execute("INSERT INTO performer_photos (name, thumb, updated_at) VALUES ('Editor’s Cut.mp4', '', 123)")
            self.conn.execute("INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES ('fucked', '', '', '[]', 123)")

        res = find_fuzzy_performer_candidates(TEST_URL)
        for grp in res.get("groups", []):
            self.assertNotIn("editor", grp["canonical"].lower())
            self.assertNotIn("fuck", grp["canonical"].lower())
            for var in grp.get("variants", []):
                self.assertNotIn("editor", var["name"].lower())
                self.assertNotIn("fuck", var["name"].lower())

    def test_already_aliased_performers_not_suggested(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES ('test:p1', 'test', 'Paddy 1', %s)",
                (json.dumps(["Paddy O'Brian"]),),
            )
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES ('test:p2', 'test', 'Paddy 2', %s)",
                (json.dumps(["Paddy O’Brian"]),),
            )

        res1 = find_fuzzy_performer_candidates(TEST_URL)
        paddy_groups1 = [g for g in res1.get("groups", []) if "paddy" in g["canonical"].lower()]
        self.assertEqual(len(paddy_groups1), 1)

        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_aliases (alias, canonical) VALUES (%s, %s) "
                "ON CONFLICT (alias) DO UPDATE SET canonical=EXCLUDED.canonical",
                ("paddy o’brian", "Paddy O'Brian"),
            )

        res2 = find_fuzzy_performer_candidates(TEST_URL)
        paddy_groups2 = [g for g in res2.get("groups", []) if "paddy" in g["canonical"].lower()]
        self.assertEqual(len(paddy_groups2), 0)

    def test_similarity_rejects_distinct_initials(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES (%s, %s, %s, %s)",
                ("t1", "p", "Vid 1", json.dumps(["Johnny"])),
            )
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES (%s, %s, %s, %s)",
                ("t2", "p", "Vid 2", json.dumps(["Johnny B"])),
            )
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, performers) VALUES (%s, %s, %s, %s)",
                ("t3", "p", "Vid 3", json.dumps(["Johnny V"])),
            )
        res = find_fuzzy_performer_candidates(TEST_URL, similarity_threshold=0.88)
        groups = res.get("groups", [])
        for g in groups:
            names = {g["canonical"].lower()} | {v["name"].lower() for v in g.get("variants", [])}
            self.assertFalse(
                {"johnny", "johnny b"}.issubset(names),
                f"Johnny B should not be merged into Johnny: {g}",
            )
            self.assertFalse(
                {"johnny", "johnny v"}.issubset(names),
                f"Johnny V should not be merged into Johnny: {g}",
            )
            self.assertFalse(
                {"johnny b", "johnny v"}.issubset(names),
                f"Johnny B and Johnny V should not be merged: {g}",
            )


if __name__ == "__main__":
    unittest.main()
