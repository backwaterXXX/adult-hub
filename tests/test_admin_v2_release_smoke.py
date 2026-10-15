import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode

from tools.admin_v2_release_smoke import RETIRED_ADMIN_API_ROUTES, _get_retired_classic_status, run_smoke


V2_STATUS = (200, "application/json", b'{"ok":true,"db":{"db_ok":true},'
             b'"sync":{"available":true,"running":false,"freshness":"fresh"},'
             b'"attention":{},"runtime":{"version":"1.2.3"},"capabilities":{}}')
V2_HOME_STATUS = (200, "application/json", b'{"ok":true,"db":{"db_ok":true},'
                 b'"sync":{"available":true,"running":false,"freshness":"fresh"},'
                 b'"attention":{},"runtime":{"version":"1.2.3"},"capabilities":{}}')
METADATA_STATUS = (200, "application/json", b'{"available":true,"catalog":{},"sources":[],'
                   b'"batch_limits":{"videos_per_run":500,"pbc_actors_per_run":50,'
                   b'"theporndb_actors_per_run":40,"stashdb_actors_per_run":35,"portraits_per_run":25},'
                   b'"sync_schedule":{"mode":"interval","daily_at":null,'
                   b'"interval_minutes":60,"run_on_startup":false}}')
QUALITY_SUMMARY = (200, "application/json", b'{"available":true,"status":"no_complete_audit",'
                  b'"latest_audit":null,"current_audit_id":null,"current_audit_state":null,'
                  b'"status_reason":"no_audit","counts":{},"total":0,"history":[],'
                  b'"fingerprint_coverage":{"available":true,"total_videos":10,"oshash":0,"phash":0}}')
QUALITY_ITEMS = (200, "application/json", b'{"audit_id":null,"items":[],"total":0,"limit":1,"offset":0}')
SOURCES_LIST = (200, "application/json", b'{"ok":true,"available":true,"sources":[],"desired_revision":0,"applied_revision":null,"applied_revision_available":true}')
SOURCES_HISTORY = (200, "application/json", json.dumps({
    "ok": True, "runs": [{
        "started_at": 100, "ended_at": 160, "managed_config_revision": 4,
        "duration_seconds": 60.0, "success": True, "sync_mode": "scheduled",
        "sources": [{"source": "telegram:-100private", "label": "Private channel",
                     "success": True, "added": 2, "removed": 0, "videos": 5,
                     "duration_seconds": 57.5}],
        "http": {"failed_attempts": 0, "retries": 1, "rate_limited": 0},
    }], "total": 1, "truncated": False, "available": True, "limit": 1,
}).encode())
ISSUES_PAGE = (200, "application/json", b'{"ok":true,"issues":[],"total":0,"offset":0,"limit":1,"truncated":false,"data":{"incomplete":false}}')
ISSUE_FAILURE = {"kind": "job_failure", "severity": "high", "status": "open",
                 "title": "Task failed", "message": "The sync operation failed.",
                 "object_type": "task", "object_id": "fixture-task",
                 "first_seen": "2026-10-10T08:28:08Z", "last_seen": "2026-10-10T08:28:08Z",
                 "action": {"label": "Open task details", "route": "/admin/v2/?job_id=fixture-task#jobs"},
                 "count": 1}
ISSUE_SYNC_FAILURE = {**ISSUE_FAILURE, "kind": "sync_failure"}
ISSUES_FILTERED_PAGE = (200, "application/json", json.dumps({
    "ok": True, "issues": [ISSUE_FAILURE], "total": 1, "offset": 0, "limit": 50,
    "truncated": False, "data": {"incomplete": False},
}).encode())
LIBRARY_PAGE = (200, "application/json", b'{"ok":true,"items":[],"page":{"limit":1,"offset":0,"total":0,"total_exact":true}}')
LIBRARY_SEARCH_TITLE = "Fixture searchable title"
LIBRARY_SEARCH_PERSON = "Fixture actor"
LIBRARY_SEARCH_ITEM = {
    "id": "telegram:-100123:42", "title": LIBRARY_SEARCH_TITLE,
    "performers": [LIBRARY_SEARCH_PERSON], "studio": "Fixture studio", "plot": "", "tags": [],
}
LIBRARY_SEARCH_PAGE = (200, "application/json", json.dumps({
    "ok": True, "items": [LIBRARY_SEARCH_ITEM],
    "page": {"limit": 50, "offset": 0, "total": 1, "total_exact": True},
}).encode())
DUPLICATES_PAGE = (200, "application/json", b'{"ok":true,"pairs":[],"total":0,"scanned":10,"complete":true,"candidate_limit_reached":false,"offset":0}')
AUDITS_PAGE = (200, "application/json", b'{"ok":true,"items":[],"total":0,"offset":0,"limit":1}')
ACTIVITY_PAGE = (200, "application/json", b'{"ok":true,"items":[],"total":0,"offset":0,"limit":1}')
SCHEDULES = (200, "application/json", b'{"ok":true,"items":[]}')
BACKUP_INVENTORY = (200, "application/json", b'{"ok":true,"available":true,"inventory_available":true,"items":[],"count":0,"total_bytes":0,"restore_test_available":false}')
PLAYBACK_STATUS = (200, "application/json", b'{"ok":true,"enabled":false,"active_count":0,"saved_count":0,"retention_days":7}')
PLAYBACK_REPORTS = (200, "application/json", b'{"ok":true,"items":[],"limit":1}')
PERFORMER_PROPOSALS = (200, "application/json", b'{"items":[],"total":0,"limit":1,"offset":0}')
PERFORMER_CATALOG = (200, "application/json", b'{"items":[]}')
PEOPLE_CATALOG = (200, "application/json", json.dumps({
    "ok": True, "available": True, "items": [{
        "name": "Fixture Performer", "video_count": 2, "has_bio": True,
        "has_photo": False, "aliases": ["Fixture Alias"],
    }], "total": 1, "limit": 100, "offset": 0,
}).encode())
PEOPLE_ALIAS_SEARCH = PEOPLE_CATALOG


