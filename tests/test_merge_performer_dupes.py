"""Merging bracketed performer nicknames: catalog tables, aliases, and the CLI."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pg_helper import PgTestCase

# Import the implementation from the sync package, not the root compatibility wrapper.
import sync.merge_performer_dupes as merger

RAW = "Adam Snow (AdamSnowXXX)"
BASE = "Adam Snow"
NICK = "AdamSnowXXX"
DEFAULT = object()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class CatalogDouble:
    """Stand in for a deployment whose schema is missing a performer name table.

    The migrated test database always has every table, so a partial schema is
    reachable only through a connection double.
    """

    def __init__(self, performer_rows=(), names=None, tables=None):
        self.performer_rows = performer_rows
        self.names = names if names is not None else {}
        self.tables = tables if tables is not None else {"videos"}

    def execute(self, sql, params=None):
        if "SELECT performers FROM videos" in sql:
            return _Rows(self.performer_rows)
        if "pg_tables" in sql:
            return _Rows([(name,) for name in sorted(self.tables)])
        for table, rows in self.names.items():
            if f"FROM {table}" in sql:
                return _Rows([(name,) for name in rows])
        return _Rows([])


class MergeHarness(PgTestCase):
    """Shared fixtures and CLI harness; carries no tests of its own."""

    # --- fixtures ---------------------------------------------------------

    def add_video(self, comp_id, performers, *, categories=DEFAULT, tags=DEFAULT,
                  studio="Studio", first_seen=1700000000):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration, performers, categories,"
                " tags, studio, first_seen, thumbnail) VALUES (%s, 'telegram', %s, 600, %s, %s, %s, %s, %s, %s)",
                (comp_id, f"Title of {comp_id}", performers,
                 json.dumps(["Action"]) if categories is DEFAULT else categories,
                 json.dumps(["tag1"]) if tags is DEFAULT else tags,
                 studio, first_seen, "http://host/pic.jpg"))

    def add_photo(self, name, thumb="thumb-url"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, %s)",
                (name, thumb, 111))

    def add_bio(self, name, bio="bio text", birth="1990-01-01", aliases="[]", updated_at=222):
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES (%s, %s, %s, %s, %s)",
                (name, bio, birth, aliases, updated_at))

    def add_alias(self, alias, canonical="Someone Else"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_aliases (alias, canonical) VALUES (%s, %s)", (alias, canonical))

    def add_profile_override(self, name, fields=("bio",)):
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_profile_overrides (name, fields) VALUES (%s, %s::jsonb)",
                (name, json.dumps({field: {"value": "manual"} for field in fields})))

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def video_performers(self, comp_id):
        row = self.conn.execute("SELECT performers FROM videos WHERE id=%s", (comp_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def seed_full_merge(self):
        """One duplicate performer referenced from every catalog surface."""
        self.add_video("telegram:1", json.dumps([RAW, "Plain Name"]))
        self.add_photo(RAW)
        self.add_bio(RAW, bio="raw bio", birth="1991-02-02")

    # --- CLI harness ------------------------------------------------------

    def run_cli(self, *flags):
        """Run ``main()`` with a deterministic argv and captured stdout."""
        args = ["--db-url", self.db_url, *flags]
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["merge_performer_dupes.py", *args]), \
                redirect_stdout(buffer):
            code = merger.main()
        return code, buffer.getvalue()


class TestPerformerMappings(MergeHarness):
    def test_collects_bracketed_names_from_every_catalog_table(self):
        self.add_video("telegram:1", json.dumps([RAW, "Plain Name", ""]))
        self.add_photo("Joey Mills (JoeyMillsXXX)")
        self.add_bio("Boti Bee (Bee)")

        mapping = merger.find_performer_mappings(self.conn)

        self.assertEqual(mapping, {
            RAW: (BASE, NICK),
            "Joey Mills (JoeyMillsXXX)": ("Joey Mills", "JoeyMillsXXX"),
            "Boti Bee (Bee)": ("Boti Bee", "Bee"),
        })

    def test_unescapes_html_entities_and_trims_whitespace(self):
        self.add_video("telegram:2", json.dumps(["Kate &amp; Co (KC)"]))
        self.add_photo("Ana &amp; Co (Nicky &amp; Co)  ")
        self.add_photo("Bo  (  Deep Nick  )")

        mapping = merger.find_performer_mappings(self.conn)

        self.assertEqual(mapping["Kate &amp; Co (KC)"], ("Kate & Co", "KC"))
        self.assertEqual(mapping["Ana &amp; Co (Nicky &amp; Co)"], ("Ana & Co", "Nicky & Co"))
        self.assertEqual(mapping["Bo  (  Deep Nick  )"], ("Bo", "Deep Nick"))

    def test_requires_a_base_name_before_mapping(self):
        self.add_photo("(Nickname Only)")
        self.add_video("telegram:3", json.dumps(["(Only Nick)"]))

        self.assertEqual(merger.find_performer_mappings(self.conn), {})

    def test_reads_name_tables_only_when_the_schema_has_them(self):
        conn = CatalogDouble(
            performer_rows=[(json.dumps([RAW]),)],
            names={"performer_photos": ["Ignored Person (Nick)"],
                   "performer_bios": ["Also Ignored (Nick)"]},
            tables={"videos", "performer_photos"},
        )

        self.assertEqual(merger.find_performer_mappings(conn),
                         {RAW: (BASE, NICK), "Ignored Person (Nick)": ("Ignored Person", "Nick")})


class TestPlanVideoUpdates(MergeHarness):
    def test_replaces_bracketed_names_with_the_canonical_one(self):
        self.add_video("telegram:7", json.dumps([RAW, "Plain Name"]))

        updates = merger.plan_video_updates(self.conn, {RAW: (BASE, NICK)})

        self.assertEqual(updates, [("telegram:7", [RAW, "Plain Name"], [BASE, "Plain Name"])])

    def test_collapses_two_names_that_resolve_to_one_performer(self):
        self.add_video("telegram:8", json.dumps(["Ann (A)", "Ann"]))

        updates = merger.plan_video_updates(self.conn, {"Ann (A)": ("Ann", "A")})

        self.assertEqual(updates, [("telegram:8", ["Ann (A)", "Ann"], ["Ann"])])

    def test_drops_empty_performers_while_rewriting(self):
        self.add_video("telegram:9", json.dumps(["", RAW]))

        updates = merger.plan_video_updates(self.conn, {RAW: (BASE, NICK)})

        self.assertEqual(updates, [("telegram:9", ["", RAW], [BASE])])

    def test_reports_only_the_videos_that_change(self):
        self.add_video("telegram:12", json.dumps([RAW]))
        self.add_video("telegram:13", json.dumps(["Untouched"]))

        updates = merger.plan_video_updates(self.conn, {RAW: (BASE, NICK)})

        self.assertEqual([vid for vid, _, _ in updates], ["telegram:12"])


class TestPreMergeBackup(MergeHarness):
    def test_make_backup_uses_the_performer_merge_prefix(self):
        calls = []

        def fake_pg_backup(url, backup_dir, *, prefix):
            calls.append((url, backup_dir, prefix))
            return "/tmp/adulthub-pre-performer-merge-x.dump"

        with patch.object(merger.db, "pg_backup", fake_pg_backup):
            result = merger.make_backup("postgresql://user@host/db")
            merger.make_backup()

        self.assertEqual(result, Path("/tmp/adulthub-pre-performer-merge-x.dump"))
        self.assertEqual([call[2] for call in calls],
                         ["adulthub-pre-performer-merge", "adulthub-pre-performer-merge"])
        self.assertEqual(calls[0][0], "postgresql://user@host/db")
        self.assertEqual(calls[1][0], merger.DB_URL)


class TestMergeDryRun(MergeHarness):
    def test_dry_run_reports_the_plan_without_touching_anything(self):
        self.seed_full_merge()
        with patch.object(merger, "make_backup") as backup:
            code, output = self.run_cli()

        self.assertEqual(code, 0)
        self.assertIn(f"'{RAW}' -> '{BASE}'", output)
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])
        self.assertEqual(self.rows("SELECT name FROM performer_photos WHERE name=%s", (BASE,)), [])
        self.assertEqual(self.rows("SELECT alias FROM performer_aliases"), [])
        backup.assert_not_called()

    def test_dry_run_tolerates_manually_overridden_profiles(self):
        self.seed_full_merge()
        self.add_profile_override(BASE)

        code, _ = self.run_cli()

        self.assertEqual(code, 0)
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])

    def test_sample_listing_is_capped_at_twenty_mappings(self):
        for index in range(25):
            self.add_photo(f"Person {index} (Nick{index})")

        _, output = self.run_cli()

        self.assertEqual(sum(1 for line in output.splitlines() if "->" in line), 20)


class TestMergeApply(MergeHarness):
    def test_apply_rewrites_videos_photos_bios_and_aliases(self):
        self.seed_full_merge()
        with patch.object(merger, "make_backup") as backup:
            code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        backup.assert_not_called()
        self.assertEqual(self.video_performers("telegram:1"), [BASE, "Plain Name"])

        photos = {row[0]: row[1] for row in self.rows("SELECT name, thumb FROM performer_photos")}
        self.assertEqual(photos, {BASE: "thumb-url"})

        bios = {row[0]: row[1:] for row in self.rows(
            "SELECT name, bio, birth, aliases, updated_at FROM performer_bios")}
        self.assertEqual(bios, {BASE: ("raw bio", "1991-02-02", "[]", 222)})

        self.assertEqual(
            sorted((row[0], row[1]) for row in self.rows("SELECT alias, canonical FROM performer_aliases")),
            sorted([(RAW.lower(), BASE), (NICK.lower(), BASE)]))

    def test_apply_backs_up_before_touching_the_catalog(self):
        self.seed_full_merge()
        seen = {}

        def fake_backup(url):
            seen["performers"] = self.video_performers("telegram:1")
            seen["url"] = url
            return Path("/tmp/pre-merge.dump")

        with patch.object(merger, "make_backup", side_effect=fake_backup) as backup:
            code, _ = self.run_cli("--apply")

        self.assertEqual(code, 0)
        self.assertEqual(seen["performers"], [RAW, "Plain Name"])
        self.assertEqual(seen["url"], self.db_url)
        backup.assert_called_once_with(self.db_url)
        self.assertEqual(self.video_performers("telegram:1"), [BASE, "Plain Name"])

    def test_apply_aborts_when_the_backup_fails(self):
        self.seed_full_merge()
        with patch.object(merger, "make_backup", side_effect=RuntimeError("pg_dump missing")):
            code, _ = self.run_cli("--apply")

        self.assertEqual(code, 1)
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])
        self.assertEqual(self.rows("SELECT alias FROM performer_aliases"), [])

    def test_apply_refuses_to_merge_manually_overridden_profiles(self):
        self.seed_full_merge()
        self.add_profile_override(BASE, fields=("bio", "photo", "aliases"))
        with patch.object(merger, "make_backup") as backup:
            code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 1)
        backup.assert_not_called()
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])
        self.assertEqual(len(self.rows("SELECT name FROM performer_photos WHERE name=%s", (RAW,))), 1)

    def test_apply_keeps_an_existing_canonical_photo_and_still_prunes_the_raw_key(self):
        self.add_video("telegram:20", json.dumps([RAW]))
        self.add_photo(RAW, thumb="raw-thumb")
        self.add_photo(BASE, thumb="canonical-thumb")

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        photos = {row[0]: row[1] for row in self.rows("SELECT name, thumb FROM performer_photos")}
        self.assertEqual(photos, {BASE: "canonical-thumb"})

    def test_apply_merges_a_bio_without_discarding_canonical_fields(self):
        self.add_video("telegram:21", json.dumps([RAW]))
        self.add_bio(RAW, bio="raw bio", birth="1991-02-02")
        self.add_bio(BASE, bio="canonical bio", birth="")

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        # The canonical bio wins; only the field the canonical row lacks is filled in.
        bios = {row[0]: row[1:] for row in self.rows("SELECT name, bio, birth FROM performer_bios")}
        self.assertEqual(bios, {BASE: ("canonical bio", "1991-02-02")})
        self.assertEqual(self.rows("SELECT name FROM performer_bios WHERE name=%s", (RAW,)), [])

    def test_apply_records_one_alias_when_the_nick_matches_the_name(self):
        self.add_video("telegram:22", json.dumps(["Ann (ann)"]))
        self.add_alias("ann (ann)", "Existing Canonical")

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        # "ann" is the canonical name lower-cased, so no second alias is added,
        # and the pre-existing alias row survives ON CONFLICT DO NOTHING.
        self.assertEqual(
            sorted((row[0], row[1]) for row in self.rows("SELECT alias, canonical FROM performer_aliases")),
            [("ann (ann)", "Existing Canonical")])


if __name__ == "__main__":
    unittest.main()
