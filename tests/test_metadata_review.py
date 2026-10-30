"""PostgreSQL tests for staged external video metadata suggestions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from tests.pg_helper import PgTestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.metadata_review import list_metadata_review_proposals, refresh_metadata_review_proposal, reject_metadata_review_proposal, store_metadata_review_proposal
from common.admin_operations import execute_operation, preview_metadata_plan


class TestMetadataReview(PgTestCase):
    def add_video(self, video_id="telegram:metadata-review:1"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,plot,performers,studio,tags) "
                "VALUES (%s,'telegram','Existing title','Current description','[\"Performer\"]',"
                "'Current studio','[]')", (video_id,),
            )
        return video_id

    def test_proposal_lists_current_and_suggested_values_and_can_be_rejected(self):
        video_id = self.add_video()
        stored = store_metadata_review_proposal(
            self.conn, video_id, {"plot": "Suggested description", "studio": "Suggested studio"},
            source="studio:algolia", external_id="item-123",
            source_url="https://catalog.example/item/123", confidence=0.93,
        )
        self.assertTrue(stored)
        self.assertFalse(store_metadata_review_proposal(
            self.conn, video_id, {"plot": "Suggested description", "studio": "Suggested studio"},
            source="studio:algolia", external_id="item-123",
            source_url="https://catalog.example/item/123", confidence=0.93,
        ))
        result = list_metadata_review_proposals(self.db_url)
        self.assertEqual(result["total"], 1)
        proposal = result["items"][0]
        self.assertEqual(proposal["current"], {"plot": "Current description", "studio": "Current studio"})
        self.assertEqual(proposal["changes"], {"plot": "Suggested description", "studio": "Suggested studio"})
        self.assertFalse(proposal["stale"])
        rejected = reject_metadata_review_proposal(self.db_url, proposal["id"])
        self.assertEqual(rejected["outcome"], "rejected")
        self.assertEqual(list_metadata_review_proposals(self.db_url)["total"], 0)

    def test_proposal_is_stale_after_catalog_change_and_cannot_be_previewed(self):
        video_id = self.add_video()
        store_metadata_review_proposal(
            self.conn, video_id, {"plot": "Suggested description"}, source="studio:algolia",
            external_id="item-123", source_url="", confidence=0.95,
        )
        proposal = list_metadata_review_proposals(self.db_url)["items"][0]
        with self.conn:
            self.conn.execute("UPDATE videos SET plot='Changed while waiting' WHERE id=%s", (video_id,))
        stale = list_metadata_review_proposals(self.db_url)["items"][0]
        self.assertTrue(stale["stale"])
        result = preview_metadata_plan(
            self.db_url, video_id, proposal["revision"], {"plot": "Suggested description"}, [], proposal["id"],
        )
        self.assertEqual(result["outcome"], "stale")

    def test_stale_proposal_can_be_refreshed_then_previewed_without_applying_it(self):
        video_id = self.add_video()
        store_metadata_review_proposal(
            self.conn, video_id, {"plot": "Suggested description"}, source="studio:algolia",
            external_id="item-123", source_url="", confidence=0.95,
        )
        proposal = list_metadata_review_proposals(self.db_url)["items"][0]
        with self.conn:
            self.conn.execute("UPDATE videos SET plot='Changed while waiting' WHERE id=%s", (video_id,))
        self.assertTrue(list_metadata_review_proposals(self.db_url)["items"][0]["stale"])
        refreshed = refresh_metadata_review_proposal(self.db_url, proposal["id"])
        self.assertEqual(refreshed["outcome"], "refreshed")
        updated = list_metadata_review_proposals(self.db_url)["items"][0]
        self.assertFalse(updated["stale"])
        self.assertEqual(updated["current"]["plot"], "Changed while waiting")
        self.assertEqual(updated["changes"]["plot"], "Suggested description")
        plan = preview_metadata_plan(self.db_url, video_id, refreshed["revision"],
                                     {"plot": "Suggested description"}, [], proposal["id"])
        self.assertEqual(plan["outcome"], "previewed")
        self.assertEqual(plan["fields"]["plot"]["before"], "Changed while waiting")
        self.assertEqual(self.conn.execute("SELECT plot FROM videos WHERE id=%s", (video_id,)).fetchone()[0], "Changed while waiting")
        with self.conn:
            self.conn.execute("UPDATE videos SET plot='Changed again' WHERE id=%s", (video_id,))
        self.assertEqual(execute_operation(self.db_url, plan["plan_id"])["outcome"], "stale")

    def test_refresh_rejects_closed_or_missing_proposal(self):
        video_id = self.add_video()
        store_metadata_review_proposal(self.conn, video_id, {"plot": "Suggested"},
                                       source="test", external_id="1", source_url="", confidence=1)
        proposal = list_metadata_review_proposals(self.db_url)["items"][0]
        reject_metadata_review_proposal(self.db_url, proposal["id"])
        self.assertEqual(refresh_metadata_review_proposal(self.db_url, proposal["id"])["outcome"], "conflict")
        self.assertEqual(refresh_metadata_review_proposal(self.db_url, 999999)["outcome"], "not_found")

    def test_acceptance_is_atomic_with_metadata_operation_enqueue(self):
        video_id = self.add_video()
        store_metadata_review_proposal(
            self.conn, video_id, {"plot": "Suggested description"}, source="studio:algolia",
            external_id="item-123", source_url="", confidence=0.95,
        )
        proposal = list_metadata_review_proposals(self.db_url)["items"][0]
        plan = preview_metadata_plan(
            self.db_url, video_id, proposal["revision"], {"plot": "Edited description"}, [], proposal["id"],
        )
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        self.assertEqual(list_metadata_review_proposals(self.db_url)["total"], 0)
        state = self.conn.execute(
            "SELECT status FROM metadata_review_proposals WHERE id=%s", (proposal["id"],),
        ).fetchone()
        self.assertEqual(state[0], "accepted")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='metadata_update' AND state='queued'",
        ).fetchone()[0], 1)

    def test_proposal_patch_must_keep_the_whole_candidate_field_set(self):
        video_id = self.add_video()
        store_metadata_review_proposal(
            self.conn, video_id, {"plot": "Suggested description", "studio": "Suggested studio"},
            source="studio:algolia", external_id="item-123", source_url="", confidence=0.95,
        )
        proposal = list_metadata_review_proposals(self.db_url)["items"][0]
        result = preview_metadata_plan(
            self.db_url, video_id, proposal["revision"], {"plot": "Edited description"}, [], proposal["id"],
        )
        self.assertEqual(result["outcome"], "stale")


if __name__ == "__main__":
    unittest.main()
