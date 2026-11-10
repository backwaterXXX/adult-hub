"""The duplicate merge tool: which pairs become clusters and what an apply writes."""

import contextlib
import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "sync" / "merge_dupes.py"

COLUMNS = ["group", "level", "shared", "provider", "title", "duration_s", "video_id"]
POLISH_COLUMNS = ["grupa", "level", "shared", "provider", "tytul", "czas_s", "video_id"]

KEEPER_FIELDS = ("provider", "title", "slug", "duration", "thumbnail", "performers",
                 "categories", "tags", "studio", "first_seen")

_UNSET = object()


def pair(*, group="1", level="STRONG", ids=("telegram:-100:1", "youtube:abc"),
         titles=(None, None), durations=("3600", "3600")):
    """One duplicate group of two rows, the only shape the merge tool understands."""
    words = tuple("Big Buck Bunny" if title is None else title for title in titles)
    return [
        {"group": group, "level": level, "shared": "Jane Doe",
         "provider": video_id.partition(":")[0], "title": title,
         "duration_s": duration, "video_id": video_id}
        for video_id, title, duration in zip(ids, words, durations)
    ]


def groups(count):
    """``count`` independent duplicate groups, used to reach the batched commit."""
    rows = []
    for index in range(count):
        rows += pair(group=str(index), ids=(f"telegram:-100:{index}", f"youtube:{index}"))
    return rows


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    """Answers the three library statements the tool runs and records the writes."""

    def __init__(self, ids=(), keepers=None, keeper_error=None):
        self.ids = list(ids)
        self.keepers = dict(keepers or {})
        self.keeper_error = keeper_error
        self.statements = []
        self.tag_writes = []
        self.commits = 0
        self.closed = False

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.statements.append((statement, params))
        if statement == "SELECT id FROM videos":
            return Cursor([(video_id,) for video_id in self.ids])
        if statement.startswith("SELECT provider, title, slug"):
            if self.keeper_error is not None:
                raise self.keeper_error
            row = self.keepers.get(params[0])
            return Cursor([row] if row is not None else [])
        if statement.startswith("UPDATE videos SET tags"):
            self.tag_writes.append((params[1], json.loads(params[0])))
        return Cursor([])

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class MergeHarness(unittest.TestCase):
    """One freshly executed copy of ``sync/merge_dupes.py`` per test, pointed at a temp media dir."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = tempfile.TemporaryDirectory(prefix="adult_hub_merge_dupes_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.out = io.StringIO()
        self.connection = FakeConnection()
        self.load()

    def load(self, *, db_url="postgresql://merge.invalid/adulthub", media_dir=None,
             blank_db_url=False, **stub_options):
        env = {"DATABASE_URL": "   " if blank_db_url else db_url,
               "MEDIA_DIR": str(media_dir if media_dir is not None else self.media)}
        self.stub = self.sync_stub(**stub_options)
        self.stack.enter_context(patch.dict(os.environ, env))
        package = types.ModuleType("sync")
        package.sync = self.stub
        self.stack.enter_context(patch.dict(sys.modules, {"sync": package, "sync.sync": self.stub}))
        path_before = list(sys.path)
        spec = importlib.util.spec_from_file_location("merge_dupes_under_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = path_before
        module.db = types.SimpleNamespace(connect=lambda _url: self.connection)
        self.module = module
        return module

    def sync_stub(self, *, purge_returns=1, nfo_error=None):
        """The pieces of the sync library an apply touches, every call recorded."""
        stub = types.ModuleType("sync.sync")
        stub.calls = []
        stub.init_db = lambda _url: self.connection
        stub.load_performer_thumbs = lambda _conn, performers: [f"thumb:{name}"
                                                                for name in performers]
        stub.format_dateadded = lambda moment: f"added@{moment}"
        stub.stable_media_basename = lambda video_id: video_id.replace(":", "_")

        def create_nfo_file(video, genres, tags, nfo_path):
            stub.calls.append(("nfo", video, sorted(tags), sorted(genres), Path(nfo_path)))
            if nfo_error is not None:
                raise nfo_error

        def denylist_ids(_conn, dropped, reason):
            stub.calls.append(("denylist", tuple(dropped), reason))

        def purge_videos(_conn, media_dir, dropped, reason, force=False):
            stub.calls.append(("purge", tuple(dropped), reason, force, Path(media_dir)))
            return purge_returns

        stub.create_nfo_file = create_nfo_file
        stub.denylist_ids = denylist_ids
        stub.purge_videos = purge_videos
        return stub

    def report(self, *rows, columns=COLUMNS, name="dupes_report.csv"):
        """Write a duplicate report in this layout and hand back its path."""
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def main(self, path, *argv):
        """Run the tool over a report, keeping its console output."""
        with contextlib.redirect_stdout(self.out):
            with patch.object(sys, "argv", ["merge_dupes.py", "--csv", str(path), *argv]):
                return self.module.main()

    def clusters(self, rows, overlap=0.35, **kwargs):
        return self.module.load_groups(self.report(*rows, **kwargs), overlap)

    def printed(self):
        return self.out.getvalue()

    def keeper_row(self, video_id, **overrides):
        fields = {
            "provider": video_id.partition(":")[0],
            "title": "Big Buck Bunny",
            "slug": "big-buck-bunny",
            "duration": 3_600,
            "thumbnail": "thumb.jpg",
            "performers": json.dumps(["Jane Doe"]),
            "categories": json.dumps(["Action"]),
            "tags": json.dumps(["Tag"]),
            "studio": "Studio",
            "first_seen": 1_767_225_600,
        }
        fields.update(overrides)
        return tuple(fields[name] for name in KEEPER_FIELDS)

    def calls_of(self, kind):
        return [call for call in self.stub.calls if call[0] == kind]

    def apply(self, rows, *, library=_UNSET, keepers=_UNSET, keeper_error=None, **stub_options):
        """Run the tool with ``--apply``; by default every video of the report is in the library."""
        self.load(**stub_options)
        ids = [row["video_id"] for row in rows] if library is _UNSET else list(library)
        if keepers is _UNSET:
            keepers = {video_id: self.keeper_row(video_id) for video_id in ids}
        self.connection = FakeConnection(ids=ids, keepers=keepers, keeper_error=keeper_error)
        return self.main(self.report(*rows), "--media", str(self.media), "--apply")


class TestStartupEnvironment(MergeHarness):
    def test_the_library_address_and_media_dir_are_read_at_import(self):
        module = self.load(db_url="postgresql://someone@db.internal/adulthub",
                           media_dir=self.root / "library")

        self.assertEqual(module.DB_URL, "postgresql://someone@db.internal/adulthub")
        self.assertEqual(Path(module.MEDIA_DIR), self.root / "library")

    def test_a_blank_library_address_is_not_an_address(self):
        module = self.load(blank_db_url=True)

        self.assertIsNone(module.DB_URL)

    def test_the_registered_source_is_kept_ahead_of_the_others(self):
        self.assertEqual(self.module.PROVIDER_RANK, {"telegram": 2})
        self.assertEqual(self.module.PROVIDER_DISPLAY, {"telegram": "Telegram"})


class TestTitleMatching(MergeHarness):
    def test_only_runs_of_three_letters_or_more_count(self):
        self.assertEqual(self.module.tokens("Big Buck 2024! Bunny"), {"big", "buck", "bunny"})

    def test_a_missing_title_has_no_tokens(self):
        self.assertEqual(self.module.tokens(None), set())

    def test_identical_titles_overlap_completely(self):
        self.assertEqual(self.module.title_overlap("Big Buck Bunny", "big buck BUNNY"), 1.0)

    def test_one_shared_word_is_counted_against_the_longer_title(self):
        self.assertAlmostEqual(self.module.title_overlap("buck", "big buck bunny"), 1 / 3)

    def test_an_empty_title_never_overlaps(self):
        self.assertEqual(self.module.title_overlap("", "buck"), 0.0)


class TestClusters(MergeHarness):
    def test_a_strong_pair_sharing_words_becomes_one_cluster(self):
        self.assertEqual(self.clusters(pair()), [["telegram:-100:1", "youtube:abc"]])

    def test_a_strong_pair_sharing_no_words_is_rejected(self):
        self.assertEqual(self.clusters(pair(titles=(None, "Completely Different Film Name"))), [])

    def test_a_strong_pair_keeps_the_floor_of_a_third_even_when_asked_higher(self):
        rows = pair(titles=("buck bunny", "buck bunny sequel trailer"))

        self.assertEqual(self.clusters(rows, overlap=0.9), [["telegram:-100:1", "youtube:abc"]])
        self.assertEqual(self.clusters(pair(titles=(None, "Completely Different Film Name")),
                                       overlap=0.9), [])

    def test_a_weak_pair_of_similar_durations_becomes_one_cluster(self):
        self.assertEqual(self.clusters(pair(level="WEAK")), [["telegram:-100:1", "youtube:abc"]])

    def test_a_weak_pair_far_apart_in_duration_is_rejected(self):
        rows = pair(level="WEAK", durations=("3600", "3781"))

        self.assertEqual(self.clusters(rows, overlap=0.3), [])

    def test_a_weak_pair_with_an_unknown_duration_is_accepted(self):
        rows = pair(level="WEAK", durations=("0", "3781"))

        self.assertEqual(self.clusters(rows), [["telegram:-100:1", "youtube:abc"]])

    def test_a_weak_pair_needs_the_full_configured_overlap(self):
        rows = pair(level="WEAK", titles=("bunny other", "bunny other thing here"))

        self.assertEqual(self.clusters(rows, overlap=0.5), [["telegram:-100:1", "youtube:abc"]])
        self.assertEqual(self.clusters(rows, overlap=0.75), [])

    def test_a_group_of_one_row_is_not_a_cluster(self):
        self.assertEqual(self.clusters(pair()[:1]), [])

    def test_a_group_of_three_rows_is_left_to_the_pairing_tool(self):
        rows = pair() + [{"group": "1", "level": "STRONG", "shared": "Jane Doe",
                          "provider": "pctv", "title": "Big Buck Bunny",
                          "duration_s": "3600", "video_id": "pctv:xyz"}]

        self.assertEqual(self.clusters(rows), [])

    def test_overlapping_pairs_chain_into_one_cluster(self):
        rows = pair() + pair(group="2", ids=("youtube:abc", "pctv:xyz"))

        self.assertEqual(self.clusters(rows), [["pctv:xyz", "telegram:-100:1", "youtube:abc"]])

    def test_the_polish_column_names_are_understood(self):
        rows = [dict(zip(POLISH_COLUMNS, values)) for values in (
            ("1", "STRONG", "Jane Doe", "telegram", "Big Buck Bunny", "3600", "telegram:-100:1"),
            ("1", "STRONG", "Jane Doe", "youtube", "Big Buck Bunny", "3600", "youtube:abc"),
        )]

        self.assertEqual(self.clusters(rows, columns=POLISH_COLUMNS),
                         [["telegram:-100:1", "youtube:abc"]])

    def test_a_missing_duration_column_counts_as_unknown(self):
        columns = [name for name in COLUMNS if name != "duration_s"]
        rows = [{key: value for key, value in row.items() if key != "duration_s"}
                for row in pair(level="WEAK", durations=("3600", "9999"))]

        self.assertEqual(self.clusters(rows, columns=columns, name="no_duration.csv"),
                         [["telegram:-100:1", "youtube:abc"]])

    def test_two_groups_in_one_report_stay_two_clusters(self):
        rows = pair() + pair(group="2", ids=("telegram:-100:2", "youtube:def"))

        self.assertEqual(self.clusters(rows), [["telegram:-100:1", "youtube:abc"],
                                               ["telegram:-100:2", "youtube:def"]])


class TestReportPlanning(MergeHarness):
    def test_a_dry_run_prints_the_plan_and_writes_nothing(self):
        path = self.report(*pair())
        self.connection = FakeConnection(ids=["telegram:-100:1", "youtube:abc"])

        code = self.main(path)

        self.assertEqual(code, 0)
        self.assertIn("Clusters to merge: 1 (1 videos to remove)", self.printed())
        self.assertIn("  KEEP [telegram] telegram:-100:1", self.printed())
        self.assertIn("    DROP [youtube] youtube:abc", self.printed())
        self.assertIn("Dry-run. Run with --apply INSIDE sync container.", self.printed())
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(self.connection.tag_writes, [])
        self.assertTrue(self.connection.closed)

    def test_the_title_overlap_limit_is_a_command_line_switch(self):
        asked = []
        self.module.load_groups = lambda path, overlap: asked.append(overlap) or []

        self.main(self.report(*pair()), "--title-overlap", "0.75")

        self.assertEqual(asked, [0.75])

    def test_a_known_source_is_kept_ahead_of_an_unknown_one(self):
        path = self.report(*pair(ids=("youtube:abc", "telegram:-100:1")))
        self.connection = FakeConnection(ids=["telegram:-100:1", "youtube:abc"])

        self.main(path)

        self.assertIn("  KEEP [telegram] telegram:-100:1", self.printed())
        self.assertIn("    DROP [youtube] youtube:abc", self.printed())

    def test_the_first_identifier_is_kept_within_a_rank(self):
        path = self.report(*pair(ids=("youtube:zzz", "youtube:aaa")))
        self.connection = FakeConnection(ids=["youtube:aaa", "youtube:zzz"])

        self.main(path)

        self.assertIn("  KEEP [youtube] youtube:aaa", self.printed())
        self.assertIn("    DROP [youtube] youtube:zzz", self.printed())

    def test_a_cluster_the_library_only_partly_holds_is_left_alone(self):
        path = self.report(*pair())
        self.connection = FakeConnection(ids=["telegram:-100:1"])

        code = self.main(path)

        self.assertEqual(code, 0)
        self.assertIn("Clusters to merge: 0 (0 videos to remove)", self.printed())

    def test_a_three_way_cluster_keeps_one_and_drops_two(self):
        rows = pair() + pair(group="2", ids=("youtube:abc", "pctv:xyz"))
        path = self.report(*rows)
        self.connection = FakeConnection(ids=["telegram:-100:1", "youtube:abc", "pctv:xyz"])

        self.main(path)

        self.assertIn("Clusters to merge: 1 (2 videos to remove)", self.printed())
        self.assertIn("  KEEP [telegram] telegram:-100:1", self.printed())
        self.assertIn("    DROP [pctv] pctv:xyz", self.printed())
        self.assertIn("    DROP [youtube] youtube:abc", self.printed())

    def test_a_missing_report_falls_back_to_the_temporary_copy(self):
        asked = []
        self.module.load_groups = lambda path, _overlap: asked.append(path) or []

        code = self.main(self.root / "gone.csv")

        self.assertEqual(code, 0)
        self.assertEqual(asked, [Path("/tmp/dupes.csv")])


class TestAppliedMerges(MergeHarness):
    def test_an_apply_tags_the_keeper_and_purges_the_rest(self):
        code = self.apply(pair())

        self.assertEqual(code, 0)
        nfo = self.calls_of("nfo")[0]
        self.assertEqual(nfo[2], ["Duplicate: youtube", "Tag"])
        self.assertEqual(nfo[4], self.media.resolve() / "telegram_-100_1.nfo")
        self.assertEqual(self.calls_of("denylist"),
                         [("denylist", ("youtube:abc",), "merged into telegram:-100:1")])
        self.assertEqual(self.calls_of("purge"),
                         [("purge", ("youtube:abc",), "merged into telegram:-100:1", True,
                           self.media.resolve())])
        self.assertIn("Removed: 1", self.printed())

    def test_a_dropped_known_source_is_named_by_its_display_name(self):
        self.apply(pair(ids=("telegram:-100:1", "telegram:-100:2")))

        self.assertEqual(self.calls_of("nfo")[0][2], ["Duplicate: Telegram", "Tag"])

    def test_the_keeper_tags_are_written_back_to_the_library(self):
        self.apply(pair())

        self.assertEqual(self.connection.tag_writes,
                         [("telegram:-100:1", ["Duplicate: youtube", "Tag"])])
        self.assertEqual(self.connection.commits, 1)
        self.assertTrue(self.connection.closed)

    def test_the_keeper_is_rebuilt_from_its_library_row(self):
        self.apply(pair())

        self.assertEqual(self.connection.statements[0], ("SELECT id FROM videos", None))
        self.assertTrue(any(statement.startswith("SELECT provider, title, slug")
                            for statement, _params in self.connection.statements))
        video = self.calls_of("nfo")[0][1]
        self.assertEqual(video, {"id": "telegram:-100:1", "provider": "telegram",
                                 "provider_video_id": "-100:1", "title": "Big Buck Bunny",
                                 "slug": "big-buck-bunny", "duration": 3_600,
                                 "thumbnail": "thumb.jpg", "performers": ["Jane Doe"],
                                 "performer_thumbs": ["thumb:Jane Doe"], "studio": "Studio",
                                 "dateadded": "added@1767225600"})
        self.assertEqual(self.calls_of("nfo")[0][3], ["Action"])

    def test_a_keeper_that_vanishes_between_plan_and_apply_is_skipped(self):
        code = self.apply(pair(), keepers={})

        self.assertEqual(code, 0)
        self.assertIn("SKIP telegram:-100:1: disappeared from DB in the meantime", self.printed())
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(self.connection.commits, 1)

    def test_a_keeper_query_that_fails_is_skipped_and_the_run_goes_on(self):
        code = self.apply(pair(), keeper_error=RuntimeError("library is locked"))

        self.assertEqual(code, 0)
        self.assertIn("SKIP telegram:-100:1: library is locked", self.printed())
        self.assertEqual(self.stub.calls, [])

    def test_a_sidecar_failure_skips_the_tag_write_but_still_merges(self):
        code = self.apply(pair(), nfo_error=RuntimeError("media is read-only"))

        self.assertEqual(code, 0)
        self.assertIn("TAG-FAIL telegram:-100:1: media is read-only", self.printed())
        self.assertEqual(self.connection.tag_writes, [])
        self.assertEqual(len(self.calls_of("denylist")), 1)
        self.assertEqual(len(self.calls_of("purge")), 1)
        self.assertIn("Removed: 1", self.printed())

    def test_malformed_library_json_falls_back_to_empty_collections(self):
        row = ("telegram", None, None, None, None, "not json", "also not", "{broken", None, None)
        self.apply(pair(), keepers={"telegram:-100:1": row})

        nfo = self.calls_of("nfo")[0]
        self.assertEqual(nfo[2], ["Duplicate: youtube"])
        self.assertEqual(nfo[3], [])
        video = nfo[1]
        self.assertEqual(video["performers"], [])
        self.assertEqual(video["performer_thumbs"], [])
        self.assertEqual(video["title"], "telegram:-100:1")
        self.assertEqual(video["slug"], "")
        self.assertEqual(video["duration"], 0)
        self.assertEqual(video["thumbnail"], "")
        self.assertEqual(video["studio"], "")
        stamped_from_a_real_clock = float(video["dateadded"].removeprefix("added@"))
        self.assertGreater(stamped_from_a_real_clock, 1_700_000_000)

    def test_the_purged_count_is_what_the_library_reports_back(self):
        self.apply(pair(), purge_returns=3)

        self.assertIn("Removed: 3", self.printed())

    def test_the_library_is_committed_every_fiftieth_cluster(self):
        code = self.apply(groups(50))

        self.assertEqual(code, 0)
        self.assertEqual(self.connection.commits, 2)
        self.assertEqual(len(self.calls_of("nfo")), 50)
        self.assertIn("Removed: 50", self.printed())

    def test_the_merge_happens_once_per_cluster(self):
        code = self.apply(pair() + pair(group="2", ids=("telegram:-100:2", "youtube:def")))

        self.assertEqual(code, 0)
        self.assertIn("Clusters to merge: 2 (2 videos to remove)", self.printed())
        self.assertEqual([call[1]["id"] for call in self.calls_of("nfo")],
                         ["telegram:-100:1", "telegram:-100:2"])


if __name__ == "__main__":
    unittest.main()
