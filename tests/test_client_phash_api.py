from __future__ import annotations

import base64
import asyncio
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolver"))
sys.path.insert(0, str(ROOT))

from PIL import Image
from starlette.requests import Request
from starlette.responses import JSONResponse

import admin_v2_phash as phash_api
from common.migrations.m0028_phash_provenance import apply as apply_phash_provenance
from pg_helper import PgTestCase


def _frame_payload(duration_us: int = 600_000_000) -> list[dict]:
    output = BytesIO()
    Image.new("RGB", (160, 90), (30, 60, 90)).save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return [
        {
            "index": index,
            "timestamp_us": int(duration_us * (0.05 + 0.90 * index / 25)),
            "png_base64": encoded,
        }
        for index in range(25)
    ]


class _FakeConnection:
    def __init__(self, provenance=None):
        self.provenance = provenance
        self.statements = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, statement, _params=()):
        self.statements.append(statement)
        if "SELECT provider FROM videos" in statement:
            return self
        if "SELECT fingerprint,generator_version" in statement:
            return self
        return self

    def fetchone(self):
        if any("SELECT provider FROM videos" in sql for sql in self.statements[-1:]):
            return ("telegram",)
        return self.provenance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class ClientPhashIntakeTests(unittest.TestCase):
    def test_decodes_all_25_timestamped_png_frames(self):
        images = phash_api._decode_frames(_frame_payload(), 600_000_000)
        self.assertEqual(len(images), 25)
        self.assertTrue(all(image.size == (160, 90) for image in images))

    def test_rejects_incomplete_or_out_of_order_frames(self):
        frames = _frame_payload()
        with self.assertRaises(ValueError):
            phash_api._decode_frames(frames[:-1], 600_000_000)
        frames[0], frames[1] = frames[1], frames[0]
        with self.assertRaises(ValueError):
            phash_api._decode_frames(frames, 600_000_000)

    def test_rejects_frame_timestamp_outside_the_sampling_position(self):
        frames = _frame_payload()
        frames[8]["timestamp_us"] += 2_000_000
        with self.assertRaises(ValueError):
            phash_api._decode_frames(frames, 600_000_000)

    def test_rejects_boolean_frame_index(self):
        frames = _frame_payload()
        frames[0]["index"] = False
        with self.assertRaises(ValueError):
            phash_api._decode_frames(frames, 600_000_000)

    def test_outer_request_rejects_unhashable_profile_and_boolean_version(self):
        payload = {
            "video_id": "telegram:-100123:42:99",
            "duration_us": 600_000_000,
            "sampling_version": 1,
            "decoder_profile": "android-metadata-retriever-v1",
            "frames": [{}] * 25,
        }
        profiles = {"android-metadata-retriever-v1"}
        self.assertTrue(phash_api._valid_frame_request(payload, profiles))
        with_profile_list = {**payload, "decoder_profile": ["android-metadata-retriever-v1"]}
        self.assertFalse(phash_api._valid_frame_request(with_profile_list, profiles))
        with_boolean_version = {**payload, "sampling_version": True}
        self.assertFalse(phash_api._valid_frame_request(with_boolean_version, profiles))

    def test_replaces_unversioned_hash_once_and_records_provenance(self):
        connection = _FakeConnection(provenance=None)
        admin = type("Admin", (), {"HUB_DB_URL": None})()
        with patch.object(phash_api.db, "connect", return_value=connection):
            result = phash_api._store(
                admin, "telegram:-100123:42:99", "0123456789abcdef",
                "android-metadata-retriever-v1", "user-id",
            )
        self.assertEqual(result, "saved")
        self.assertTrue(connection.committed)
        self.assertTrue(any("INSERT INTO video_fingerprints" in sql for sql in connection.statements))
        self.assertTrue(any("INSERT INTO video_phash_provenance" in sql for sql in connection.statements))

    def test_new_generator_version_replaces_stale_fingerprint(self):
        connection = _FakeConnection(provenance=(
            "fedcba9876543210", "old-generator:old-decoder",
        ))
        admin = type("Admin", (), {"HUB_DB_URL": None})()
        with patch.object(phash_api.db, "connect", return_value=connection):
            result = phash_api._store(
                admin, "telegram:-100123:42:99", "0123456789abcdef",
                "android-metadata-retriever-v1", "user-id",
            )
        self.assertEqual(result, "saved")
        self.assertTrue(connection.committed)
        self.assertTrue(any("INSERT INTO video_fingerprints" in sql for sql in connection.statements))
        self.assertTrue(any("UPDATE video_phash_provenance" in sql for sql in connection.statements))

    def test_same_generator_version_rejects_conflicting_hash(self):
        connection = _FakeConnection(provenance=(
            "fedcba9876543210",
            phash_api.PHASH_ALGORITHM_VERSION + ":android-metadata-retriever-v1",
        ))
        admin = type("Admin", (), {"HUB_DB_URL": None})()
        with patch.object(phash_api.db, "connect", return_value=connection):
            result = phash_api._store(
                admin, "telegram:-100123:42:99", "0123456789abcdef",
                "android-metadata-retriever-v1", "user-id",
            )
        self.assertEqual(result, "conflict")
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)

    def test_migration_adds_a_provenance_record_per_video(self):
        statements = []
        apply_phash_provenance(type("Connection", (), {"execute": lambda _self, sql: statements.append(sql)})())
        self.assertIn("video_phash_provenance", statements[0])
        self.assertIn("submitted_by_user_id", statements[0])


