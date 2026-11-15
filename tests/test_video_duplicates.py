"""Tests for duplicate candidate detection, ranking scores, rules, and dismissal indexing."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

from pg_helper import PgTestCase, TEST_URL
from common import db
from tests.helpers.db_session import db_session
import resolver.admin_v2_jobs as admin_v2_jobs
from resolver.video_duplicates import (
    find_duplicate_candidates,
    scan_duplicate_index,
    _score,
    _dismissals_for_group,
    _index_dismissals,
)
import resolver.video_duplicates as video_duplicates
import sync.video_duplicate_automation as video_duplicate_automation


class TestAutomaticDuplicateRules(unittest.TestCase):
    def test_split_part_identity_uses_original_filename_markers(self):
        first = {"source_filename": "Studio_scene_s1.mp4"}
        second = {"source_filename": "Studio_scene_s2.mp4"}
        self.assertEqual(video_duplicate_automation._part_identity(first), (True, 1))
        self.assertEqual(video_duplicate_automation._part_identity(second), (True, 2))

    def test_split_scene_provenance_without_number_is_ambiguous(self):
        row = {"scene_match_source": "stashdb:split_title_duration_v1",
               "source_filename": "Studio_scene.mp4", "title": "Same catalog title"}
        self.assertEqual(video_duplicate_automation._part_identity(row), (True, None))

    def test_multi_marker_part_identity_detects_conflicts(self):
        first = {"title": "Series Vol 2 Part 1"}
        second = {"title": "Series Vol 2 Part 2"}
        third = {"title": "Series Vol 2 Part 1"}
        self.assertEqual(video_duplicate_automation._part_identity(first), (True, (('part', 1), ('vol', 2))))
        self.assertEqual(video_duplicate_automation._part_identity(second), (True, (('part', 2), ('vol', 2))))
        self.assertNotEqual(video_duplicate_automation._part_identity(first), video_duplicate_automation._part_identity(second))
        self.assertEqual(video_duplicate_automation._part_identity(first), video_duplicate_automation._part_identity(third))

    def test_conflicting_part_numbers_within_row_is_ambiguous(self):
        row = {"source_filename": "Series_Part1.mp4", "title": "Series Part 2"}
        self.assertEqual(video_duplicate_automation._part_identity(row), (True, None))

    def test_duration_tolerance_is_bounded(self):
        self.assertTrue(video_duplicate_automation._duration_matches(
            {"duration": 858}, {"duration": 866}))
        self.assertFalse(video_duplicate_automation._duration_matches(
            {"duration": 858}, {"duration": 880}))

    def test_telegram_document_id_extracted_correctly(self):
        from common.duplicate_evidence import _telegram_document_id
        valid = "telegram:-1001000:1389:5000000000000000001"
        self.assertEqual(_telegram_document_id(valid), "5000000000000000001")
        self.assertIsNone(_telegram_document_id("telegram:bad_format"))
        self.assertIsNone(_telegram_document_id("other:123:456"))
        self.assertIsNone(_telegram_document_id(""))

    def test_pair_evidence_confirms_identical_telegram_document_id(self):
        from common.duplicate_evidence import pair_evidence

        class MockConn:
            def execute(self, sql, params=()):
                if "SELECT v.id" in sql:
                    class Result:
                        def fetchall(self_inner):
                            return [
                                ("telegram:-1001:1:doc123", 1000, 50000, "Title 1", "f1.mp4", "text 1", None, None, None),
                                ("telegram:-1002:2:doc123", 1000, 50000, "Title 2", "f2.mp4", "text 2", None, None, None),
                            ]
                    return Result()
                elif "SELECT 1 FROM video_duplicate_dismissals" in sql:
                    class DismissalResult:
                        def fetchone(self_inner):
                            return None
                    return DismissalResult()
                raise NotImplementedError(sql)

        conn = MockConn()
        proof = pair_evidence(conn, "telegram:-1001:1:doc123", "telegram:-1002:2:doc123")
        self.assertEqual(proof, {"type": "telegram_file_id", "file_id": "doc123"})

    def test_reconcile_does_not_merge_when_execute_merges_false(self):
        left_id = "telegram:-1001:1:doc999"
        right_id = "telegram:-1001:2:doc999"
        scan_mock = {
            "complete": True,
            "scanned": 2,
            "pairs": [{
                "evidence": "telegram_file_id",
                "left": {"id": left_id},
                "right": {"id": right_id},
                "members": [{"id": left_id}, {"id": right_id}],
                "reasons": ["Same Telegram file, different messages"],
            }],
        }
        enqueued_batches = []

        cols = [
            "id", "title", "duration", "playable", "file_size_bytes", "scene_match_source",
            "scene_match_external_id", "source_filename", "source_text", "performers",
            "fingerprint", "media_probe_data",
        ]
        class MockRow(dict):
            pass

        class MockConn:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def close(self): pass
            def commit(self): pass
            def execute(self, sql, params=()):
                class Result:
                    def fetchall(self_inner):
                        if "SELECT v.id" in sql:
                            r1 = MockRow(zip(cols, (left_id, "Keeper", 500, True, 10000, None, None, "f1.mp4", "text 1", "[]", None, None)))
                            r2 = MockRow(zip(cols, (right_id, "Loser", 500, True, 10000, None, None, "f2.mp4", "text 2", "[]", None, None)))
                            return [r1, r2]
                        return []
                    def fetchone(self_inner):
                        return (100,)
                return Result()

        with patch("resolver.video_duplicates.scan_duplicate_index", return_value=scan_mock), \
             patch.object(video_duplicate_automation, "_reconcile_visual", return_value={}), \
             patch("common.db.connect", return_value=MockConn()), \
             patch("common.duplicate_evidence.pair_evidence", return_value={"type": "telegram_file_id", "file_id": "doc999"}), \
             patch("common.job_queue.enqueue_jobs_batch", side_effect=lambda db_url, work: [enqueued_batches.extend(work) or "job-1"]):

            res = video_duplicate_automation.reconcile(None, execute_merges=False)
            self.assertEqual(res["outcome"], "completed")
            self.assertEqual(len(enqueued_batches), 0)
            self.assertEqual(res["mode"], "report")

            video_duplicate_automation.reconcile(None, execute_merges=True, merge_telegram_file_id=True)
            self.assertEqual(len(enqueued_batches), 1)
            action, payload = enqueued_batches[0]
            self.assertEqual(action, "hide")
            self.assertEqual(payload["evidence"], "telegram_file_id")
            self.assertTrue(payload["automatic"])

            enqueued_batches.clear()
            video_duplicate_automation.reconcile(None, execute_merges=True, merge_telegram_file_id=False)
            self.assertEqual(len(enqueued_batches), 0)

    def test_find_duplicate_candidates_can_exclude_same_telegram_file_for_decision_panel(self):
        found_mock = [
            {"evidence": "telegram_file_id", "category": "Same file", "reasons": ["Same Telegram file, different messages"]},
            {"evidence": "oshash", "category": "Strong suggestion", "reasons": ["Matching OSHASH fingerprint"]},
        ]
        with patch("resolver.video_duplicates._collect_duplicate_candidates", return_value=(found_mock, 2, True)):
            panel_res = video_duplicates.find_duplicate_candidates(None, include_same_telegram_file=False)
            self.assertEqual(panel_res["total"], 1)
            self.assertEqual(panel_res["pairs"][0]["evidence"], "oshash")

            all_res = video_duplicates.find_duplicate_candidates(None, include_same_telegram_file=True)
            self.assertEqual(all_res["total"], 2)

    def test_same_telegram_file_pairs_stay_visible_unless_a_caller_opts_out(self):
        found_mock = [
            {"evidence": "telegram_file_id", "category": "Same file", "reasons": ["Same Telegram file, different messages"]},
            {"evidence": "oshash", "category": "Strong suggestion", "reasons": ["Matching OSHASH fingerprint"]},
        ]
        with patch("resolver.video_duplicates._collect_duplicate_candidates", return_value=(found_mock, 2, True)):
            self.assertEqual(video_duplicates.find_duplicate_candidates(None)["total"], 2)
            same_file = video_duplicates.find_duplicate_candidates(
                None, category="same_file", include_same_telegram_file=False)
            self.assertEqual(same_file["total"], 1)
            self.assertEqual(same_file["pairs"][0]["evidence"], "telegram_file_id")



class TestDuplicateDismissalIndex(unittest.TestCase):
    def test_many_dismissals_are_partitioned_by_video_and_group(self):
        count = 10_000
        dismissed = {(f"left:{i}", f"right:{i}") for i in range(count)}

        by_video = _index_dismissals(dismissed)

        self.assertEqual(sum(len(pairs) for pairs in by_video.values()), count * 2)
        self.assertEqual(
            _dismissals_for_group({"left:731", "right:731"}, by_video),
            {("left:731", "right:731")},
        )
        # Pairs that touch the group but cross into a different file group must
        # not suppress the group's otherwise-visible same-file candidate.
        crossed = _index_dismissals(dismissed | {("left:731", "right:732")})
        self.assertEqual(
            _dismissals_for_group({"left:731", "right:731"}, crossed),
            {("left:731", "right:731")},
        )

    def test_large_scan_uses_group_local_dismissal_lookup(self):
        group_count = 10_000
        rows = []
        dismissals = []
        for index in range(group_count):
            file_id = f"file{index:04d}"
            left = f"telegram:-1003000:{index * 2}:{file_id}"
            right = f"telegram:-1004000:{index * 2 + 1}:{file_id}"
            rows.extend((
                {"id": left, "provider": "telegram", "title": f"First {index}",
                 "slug": file_id, "duration": 600, "performers": "[]", "studio": "",
                 "thumbnail": None, "source_url": ""},
                {"id": right, "provider": "telegram", "title": f"Second {index}",
                 "slug": file_id, "duration": 600, "performers": "[]", "studio": "",
                 "thumbnail": None, "source_url": ""},
            ))
            dismissals.append((left, right))

        class Connection:
            row_factory = None

            def execute(self, query):
                if "SELECT COUNT(*) FROM videos" in query:
                    class CountResult:
                        def fetchone(self):
                            return (len(rows),)
                    return CountResult()
                if "FROM videos ORDER BY id" in query:
                    return rows
                if "FROM performer_aliases" in query or "FROM video_duplicate_decisions" in query:
                    return []
                if "FROM video_duplicate_dismissals" in query:
                    return dismissals
                raise AssertionError(f"Unexpected query: {query}")

            def close(self):
                pass

        connection = Connection()
        with (patch("resolver.video_duplicates.db.connect", return_value=connection),
              patch("resolver.video_duplicates.db.table_columns", return_value={
                  "id", "provider", "title", "slug", "duration", "performers",
                  "studio", "thumbnail", "url",
              }),
              patch("resolver.video_duplicates.db.table_names", return_value={
                  "videos", "performer_aliases", "video_duplicate_decisions",
                  "video_duplicate_dismissals",
              }),
              patch("resolver.video_duplicates._dismissals_for_group",
                    wraps=_dismissals_for_group) as group_lookup):
            result = find_duplicate_candidates("fixture", limit=100)

        self.assertEqual(result["scanned"], group_count * 2)
        self.assertEqual(result["total"], 0)
        self.assertEqual(group_lookup.call_count, group_count)



class TestVideoDuplicateCandidates(PgTestCase):
    def add_video(self, video_id, title, *, provider="source", slug="", duration=600,
                  performers=None, studio=""):
        with db_session(TEST_URL) as conn:
            conn.execute(
                "INSERT INTO videos (id, provider, title, slug, duration, performers, studio) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (video_id, provider, title, slug, duration,
                 json.dumps(performers or []), studio),
            )

    def test_identical_title_does_not_create_suggestion(self):
        self.add_video("a:1", "A Very Specific Scene With Alex", provider="a",
                       duration=601, performers=["Alex", "Blake"])
        self.add_video("b:2", "A Very Specific Scene With Alex", provider="b",
                       duration=603, performers=["Alex", "Casey"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)

        self.assertEqual(result["total"], 0)
        conn = db.connect(TEST_URL)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM videos").fetchone()[0], 2)
            tables = db.table_names(conn)
            self.assertIn("videos", tables)
        finally:
            conn.close()

    def test_score_uses_cast_and_time_not_title(self):
        self.add_video("a:1", "Only Fans", duration=1200,
                       performers=["Alex", "Blake"])
        self.add_video("b:2", "Lucas Entertainment", duration=1205,
                       performers=["Alex", "Blake"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)

        self.assertEqual(result["total"], 1)
        self.assertAlmostEqual(result["pairs"][0]["score"], 85.4426357, places=4)
        self.assertEqual(result["pairs"][0]["category"], "Strong suggestion")
        self.assertIn("Shared cast: Alex, Blake", result["pairs"][0]["reasons"])

    def test_score_threshold_boundaries_and_incomplete_data(self):
        def row(duration, cast):
            return {"duration": duration, "performer_norm": set(cast)}

        at_70 = _score(row(3000, "abcd"), row(3014, "abcde"), set("abcd"))
        self.assertAlmostEqual(at_70["score"], 70.0)
        self.assertEqual(at_70["category"], "Needs review")
        at_85 = _score(row(896, "ab"), row(900, "ab"), set("ab"))
        self.assertAlmostEqual(at_85["score"], 85.0)
        self.assertEqual(at_85["category"], "Strong suggestion")
        self.assertLessEqual(at_85["score"], 95)
        self.assertIsNone(_score(row(0, "abc"), row(1, "abc"), set("abc")))

    def test_close_time_with_matching_cast_is_a_candidate(self):
        self.add_video("a:1", "Same title", duration=3000,
                       performers=["A", "B", "C", "D"])
        self.add_video("b:2", "Same title", duration=3015,
                       performers=["A", "B", "C", "D", "E"])
        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 1)

    def test_one_shared_actor_does_not_create_suggestion(self):
        self.add_video("a:1", "Men", duration=600, performers=["Alex"])
        self.add_video("b:2", "Men", duration=602, performers=["Alex"])

        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_solo_scene_duplicate_detected(self):
        self.add_video("a:1", "Morning Workout Session 1", duration=900, performers=["Alex"])
        self.add_video("b:2", "Morning Workout Session 1 HD", duration=903, performers=["Alex"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["evidence"], "solo_title_duration")
        self.assertIn("Matching solo performer (Alex)", result["pairs"][0]["reasons"][0])

    def test_solo_scene_different_performer_rejected(self):
        self.add_video("a:1", "Morning Workout Session 1", duration=900, performers=["Alex"])
        self.add_video("b:2", "Morning Workout Session 1", duration=900, performers=["Blake"])

        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_solo_scene_different_duration_rejected(self):
        self.add_video("a:1", "Morning Workout Session 1", duration=900, performers=["Alex"])
        self.add_video("b:2", "Morning Workout Session 1", duration=930, performers=["Alex"])

        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_different_volume_numbers_are_not_duplicates(self):
        self.add_video("a:1", "British Boys Fetish Club Discipline Tales Vol 12",
                       duration=2910, performers=["Discipline"])
        self.add_video("b:2", "British Boys Fetish Club Discipline Tales Vol 131",
                       duration=2905, performers=["Discipline"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 0)

    def test_discipline_tales_different_volumes_are_not_duplicates(self):
        self.add_video("telegram:-1001618765010:18190:5388927107315301064",
                       "🔥  #SPANKING British Boys Fetish Club Discipline Tales Vol 247",
                       provider="telegram", duration=2913, performers=["Discipline"])
        self.add_video("telegram:-1001618765010:18991:5238030749994623110",
                       "British Boys Fetish Club Discipline Tales Vol 273",
                       provider="telegram", duration=2898, performers=["Discipline"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 0)

    def test_same_volume_scenes_are_duplicates(self):
        self.add_video("a:1", "🔥  #SPANKING British Boys Fetish Club Discipline Tales Vol 247",
                       duration=2913, performers=["Discipline"])
        self.add_video("b:2", "British Boys Fetish Club Discipline Tales Vol 247 HD",
                       duration=2915, performers=["Discipline"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["evidence"], "solo_title_duration")

    def test_multi_performer_different_parts_are_not_duplicates(self):
        self.add_video("a:1", "Next Stop Vegas - Part 2-1", duration=1200,
                       performers=["Drew Dixon", "Roman Todd"])
        self.add_video("b:2", "Next Stop Vegas - Part 2-2", duration=1205,
                       performers=["Drew Dixon", "Roman Todd"])

        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 0)

    def test_same_telegram_file_id_groups_different_titles(self):
        self.add_video("telegram:-1001000:1389:5000000000000000001", "#OnlyFans",
                       provider="telegram", slug="5000000000000000001", duration=1220,
                       performers=["Dato Foland", "Gabriel Cross", "Griffin Barrows"])
        self.add_video("telegram:-1001000:1244:5000000000000000001", "Other title",
                       provider="telegram", slug="5000000000000000001", duration=1220,
                       performers=["Dato Foland", "Gabriel Cross", "Griffin Barrows"])
        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["score"], 100.0)
        self.assertEqual(result["pairs"][0]["category"], "Same file")
        self.assertIn("Same Telegram file", result["pairs"][0]["reasons"][0])

    def test_oshash_only_group_is_a_review_suggestion_not_same_file(self):
        first = "telegram:-1001:1:file-a"
        second = "telegram:-1002:2:file-b"
        self.add_video(first, "First upload", provider="telegram", slug="file-a")
        self.add_video(second, "Second upload", provider="telegram", slug="file-b")
        with db_session(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) VALUES (%s,'OSHASH',%s)",
                    ((first, "0123456789abcdef"), (second, "0123456789abcdef")),
                )

        exact = find_duplicate_candidates(TEST_URL, category="same_file")
        suggestions = find_duplicate_candidates(TEST_URL, category="strong")

        self.assertEqual(exact["total"], 0)
        self.assertEqual(suggestions["total"], 1)
        pair = suggestions["pairs"][0]
        self.assertEqual(pair["evidence"], "oshash")
        self.assertEqual(pair["category"], "Strong suggestion")
        self.assertLess(pair["score"], 100)
        self.assertIn("Matching OSHASH", pair["reasons"][0])

    def test_telegram_ref_remains_identity_when_slug_conflicts(self):
        self.add_video("telegram:-1001:1:900719925474099312345", "#OnlyFans",
                       provider="telegram", slug="wrong", duration=600)
        self.add_video("telegram:-1001:2:900719925474099312345", "Men",
                       provider="telegram", slug="900719925474099312345", duration=600)
        result = find_duplicate_candidates(TEST_URL, include_same_telegram_file=True)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["category"], "Same file")
        self.assertTrue(any("slug" in reason for reason in result["pairs"][0]["reasons"]))

    def test_matching_web_slug_alone_is_not_a_candidate(self):
        self.add_video("web-a:1", "Lucas Entertainment", provider="web-a", slug="same-slug")
        self.add_video("web-b:2", "Lucas Entertainment", provider="web-b", slug="same-slug")
        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_offset_paginates_candidates(self):
        for index, cast in enumerate((["Alex", "Blake"], ["Casey", "Devon"], ["Ellis", "Frankie"])):
            self.add_video(f"a:{index}", f"Scene {index}", provider="a", duration=1200, performers=cast)
            self.add_video(f"b:{index}", f"Other {index}", provider="b", duration=1205, performers=cast)
        full = find_duplicate_candidates(TEST_URL, limit=50)
        self.assertEqual(full["total"], 3)
        self.assertFalse(full["truncated"])
        self.assertEqual(full["offset"], 0)
        pages = [find_duplicate_candidates(TEST_URL, limit=1, offset=offset) for offset in (0, 1, 2)]
        self.assertEqual([len(page["pairs"]) for page in pages], [1, 1, 1])
        self.assertTrue(pages[0]["truncated"])
        self.assertTrue(pages[1]["truncated"])
        self.assertFalse(pages[2]["truncated"])
        self.assertEqual({page["pairs"][0]["left"]["id"] for page in pages}, {"a:0", "a:1", "a:2"})
        past_end = find_duplicate_candidates(TEST_URL, limit=1, offset=99)
        self.assertEqual(past_end["pairs"], [])
        self.assertFalse(past_end["truncated"])

    def test_full_index_is_not_truncated_for_decision_validation(self):
        for index in range(520):
            cast = [f"Actor{index}", f"Partner{index}"]
            self.add_video(f"a:{index}", f"Scene {index}", provider="a",
                           duration=1200, performers=cast)
            self.add_video(f"b:{index}", f"Other {index}", provider="b",
                           duration=1205, performers=cast)
        page = find_duplicate_candidates(TEST_URL, limit=500)
        self.assertEqual(page["total"], 520)
        self.assertTrue(page["truncated"])
        full_index = scan_duplicate_index(TEST_URL)
        self.assertEqual(full_index["total"], 520)
        self.assertTrue(any(
            "a:519" in {pair["left"]["id"], pair["right"]["id"], *(m["id"] for m in pair.get("members", []))}
            for pair in full_index["pairs"]))

    def test_large_exact_duplicate_index_is_complete_and_pages_without_dropping_groups(self):
        group_count = 1200
        rows = []
        for index in range(group_count):
            file_id = f"largefile{index:04d}"
            rows.extend((
                (f"telegram:-1001000:{index * 2}:{file_id}", f"First copy {index}", file_id),
                (f"telegram:-1002000:{index * 2 + 1}:{file_id}", f"Second copy {index}", file_id),
            ))
        with db_session(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio) "
                    "VALUES (%s,'telegram',%s,%s,600,'[]','')", rows,
                )

        scan = scan_duplicate_index(TEST_URL)
        middle = find_duplicate_candidates(TEST_URL, limit=500, offset=500, category="same_file")
        last = find_duplicate_candidates(TEST_URL, limit=500, offset=1000, category="same_file")
        self.assertTrue(scan["complete"])
        self.assertEqual(scan["scanned"], group_count * 2)
        self.assertEqual(scan["total"], group_count)
        self.assertEqual(middle["total"], group_count)
        self.assertEqual(len(middle["pairs"]), 500)
        self.assertTrue(middle["truncated"])
        self.assertEqual(len(last["pairs"]), 200)
        self.assertFalse(last["truncated"])
        flattened = [member["id"] for pair in (*middle["pairs"], *last["pairs"])
                     for member in pair["members"]]
        self.assertEqual(len(flattened), 1400)
        self.assertEqual(len(set(flattened)), 1400)

    def test_large_exact_duplicate_scan_uses_active_dismissals_per_group(self):
        group_count = 5_000
        videos = []
        dismissals = []
        for index in range(group_count):
            file_id = f"dismissedfile{index:04d}"
            left = f"telegram:-1003000:{index * 2}:{file_id}"
            right = f"telegram:-1004000:{index * 2 + 1}:{file_id}"
            videos.extend(((left, f"First {index}", file_id), (right, f"Second {index}", file_id)))
            dismissals.append((left, right, 1, f"dismiss-{index}", 1))

        with db_session(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio) "
                    "VALUES (%s,'telegram',%s,%s,600,'[]','')", videos,
                )
                conn.executemany(
                    "INSERT INTO video_duplicate_dismissals "
                    "(left_id,right_id,active,operation_id,created_at) VALUES (%s,%s,%s,%s,%s)",
                    dismissals,
                )

        scan = scan_duplicate_index(TEST_URL)
        self.assertTrue(scan["complete"])
        self.assertEqual(scan["scanned"], group_count * 2)
        self.assertEqual(scan["total"], 0)

    def test_category_filter_applies_before_pagination(self):
        self.add_video("telegram:-1001:1:shared", "Alpha", provider="telegram",
                       duration=600, performers=["A", "B"])
        self.add_video("telegram:-1001:2:shared", "Beta", provider="telegram",
                       duration=600, performers=["A", "B"])
        self.add_video("a:1", "Only Fans", provider="a", duration=1200, performers=["Alex", "Blake"])
        self.add_video("b:2", "Lucas Entertainment", provider="b", duration=1205, performers=["Alex", "Blake"])
        self.assertEqual(find_duplicate_candidates(TEST_URL, limit=50)["total"], 2)
        same = find_duplicate_candidates(TEST_URL, limit=50, category="same_file")
        self.assertEqual(same["total"], 1)
        self.assertEqual(same["pairs"][0]["category"], "Same file")
        self.assertEqual(same["category"], "same_file")
        with patch.object(video_duplicates, "_candidate_pairs",
                          side_effect=AssertionError("same-file scan must not run heuristics")):
            exact_only = find_duplicate_candidates(TEST_URL, limit=50, category="same_file")
        self.assertTrue(exact_only["complete"])
        self.assertEqual(exact_only["total"], 1)
        strong = find_duplicate_candidates(TEST_URL, limit=50, category="strong")
        self.assertEqual(strong["total"], 1)
        self.assertEqual(strong["pairs"][0]["category"], "Strong suggestion")
        review = find_duplicate_candidates(TEST_URL, limit=50, category="review")
        self.assertEqual(review["total"], 0)
        self.assertEqual(review["pairs"], [])

    def test_same_file_group_over_250_is_one_group(self):
        for i in range(260):
            self.add_video(f"telegram:-1001:{i+1}:hugeopaqueid", f"title {i}", provider="telegram",
                           slug="hugeopaqueid", duration=600, performers=[])
        result = find_duplicate_candidates(TEST_URL, limit=5)
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["pairs"][0]["members"]), 260)
        self.assertFalse(result["truncated"])

    def test_handles_null_performers_and_caps_results(self):
        self.add_video("a:1", "This Is A Very Specific Scene", duration=600, performers=["Alex", "Blake"])
        self.add_video("b:2", "Different title", duration=602, performers=["Alex", "Blake"])
        with db_session(TEST_URL) as conn:
            conn.execute("UPDATE videos SET performers = NULL WHERE id = 'a:1'")

        result = find_duplicate_candidates(TEST_URL, limit=0)

        self.assertEqual(len(result["pairs"]), 0)
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["truncated"])


    def test_v2_duplicate_route_paginates_real_large_candidate_index(self):
        group_count = 1200
        padding_count = 48_000
        videos = []
        for index in range(group_count):
            file_id = f"routefile{index:04d}"
            videos.extend((
                (f"telegram:-1005000:{index * 2}:{file_id}", "telegram", f"First {index}", file_id),
                (f"telegram:-1006000:{index * 2 + 1}:{file_id}", "telegram", f"Second {index}", file_id),
            ))
        videos.extend(
            (f"catalog-padding:{index:05d}", "other", f"Unrelated item {index}", "")
            for index in range(padding_count)
        )
        with db_session(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio) "
                    "VALUES (%s,%s,%s,%s,600,'[]','')", videos,
                )

        admin_v2 = SimpleNamespace(
            _admin_blocked=lambda _request: False,
            JSONResponse=JSONResponse,
            asyncio=asyncio,
            HUB_DB_URL=TEST_URL,
        )
        router = admin_v2_jobs.build_router(admin_v2)
        endpoint = next(route.endpoint for route in router.routes
                        if route.path == "/api/v2/duplicates")

        def make_request(offset):
            path = "/admin/api/v2/duplicates"
            return Request({
                "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": path, "raw_path": path.encode(),
                "query_string": f"limit=200&offset={offset}&category=same_file".encode(),
                "headers": [], "client": ("127.0.0.1", 1), "server": ("test", 80),
                "root_path": "",
            })

        pages = [asyncio.run(endpoint(make_request(offset), 200, offset, "same_file"))
                 for offset in range(0, group_count, 200)]
        flattened = [member["id"] for page in pages for pair in page["pairs"]
                     for member in pair["members"]]

        self.assertEqual(len(pages), 6)
        self.assertTrue(all(page["ok"] for page in pages))
        self.assertTrue(all(page["total"] == group_count for page in pages))
        self.assertTrue(all(page["scanned"] == group_count * 2 + padding_count for page in pages))
        self.assertTrue(all(page["complete"] for page in pages))
        self.assertTrue(all(len(page["pairs"]) == 200 for page in pages))
        self.assertEqual(len(flattened), group_count * 2)
        self.assertEqual(len(set(flattened)), group_count * 2)



if __name__ == "__main__":
    unittest.main()