def release_fetch(metadata_payload, quality_summary=QUALITY_SUMMARY, quality_items=QUALITY_ITEMS,
                  people_catalog=PEOPLE_CATALOG, people_alias_search=PEOPLE_ALIAS_SEARCH,
                  route_responses=None):
    route_responses = route_responses or {}
    def fetch(path):
        if path in route_responses:
            return route_responses[path]
        if path == "/admin/classic/":
            return 404, "text/html", b"Not Found"
        if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
            return 404, "application/json", b'{"detail":"Not Found"}'
        if path == "/admin/health":
            return 200, "application/json", b'{"ok":true}'
        if path in {"/admin/", "/admin/v2/"}:
            return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
        if path == "/admin/api/v2/status":
            return V2_HOME_STATUS
        if path == "/admin/api/v2/capabilities":
            return 200, "application/json", b'{"worker_ready":true,"actions_enabled":true,"worker":{"actions":["metadata_backfill"]}}'
        if path == "/admin/api/v2/metadata/enrichment":
            return 200, "application/json", json.dumps(metadata_payload).encode()
        if path == "/admin/api/v2/sources/list":
            return SOURCES_LIST
        if path == "/admin/api/v2/sources/sync-history?limit=1":
            return SOURCES_HISTORY
        if path == "/admin/api/v2/issues?limit=1&offset=0":
            return ISSUES_PAGE
        if path == "/admin/api/v2/issues?severity=high&kind=job_failure&limit=50&offset=0":
            return ISSUES_FILTERED_PAGE
        if path == "/admin/api/v2/library/videos?limit=1&offset=0":
            return LIBRARY_PAGE
        if path == "/admin/api/v2/library/videos?limit=50&offset=0":
            return LIBRARY_SEARCH_PAGE
        for term in (LIBRARY_SEARCH_TITLE, LIBRARY_SEARCH_PERSON, LIBRARY_SEARCH_ITEM["id"]):
            if path == "/admin/api/v2/library/videos?" + urlencode({
                    "q": term, "limit": 50, "offset": 0}):
                return LIBRARY_SEARCH_PAGE
        if path == "/admin/api/v2/duplicates?limit=1&offset=0&category=same_file":
            return DUPLICATES_PAGE
        if path == "/admin/api/v2/audits?limit=1&offset=0":
            return AUDITS_PAGE
        if path in {"/admin/api/v2/activity?limit=1&offset=0",
                    "/admin/api/v2/activity/changes?limit=1&offset=0"}:
            return ACTIVITY_PAGE
        if path == "/admin/api/v2/schedules":
            return SCHEDULES
        if path == "/admin/api/v2/backups":
            return BACKUP_INVENTORY
        if path == "/admin/api/v2/diagnostics/playback/status":
            return PLAYBACK_STATUS
        if path == "/admin/api/v2/diagnostics/playback/reports?limit=1":
            return PLAYBACK_REPORTS
        if path == "/admin/api/v2/performers/proposals?limit=1&offset=0":
            return PERFORMER_PROPOSALS
        if path == "/admin/api/v2/performers/catalog?limit=1":
            return PERFORMER_CATALOG
        if path == "/admin/api/v2/organize/people?limit=100&offset=0":
            return people_catalog
        if path == "/admin/api/v2/organize/people?" + urlencode({
                "q": "Fixture Alias", "limit": 100, "offset": 0}):
            return people_alias_search
        if path == "/admin/api/v2/quality/summary":
            return quality_summary
        if path == "/admin/api/v2/quality/items?limit=1&offset=0":
            return quality_items
        if path == "/admin/api/v2/jobs?action=metadata_backfill&state=all&limit=1":
            return ISSUES_FILTERED_PAGE if path.startswith("/admin/api/v2/issues?severity=") else (200, "application/json", b'{"ok":true,"jobs":[],"total":0}')
        raise AssertionError(f"unexpected URL path: {path}")

    return fetch


def ready_quality_summary(*, checked=13565, catalog_total=13565, inventory="partial"):
    payload = json.loads(QUALITY_SUMMARY[2])
    counts = {kind: 0 for kind in (
        "missing_metadata", "missing_nfo", "invalid_nfo", "nfo_metadata_mismatch", "missing_artwork",
    )}
    counts["missing_metadata"] = 1
    audit = {"id": "audit-1", "created_at": "2026-10-10T10:00:00Z",
             "checked": checked, "total": catalog_total,
             "library_inventory_status": inventory, "library_inventory_entries": 50000,
             "jellyfin_import_state": "unknown"}
    history = {**audit, "counts": counts, "open_total": 1}
    payload.update({"status": "ready", "total": 1, "counts": counts,
                    "latest_audit": audit, "history": [history]})
    return payload


