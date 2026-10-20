"""Privacy and pagination tests for the Admin v2 activity history."""

from __future__ import annotations

from common.admin_action_events import list_events, record_event
from common.admin_audit_events import list_change_events, record_metadata_change, record_source_settings_change
from tests.pg_helper import PgTestCase


class TestAdminActionEvents(PgTestCase):
    def test_records_only_route_template_and_outcome(self):
        record_event(
            self.db_url, method="POST", route="/admin/api/v2/audits/{job_id}/repairs/preview",
            status_code=202, correlation_id="a" * 32,
        )
        result = list_events(self.db_url)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0], {
            "id": 1,
            "created_at": result["items"][0]["created_at"],
            "method": "POST",
            "route": "/admin/api/v2/audits/{job_id}/repairs/preview",
            "status_code": 202,
            "outcome": "accepted",
            "correlation_id": "a" * 32,
            "actor_type": "shared_admin",
        })

    def test_invalid_correlation_id_is_discarded(self):
        record_event(
            self.db_url, method="PATCH", route="/admin/api/v2/sources/{source_id}",
            status_code=200, correlation_id="secret-value",
        )
        item = list_events(self.db_url)["items"][0]
        self.assertIsNone(item["correlation_id"])
        self.assertNotIn("secret-value", repr(item))

    def test_ignores_non_v2_routes_and_unsupported_methods(self):
        record_event(self.db_url, method="POST", route="/admin/api/jobs", status_code=202)
        record_event(self.db_url, method="GET", route="/admin/api/v2/activity", status_code=200)
        record_event(self.db_url, method="POST", route="/admin/api/v2/operations/preview", status_code=202)
        record_event(self.db_url, method="POST", route="/admin/api/v2/denied", status_code=403)
        self.assertEqual(list_events(self.db_url)["total"], 1)

    def test_history_is_newest_first_and_bounded(self):
        for index in range(4):
            record_event(self.db_url, method="POST", route=f"/admin/api/v2/test/{index}", status_code=200)
        first = list_events(self.db_url, limit=2, offset=0)
        second = list_events(self.db_url, limit=2, offset=2)
        self.assertEqual(first["total"], 4)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 2)
        self.assertEqual({item["route"] for item in first["items"] + second["items"]},
                         {f"/admin/api/v2/test/{index}" for index in range(4)})

    def test_action_filters_match_method_outcome_and_fixed_section_prefix(self):
        record_event(self.db_url, method="POST", route="/admin/api/v2/library/videos/metadata",
                     status_code=202)
        record_event(self.db_url, method="PATCH", route="/admin/api/v2/sources/save",
                     status_code=200)
        record_event(self.db_url, method="POST", route="/admin/api/v2/jobs/retry",
                     status_code=202)
        record_event(self.db_url, method="POST", route="/admin/api/v2/audits/execute",
                     status_code=202)
        library_posts = list_events(self.db_url, method="post", outcome="accepted", area="library")
        self.assertEqual(library_posts["total"], 1)
        self.assertEqual(library_posts["items"][0]["route"], "/admin/api/v2/library/videos/metadata")
        self.assertEqual(list_events(self.db_url, method="PATCH", outcome="completed")["total"], 1)
        self.assertEqual(list_events(self.db_url, area="sources")["total"], 1)
        self.assertEqual(list_events(self.db_url, area="quality")["total"], 1)
        self.assertEqual(list_events(self.db_url, area="system")["total"], 0)

    def test_action_filters_reject_unbounded_or_unknown_filter_values(self):
        with self.assertRaises(ValueError):
            list_events(self.db_url, method="GET")
        with self.assertRaises(ValueError):
            list_events(self.db_url, outcome="failed")
        with self.assertRaises(ValueError):
            list_events(self.db_url, area="../")

    def test_metadata_change_event_is_before_after_and_idempotent(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
                "VALUES ('job-metadata-1','metadata_update','done','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        args = {
            "job_id": "job-metadata-1", "video_id": "telegram:10:item",
            "before": {"title": "Source title", "plot": None},
            "after": {"title": "Manual title", "plot": ""},
            "before_overrides": {}, "after_overrides": {"title": "Manual title", "plot": ""},
            "fields": ["title", "plot", "ignored_secret"],
        }
        with self.conn:
            self.assertTrue(record_metadata_change(self.conn, **args))
        with self.conn:
            self.assertFalse(record_metadata_change(self.conn, **args))
        result = list_change_events(self.conn)
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["object_id"], "telegram:10:item")
        self.assertEqual(item["changes"]["title"]["before"], "Source title")
        self.assertEqual(item["changes"]["title"]["after"], "Manual title")
        self.assertNotIn("ignored_secret", item["changes"])
        self.assertEqual(item["actor_type"], "shared_admin")

    def test_change_history_filters_type_object_and_safe_identifier(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
                "VALUES ('filter-audit-job','metadata_update','done','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
            record_metadata_change(
                self.conn, job_id="filter-audit-job", video_id="telegram:12:filter-video",
                before={"title": "Old"}, after={"title": "New"}, before_overrides={},
                after_overrides={"title": "New"}, fields=["title"],
            )
        video_matches = list_change_events(
            self.conn, event_type="metadata_update", object_type="video", query="filter-video"
        )
        self.assertEqual(video_matches["total"], 1)
        self.assertEqual(video_matches["items"][0]["object_id"], "telegram:12:filter-video")
        job_matches = list_change_events(self.conn, query="filter-audit-job")
        self.assertEqual(job_matches["total"], 1)
        self.assertEqual(list_change_events(self.conn, event_type="schedule_created")["total"], 0)
        with self.assertRaises(ValueError):
            list_change_events(self.conn, event_type="unsupported")

    def test_source_change_event_allowlists_fields_and_excludes_addresses(self):
        changes = [
            {"path": ["telegram", "channels", "-100111", "studio"], "before": "Old", "after": "New"},
            {"path": ["telegram", "channels", "-100111", "address"], "before": "old-secret", "after": "new-secret"},
            {"path": ["telegram", "channels", "-100222", "tags_whitelist"], "before": [], "after": ["scene"]},
        ]
        self.assertTrue(record_source_settings_change(self.db_url, revision=4, changes=changes))
        self.assertTrue(record_source_settings_change(self.db_url, revision=4, changes=changes),
                        "replaying an already committed event is successful and idempotent")
        item = list_change_events(self.conn)["items"][0]
        self.assertEqual(item["event_type"], "source_settings_update")
        self.assertEqual(item["object_id"], "managed_sources")
        self.assertEqual(item["changes"], {
            "telegram.channels.channel_1.studio": {"before": "Old", "after": "New"},
            "telegram.channels.channel_2.tags_whitelist": {"before": [], "after": ["scene"]},
        })
        self.assertFalse(item["truncated"])
        self.assertNotIn("old-secret", repr(item))
        self.assertNotIn("-100111", repr(item))
        self.assertNotIn("-100222", repr(item))

    def test_source_change_event_covers_non_telegram_fields_and_replay(self):
        changes = [
            {"path": ["exampleweb", "enabled"], "before": True, "after": False},
            {"path": ["exampleweb", "max_pages"], "before": 3, "after": 8},
            {"path": ["exampleweb", "address"], "before": "secret-url", "after": "other-secret-url"},
        ]
        self.assertTrue(record_source_settings_change(self.db_url, revision=5, changes=changes))
        self.assertTrue(record_source_settings_change(self.db_url, revision=5, changes=changes))
        result = list_change_events(self.conn)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["changes"], {
            "exampleweb.enabled": {"before": True, "after": False},
            "exampleweb.max_pages": {"before": 3, "after": 8},
        })
        self.assertNotIn("secret-url", repr(result))
