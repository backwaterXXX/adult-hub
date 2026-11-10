"""Database-free backup route, path-safety, and restore-target contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_backups as admin_v2_backups


def request(path, payload=None, *, method="GET"):
    body = json.dumps(payload).encode() if payload is not None else b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if body:
        headers.extend([(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())])
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive=receive)


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


def make_admin(directory, *, denied=None):
    wakes = []
    admin = SimpleNamespace(
        _admin_blocked=lambda _request: False,
        _write_denied=lambda _request: denied,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        BACKUP_DIR=str(directory),
        BACKUP_RESTORE_DATABASE_URL="postgresql://restore.invalid/maintenance",
        HUB_DB_URL="postgresql://primary.invalid/catalog",
        _wake_worker=lambda worker: wakes.append(worker),
        _backup_status=lambda: {"available": True, "inventory_available": True,
                                "count": 0, "total_bytes": 0, "files": []},
    )
    admin.wakes = wakes
    return admin


class TestAdminV2Backups(unittest.IsolatedAsyncioTestCase):
    async def test_manual_backup_requires_write_access_and_queues_database_only_action(self):
        with tempfile.TemporaryDirectory() as directory:
            denied = JSONResponse({"error": "Read only"}, status_code=403)
            admin = make_admin(directory, denied=denied)
            create = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                          if route.path == "/api/v2/backups/create")
            with patch("common.job_queue.enqueue_job") as enqueue:
                response = await create(request("/admin/api/v2/backups/create", method="POST"))
            self.assertEqual(response.status_code, 403)
            enqueue.assert_not_called()
            self.assertEqual(admin.wakes, [])

            admin._write_denied = lambda _request: None
            with patch("common.job_queue.enqueue_job", return_value="backup-job-1") as enqueue:
                response = await create(request("/admin/api/v2/backups/create", method="POST"))
            self.assertEqual(response.status_code, 202)
            self.assertEqual(json.loads(response.body), {
                "ok": True, "job_id": "backup-job-1", "state": "queued",
            })
            enqueue.assert_called_once_with(admin.HUB_DB_URL, "backup_create", {})
            self.assertEqual(admin.wakes, ["jobs"])

    async def test_manual_backup_queue_failure_is_safe_and_does_not_wake_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            admin = make_admin(directory)
            create = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                          if route.path == "/api/v2/backups/create")
            with patch("common.job_queue.enqueue_job", side_effect=RuntimeError(
                    "database unavailable: postgresql://user:secret@db/catalog")) as enqueue:
                response = await create(request("/admin/api/v2/backups/create", method="POST"))
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.body.decode())
            self.assertNotIn("job_id", json.loads(response.body))
            enqueue.assert_called_once_with(admin.HUB_DB_URL, "backup_create", {})
            self.assertEqual(admin.wakes, [])

    async def test_inventory_reports_directory_readability_separately_from_backup_count(self):
        with tempfile.TemporaryDirectory() as directory:
            admin = make_admin(directory)
            inventory = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                             if route.path == "/api/v2/backups")

            admin._backup_status = lambda: {
                "available": False, "inventory_available": True, "count": 0,
                "total_bytes": 0, "files": [],
            }
            empty = await inventory(request("/admin/api/v2/backups"))
            admin._backup_status = lambda: {
                "available": False, "inventory_available": False, "count": 0,
                "total_bytes": 0, "files": [],
            }
            unreadable = await inventory(request("/admin/api/v2/backups"))

        self.assertTrue(empty["inventory_available"])
        self.assertFalse(empty["available"])
        self.assertFalse(unreadable["inventory_available"])
        self.assertFalse(unreadable["available"])

    async def test_verification_endpoint_rejects_path_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            backup = root / "adult_hub-good.db"
            backup.write_bytes(b"legacy sqlite snapshot")
            (root / "adult_hub-link.db").symlink_to(Path(outside) / "adult_hub-good.db")
            admin = make_admin(root)
            verify = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                          if route.path == "/api/v2/backups/verify")

            valid = await verify(request("/admin/api/v2/backups/verify"), "adult_hub-good.db")
            traversal = await verify(request("/admin/api/v2/backups/verify"), "../adult_hub-good.db")
            symlink = await verify(request("/admin/api/v2/backups/verify"), "adult_hub-link.db")

        self.assertEqual(valid["archive_status"], "unsupported_format")
        self.assertEqual(traversal.status_code, 404)
        self.assertEqual(symlink.status_code, 404)

    async def test_restore_test_requires_a_dedicated_target_and_queues_only_safe_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "adulthub-nightly.dump.gz"
            backup.write_bytes(b"verified elsewhere")
            admin = make_admin(directory)
            restore = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                           if route.path == "/api/v2/backups/restore-test")
            payload = {"filename": backup.name}

            with patch("resolver.admin_v2_backups.db.restore_test_target_available", return_value=False), \
                 patch("common.job_queue.enqueue_job") as enqueue:
                unavailable = await restore(request("/admin/api/v2/backups/restore-test", payload, method="POST"))
            self.assertEqual(unavailable.status_code, 503)
            enqueue.assert_not_called()
            self.assertNotIn("restore.invalid", unavailable.body.decode())

            with patch("resolver.admin_v2_backups.db.restore_test_target_available", return_value=True), \
                 patch("common.job_queue.enqueue_job", return_value="restore-job-1") as enqueue:
                queued = await restore(request("/admin/api/v2/backups/restore-test", payload, method="POST"))
            self.assertEqual(queued.status_code, 202)
            enqueue.assert_called_once_with(admin.HUB_DB_URL, "backup_restore_test", {"filename": backup.name})
            self.assertEqual(admin.wakes, ["jobs"])

    async def test_restore_test_queue_failure_is_not_reported_as_accepted_or_woken(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "adulthub-nightly.dump.gz"
            backup.write_bytes(b"verified elsewhere")
            admin = make_admin(directory)
            restore = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                           if route.path == "/api/v2/backups/restore-test")

            with patch("resolver.admin_v2_backups.db.restore_test_target_available", return_value=True), \
                 patch("common.job_queue.enqueue_job", side_effect=RuntimeError(
                     "database unavailable: postgresql://restore:secret@db/maintenance")) as enqueue:
                response = await restore(request(
                    "/admin/api/v2/backups/restore-test",
                    {"filename": backup.name}, method="POST",
                ))

            self.assertEqual(response.status_code, 503)
            self.assertNotIn("job_id", json.loads(response.body))
            self.assertNotIn("secret", response.body.decode())
            enqueue.assert_called_once_with(
                admin.HUB_DB_URL, "backup_restore_test", {"filename": backup.name}
            )
            self.assertEqual(admin.wakes, [])

    async def test_restore_test_denies_write_before_parsing_and_rejects_non_postgres_backup(self):
        denied = JSONResponse({"error": "Read only"}, status_code=403)
        with tempfile.TemporaryDirectory() as directory:
            old_snapshot = Path(directory) / "adult_hub-legacy.db"
            old_snapshot.write_bytes(b"legacy")
            admin = make_admin(directory, denied=denied)
            restore = next(route.endpoint for route in admin_v2_backups.build_router(admin).routes
                           if route.path == "/api/v2/backups/restore-test")
            with patch("common.job_queue.enqueue_job") as enqueue:
                response = await restore(request("/admin/api/v2/backups/restore-test", method="POST"))
            self.assertEqual(response.status_code, 403)
            enqueue.assert_not_called()

            admin._write_denied = lambda _request: None
            with patch("resolver.admin_v2_backups.db.restore_test_target_available") as target:
                legacy = await restore(request("/admin/api/v2/backups/restore-test",
                                               {"filename": old_snapshot.name}, method="POST"))
            self.assertEqual(legacy.status_code, 404)
            target.assert_not_called()


if __name__ == "__main__":
    unittest.main()