class ClientPhashStatusTests(unittest.TestCase):
    @staticmethod
    async def _inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    @staticmethod
    def _endpoint(admin):
        router = phash_api.build_router(admin)
        return next(route.endpoint for route in router.routes
                    if getattr(route, "path", "") == "/api/v2/video-phash/status")

    @staticmethod
    def _request():
        return Request({
            "type": "http", "method": "GET", "path": "/admin/api/v2/video-phash/status",
            "headers": [(b"x-emby-token", b"jellyfin-session")],
        })

    @staticmethod
    def _jellyfin_response():
        response = Mock(status_code=200)
        response.json.return_value = {
            "Id": "admin-user", "Policy": {"IsAdministrator": True},
        }
        return response

    def test_status_rejects_unapproved_profile_before_catalog_lookup(self):
        class Admin:
            HUB_DB_URL = None
            JSONResponse = JSONResponse

        endpoint = self._endpoint(Admin)

        async def run():
            with patch.dict(os.environ, {
                "PHASH_UPLOAD_ENABLED": "true",
                "PHASH_ACCEPTED_DECODER_PROFILES": "approved-profile-v1",
                "JELLYFIN_URL": "http://jellyfin.invalid",
            }), patch.object(phash_api.httpx.AsyncClient, "get", new=AsyncMock(return_value=self._jellyfin_response())), \
                    patch.object(phash_api.db, "connect") as connect:
                response = await endpoint(self._request(), "telegram:-100123:42:99", "unapproved-profile")
            connect.assert_not_called()
            return response

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.body, b'{"error":"Unsupported decoder profile."}')

    def test_status_reports_the_configured_profile_and_existing_fingerprint(self):
        class Admin:
            HUB_DB_URL = None
            JSONResponse = JSONResponse

        class StatusConnection:
            def __init__(self):
                self.query = ""
                self.closed = False

            def execute(self, query, _params):
                self.query = query
                return self

            def fetchone(self):
                if "FROM videos" in self.query:
                    return (1,)
                return ("0123456789abcdef", phash_api.PHASH_ALGORITHM_VERSION + ":approved-profile-v1")

            def close(self):
                self.closed = True

        connection = StatusConnection()
        endpoint = self._endpoint(Admin)

        async def run():
            with patch.dict(os.environ, {
                "PHASH_UPLOAD_ENABLED": "true",
                "PHASH_ACCEPTED_DECODER_PROFILES": "approved-profile-v1",
                "JELLYFIN_URL": "http://jellyfin.invalid",
            }), patch.object(phash_api.httpx.AsyncClient, "get", new=AsyncMock(return_value=self._jellyfin_response())), \
                    patch.object(phash_api.db, "connect", return_value=connection), \
                    patch.object(phash_api.asyncio, "to_thread", new=self._inline_to_thread):
                return await endpoint(self._request(), "telegram:-100123:42:99", "approved-profile-v1")

        result = asyncio.run(run())
        self.assertEqual(result, {
            "enabled": True,
            "already_computed": True,
            "accepted_profile": "approved-profile-v1",
        })
        self.assertTrue(connection.closed)


