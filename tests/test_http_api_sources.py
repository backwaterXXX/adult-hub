"""Request-level checks for resolver sources, people catalog, and media streams."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pg_helper import TEST_URL
from common import db
import resolver.admin as admin_module
import resolver.app as resolver_app
from tests.helpers.db_session import db_session
from tests.test_http_api import BAD_URL, BaseHttpApiTestCase


class TestResolverHttpApiSources(BaseHttpApiTestCase):
    def test_http_range_with_unknown_size_streams_full_resource_as_200(self):
        calls = []

        class FakeTelegram:
            async def resolve_stream(self, ref):
                return "tg://fake-reference"

            async def file_size(self, ref):
                return None

            async def download_range(self, ref, offset, length):
                calls.append((ref, offset, length))
                yield b"full-"
                yield b"video"

        with patch.object(resolver_app, "get_provider", return_value=FakeTelegram()):
            response = self.request(
                "GET", "/admin/play/telegram:fake-id/stream",
                headers={"Range": "bytes=-100"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"full-video")
        self.assertEqual(calls, [("fake-reference", 0, None)])


    def test_client_phash_intake_is_disabled_by_default_and_does_not_accept_admin_token(self):
        path = "/admin/api/v2/video-phash/frames"
        with self.admin_config(), patch.dict(os.environ, {"PHASH_UPLOAD_ENABLED": "false"}):
            disabled = self.request(
                "POST", path, base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={},
            )
        self.assertEqual(disabled.status_code, 404)

        with self.admin_config(), patch.dict(os.environ, {
            "PHASH_UPLOAD_ENABLED": "true",
        }):
            admin_token_only = self.request(
                "POST", path, base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={},
            )
        self.assertEqual(admin_token_only.status_code, 401)


    def test_performer_proposal_id_is_validated_after_write_authorization(self):
        path = "/admin/api/v2/performers/proposals/not-an-id"
        with self.admin_config():
            denied = self.request("POST", path, base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={"action": "reject"})
            allowed = self.request("POST", path, base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"action": "reject"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 400)
        self.assertIn("positive integer", allowed.json()["error"]["message"])


    def test_v2_people_catalog_paginates_and_exposes_safe_filmography(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            with db_session(TEST_URL) as conn:
                conn.execute(
                    "INSERT INTO videos (id,provider,title,performers,categories,tags,studio) "
                    "VALUES ('telegram:99:actor','telegram','Actor scene','[\"Alex Actor\"]','[]','[]','Studio')"
                )
                conn.execute("INSERT INTO performer_bios (name,bio,aliases) VALUES ('Alex Actor','Biography',%s)",
                             (json.dumps(["Alexandra Stage"]),))
                conn.execute("INSERT INTO performer_aliases (alias,canonical) VALUES ('Alex A','Alex Actor')")
                conn.execute("INSERT INTO performer_photos (name,thumb) VALUES ('Alex Actor','https://example.org/private.jpg')")
                conn.execute(
                    "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) "
                    "VALUES ('telegram:99:actor','telegram:99:actor','operation-safe','active',1,%s)",
                    (json.dumps({"video": {"title": "Actor scene", "provider": "telegram"}, "files": ["private/path"]}),)
                )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                page = self.request("GET", "/admin/api/v2/organize/people?q=Alex&limit=10", base_url="http://localhost")
                bio_alias_search = self.request("GET", "/admin/api/v2/organize/people?q=Alexandra%20Stage", base_url="http://localhost")
                mapped_alias_search = self.request("GET", "/admin/api/v2/organize/people?q=Alex%20A", base_url="http://localhost")
                detail = self.request("GET", "/admin/api/v2/organize/people/Alex%20Actor", base_url="http://localhost")
                quarantine = self.request("GET", "/admin/api/v2/organize/quarantine?limit=10", base_url="http://localhost")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 1)
        self.assertTrue(page.json()["items"][0]["has_bio"])
        self.assertTrue(page.json()["items"][0]["has_photo"])
        self.assertEqual(page.json()["items"][0]["video_count"], 1)
        self.assertEqual(page.json()["items"][0]["aliases"], ["Alexandra Stage", "Alex A"])
        for response in (bio_alias_search, mapped_alias_search):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual(response.json()["items"][0]["name"], "Alex Actor")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["filmography"][0]["id"], "telegram:99:actor")
        self.assertEqual(detail.json()["filmography"][0]["thumbnail"], None)
        self.assertEqual(detail.json()["total"], 1)
        self.assertEqual(detail.json()["offset"], 0)
        self.assertEqual(detail.json()["limit"], 50)
        self.assertEqual(quarantine.status_code, 200, quarantine.text)
        self.assertEqual(quarantine.json()["items"][0]["mode"], "quarantine")
        self.assertNotIn("private/path", quarantine.text)


    def test_v2_people_catalog_loads_mapped_aliases_once_per_page(self):
        """Aggregating aliases inside the grouped page query makes cost grow with OFFSET."""
        with tempfile.TemporaryDirectory(), self.admin_config():
            with db_session(TEST_URL) as conn:
                for index in range(3):
                    name = f"Paginated Actor {index}"
                    conn.execute(
                        "INSERT INTO videos (id,provider,title,performers,categories,tags,studio) "
                        "VALUES (%s,'telegram','Scene','[]','[]','[]','Studio')",
                        (f"telegram:99:page-{index}",),
                    )
                    conn.execute(
                        "INSERT INTO video_performers (video_id,performer_name,position) VALUES (%s,%s,0)",
                        (f"telegram:99:page-{index}", name),
                    )
                    conn.execute("INSERT INTO performer_aliases (alias,canonical) VALUES (%s,%s)",
                                 (f"{name} alias", name))

            executed = []
            real_connect = admin_module.admin_v2_organize.db.connect

            class RecordingConnection:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=None):
                    executed.append(" ".join(str(sql).split()))
                    return self._inner.execute(sql, params)

                def close(self):
                    self._inner.close()

                def __getattr__(self, item):
                    return getattr(self._inner, item)

            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module.admin_v2_organize.db, "connect",
                              lambda url: RecordingConnection(real_connect(url))):
                page = self.request("GET", "/admin/api/v2/organize/people?limit=2&offset=1",
                                    base_url="http://localhost")

        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual([item["name"] for item in page.json()["items"]],
                         ["Paginated Actor 1", "Paginated Actor 2"])
        self.assertEqual(page.json()["items"][1]["aliases"], ["Paginated Actor 2 alias"],
                         "a later page carries its aliases like the first one")
        listing = [sql for sql in executed if "FROM video_performers p" in sql and "GROUP BY" in sql]
        self.assertEqual(len(listing), 1)
        self.assertNotIn("performer_aliases", listing[0],
                         "the page query must not aggregate aliases per row")
        self.assertEqual(len([sql for sql in executed if "jsonb_agg(pa.alias" in sql]), 1,
                         "one alias lookup covers the whole page")


    def test_v2_person_filmography_paginates_large_catalog_with_stable_order(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            with db_session(TEST_URL) as conn:
                for index in range(105):
                    video_id = f"telegram:99:filmography-{index:03d}"
                    conn.execute(
                        "INSERT INTO videos (id,provider,title,performers,categories,tags,studio) "
                        "VALUES (%s,'telegram','Same title','[]','[]','[]','Studio')",
                        (video_id,),
                    )
                    conn.execute(
                        "INSERT INTO video_performers (video_id,performer_name,position) VALUES (%s,'Pagination Actor',0)",
                        (video_id,),
                    )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                first = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?limit=50", base_url="http://localhost")
                second = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?offset=50&limit=50", base_url="http://localhost")
                last = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?offset=100&limit=50", base_url="http://localhost")
                invalid = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?offset=-1", base_url="http://localhost")
                too_large = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?limit=101", base_url="http://localhost")
        for response in (first, second, last):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 105)
            self.assertEqual(response.json()["limit"], 50)
        self.assertEqual([len(first.json()["filmography"]), len(second.json()["filmography"]), len(last.json()["filmography"])], [50, 50, 5])
        ids = [video["id"] for response in (first, second, last) for video in response.json()["filmography"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [f"telegram:99:filmography-{index:03d}" for index in range(105)])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(too_large.status_code, 400)


    def test_source_access_check_uses_existing_provider_and_sanitizes_failures(self):
        async def oversized_stream():
            yield b'{"source_id":"-100111","padding":"'
            yield b"x" * 5000
            yield b'"}'

        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n        enabled: false\n",
                encoding="utf-8",
            )
            provider = Mock()
            provider.check_configured_channel_access = AsyncMock(return_value=None)
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch("resolver.providers.get_provider", return_value=provider):
                checked = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100111"})
                missing = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100999"})
                oversized_headers = {**headers, "Content-Length": "5000"}
                oversized = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=oversized_headers, content="x" * 5000)
                streamed_oversized = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, content=oversized_stream())
                provider.check_configured_channel_access.side_effect = RuntimeError("session secret leaked")
                unknown = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100111"})
                private_error = type("ChannelPrivateError", (Exception,), {})("private channel detail")
                provider.check_configured_channel_access.side_effect = private_error
                denied_access = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100111"})
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertEqual(checked.json()["status"], "accessible")
            self.assertEqual(checked.json()["message"], "Telegram read access confirmed.")
            self.assertEqual(unknown.status_code, 200, unknown.text)
            self.assertEqual(unknown.json()["status"], "unknown")
            self.assertNotIn("session secret", unknown.text)
            self.assertEqual(denied_access.json()["status"], "inaccessible")
            self.assertNotIn("private channel detail", denied_access.text)
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(streamed_oversized.status_code, 413)
            self.assertEqual(provider.check_configured_channel_access.await_count, 3)


    def test_v2_source_preview_and_save_are_revision_checked_and_secret_free(self):
        import os
        from common.config_loader import load_config
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            body = {"expected_revision": 0,
                    "overrides": {"telegram": {"channels": {"-100111": {"studio": "Managed"}}}}}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                preview = self.request("POST", "/admin/api/v2/sources/preview", base_url="http://localhost", json=body)
                self.assertFalse(overlay.exists(), "Preview must not write the settings file")
                denied = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                      headers={"Origin": "http://localhost"}, json=body)
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                saved = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                     headers=headers, json=body)
                stale = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                     headers=headers, json=body)
                settings = self.request("GET", "/admin/api/v2/sources/settings", base_url="http://localhost")
                effective = load_config(str(config), force_reload=True)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["changes"][0]["after"], "Managed")
            self.assertNotIn("private.invalid", preview.text)
            self.assertNotIn("do-not-return", preview.text)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["revision"], 1)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(settings.status_code, 200, settings.text)
            self.assertEqual(settings.json()["revision"], 1)
            self.assertEqual(settings.json()["overrides"]["telegram"]["channels"]["-100111"]["studio"], "Managed")
            self.assertNotIn("private.invalid", settings.text)
            self.assertEqual(effective.managed_revision, 1)
            self.assertEqual(effective.sources["telegram"].channels[0].studio, "Managed")


    def test_v2_source_audit_outbox_recovers_after_database_outage(self):
        import os
        from common.managed_sources import read_overrides, read_pending_source_audit
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            body = {"expected_revision": 0,
                    "overrides": {"telegram": {"channels": {"-100111": {"studio": "Managed"}}}}}
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", BAD_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                saved = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                     headers=headers, json=body)
                pending = read_pending_source_audit(overlay)
                blocked = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                       headers=headers, json={**body, "expected_revision": 1})
                self.assertEqual(read_overrides(overlay)["revision"], 1)
                with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                    from common import managed_sources
                    with patch.object(managed_sources, "clear_pending_source_audit", side_effect=OSError("simulated crash after audit commit")):
                        first_recovery = self.request("GET", "/admin/api/v2/sources/settings", base_url="http://localhost")
                    settings = self.request("GET", "/admin/api/v2/sources/settings", base_url="http://localhost")
                    events = db.connect(TEST_URL)
                    try:
                        audit_count = events.execute(
                            "SELECT count(*) FROM admin_audit_events WHERE event_key='source-settings:1'"
                        ).fetchone()[0]
                    finally:
                        events.close()
                cleared = read_overrides(overlay)
                outbox_cleared = read_pending_source_audit(overlay)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(saved.json()["audit_pending"])
            self.assertEqual(pending["revision"], 1)
            self.assertEqual(blocked.status_code, 503, blocked.text)
            self.assertTrue(first_recovery.json()["audit_pending"])
            self.assertEqual(settings.status_code, 200, settings.text)
            self.assertFalse(settings.json()["audit_pending"])
            self.assertNotIn("pending_audit", cleared)
            self.assertIsNone(outbox_cleared)
            self.assertEqual(audit_count, 1)


    def test_v2_can_add_new_telegram_channel_with_revision_check(self):
        import os
        from common.config_loader import load_config
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            body = {"expected_revision": 0, "channel": {"id": "@new_channel", "studio": "New Studio"}}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                denied = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", json=body)
                created = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers, json=body)
                duplicate = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers, json=body)
                stale = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers,
                                     json={"expected_revision": 0, "channel": {"id": "@second", "studio": "Second"}})
                effective = load_config(str(config), force_reload=True)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["source_id"], "@new_channel")
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual([channel.id for channel in effective.sources["telegram"].channels],
                             ["-100111", "@new_channel"])


    def test_v2_can_add_new_telegram_channel_during_database_outage(self):
        import os
        from common.managed_sources import read_overrides, read_pending_source_audit
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            body = {"expected_revision": 0, "channel": {"id": "@offline_channel", "studio": "Offline Studio"}}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", BAD_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                # 1. Legitimately new channel succeeds offline
                created = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers, json=body)
                self.assertEqual(created.status_code, 200, created.text)
                self.assertTrue(created.json()["audit_pending"])
                self.assertEqual(created.json()["revision"], 1)
                self.assertEqual(read_overrides(overlay)["revision"], 1)
                self.assertIsNotNone(read_pending_source_audit(overlay))

                # 2. Re-adding a channel that was marked removed fails closed with 503 while DB is down
                from common.managed_sources import audit_outbox_path
                audit_outbox_path(overlay).unlink(missing_ok=True)
                overlay.write_text(
                    json.dumps({
                        "revision": 2,
                        "overrides": {
                            "telegram": {
                                "channels": {"-100111": {"studio": "Base"}},
                                "removed_channels": ["-100999"]
                            }
                        }
                    }),
                    encoding="utf-8"
                )
                readd_body = {"expected_revision": 2, "channel": {"id": "-100999", "studio": "Restored"}}
                readd_blocked = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers, json=readd_body)
                self.assertEqual(readd_blocked.status_code, 503, readd_blocked.text)
                self.assertIn("Could not verify active channel operations", readd_blocked.text)


    def test_open_proxy_routes_removed(self):
        res1 = self.request("GET", "/admin/px?u=https://example.com")
        self.assertEqual(res1.status_code, 404)
        res2 = self.request("GET", "/px?u=https://example.com")
        self.assertEqual(res2.status_code, 404)
        res3 = self.request("GET", "/admin/px/seg.ts?u=https://example.com/seg.ts")
        self.assertEqual(res3.status_code, 404)
        res4 = self.request("GET", "/px/seg.ts?u=https://example.com/seg.ts")
        self.assertEqual(res4.status_code, 404)


    def test_unauthorized_telegram_channel_blocked_at_api(self):
        class MockTelegramProvider:
            def is_channel_authorized(self, ch):
                return False

        with patch.object(resolver_app, "get_provider", return_value=MockTelegramProvider()):
            res_play = self.request("GET", "/admin/play/telegram:me:42:unique/stream")
            self.assertEqual(res_play.status_code, 403)
            res_thumb = self.request("GET", "/admin/api/thumb?provider=telegram&ref=me:42:unique")
            self.assertEqual(res_thumb.status_code, 403)
            res_hist = self.request("GET", "/admin/api/telegram/history/me")
            self.assertEqual(res_hist.status_code, 403)
            res_cat = self.request("GET", "/admin/api/category/me?provider=telegram")
            self.assertEqual(res_cat.status_code, 403)



if __name__ == "__main__":
    unittest.main()
