"""PostgreSQL integration checks for persistent performer proposal review."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from common import performer_proposals
from common.fuzzy_performers import find_fuzzy_performer_candidates
from common.admin_operations import execute_operation, preview_performer_merge_plan
from common.job_queue import read_job
from pg_helper import PgTestCase
from test_support import load_sync_module

sync = load_sync_module()


class TestPerformerProposalIntegration(PgTestCase):
    def setUp(self):
        super().setUp()
        self.env_patch = patch.dict(os.environ, {
            "JELLYFIN_URL": os.getenv("JELLYFIN_URL") or "http://jellyfin.test:8096",
            "JELLYFIN_API_KEY": os.getenv("JELLYFIN_API_KEY") or "test-jellyfin-key",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.conn.execute(
            "INSERT INTO performer_catalog(id,source,source_id,name) VALUES (%s,'manual','p1','Canonical Name')",
            ("p1",),
        )
        self.conn.execute(
            "INSERT INTO videos(id,provider,title,performers) VALUES (%s,'telegram',%s,%s)",
            ("telegram:proposal-1", "Proposal test", json.dumps(["Alias Name", "Other Name"])),
        )
        self.conn.commit()

    def add_proposal(self, candidate, *, video_id="telegram:proposal-1"):
        normalized = candidate.casefold().replace(" ", "")
        return self.conn.execute(
            """INSERT INTO telegram_performer_proposals
               (video_id,candidate,normalized,matched_text,source_field,reason)
               VALUES (%s,%s,%s,%s,'caption','possible_typo') RETURNING id""",
            (video_id, candidate, normalized, candidate),
        ).fetchone()[0]

    def test_pending_list_and_catalog_search_use_real_schema_and_pagination(self):
        empty = performer_proposals.list_pending_proposals(self.db_url)
        self.assertEqual((empty["total"], empty["items"]), (0, []))
        self.add_proposal("Alias One")
        second = self.add_proposal("Alias Two")
        third = self.add_proposal("Alias Three")
        self.conn.commit()

        page = performer_proposals.list_pending_proposals(self.db_url, limit=1, offset=1)
        catalog = performer_proposals.search_catalog(self.db_url, "canonical")
        punctuation_only = performer_proposals.search_catalog(self.db_url, "---")

        self.assertEqual(page["total"], 3)
        self.assertEqual((page["limit"], page["offset"]), (1, 1))
        self.assertEqual(page["items"][0]["id"], second)
        self.assertEqual(page["items"][0]["title"], "Proposal test")
        self.assertEqual([item["id"] for item in catalog["items"]], ["p1"])
        self.assertEqual(punctuation_only["items"], [])
        self.assertEqual(performer_proposals.list_pending_proposals(self.db_url, 10, 0)["items"][-1]["id"], third)

    def test_legacy_profiles_are_searchable_and_promoted_on_approval(self):
        self.conn.execute(
            "INSERT INTO performer_bios(name,bio,aliases,updated_at) VALUES (%s,%s,%s,1)",
            ("Legacy Bio Person", "Existing profile", json.dumps(["Bio Stage Name"])),
        )
        self.conn.execute(
            "INSERT INTO performer_aliases(alias,canonical) VALUES (%s,%s)",
            ("old stage name", "Mapped Legacy Person"),
        )
        proposal_id = self.add_proposal("Mapped Alias")
        self.conn.commit()

        bio_match = performer_proposals.search_catalog(self.db_url, "bio stage")
        mapped_match = performer_proposals.search_catalog(self.db_url, "old stage")
        self.assertEqual(bio_match["items"], [
            {"id": "local:legacybioperson", "name": "Legacy Bio Person"},
        ])
        self.assertEqual(mapped_match["items"], [
            {"id": "local:mappedlegacyperson", "name": "Mapped Legacy Person"},
        ])

        wake = []
        result = performer_proposals.review_proposal(
            self.db_url, proposal_id,
            {"action": "approve", "performer_id": "local:mappedlegacyperson"},
            wake_worker=wake.append,
        )
        self.assertEqual((result["status"], result["performer"]),
                         ("approved", "Mapped Legacy Person"))
        catalog_id = self.conn.execute(
            "SELECT performer_id FROM telegram_performer_proposals WHERE id=%s",
            (proposal_id,),
        ).fetchone()[0]
        self.assertTrue(catalog_id.startswith("local:"))
        self.assertEqual(self.conn.execute(
            "SELECT name FROM performer_catalog WHERE id=%s", (catalog_id,),
        ).fetchone(), ("Mapped Legacy Person",))
        self.assertEqual(self.conn.execute(
            "SELECT canonical FROM performer_aliases WHERE alias='mapped alias'",
        ).fetchone(), ("Mapped Legacy Person",))
        self.assertEqual(self.conn.execute(
            "SELECT alias,origin,approved FROM performer_catalog_aliases "
            "WHERE performer_id=%s ORDER BY alias", (catalog_id,),
        ).fetchall(), [
            ("Mapped Alias", "reviewed", True),
            ("Mapped Legacy Person", "legacy", True),
            ("old stage name", "legacy", True),
        ])
        self.assertEqual(wake, ["jobs"])

        bio_proposal_id = self.add_proposal("Bio Stage Name")
        self.conn.commit()
        bio_result = performer_proposals.review_proposal(
            self.db_url, bio_proposal_id,
            {"action": "approve", "performer_id": "local:legacybioperson"},
            wake_worker=wake.append,
        )
        bio_catalog_id = self.conn.execute(
            "SELECT performer_id FROM telegram_performer_proposals WHERE id=%s",
            (bio_proposal_id,),
        ).fetchone()[0]
        self.assertEqual((bio_result["status"], bio_result["performer"]),
                         ("approved", "Legacy Bio Person"))
        self.assertEqual(self.conn.execute(
            "SELECT bio FROM performer_bios WHERE name='Legacy Bio Person'",
        ).fetchone(), ("Existing profile",))
        self.assertEqual(self.conn.execute(
            "SELECT alias,origin,approved FROM performer_catalog_aliases "
            "WHERE performer_id=%s ORDER BY alias", (bio_catalog_id,),
        ).fetchall(), [
            ("Bio Stage Name", "legacy", True),
            ("Bio Stage Name", "reviewed", True),
            ("Legacy Bio Person", "legacy", True),
        ])
        self.assertEqual(wake, ["jobs", "jobs"])

    def test_fuzzy_scan_reads_jsonb_performers_and_finds_diacritic_variants(self):
        self.conn.execute(
            "INSERT INTO videos(id,provider,title,performers) VALUES (%s,'telegram',%s,%s),(%s,'telegram',%s,%s)",
            ("telegram:fuzzy-1", "Name without accents", json.dumps(["Nicolas Rios"]),
             "telegram:fuzzy-2", "Name with accents", json.dumps(["Nicolás Ríos"])),
        )
        self.conn.commit()

        report = find_fuzzy_performer_candidates(self.db_url, similarity_threshold=0.88)
        group = next(group for group in report["groups"] if group["category"] == "diacritics")
        names = {group["canonical"], *(variant["name"] for variant in group["variants"])}

        self.assertEqual(report["ok"], True)
        self.assertIn("Nicolas Rios", names)
        self.assertIn("Nicolás Ríos", names)
        self.assertEqual(group["canonical_video_count"], 1)
        self.assertEqual(sum(variant["video_count"] for variant in group["variants"]), 1)

    def test_fuzzy_merge_preview_execute_and_worker_update_jsonb_and_nfo(self):
        self.conn.execute(
            "INSERT INTO videos(id,provider,title,performers,categories,tags) "
            "VALUES (%s,'telegram',%s,%s,%s,%s),(%s,'telegram',%s,%s,%s,%s)",
            ("telegram:merge-alias", "Alias appearance", json.dumps(["Nicolas Rios", "Other Name"]),
             json.dumps(["Drama"]), json.dumps(["Tag One"]),
             "telegram:merge-canonical", "Canonical appearance", json.dumps(["Nicolás Ríos"]),
             json.dumps(["Drama"]), json.dumps(["Tag Two"])),
        )
        self.conn.commit()

        preview = preview_performer_merge_plan(self.db_url, "Nicolas Rios", "Nicolás Ríos")
        self.assertEqual(preview["outcome"], "previewed")
        self.assertEqual(preview["items"][0]["alias_videos"], 1)
        self.assertEqual(preview["items"][0]["canonical_videos"], 1)
        queued = execute_operation(self.db_url, preview["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        job_id = queued["jobs"][0]["job_id"]

        with tempfile.TemporaryDirectory(prefix="performer-fuzzy-merge-") as directory:
            root = Path(directory)
            media = root / "library"
            media.mkdir()
            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch("common.fuzzy_performers.make_backup",
                       return_value=root / "pre-merge.dump.gz"), \
                 patch("common.fuzzy_performers.trigger_jellyfin_refresh", return_value=True) as refresh:
                self.assertEqual(sync.process_video_duplicate_jobs(self.conn, media), 1)
            refresh.assert_called_once_with()
            self.assertTrue((media / f"{sync.stable_media_basename('telegram:merge-alias')}.nfo").is_file())
            self.assertTrue((media / f"{sync.stable_media_basename('telegram:merge-canonical')}.nfo").is_file())

        names = self.conn.execute(
            "SELECT performers FROM videos WHERE id IN (%s,%s) ORDER BY id",
            ("telegram:merge-alias", "telegram:merge-canonical"),
        ).fetchall()
        parsed_names = [row[0] if isinstance(row[0], list) else json.loads(row[0]) for row in names]
        self.assertEqual(parsed_names, [["Nicolás Ríos", "Other Name"], ["Nicolás Ríos"]])
        self.assertEqual(self.conn.execute(
            "SELECT canonical FROM performer_aliases WHERE alias='nicolas rios'"
        ).fetchone()[0], "Nicolás Ríos")
        merged = read_job(self.db_url, job_id)
        self.assertEqual(merged["state"], "done")
        merge_result = merged["result"]
        if isinstance(merge_result, str):
            merge_result = json.loads(merge_result)
        self.assertEqual(merge_result["videos_updated"], 1)
        self.assertEqual(merge_result["nfos_updated"], 2)

    def test_approval_commits_catalog_change_and_real_refresh_job(self):
        proposal_id = self.add_proposal("Alias Name")
        self.conn.commit()
        wake = []

        result = performer_proposals.review_proposal(
            self.db_url, proposal_id, {"action": "approve", "performer_id": "p1"},
            wake_worker=wake.append,
        )

        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["refresh_pending"])
        self.assertEqual(wake, ["jobs"])
        proposal = self.conn.execute(
            "SELECT status,performer_id FROM telegram_performer_proposals WHERE id=%s", (proposal_id,)
        ).fetchone()
        self.assertEqual(proposal, ("approved", "p1"))
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT performers FROM videos WHERE id='telegram:proposal-1'"
        ).fetchone()[0]), ["Canonical Name", "Other Name"])
        self.assertEqual(self.conn.execute(
            "SELECT alias,approved FROM performer_catalog_aliases WHERE performer_id='p1' AND origin='reviewed'"
        ).fetchone(), ("Alias Name", True))
        self.assertEqual(self.conn.execute(
            "SELECT canonical FROM performer_aliases WHERE alias='alias name'"
        ).fetchone()[0], "Canonical Name")
        job = read_job(self.db_url, result["refresh_job_id"])
        payload = job["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertEqual((job["action"], job["state"], payload),
                         ("performer_refresh", "queued", {"video_ids": ["telegram:proposal-1"]}))
        self.conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s",
                          (result["refresh_job_id"],))
        self.conn.commit()

        with tempfile.TemporaryDirectory(prefix="performer-proposal-refresh-") as directory:
            root = Path(directory)
            media = root / "library"
            media.mkdir()
            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.object(sync, "create_nfo_file") as create_nfo, \
                 patch.object(sync, "trigger_jellyfin_refresh", return_value=True) as refresh:
                self.assertEqual(sync.process_video_duplicate_jobs(self.conn, media), 1)
            create_nfo.assert_called_once()
            self.assertEqual(create_nfo.call_args.args[0]["performers"], ["Canonical Name", "Other Name"])
            refresh.assert_called_once_with()
        finished_job = read_job(self.db_url, result["refresh_job_id"])
        self.assertEqual(finished_job["state"], "done")
        finished_result = finished_job["result"]
        if isinstance(finished_result, str):
            finished_result = json.loads(finished_result)
        self.assertEqual(finished_result["videos_refreshed"], 1)
        self.assertEqual(finished_result["jellyfin_refresh"], "requested")

        with self.assertRaises(performer_proposals.ProposalError) as repeated:
            performer_proposals.review_proposal(
                self.db_url, proposal_id, {"action": "approve", "performer_id": "p1"},
                wake_worker=wake.append,
            )
        self.assertEqual(repeated.exception.status_code, 404)
        self.assertEqual(wake, ["jobs"])

    def test_approval_can_create_a_local_performer_without_catalog_selection(self):
        proposal_id = self.add_proposal("New Local Performer")
        self.conn.commit()
        wake = []

        result = performer_proposals.review_proposal(
            self.db_url, proposal_id,
            {"action": "approve", "canonical": "New Local Performer"},
            wake_worker=wake.append,
        )

        self.assertEqual(result["status"], "approved")
        self.assertEqual((result["performer"], result["refresh_pending"]),
                         ("New Local Performer", True))
        self.assertEqual(wake, ["jobs"])
        performer_id = self.conn.execute(
            "SELECT performer_id FROM telegram_performer_proposals WHERE id=%s",
            (proposal_id,),
        ).fetchone()[0]
        self.assertTrue(performer_id.startswith("local:"))
        self.assertEqual(self.conn.execute(
            "SELECT source,name,active FROM performer_catalog WHERE id=%s",
            (performer_id,),
        ).fetchone(), ("local", "New Local Performer", True))
        self.assertEqual(self.conn.execute(
            "SELECT performer_id,alias,approved FROM performer_catalog_aliases "
            "WHERE performer_id=%s AND origin='reviewed' ORDER BY alias",
            (performer_id,),
        ).fetchall(), [(performer_id, "New Local Performer", True)])
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT performers FROM videos WHERE id='telegram:proposal-1'"
        ).fetchone()[0]), ["Alias Name", "Other Name", "New Local Performer"])
        refresh_job = read_job(self.db_url, result["refresh_job_id"])
        self.assertEqual((refresh_job["action"], refresh_job["state"]),
                         ("performer_refresh", "queued"))

    def test_reject_and_manual_override_conflict_are_durable_and_safe(self):
        rejected_id = self.add_proposal("Rejected Alias")
        conflicted_id = self.add_proposal("Conflict Alias")
        self.conn.execute(
            "INSERT INTO video_metadata_overrides(video_id,fields,revision) VALUES (%s,%s,1)",
            ("telegram:proposal-1", json.dumps({"performers": ["Pinned Performer"]})),
        )
        self.conn.commit()
        wake = []

        rejected = performer_proposals.review_proposal(
            self.db_url, rejected_id, {"action": "reject"}, wake_worker=wake.append,
        )
        self.assertEqual(rejected, {"ok": True, "status": "rejected"})
        with self.assertRaises(performer_proposals.ProposalError) as conflict:
            performer_proposals.review_proposal(
                self.db_url, conflicted_id, {"action": "approve", "performer_id": "p1"},
                wake_worker=wake.append,
            )
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM telegram_performer_proposals WHERE id=%s", (conflicted_id,)
        ).fetchone()[0], "pending")
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT performers FROM videos WHERE id='telegram:proposal-1'"
        ).fetchone()[0]), ["Alias Name", "Other Name"])
        self.assertEqual(wake, [])

    def test_approval_refuses_to_reassign_an_alias_owned_by_another_performer(self):
        self.conn.execute(
            "INSERT INTO performer_aliases(alias,canonical) VALUES (%s,%s)",
            ("alias name", "Other Performer"),
        )
        proposal_id = self.add_proposal("Alias Name")
        self.conn.commit()

        with self.assertRaises(performer_proposals.ProposalError) as raised:
            performer_proposals.review_proposal(
                self.db_url, proposal_id, {"action": "approve", "performer_id": "p1"},
                wake_worker=lambda _worker: None,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Other Performer", raised.exception.message)
        self.assertEqual(self.conn.execute(
            "SELECT canonical FROM performer_aliases WHERE alias='alias name'"
        ).fetchone()[0], "Other Performer")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM telegram_performer_proposals WHERE id=%s", (proposal_id,)
        ).fetchone()[0], "pending")
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT performers FROM videos WHERE id='telegram:proposal-1'"
        ).fetchone()[0]), ["Alias Name", "Other Name"])

    def test_concurrent_reviews_apply_only_one_decision_and_at_most_one_refresh_job(self):
        proposal_id = self.add_proposal("Race Alias")
        self.conn.commit()
        barrier = __import__("threading").Barrier(2)

        def review(action):
            barrier.wait(timeout=5)
            try:
                return performer_proposals.review_proposal(
                    self.db_url, proposal_id,
                    ({"action": "approve", "performer_id": "p1"} if action == "approve"
                     else {"action": "reject"}), wake_worker=lambda _worker: None,
                )
            except performer_proposals.ProposalError as error:
                return {"error": error.status_code}

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(review, ("approve", "reject")))

        self.assertEqual(sum("error" not in result for result in outcomes), 1)
        self.assertEqual(sum(result.get("error") == 404 for result in outcomes), 1)
        status = self.conn.execute(
            "SELECT status FROM telegram_performer_proposals WHERE id=%s", (proposal_id,)
        ).fetchone()[0]
        queued_refreshes = self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='performer_refresh'"
        ).fetchone()[0]
        self.assertIn(status, {"approved", "rejected"})
        self.assertEqual(queued_refreshes, 1 if status == "approved" else 0)


    def test_group_review_and_reconciliation_expose_refresh_task_references(self):
        self.add_proposal("Alias Name")
        self.conn.commit()
        grouped = performer_proposals.review_proposal_group(
            self.db_url, "aliasname", "possible_typo", {"action": "approve", "performer_id": "p1"},
            wake_worker=lambda *_: None,
        )
        self.assertEqual(grouped["reviewed"], 1)
        self.assertEqual(len(grouped["refresh_job_ids"]), 1)
        self.assertEqual(read_job(self.db_url, grouped["refresh_job_ids"][0])["action"], "performer_refresh")
        self.conn.execute("INSERT INTO videos(id,provider,title,performers,source_text) VALUES ('telegram:proposal-2','telegram','Reconcile fixture','[]','Cast: Rowan Example')")
        proposal_id = self.add_proposal("Rowan Example", video_id="telegram:proposal-2")
        self.conn.execute("UPDATE telegram_performer_proposals SET reason='uncatalogued_credit' WHERE id=%s", (proposal_id,))
        self.conn.commit()
        reconciled = performer_proposals.reconcile_clear_matches(self.db_url, wake_worker=lambda *_: None)
        self.assertEqual(reconciled["resolved"], 1)
        self.assertTrue(reconciled["refresh_job_ids"])
        for job_id in reconciled["refresh_job_ids"]:
            self.assertEqual(read_job(self.db_url, job_id)["state"], "queued")


if __name__ == "__main__":
    unittest.main()