class AdminV2ReleaseSmokeTests(unittest.TestCase):
    def test_enrichment_asset_release_gate_requires_no_store_and_safe_label(self):
        metadata = json.loads(METADATA_STATUS[2])
        body = (b"const source = String(row.source || '').trim().toLowerCase(); "
                b"return 'Telegram scene lookup';")

        def asset_fetcher(path, cache_control="no-store", asset_body=body):
            self.assertEqual(path, "/admin/assets-v2/enrichment.js")
            return 200, "text/javascript", {"Cache-Control": cache_control}, asset_body

        result = run_smoke(
            "https://admin.example", fetcher=release_fetch(metadata),
            asset_fetcher=asset_fetcher,
        )
        check = next(item for item in result if item.name == "enrichment_asset_uncached")
        self.assertTrue(check.passed, check.detail)

        stale_result = run_smoke(
            "https://admin.example", fetcher=release_fetch(metadata),
            asset_fetcher=lambda path: asset_fetcher(path, "no-cache, must-revalidate"),
        )
        stale_check = next(item for item in stale_result if item.name == "enrichment_asset_uncached")
        self.assertFalse(stale_check.passed)

        wrong_label_result = run_smoke(
            "https://admin.example", fetcher=release_fetch(metadata),
            asset_fetcher=lambda path: asset_fetcher(path, asset_body=b"return row.source;"),
        )
        wrong_label_check = next(item for item in wrong_label_result
                                 if item.name == "enrichment_asset_uncached")
        self.assertFalse(wrong_label_check.passed)

    def test_smoke_requires_classic_route_to_be_absent(self):
        metadata = json.loads(METADATA_STATUS[2])
        results = run_smoke("https://admin.example", fetcher=release_fetch(metadata))
        check = next(result for result in results if result.name == "admin_classic_retired")
        self.assertTrue(check.passed)
        self.assertEqual(check.detail, "HTTP 404")

    def test_smoke_fails_when_classic_route_redirects(self):
        metadata = json.loads(METADATA_STATUS[2])
        fetcher = release_fetch(metadata, route_responses={
            "/admin/classic/": (308, "text/html", b"Redirect"),
        })
        results = run_smoke("https://admin.example", fetcher=fetcher)
        check = next(result for result in results if result.name == "admin_classic_retired")
        self.assertFalse(check.passed)
        self.assertIn("HTTP 308", check.detail)

    def test_smoke_requires_every_retired_admin_api_route_to_be_absent(self):
        metadata = json.loads(METADATA_STATUS[2])
        results = run_smoke("https://admin.example", fetcher=release_fetch(metadata))
        retired = [item for item in results if item.name.startswith("admin_v1_")]
        self.assertEqual(len(retired), len(RETIRED_ADMIN_API_ROUTES))
        self.assertTrue(all(item.passed and item.detail == "HTTP 404" for item in retired))

    def test_smoke_fails_if_a_retired_admin_api_route_returns_or_redirects(self):
        metadata = json.loads(METADATA_STATUS[2])
        path = "/admin/api/jobs"
        for response in ((200, "application/json", b'{"items":[]}'),
                         (308, "text/html", b"Redirect")):
            with self.subTest(status=response[0]):
                results = run_smoke("https://admin.example", fetcher=release_fetch(
                    metadata, route_responses={path: response},
                ))
                check = next(item for item in results if item.name == "admin_v1_jobs_retired")
                self.assertFalse(check.passed)
                self.assertIn(f"HTTP {response[0]}", check.detail)

    def test_retired_classic_probe_does_not_follow_redirects(self):
        headers = Message()
        headers["Location"] = "/admin/"
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://admin.example/admin/classic/", 308, "Permanent Redirect", headers, None
        )
        with patch("tools.admin_v2_release_smoke.build_opener", return_value=opener):
            status = _get_retired_classic_status("https://admin.example", "read-token", 5)
        self.assertEqual(status, 308)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://admin.example/admin/classic/")
        self.assertEqual(request.get_header("Authorization"), "Bearer read-token")



    def test_passes_only_when_current_runtime_has_metadata_backfill(self):
        calls = []

        def fetch(path):
            calls.append(path)
            if path == "/admin/classic/":
                return 404, "text/html", b"Not Found"
            if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/health":
                return 200, "application/json", b'{"ok":true}'
            if path in {"/admin/", "/admin/v2/"}:
                return 200, "text/html; charset=utf-8", b'<link rel="stylesheet" href="/admin/assets-v2/admin-v2.css">'
            if path == "/admin/api/v2/status":
                return V2_HOME_STATUS
            if path == "/admin/api/v2/capabilities":
                payload = (b'{"worker_ready":true,"actions_enabled":true,'
                           b'"worker":{"actions":["metadata_backfill"]}}')
                return 200, "application/json", payload
            if path == "/admin/api/v2/metadata/enrichment":
                return METADATA_STATUS
            if path == "/admin/api/v2/sources/list":
                return SOURCES_LIST
            if path == "/admin/api/v2/sources/sync-history?limit=1":
                return SOURCES_HISTORY
            if path == "/admin/api/v2/issues?limit=1&offset=0":
                return ISSUES_PAGE
            if path == "/admin/api/v2/issues?severity=high&kind=job_failure&limit=50&offset=0":
                return ISSUES_FILTERED_PAGE
            if path == "/admin/api/v2/library/videos?limit=1&offset=0":
                return LIBRARY_PAGE
            if path == "/admin/api/v2/library/videos?limit=50&offset=0":
                return LIBRARY_SEARCH_PAGE
            for term in (LIBRARY_SEARCH_TITLE, LIBRARY_SEARCH_PERSON, LIBRARY_SEARCH_ITEM["id"]):
                if path == "/admin/api/v2/library/videos?" + urlencode({
                        "q": term, "limit": 50, "offset": 0}):
                    return LIBRARY_SEARCH_PAGE
            if path == "/admin/api/v2/duplicates?limit=1&offset=0&category=same_file":
                return DUPLICATES_PAGE
            if path == "/admin/api/v2/audits?limit=1&offset=0":
                return AUDITS_PAGE
            if path in {"/admin/api/v2/activity?limit=1&offset=0",
                        "/admin/api/v2/activity/changes?limit=1&offset=0"}:
                return ACTIVITY_PAGE
            if path == "/admin/api/v2/schedules":
                return SCHEDULES
            if path == "/admin/api/v2/backups":
                return BACKUP_INVENTORY
            if path == "/admin/api/v2/diagnostics/playback/status":
                return PLAYBACK_STATUS
            if path == "/admin/api/v2/diagnostics/playback/reports?limit=1":
                return PLAYBACK_REPORTS
            if path == "/admin/api/v2/performers/proposals?limit=1&offset=0":
                return PERFORMER_PROPOSALS
            if path == "/admin/api/v2/performers/catalog?limit=1":
                return PERFORMER_CATALOG
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return PEOPLE_CATALOG
            if path == "/admin/api/v2/organize/people?" + urlencode({
                    "q": "Fixture Alias", "limit": 100, "offset": 0}):
                return PEOPLE_ALIAS_SEARCH
            if path == "/admin/api/v2/quality/summary":
                return QUALITY_SUMMARY
            if path == "/admin/api/v2/quality/items?limit=1&offset=0":
                return QUALITY_ITEMS
            if path == "/admin/api/v2/jobs?action=metadata_backfill&state=all&limit=1":
                return ISSUES_FILTERED_PAGE if path.startswith("/admin/api/v2/issues?severity=") else (200, "application/json", b'{"ok":true,"jobs":[],"total":0}')
            self.fail(f"unexpected URL path: {path}")

        results = run_smoke("https://admin.example/", fetcher=fetch)
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual(len(calls), 37)
        self.assertEqual([result.name for result in results[:3]], [
            "health", "admin_primary_route", "admin_v2_page",
        ])
        self.assertTrue(all(result.passed for result in results[1:3]))
        self.assertEqual(results[3].name, "admin_classic_retired")
        self.assertEqual([result.name for result in results[4:10]], [name for name, _ in RETIRED_ADMIN_API_ROUTES])
        self.assertEqual(results[10].name, "admin_v2_status")
        self.assertEqual(results[11].name, "capabilities")
        self.assertEqual(results[29].name, "people_catalog")
        self.assertEqual(results[30].name, "people_alias_search")
        self.assertEqual(results[-3].name, "quality_summary")
        self.assertEqual(results[-2].name, "quality_items")
        self.assertEqual(results[-1].name, "metadata_backfill_job_filter")
        self.assertEqual([result.name for result in results[13:21]], [
            "sources_list", "sources_sync_history", "issues", "issues_filtered", "library", "library_search", "duplicates", "audit_history",
        ])
        self.assertEqual([result.name for result in results[27:29]], [
            "performer_proposals", "performer_catalog",
        ])
        self.assertEqual([result.name for result in results[21:27]], [
            "admin_activity", "admin_change_history", "schedules", "backup_inventory",
            "playback_diagnostics_status", "playback_diagnostics_reports",
        ])

    def test_fails_closed_on_unavailable_or_incomplete_core_sections(self):
        responses = {
            "/admin/api/v2/sources/list": (200, "application/json", b'{"ok":true,"available":false,"sources":[]}'),
            "/admin/api/v2/sources/sync-history?limit=1": (200, "application/json", b'{"ok":true,"available":false,"runs":[],"total":0,"limit":1,"truncated":false}'),
            "/admin/api/v2/issues?limit=1&offset=0": (200, "application/json", b'{"ok":true,"issues":[],"total":0,"offset":0,"limit":1,"truncated":false,"data":{"incomplete":true}}'),
            "/admin/api/v2/issues?severity=high&kind=job_failure&limit=50&offset=0": (
                200, "application/json", json.dumps({
                    "ok": True, "issues": [ISSUE_SYNC_FAILURE], "total": 1, "offset": 0,
                    "limit": 50, "truncated": False, "data": {"incomplete": False},
                }).encode()),
            "/admin/api/v2/library/videos?limit=1&offset=0": (200, "application/json", b'{"ok":true,"items":[],"page":{"limit":1,"offset":0,"total":0,"total_exact":false}}'),
            "/admin/api/v2/duplicates?limit=1&offset=0&category=same_file": (200, "application/json", b'{"ok":true,"pairs":[],"total":0,"scanned":0,"complete":false,"candidate_limit_reached":false,"offset":0}'),
            "/admin/api/v2/audits?limit=1&offset=0": (200, "application/json", b'{"ok":true,"items":[],"total":0,"offset":0,"limit":2}'),
            "/admin/api/v2/performers/proposals?limit=1&offset=0": (200, "application/json", b'{"items":[],"total":true,"limit":1,"offset":0}'),
            "/admin/api/v2/performers/catalog?limit=1": (200, "application/json", b'{"items":[{"id":1,"name":"Fixture"}]}'),
            "/admin/api/v2/activity?limit=1&offset=0": (200, "application/json", b'{"ok":true,"items":[],"total":true,"offset":0,"limit":1}'),
            "/admin/api/v2/activity/changes?limit=1&offset=0": (200, "application/json", b'{"ok":true,"items":[],"total":0,"offset":0,"limit":2}'),
            "/admin/api/v2/schedules": (200, "application/json", b'{"ok":true,"items":[{"enabled":"yes","revision":1}]}'),
            "/admin/api/v2/backups": (200, "application/json", b'{"ok":true,"available":true,"inventory_available":false,"items":[],"count":0,"restore_test_available":false}'),
            "/admin/api/v2/diagnostics/playback/status": (200, "application/json", b'{"ok":true,"enabled":"off","active_count":0,"saved_count":0,"retention_days":7}'),
            "/admin/api/v2/diagnostics/playback/reports?limit=1": (200, "application/json", b'{"ok":true,"items":[],"limit":2}'),
        }
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), route_responses=responses))
        checks = {result.name: result for result in results}
        expected = {
            "sources_list": "Sources configuration is unavailable",
            "sources_sync_history": "Source sync history is unavailable",
            "issues": "Issue Center data is incomplete",
            "issues_filtered": "Issue Center filters returned a non-matching item",
            "library": "Library page has an unexpected or inexact shape",
            "duplicates": "Same-file duplicate scan is incomplete or has an unexpected shape",
            "audit_history": "Audit history has an unexpected shape",
            "performer_proposals": "Performer proposals have an unexpected shape",
            "performer_catalog": "Performer catalogue has an unexpected shape",
            "admin_activity": "Admin activity page has an unexpected shape",
            "admin_change_history": "Admin activity page has an unexpected shape",
            "schedules": "Schedule inventory has an unexpected item shape",
            "backup_inventory": "Backup inventory is unavailable or has an unexpected shape",
            "playback_diagnostics_status": "Playback diagnostics status has an unexpected shape",
            "playback_diagnostics_reports": "Playback diagnostics reports have an unexpected shape",
        }
        for name, detail in expected.items():
            with self.subTest(name=name):
                self.assertFalse(checks[name].passed)
                self.assertEqual(checks[name].detail, detail)

    def test_library_search_fails_when_api_ignores_sampled_title(self):
        ignored_filter = (200, "application/json", json.dumps({
            "ok": True,
            "items": [{**LIBRARY_SEARCH_ITEM, "id": "unrelated", "title": "Different title",
                       "studio": "Another studio", "plot": "", "performers": [], "tags": []}],
            "page": {"limit": 50, "offset": 0, "total": 1, "total_exact": True},
        }).encode())
        query_path = "/admin/api/v2/library/videos?" + urlencode({
            "q": LIBRARY_SEARCH_TITLE, "limit": 50, "offset": 0,
        })
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), route_responses={query_path: ignored_filter}))
        check = next(result for result in results if result.name == "library_search")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "Library title search returned a non-matching item")
        self.assertNotIn(LIBRARY_SEARCH_TITLE, json.dumps([check.name, check.detail]))

    def test_library_search_fails_when_api_ignores_sampled_id_or_person(self):
        ignored_filter = (200, "application/json", json.dumps({
            "ok": True,
            "items": [{"id": "unrelated", "title": "Different title", "performers": [],
                       "studio": "Another studio", "plot": "", "tags": []}],
            "page": {"limit": 50, "offset": 0, "total": 1, "total_exact": True},
        }).encode())
        for term, label in ((LIBRARY_SEARCH_ITEM["id"], "ID"),
                            (LIBRARY_SEARCH_PERSON, "person")):
            with self.subTest(field=label):
                query_path = "/admin/api/v2/library/videos?" + urlencode({
                    "q": term, "limit": 50, "offset": 0,
                })
                results = run_smoke("https://admin.example", fetcher=release_fetch(
                    json.loads(METADATA_STATUS[2]), route_responses={query_path: ignored_filter}))
                check = next(result for result in results if result.name == "library_search")
                self.assertFalse(check.passed)
                self.assertEqual(check.detail, f"Library {label} search returned a non-matching item")
                self.assertNotIn(term, json.dumps([check.name, check.detail]))

    def test_issue_filter_smoke_rejects_nonmatching_issue(self):
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), route_responses={
                "/admin/api/v2/issues?severity=high&kind=job_failure&limit=50&offset=0": (
                    200, "application/json", json.dumps({
                        "ok": True, "issues": [ISSUE_SYNC_FAILURE], "total": 1, "offset": 0,
                        "limit": 50, "truncated": False, "data": {"incomplete": False},
                    }).encode(),
                ),
            }))
        check = next(result for result in results if result.name == "issues_filtered")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "Issue Center filters returned a non-matching item")

    def test_fails_closed_when_deployed_worker_lacks_action_even_if_jobs_returns_200(self):
        def fetch(path):
            if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/health":
                return 200, "application/json", b'{"ok":true}'
            if path in {"/admin/", "/admin/v2/"}:
                return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
            if path == "/admin/api/v2/status":
                return V2_STATUS
            if path == "/admin/api/v2/capabilities":
                return 200, "application/json", b'{"worker_ready":true,"actions_enabled":true,"worker":{"actions":["sync"]}}'
            if path == "/admin/api/v2/metadata/enrichment":
                return METADATA_STATUS
            return ISSUES_FILTERED_PAGE if path.startswith("/admin/api/v2/issues?severity=") else (200, "application/json", b'{"ok":true,"jobs":[],"total":0}')

        results = run_smoke("https://admin.example", fetcher=fetch)
        self.assertTrue(results[1].passed)  # primary route still responds
        self.assertTrue(results[2].passed)  # direct V2 route
        capability_check = next(result for result in results if result.name == "capabilities")
        self.assertFalse(capability_check.passed)
        self.assertIn("does not support metadata_backfill", capability_check.detail)
        self.assertFalse(all(result.passed for result in results))

    def test_fails_when_action_filter_is_rejected_by_old_jobs_api(self):
        def fetch(path):
            if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/health":
                return 200, "application/json", b'{"ok":true}'
            if path in {"/admin/", "/admin/v2/"}:
                return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
            if path == "/admin/api/v2/status":
                return V2_STATUS
            if path == "/admin/api/v2/capabilities":
                return 200, "application/json", b'{"worker_ready":true,"actions_enabled":true,"worker":{"actions":["metadata_backfill"]}}'
            if path == "/admin/api/v2/metadata/enrichment":
                return METADATA_STATUS
            return 400, "application/json", b'{"error":"Invalid job action filter."}'

        results = run_smoke("https://admin.example", fetcher=fetch)
        self.assertFalse(results[-1].passed)
        self.assertEqual(results[-1].detail, "HTTP 400")






    def test_fails_cleanly_on_malformed_worker_action_list(self):
        def fetch(path):
            if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/health":
                return 200, "application/json", b'{"ok":true}'
            if path in {"/admin/", "/admin/v2/"}:
                return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
            if path == "/admin/api/v2/status":
                return V2_STATUS
            if path == "/admin/api/v2/capabilities":
                return 200, "application/json", b'{"worker_ready":true,"actions_enabled":true,"worker":{"actions":null}}'
            if path == "/admin/api/v2/metadata/enrichment":
                return METADATA_STATUS
            return ISSUES_FILTERED_PAGE if path.startswith("/admin/api/v2/issues?severity=") else (200, "application/json", b'{"ok":true,"jobs":[],"total":0}')

        results = run_smoke("https://admin.example", fetcher=fetch)
        status_check = next(result for result in results if result.name == "admin_v2_status")
        capability_check = next(result for result in results if result.name == "capabilities")
        self.assertTrue(status_check.passed)
        self.assertFalse(capability_check.passed)
        self.assertEqual(capability_check.detail, "worker action list is invalid")

    def test_fails_when_previous_release_does_not_serve_v2_status(self):
        def fetch(path):
            if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/health":
                return 200, "application/json", b'{"ok":true}'
            if path == "/admin/":
                return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
            if path == "/admin/v2/":
                return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
            if path == "/admin/api/v2/status":
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/api/v2/capabilities":
                return 200, "application/json", b'{"worker_ready":true,"actions_enabled":true,"worker":{"actions":["metadata_backfill"]}}'
            if path == "/admin/api/v2/metadata/enrichment":
                return METADATA_STATUS
            return ISSUES_FILTERED_PAGE if path.startswith("/admin/api/v2/issues?severity=") else (200, "application/json", b'{"ok":true,"jobs":[],"total":0}')

        results = run_smoke("https://admin.example", fetcher=fetch)
        status_check = next(result for result in results if result.name == "admin_v2_status")
        self.assertFalse(status_check.passed)
        self.assertEqual(status_check.detail, "HTTP 404")
        self.assertFalse(all(result.passed for result in results))

    def test_fails_when_primary_route_does_not_serve_v2(self):
        def fetch(path):
            if path in {route for _, route in RETIRED_ADMIN_API_ROUTES}:
                return 404, "application/json", b'{"detail":"Not Found"}'
            if path == "/admin/health":
                return 200, "application/json", b'{"ok":true}'
            if path == "/admin/":
                return 200, "text/html", b'/admin/assets/admin.css'
            if path == "/admin/v2/":
                return 200, "text/html", b'/admin/assets-v2/admin-v2.css'
            if path == "/admin/api/v2/status":
                return V2_STATUS
            if path == "/admin/api/v2/capabilities":
                return 200, "application/json", b'{"worker_ready":true,"actions_enabled":true,"worker":{"actions":["metadata_backfill"]}}'
            if path == "/admin/api/v2/metadata/enrichment":
                return METADATA_STATUS
            return ISSUES_FILTERED_PAGE if path.startswith("/admin/api/v2/issues?severity=") else (200, "application/json", b'{"ok":true,"jobs":[],"total":0}')

        results = run_smoke("https://admin.example", fetcher=fetch)
        primary = next(result for result in results if result.name == "admin_primary_route")
        self.assertFalse(primary.passed)
        self.assertIn("expected matching Admin HTML", primary.detail)
        self.assertTrue(next(result for result in results
                             if result.name == "admin_v2_page").passed)

    def test_rejects_credentials_and_non_http_base_urls(self):
        for base_url in ("file:///etc/passwd", "https://user:secret@example.com", "https://example.com/?token=x"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                run_smoke(base_url, fetcher=lambda _path: self.fail("must reject before requests"))

    def test_fails_when_metadata_status_omits_configured_limits_and_cadence(self):
        payload = {"available": True, "catalog": {}, "sources": []}
        results = run_smoke("https://admin.example", fetcher=release_fetch(payload))
        metadata = next(result for result in results if result.name == "metadata_status")
        self.assertFalse(metadata.passed)
        self.assertEqual(metadata.detail, "configured metadata batch limits are missing or invalid")

    def test_fails_closed_when_people_catalog_or_alias_shape_is_unavailable(self):
        valid = json.loads(PEOPLE_CATALOG[2])
        malformed = json.loads(json.dumps(valid))
        malformed["items"] = [{"name": "Actor", "video_count": 2, "has_bio": True,
                                "has_photo": False, "aliases": "not-a-list"}]
        unavailable = json.loads(json.dumps(valid))
        unavailable["available"] = False
        for payload, expected in (
            ((503, "application/json", b'{"ok":false}'), "HTTP 503"),
            ((200, "application/json", json.dumps(malformed).encode()),
             "People catalog response has an unexpected item shape"),
            ((200, "application/json", json.dumps(unavailable).encode()),
             "People catalog is unavailable"),
        ):
            with self.subTest(expected=expected):
                results = run_smoke("https://admin.example", fetcher=release_fetch(
                    json.loads(METADATA_STATUS[2]), people_catalog=payload))
                check = next(result for result in results if result.name == "people_catalog")
                self.assertFalse(check.passed)
                self.assertEqual(check.detail, expected)

    def test_people_alias_search_must_return_the_canonical_person(self):
        wrong_person = (200, "application/json", json.dumps({
            "ok": True, "available": True, "items": [{
                "name": "Different Performer", "aliases": ["Fixture Alias"],
            }], "total": 1, "limit": 100, "offset": 0,
        }).encode())
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), people_alias_search=wrong_person))
        check = next(result for result in results if result.name == "people_alias_search")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "People alias search did not return its canonical person")

    def test_people_alias_search_skips_aliases_that_can_match_the_canonical_name(self):
        metadata = json.loads(METADATA_STATUS[2])
        base_fetch = release_fetch(metadata)
        catalog = (200, "application/json", json.dumps({
            "ok": True, "available": True, "items": [{
                "name": "Fixture Performer", "video_count": 2, "has_bio": True,
                "has_photo": False, "aliases": ["Fixture Performer", "Independent Alias"],
            }], "total": 1, "limit": 100, "offset": 0,
        }).encode())
        alias_result = (200, "application/json", json.dumps({
            "ok": True, "available": True, "items": [{
                "name": "Fixture Performer", "aliases": ["Independent Alias"],
            }], "total": 1, "limit": 100, "offset": 0,
        }).encode())
        calls = []

        def fetch(path):
            calls.append(path)
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return catalog
            if "q=Independent+Alias" in path:
                return alias_result
            if "q=Fixture+Performer" in path:
                self.fail("an alias searchable by canonical name must not prove alias search")
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=fetch)
        self.assertTrue(all(result.passed for result in results))
        self.assertTrue(any("q=Independent+Alias" in path for path in calls))

    def test_people_alias_search_follows_pages_to_find_the_sampled_person(self):
        metadata = json.loads(METADATA_STATUS[2])
        base_fetch = release_fetch(metadata)
        first_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": f"Other Performer {index:03d}", "aliases": []}
                      for index in range(100)],
            "total": 101, "limit": 100, "offset": 0,
        }).encode())
        second_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": "Fixture Performer", "aliases": ["Fixture Alias"]}],
            "total": 101, "limit": 100, "offset": 100,
        }).encode())
        calls = []

        def fetch(path):
            calls.append(path)
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return PEOPLE_CATALOG
            if path == "/admin/api/v2/organize/people?q=Fixture+Alias&limit=100&offset=0":
                return first_page
            if path == "/admin/api/v2/organize/people?q=Fixture+Alias&limit=100&offset=100":
                return second_page
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=fetch)
        self.assertTrue(all(result.passed for result in results))
        self.assertIn("/admin/api/v2/organize/people?q=Fixture+Alias&limit=100&offset=100", calls)

    def test_people_alias_search_stops_after_1000_results(self):
        metadata = json.loads(METADATA_STATUS[2])
        base_fetch = release_fetch(metadata)
        calls = []

        def result_page(offset):
            return (200, "application/json", json.dumps({
                "ok": True, "available": True,
                "items": [{"name": f"Other Performer {offset + index:04d}", "aliases": []}
                          for index in range(100)],
                "total": 1500, "limit": 100, "offset": offset,
            }).encode())

        def fetch(path):
            calls.append(path)
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return PEOPLE_CATALOG
            if path.startswith("/admin/api/v2/organize/people?q=Fixture+Alias&"):
                offset = int(path.rsplit("offset=", 1)[1])
                return result_page(offset)
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=fetch)
        check = next(result for result in results if result.name == "people_alias_search")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail,
                         "People alias search exceeded the bounded verification window")
        searched_offsets = [int(path.rsplit("offset=", 1)[1]) for path in calls
                            if path.startswith("/admin/api/v2/organize/people?q=")]
        self.assertEqual(searched_offsets, list(range(0, 1000, 100)))

    def test_people_alias_search_rejects_underfilled_pages(self):
        metadata = json.loads(METADATA_STATUS[2])
        base_fetch = release_fetch(metadata)
        incomplete_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": f"Other Performer {index:03d}", "aliases": []}
                      for index in range(50)],
            "total": 101, "limit": 100, "offset": 0,
        }).encode())
        calls = []

        def fetch(path):
            calls.append(path)
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return PEOPLE_CATALOG
            if path.startswith("/admin/api/v2/organize/people?q=Fixture+Alias&"):
                return incomplete_page
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=fetch)
        checks = {result.name: result for result in results}
        self.assertTrue(checks["people_catalog"].passed)
        self.assertFalse(checks["people_alias_search"].passed)
        self.assertEqual(checks["people_alias_search"].detail,
                         "People alias search returned an incomplete page")
        self.assertFalse(any("offset=100" in path for path in calls
                             if path.startswith("/admin/api/v2/organize/people?q=")))

    def test_people_alias_search_is_a_validated_noop_when_catalog_has_no_aliases(self):
        no_aliases = (200, "application/json", json.dumps({
            "ok": True, "available": True, "items": [{
                "name": "Fixture Performer", "video_count": 2, "has_bio": True,
                "has_photo": False, "aliases": [],
            }], "total": 1, "limit": 100, "offset": 0,
        }).encode())
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), people_catalog=no_aliases))
        check = next(result for result in results if result.name == "people_alias_search")
        self.assertTrue(check.passed)
        self.assertEqual(check.detail, "No alias sample in the bounded people scan")

    def test_people_alias_sample_searches_later_pages_but_stops_at_a_fixed_bound(self):
        metadata = json.loads(METADATA_STATUS[2])
        base_fetch = release_fetch(metadata)
        first_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": f"Performer {index:03d}", "video_count": 1,
                       "has_bio": False, "has_photo": False, "aliases": []}
                      for index in range(100)],
            "total": 101, "limit": 100, "offset": 0,
        }).encode())
        second_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": "Late Performer", "video_count": 1,
                       "has_bio": True, "has_photo": False, "aliases": ["Later Alias"]}],
            "total": 101, "limit": 100, "offset": 100,
        }).encode())
        alias_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": "Late Performer", "video_count": 1,
                       "has_bio": True, "has_photo": False, "aliases": ["Later Alias"]}],
            "total": 1, "limit": 100, "offset": 0,
        }).encode())
        calls = []

        def fetch(path):
            calls.append(path)
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return first_page
            if path == "/admin/api/v2/organize/people?limit=100&offset=100":
                return second_page
            if "q=Later+Alias" in path:
                return alias_page
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=fetch)
        self.assertTrue(all(result.passed for result in results))
        self.assertIn("/admin/api/v2/organize/people?limit=100&offset=100", calls)
        self.assertTrue(any("q=Later+Alias" in path for path in calls))

        calls.clear()
        def huge_page_payload(offset):
            return (200, "application/json", json.dumps({
                "ok": True, "available": True,
                "items": [{"name": f"Performer {offset + index:04d}", "video_count": 1,
                           "has_bio": False, "has_photo": False, "aliases": []}
                          for index in range(100)],
                "total": 2500, "limit": 100, "offset": offset,
            }).encode())

        def bounded_fetch(path):
            calls.append(path)
            if path.startswith("/admin/api/v2/organize/people?") and "q=" not in path:
                offset = int(path.split("offset=", 1)[1])
                return huge_page_payload(offset)
            if "q=" in path and path.startswith("/admin/api/v2/organize/people?"):
                self.fail("alias query should not run when the bounded sample has no aliases")
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=bounded_fetch)
        self.assertTrue(all(result.passed for result in results))
        sampled_offsets = [int(path.split("offset=", 1)[1]) for path in calls
                           if path.startswith("/admin/api/v2/organize/people?") and "q=" not in path]
        self.assertEqual(sampled_offsets, list(range(0, 1000, 100)))

    def test_people_alias_check_fails_when_a_later_catalog_page_is_invalid(self):
        base_fetch = release_fetch(json.loads(METADATA_STATUS[2]))
        first_page = (200, "application/json", json.dumps({
            "ok": True, "available": True,
            "items": [{"name": f"Performer {index:03d}", "video_count": 1,
                       "has_bio": False, "has_photo": False, "aliases": []}
                      for index in range(100)],
            "total": 101, "limit": 100, "offset": 0,
        }).encode())
        invalid_later_page = (200, "application/json", json.dumps({
            "ok": True, "available": True, "items": "unexpected",
            "total": 101, "limit": 100, "offset": 100,
        }).encode())

        def fetch(path):
            if path == "/admin/api/v2/organize/people?limit=100&offset=0":
                return first_page
            if path == "/admin/api/v2/organize/people?limit=100&offset=100":
                return invalid_later_page
            return base_fetch(path)

        results = run_smoke("https://admin.example", fetcher=fetch)
        catalog = next(result for result in results if result.name == "people_catalog")
        alias_search = next(result for result in results if result.name == "people_alias_search")
        self.assertFalse(catalog.passed)
        self.assertEqual(catalog.detail, "People catalog response has an unexpected page shape")
        self.assertFalse(alias_search.passed)
        self.assertEqual(alias_search.detail, "People catalog check failed")

    def test_fails_closed_on_invalid_metadata_schedule_or_batch_limit(self):
        valid = json.loads(METADATA_STATUS[2])
        invalid_limit = json.loads(json.dumps(valid))
        invalid_limit["batch_limits"]["videos_per_run"] = True
        invalid_schedule = json.loads(json.dumps(valid))
        invalid_schedule["sync_schedule"] = {
            "mode": "daily", "daily_at": "25:99", "interval_minutes": None,
            "run_on_startup": False,
        }
        for payload, expected in (
            (invalid_limit, "configured metadata batch limits are missing or invalid"),
            (invalid_schedule, "configured metadata sync schedule is missing or invalid"),
        ):
            with self.subTest(expected=expected):
                results = run_smoke("https://admin.example", fetcher=release_fetch(payload))
                metadata = next(result for result in results if result.name == "metadata_status")
                self.assertFalse(metadata.passed)
                self.assertEqual(metadata.detail, expected)

    def test_quality_summary_accepts_explicit_no_audit_state_but_rejects_false_zero(self):
        valid = json.loads(QUALITY_SUMMARY[2])
        invalid_total = json.loads(json.dumps(valid))
        invalid_total["total"] = 1
        missing_reason = json.loads(json.dumps(valid))
        del missing_reason["status_reason"]
        missing_reason["current_audit_id"] = "audit-1"
        missing_reason["current_audit_state"] = "done"
        for payload, expected in (
            (invalid_total, "Quality summary reports findings without a complete audit"),
            (missing_reason, "Quality summary does not explain missing complete audit"),
        ):
            with self.subTest(expected=expected):
                results = run_smoke("https://admin.example", fetcher=release_fetch(
                    json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
                check = next(result for result in results if result.name == "quality_summary")
                self.assertFalse(check.passed)
                self.assertEqual(check.detail, expected)

    def test_quality_summary_accepts_legacy_no_audit_shape_when_no_audit_exists(self):
        payload = json.loads(QUALITY_SUMMARY[2])
        del payload["status_reason"]
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_summary")
        self.assertTrue(check.passed, check.detail)

    def test_quality_summary_accepts_legacy_audit_with_explicit_unverified_coverage(self):
        payload = json.loads(QUALITY_SUMMARY[2])
        payload.update({"current_audit_id": "audit-1", "current_audit_state": "done",
                        "status_reason": "legacy_missing_catalog_total"})
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_summary")
        self.assertTrue(check.passed, check.detail)

    def test_quality_ready_state_requires_a_verified_complete_audit(self):
        payload = json.loads(QUALITY_SUMMARY[2])
        payload.update({"status": "ready", "latest_audit": None})
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_summary")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "Quality summary lacks verified full catalog coverage")

    def test_quality_ready_accepts_full_catalog_coverage_with_partial_file_inventory(self):
        payload = ready_quality_summary()
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_summary")
        self.assertTrue(check.passed, check.detail)

    def test_quality_ready_rejects_partial_catalog_coverage_even_if_file_inventory_is_complete(self):
        payload = ready_quality_summary(checked=13564, inventory="complete")
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_summary")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "Quality summary lacks verified full catalog coverage")

    def test_quality_ready_rejects_incomplete_audit_in_trend(self):
        payload = ready_quality_summary()
        payload["history"].append({"id": "partial-audit", "checked": 10, "total": 11,
                                   "library_inventory_status": "complete", "open_total": 0,
                                   "counts": {kind: 0 for kind in payload["counts"]}})
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_summary=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_summary")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "Quality summary history contains an incomplete catalog audit")

    def test_quality_findings_page_rejects_findings_without_complete_audit(self):
        payload = {"audit_id": None, "items": [{"kind": "missing_nfo"}],
                   "total": 1, "limit": 1, "offset": 0}
        results = run_smoke("https://admin.example", fetcher=release_fetch(
            json.loads(METADATA_STATUS[2]), quality_items=(200, "application/json", json.dumps(payload).encode())))
        check = next(result for result in results if result.name == "quality_items")
        self.assertFalse(check.passed)
        self.assertEqual(check.detail, "Quality findings are present without a complete audit")


if __name__ == "__main__":
    unittest.main()
