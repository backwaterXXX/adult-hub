"""Database-free API and validation contracts for performer profile editing."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_organize as admin_v2_organize
from common.performer_profiles import PerformerProfileError, validate_profile, validate_photo_url


def request(path, payload=None, *, method="GET"):
    body = json.dumps(payload).encode() if payload is not None else b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-length", str(len(body)).encode())] if body else []
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive)


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        if "SELECT performer_name FROM video_performers" in sql:
            return SimpleNamespace(fetchone=lambda: ("Canonical Name",))
        raise AssertionError(f"Unexpected SQL: {sql}")

    def close(self):
        pass


class TestPerformerProfileValidation(unittest.TestCase):
    def test_validates_bio_aliases_and_https_artwork(self):
        self.assertEqual(validate_photo_url("https://EXAMPLE.com:443/art.jpg"),
                         "https://example.com/art.jpg")
        profile = validate_profile("Canonical Name", "  biography  ", "", ["Alias Name"])
        self.assertEqual(profile, {"bio": "biography", "photo": "", "aliases": ["Alias Name"]})

    def test_rejects_secret_artwork_urls_and_duplicate_aliases(self):
        for url in ("http://example.com/a.jpg", "https://u:p@example.com/a.jpg",
                    "https://example.com/a.jpg?access_token=secret", "https://example.com/a.jpg#secret"):
            with self.subTest(url=url), self.assertRaises(PerformerProfileError):
                validate_photo_url(url)
        with self.assertRaises(PerformerProfileError):
            validate_profile("Canonical Name", "", "", ["Canonical Name"])


class TestPerformerProfileRoutes(unittest.IsolatedAsyncioTestCase):
    def make_routes(self, *, denied=None):
        admin = SimpleNamespace(
            _admin_blocked=lambda _request: False,
            _write_denied=lambda _request: denied,
            JSONResponse=JSONResponse,
            HUB_DB_URL="postgresql://fixture.invalid/catalog",
        )
        return {route.path: route.endpoint for route in admin_v2_organize.build_router(admin).routes}

    async def test_read_only_denial_precedes_database_access(self):
        denied = JSONResponse({"ok": False, "error": "Read-only"}, status_code=403)
        routes = self.make_routes(denied=denied)
        path = "/api/v2/organize/people/Canonical%20Name/profile"
        with patch("resolver.admin_v2_organize.db.connect") as connect:
            response = await routes["/api/v2/organize/people/{name}/profile"](
                request(path, {"expected_revision": 0, "bio": "x", "photo": "", "aliases": []}, method="PUT"),
                "Canonical Name",
            )
        self.assertIs(response, denied)
        connect.assert_not_called()

    async def test_put_requires_complete_profile_and_forwards_revision_cas(self):
        routes = self.make_routes()
        endpoint = routes["/api/v2/organize/people/{name}/profile"]
        path = "/api/v2/organize/people/Canonical%20Name/profile"
        with patch("resolver.admin_v2_organize.db.connect") as connect:
            incomplete = await endpoint(request(path, {"expected_revision": 0, "reset_fields": ["bio"]}, method="PUT"), "Canonical Name")
            self.assertEqual(incomplete.status_code, 400)
            connect.assert_not_called()

            connection = Connection()
            connect.return_value = connection
            expected = {"name": "Canonical Name", "revision": 1, "bio": "Edited", "photo": "", "aliases": ["Alias"], "overrides": ["bio"]}
            with patch("common.performer_profiles.update_profile", return_value=expected) as update:
                response = await endpoint(request(path, {
                    "expected_revision": 0, "bio": "Edited", "photo": "", "aliases": ["Alias"],
                }, method="PUT"), "canonical name")
            self.assertEqual(response, {"ok": True, "profile": expected})
            update.assert_called_once_with(
                connection, name="Canonical Name", expected_revision=0,
                profile={"bio": "Edited", "photo": "", "aliases": ["Alias"]}, reset_fields=[],
            )

