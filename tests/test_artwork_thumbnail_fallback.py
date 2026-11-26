"""Artwork for streams ffmpeg cannot decode: fall back to the stored preview."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sync import duplicate_job_external_handlers as handlers


class _FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeClient:
    """Minimal httpx.Client stand-in so the real fetch helper is exercised."""

    def __init__(self, content, **_kwargs):
        self._content = content
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return _FakeResponse(self._content)


class _RowConnection:
    def __init__(self, thumbnail):
        self.thumbnail = thumbnail
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        return self

    def fetchone(self):
        return (self.thumbnail,)


class ThumbnailFallbackTests(unittest.TestCase):
    def test_preview_proxy_url_is_downloaded_over_the_internal_api(self):
        conn = _RowConnection("/admin/api/thumb?provider=telegram&ref=-100%3A1%3A2")
        dest = Path("/media/adult/some-item.jpg")
        captured = {}

        def fake_download(url, target, *, services=None):
            captured["url"] = url
            captured["dest"] = target
            captured["fail_ttl"] = services.fail_ttl_seconds
            return True

        with mock.patch.object(handlers, "_core", SimpleNamespace(
                os=mock.Mock(getenv=lambda key, default=None: {
                    "RESOLVER_PUBLIC_URL": "https://adult.example.test",
                    "RESOLVER_API_URL": "http://resolver:8090",
                }.get(key, default)),
                time=mock.Mock())), \
                mock.patch("sync.thumbnails.download_image_if_missing", fake_download):
            self.assertEqual(handlers._artwork_thumbnail_fallback(conn, "telegram:1:2", dest), "stored")

        self.assertEqual(captured["url"], "http://resolver:8090/admin/api/thumb"
                                          "?provider=telegram&ref=-100%3A1%3A2")
        self.assertEqual(captured["dest"], dest)
        # A retry path must not be skipped because of a download failure marker.
        self.assertEqual(captured["fail_ttl"], 0)

    def test_missing_thumbnail_is_not_a_fallback(self):
        conn = _RowConnection("")
        with mock.patch("sync.thumbnails.download_image_if_missing") as download:
            self.assertEqual(handlers._artwork_thumbnail_fallback(conn, "telegram:1:2", Path("/x.jpg")),
                             "unavailable")
        download.assert_not_called()

    def test_preview_bytes_are_validated_and_stored_next_to_the_stream(self):
        import io
        import random
        import tempfile

        from PIL import Image

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        dest = Path(directory.name) / "item.jpg"
        image = Image.new("RGB", (400, 400))
        image.putdata([(random.randrange(256), random.randrange(256), random.randrange(256))
                       for _ in range(400 * 400)])
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=92)

        conn = _RowConnection("/admin/api/thumb?provider=telegram&ref=-100%3A1%3A2")
        with mock.patch.object(handlers, "_core", SimpleNamespace(
                os=mock.Mock(getenv=lambda key, default=None: default),
                time=mock.Mock())), \
                mock.patch("httpx.Client", lambda **_kwargs: _FakeClient(buffer.getvalue())):
            self.assertEqual(handlers._artwork_thumbnail_fallback(conn, "telegram:1:2", dest), "stored")

        self.assertTrue(dest.is_file())
        self.assertGreater(dest.stat().st_size, 1000)

    def test_unusable_preview_reports_failure(self):
        conn = _RowConnection("https://cdn.example.test/poster.jpg")
        with mock.patch.object(handlers, "_core", SimpleNamespace(
                os=mock.Mock(getenv=lambda key, default=None: default),
                time=mock.Mock())), \
                mock.patch("sync.thumbnails.download_image_if_missing", return_value=False):
            self.assertEqual(handlers._artwork_thumbnail_fallback(conn, "telegram:1:2", Path("/x.jpg")),
                             "error")

    def test_a_missing_preview_is_reported_as_terminal(self):
        import httpx

        response = httpx.Response(404, request=httpx.Request("GET", "http://resolver:8090/admin/api/thumb"))
        conn = _RowConnection("/admin/api/thumb?provider=telegram&ref=-100%3A1%3A2")
        with mock.patch.object(handlers, "_core", SimpleNamespace(
                os=mock.Mock(getenv=lambda key, default=None: default),
                time=mock.Mock())), \
                mock.patch("httpx.Client", side_effect=httpx.HTTPStatusError(
                    "404", request=response.request, response=response)):
            self.assertEqual(handlers._artwork_thumbnail_fallback(conn, "telegram:1:2", Path("/x.jpg")),
                             "missing")


class ArtworkGenerateFallbackTests(unittest.TestCase):
    """The generate action stores the preview instead of failing the job."""

    def _run(self, extract_result, fallback_result):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = ("Chasing the Sun", "BelAmi")
        progress = mock.Mock()
        core = SimpleNamespace(os=mock.Mock(), time=mock.Mock(), _record_job_progress=progress)
        dest = Path("/media/adult/item.jpg")
        with mock.patch.object(handlers, "_core", core), \
                mock.patch.object(handlers, "_artwork_destination", return_value=dest), \
                mock.patch.object(handlers, "_artwork_api_base", return_value="http://resolver:8090/admin"), \
                mock.patch.object(handlers, "_artwork_variant", return_value=None), \
                mock.patch.object(handlers, "_artwork_already_valid", return_value=False), \
                mock.patch.object(handlers, "_extract_artwork_frame", return_value=extract_result), \
                mock.patch.object(handlers, "_artwork_thumbnail_fallback",
                                  return_value=fallback_result) as fallback:
            result, success = handlers._action_artwork_generate(
                conn, "job-1", {"video_id": "telegram:1:2"}, Path("/media/adult"), None
            )
        return result, success, fallback

    def test_undecodable_stream_still_gets_a_poster(self):
        result, success, fallback = self._run((False, "ffmpeg failed"), "stored")

        self.assertTrue(success)
        self.assertEqual(result["local_state"], "artwork_from_thumbnail")
        fallback.assert_called_once()

    def test_terminal_items_get_a_generated_poster(self):
        with mock.patch("common.artwork_placeholder.write_placeholder_artwork", return_value=True) as write:
            result, success, _fallback = self._run((False, "ffmpeg failed"), "missing")

        self.assertTrue(success)
        self.assertEqual(result["local_state"], "artwork_placeholder")
        self.assertEqual(write.call_args.kwargs["title"], "Chasing the Sun")
        self.assertEqual(write.call_args.kwargs["studio"], "BelAmi")

    def test_without_a_usable_preview_the_job_still_fails(self):
        import httpx

        with mock.patch.object(httpx, "get", side_effect=httpx.ConnectError("no resolver")):
            with self.assertRaises(RuntimeError) as raised:
                self._run((False, "ffmpeg failed"), "error")

        self.assertIn("Failed to generate artwork", str(raised.exception))



class PlaceholderArtworkTests(unittest.TestCase):
    def test_initials_never_come_back_empty(self):
        from common.artwork_placeholder import placeholder_initials

        self.assertEqual(placeholder_initials("Chasing the Sun"), "CT")
        self.assertEqual(placeholder_initials("Men"), "M")
        self.assertEqual(placeholder_initials(""), "?")
        self.assertEqual(placeholder_initials("---"), "?")

    def test_placeholder_is_a_readable_non_black_poster(self):
        import tempfile

        from PIL import Image

        from common.artwork_placeholder import write_placeholder_artwork
        from common.image_utils import is_black_image

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        dest = Path(directory.name) / "nested" / "item.jpg"

        self.assertTrue(write_placeholder_artwork(dest, title="Chasing the Sun", studio="BelAmi"))
        data = dest.read_bytes()
        self.assertFalse(is_black_image(data))
        with Image.open(dest) as image:
            self.assertEqual(image.size, (640, 360))
            self.assertEqual(image.mode, "RGB")

    def test_a_symlink_destination_is_refused(self):
        import tempfile

        from common.artwork_placeholder import write_placeholder_artwork

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "real.jpg"
        target.write_bytes(b"x")
        link = Path(directory.name) / "link.jpg"
        link.symlink_to(target)

        self.assertFalse(write_placeholder_artwork(link, title="Anything"))
        self.assertEqual(target.read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