class _ConcurrencyGate:
    def __init__(self):
        self.first_has_video_lock = threading.Event()
        self.release_first = threading.Event()
        self.second_query_attempted = threading.Event()
        self.second_query_returned = threading.Event()


class _PausingConnection:
    def __init__(self, inner, gate, pause_after_video_lock=False, track_second=False):
        self.inner = inner
        self.gate = gate
        self.pause_after_video_lock = pause_after_video_lock
        self.track_second = track_second

    def execute(self, statement, params=()):
        is_video_lock = "SELECT provider FROM videos" in statement
        if self.track_second and is_video_lock:
            self.gate.second_query_attempted.set()
        result = self.inner.execute(statement, params)
        if self.track_second and is_video_lock:
            self.gate.second_query_returned.set()
        if self.pause_after_video_lock and is_video_lock:
            self.gate.first_has_video_lock.set()
            if not self.gate.release_first.wait(timeout=10):
                raise TimeoutError("test did not release the first row lock")
        return result

    def commit(self):
        return self.inner.commit()

    def rollback(self):
        return self.inner.rollback()

    def close(self):
        return self.inner.close()


def _pausing_connect(real_connect, gate):
    connect_lock = threading.Lock()
    connect_count = 0

    def connect(url=None):
        nonlocal connect_count
        with connect_lock:
            connect_count += 1
            number = connect_count
        return _PausingConnection(real_connect(url), gate, pause_after_video_lock=(number == 1),
                                  track_second=(number == 2))

    return connect


class ClientPhashConcurrentStoreTests(PgTestCase):
    def test_first_concurrent_submissions_cannot_replace_each_others_hash(self):
        video_id = "telegram:-100123:42:99"
        self.conn.execute("INSERT INTO videos(id,provider,title) VALUES (%s,'telegram','Test')", (video_id,))
        self.conn.commit()
        admin = type("Admin", (), {"HUB_DB_URL": self.db_url})()

        gate = _ConcurrencyGate()
        real_connect = phash_api.db.connect

        def save(fingerprint):
            return phash_api._store(
                admin, video_id, fingerprint, "android-metadata-retriever-v1", "user-id",
            )

        with patch.object(phash_api.db, "connect", side_effect=_pausing_connect(real_connect, gate)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(save, "0123456789abcdef")
                self.assertTrue(gate.first_has_video_lock.wait(timeout=10))
                second = pool.submit(save, "fedcba9876543210")
                try:
                    self.assertTrue(gate.second_query_attempted.wait(timeout=10))
                    self.assertFalse(gate.second_query_returned.wait(timeout=0.2),
                                     "second submission should wait for the per-video lock")
                finally:
                    gate.release_first.set()
                self.assertEqual(first.result(timeout=10), "saved")
                self.assertEqual(second.result(timeout=10), "conflict")

        saved_fingerprint = self.conn.execute(
            "SELECT fingerprint FROM video_fingerprints WHERE video_id=%s AND algorithm='PHASH'",
            (video_id,),
        ).fetchone()[0]
        provenance_fingerprint = self.conn.execute(
            "SELECT fingerprint FROM video_phash_provenance WHERE video_id=%s", (video_id,),
        ).fetchone()[0]
        self.assertEqual(saved_fingerprint, "0123456789abcdef")
        self.assertEqual(provenance_fingerprint, saved_fingerprint)


if __name__ == "__main__":
    unittest.main()
