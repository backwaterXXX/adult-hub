"""Merging bracketed performer nicknames: catalog tables, aliases, disk sidecars, and the CLI."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pg_helper import PgTestCase

# Import the implementation from the sync package, not the root compatibility wrapper.
import sync.merge_performer_dupes as merger
from test_support import load_sync_module
sync_facade = load_sync_module()

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

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media_dir = Path(self.tmp.name) / "media"
        self.media_dir.mkdir()

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

    def add_person_sync(self, name):
        with self.conn:
            self.conn.execute(
                "INSERT INTO jellyfin_person_sync (name, pbc_updated_at) VALUES (%s, 333)", (name,))

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
        self.add_person_sync(RAW)

    # --- CLI harness ------------------------------------------------------

    def stub_sync(self, **overrides):
        stubs = {
            "stable_media_basename": lambda vid: vid.replace(":", "_"),
            "load_performer_thumbs": lambda conn, names: {},
            "format_dateadded": lambda ts: "2026-01-01 00:00:00",
            "create_nfo_file": MagicMock(),
        }
        stubs.update(overrides)
        for name, value in stubs.items():
            patcher = patch.object(sync_facade, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return SimpleNamespace(**stubs)

    def run_cli(self, *flags, jf_url="", jf_key="", env_file=None):
        """Run ``main()`` with a deterministic environment and argv.

        ``JELLYFIN_*`` are forced empty by default so a developer's real ``.env``
        cannot make the run call a live server, and ``__file__`` is redirected
        into a scratch directory so the ``.env`` fallback never reads the
        repository copy.
        """
        scratch = Path(self.tmp.name)
        if env_file is not None:
            (scratch / ".env").write_text(env_file, encoding="utf-8")
        args = ["--db-url", self.db_url, "--media", str(self.media_dir), *flags]
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["merge_performer_dupes.py", *args]), \
                patch.object(merger, "__file__", str(scratch / "merge_performer_dupes.py")), \
                patch.dict(os.environ, {"JELLYFIN_URL": jf_url, "JELLYFIN_API_KEY": jf_key}), \
                redirect_stdout(buffer):
            code = merger.main()
        return code, buffer.getvalue()


class TestPerformerMappings(MergeHarness):
    def test_collects_bracketed_names_from_every_catalog_table(self):
        self.add_video("telegram:1", json.dumps([RAW, "Plain Name", ""]))
        self.add_photo("Joey Mills (JoeyMillsXXX)")
        self.add_bio("Boti Bee (Bee)")
        self.add_person_sync("Zed (ZedNick)")

        mapping = merger.find_performer_mappings(self.conn)

        self.assertEqual(mapping, {
            RAW: (BASE, NICK),
            "Joey Mills (JoeyMillsXXX)": ("Joey Mills", "JoeyMillsXXX"),
            "Boti Bee (Bee)": ("Boti Bee", "Bee"),
            "Zed (ZedNick)": ("Zed", "ZedNick"),
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


class TestBackupAndJellyfinRefresh(MergeHarness):
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

    def test_jellyfin_refresh_requires_both_settings(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen") as urlopen:
            merger.trigger_jellyfin_refresh("", "key")
            merger.trigger_jellyfin_refresh("http://jf:8096", "")

        urlopen.assert_not_called()

    def test_jellyfin_refresh_posts_a_mediabrowser_token(self):
        import urllib.request
        captured = {}

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            merger.trigger_jellyfin_refresh("http://jf:8096/", "secret-key")

        request = captured["request"]
        self.assertEqual(request.full_url, "http://jf:8096/Library/Refresh")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), 'MediaBrowser Token="secret-key"')
        self.assertEqual(captured["timeout"], 10)

    def test_jellyfin_refresh_never_raises_at_the_caller(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen", side_effect=OSError("unreachable")):
            merger.trigger_jellyfin_refresh("http://jf:8096", "key")


class TestMergeDryRun(MergeHarness):
    def test_dry_run_reports_the_plan_without_touching_anything(self):
        self.seed_full_merge()
        sync = self.stub_sync()
        with patch.object(merger, "make_backup") as backup, \
                patch.object(merger, "trigger_jellyfin_refresh") as refresh:
            code, output = self.run_cli()

        self.assertEqual(code, 0)
        self.assertIn(f"'{RAW}' -> '{BASE}'", output)
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])
        self.assertEqual(self.rows("SELECT name FROM performer_photos WHERE name=%s", (BASE,)), [])
        self.assertEqual(self.rows("SELECT alias FROM performer_aliases"), [])
        self.assertEqual(len(self.rows("SELECT name FROM jellyfin_person_sync")), 1)
        backup.assert_not_called()
        refresh.assert_not_called()
        sync.create_nfo_file.assert_not_called()

    def test_dry_run_tolerates_manually_overridden_profiles(self):
        self.seed_full_merge()
        self.add_profile_override(BASE)
        self.stub_sync()

        code, _ = self.run_cli()

        self.assertEqual(code, 0)
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])

    def test_sample_listing_is_capped_at_twenty_mappings(self):
        for index in range(25):
            self.add_photo(f"Person {index} (Nick{index})")
        self.stub_sync()

        _, output = self.run_cli()

        self.assertEqual(sum(1 for line in output.splitlines() if "->" in line), 20)


class TestMergeApply(MergeHarness):
    def test_apply_rewrites_videos_photos_bios_aliases_and_nfo(self):
        self.seed_full_merge()
        sync = self.stub_sync()
        with patch.object(merger, "make_backup") as backup, \
                patch.object(merger, "trigger_jellyfin_refresh") as refresh:
            code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        backup.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(self.video_performers("telegram:1"), [BASE, "Plain Name"])

        photos = {row[0]: row[1] for row in self.rows("SELECT name, thumb FROM performer_photos")}
        self.assertEqual(photos, {BASE: "thumb-url"})

        bios = {row[0]: row[1:] for row in self.rows(
            "SELECT name, bio, birth, aliases, updated_at FROM performer_bios")}
        self.assertEqual(bios, {BASE: ("raw bio", "1991-02-02", "[]", 222)})

        self.assertEqual(
            sorted((row[0], row[1]) for row in self.rows("SELECT alias, canonical FROM performer_aliases")),
            sorted([(RAW.lower(), BASE), (NICK.lower(), BASE)]))
        self.assertEqual(self.rows("SELECT name FROM jellyfin_person_sync"), [])

        sync.create_nfo_file.assert_called_once()
        video_dict, genres, tags, nfo_path = sync.create_nfo_file.call_args[0]
        self.assertEqual(nfo_path, self.media_dir / "telegram_1.nfo")
        self.assertEqual(video_dict["performers"], [BASE, "Plain Name"])
        self.assertEqual(video_dict["id"], "telegram:1")
        self.assertEqual(video_dict["provider_video_id"], "1")
        self.assertEqual(video_dict["dateadded"], "2026-01-01 00:00:00")
        self.assertEqual(genres, {"Action"})
        self.assertEqual(tags, {"tag1"})

    def test_apply_defaults_missing_studio_and_first_seen(self):
        # categories/tags are NOT NULL jsonb arrays, so an empty library row is
        # the closest reachable case to "no metadata at all".
        self.add_video("telegram:30", json.dumps([RAW]), categories='[]', tags='[]',
                       studio=None, first_seen=None)
        date_inputs = []
        sync = self.stub_sync(format_dateadded=lambda ts: date_inputs.append(ts) or "2026-01-01 00:00:00")

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        video_dict, genres, tags, _ = sync.create_nfo_file.call_args[0]
        self.assertEqual((genres, tags), (set(), set()))
        self.assertEqual(video_dict["studio"], "")
        # first_seen is NULL, so the merge falls back to "now".
        self.assertIsInstance(date_inputs[0], float)
        self.assertGreater(date_inputs[0], 1_600_000_000)

    def test_apply_backs_up_before_touching_the_catalog(self):
        self.seed_full_merge()
        self.stub_sync()
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
        self.stub_sync()
        with patch.object(merger, "make_backup", side_effect=RuntimeError("pg_dump missing")):
            code, _ = self.run_cli("--apply")

        self.assertEqual(code, 1)
        self.assertEqual(self.video_performers("telegram:1"), [RAW, "Plain Name"])
        self.assertEqual(self.rows("SELECT alias FROM performer_aliases"), [])

    def test_apply_refuses_to_merge_manually_overridden_profiles(self):
        self.seed_full_merge()
        self.add_profile_override(BASE, fields=("bio", "photo", "aliases"))
        self.stub_sync()
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
        self.stub_sync()

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        photos = {row[0]: row[1] for row in self.rows("SELECT name, thumb FROM performer_photos")}
        self.assertEqual(photos, {BASE: "canonical-thumb"})

    def test_apply_merges_a_bio_without_discarding_canonical_fields(self):
        self.add_video("telegram:21", json.dumps([RAW]))
        self.add_bio(RAW, bio="raw bio", birth="1991-02-02")
        self.add_bio(BASE, bio="canonical bio", birth="")
        self.stub_sync()

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        # The canonical bio wins; only the field the canonical row lacks is filled in.
        bios = {row[0]: row[1:] for row in self.rows("SELECT name, bio, birth FROM performer_bios")}
        self.assertEqual(bios, {BASE: ("canonical bio", "1991-02-02")})
        self.assertEqual(self.rows("SELECT name FROM performer_bios WHERE name=%s", (RAW,)), [])

    def test_apply_records_one_alias_when_the_nick_matches_the_name(self):
        self.add_video("telegram:22", json.dumps(["Ann (ann)"]))
        self.add_alias("ann (ann)", "Existing Canonical")
        self.stub_sync()

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        # "ann" is the canonical name lower-cased, so no second alias is added,
        # and the pre-existing alias row survives ON CONFLICT DO NOTHING.
        self.assertEqual(
            sorted((row[0], row[1]) for row in self.rows("SELECT alias, canonical FROM performer_aliases")),
            [("ann (ann)", "Existing Canonical")])

    def test_apply_skips_videos_that_disappeared_before_the_nfo_pass(self):
        self.seed_full_merge()
        sync = self.stub_sync()
        with patch.object(merger, "plan_video_updates",
                          return_value=[("telegram:gone", [RAW], [BASE])]):
            code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        sync.create_nfo_file.assert_not_called()

    def test_apply_continues_when_one_nfo_rewrite_fails(self):
        self.add_video("telegram:24", json.dumps([RAW]))
        self.add_video("telegram:25", json.dumps([RAW]))
        failing = self.media_dir / "telegram_24.nfo"

        def create_nfo(video, genres, tags, path):
            if path == failing:
                raise OSError("disk full")

        sync = self.stub_sync(create_nfo_file=MagicMock(side_effect=create_nfo))

        code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        self.assertEqual(sync.create_nfo_file.call_count, 2)
        self.assertEqual(self.video_performers("telegram:24"), [BASE])
        self.assertEqual(self.video_performers("telegram:25"), [BASE])

    def test_apply_completes_when_the_sync_facade_cannot_be_imported(self):
        self.seed_full_merge()
        with patch.object(merger, "plan_video_updates",
                          return_value=[("telegram:1", [RAW, "Plain Name"], [BASE, "Plain Name"])]), \
                patch.dict(sys.modules, {"sync": None}):
            code, _ = self.run_cli("--apply", "--skip-backup")

        self.assertEqual(code, 0)
        self.assertEqual(self.video_performers("telegram:1"), [BASE, "Plain Name"])

    def test_apply_refreshes_jellyfin_from_the_environment(self):
        self.seed_full_merge()
        self.stub_sync()
        with patch.object(merger, "trigger_jellyfin_refresh") as refresh:
            code, _ = self.run_cli("--apply", "--skip-backup",
                                   jf_url="http://10.0.0.5:8096", jf_key="api-key")

        self.assertEqual(code, 0)
        refresh.assert_called_once_with("http://10.0.0.5:8096", "api-key")

    def test_apply_reads_jellyfin_credentials_from_the_env_file(self):
        self.seed_full_merge()
        self.stub_sync()
        env_file = 'OTHER=1\nJELLYFIN_URL="http://10.0.0.9:8096/"\nJELLYFIN_API_KEY=\'file-key\'\n'
        with patch.object(merger, "trigger_jellyfin_refresh") as refresh:
            code, _ = self.run_cli("--apply", "--skip-backup", env_file=env_file)

        self.assertEqual(code, 0)
        refresh.assert_called_once_with("http://10.0.0.9:8096/", "file-key")

    def test_apply_stays_quiet_when_no_jellyfin_target_is_configured(self):
        self.seed_full_merge()
        self.stub_sync()
        with patch.object(merger, "trigger_jellyfin_refresh") as refresh:
            code, _ = self.run_cli("--apply", "--skip-backup", env_file="OTHER=1\n")

        self.assertEqual(code, 0)
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
