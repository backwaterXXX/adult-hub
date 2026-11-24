"""Tests for the performer thumbnail download path."""

import io
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image
from pg_helper import PgTestCase

import sync.performer_artwork_download as performer_artwork_download


def _jpeg_bytes(*, colour=(180, 120, 90), size=128, quality=90) -> bytes:
    image = Image.new("RGB", (size, size), colour)
    data = io.BytesIO()
    image.save(data, "JPEG", quality=quality)
    return data.getvalue()


class HostAllowlistTests(unittest.TestCase):
    def test_provider_hosts_are_allowed(self):
        for url in (
            "https://stashdb.org/images/abc",
            "https://cdn.theporndb.net/performer/x/poster/y.webp",
            "https://cdn2.pbc.wiki/x.jpg",
            "https://fansdb.cc/x.jpg",
        ):
            self.assertTrue(performer_artwork_download._source_url(url), url)

    def test_unknown_or_insecure_urls_are_rejected(self):
        for url in (
            "http://stashdb.org/images/abc",
            "https://evil.test/x.jpg",
            "https://stashdb.org.evil.test/x.jpg",
            "file:///etc/passwd",
            "https://127.0.0.1/x.jpg",
            "",
        ):
            self.assertEqual(performer_artwork_download._source_url(url), "", url)

    def test_lookalike_suffix_is_not_treated_as_subdomain(self):
        self.assertFalse(performer_artwork_download._host_allowed("notstashdb.org"))


class ArtworkTargetTests(unittest.TestCase):
    def test_target_stays_inside_the_performers_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("../../etc/passwd", "Normal Name", "Ünïcode / Name", "  "):
                target = performer_artwork_download.artwork_target(root, name)
                self.assertTrue(target.is_relative_to(root / "performers"))
                self.assertEqual(target.suffix, ".jpg")

    def test_stored_path_is_relative_to_the_media_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stored = performer_artwork_download.stored_thumb_path(root, "Andy Rodrigues")
            self.assertTrue(stored.startswith("performers/"))
            self.assertFalse(stored.startswith("/"))
            self.assertTrue((root / stored).is_relative_to(root))


class _NoCallClient:
    def get(self, *args, **kwargs):  # pragma: no cover - asserted never to run
        raise AssertionError("no HTTP request expected")


class DownloadTests(PgTestCase):
    def _seed(self, name: str, thumb: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb) VALUES (%s, %s)", (name, thumb)
            )

    def _thumb(self, name: str) -> str:
        row = self.conn.execute(
            "SELECT thumb FROM performer_photos WHERE name=%s", (name,)
        ).fetchone()
        return str(row[0] or "")

    def test_download_stores_relative_path_and_file(self):
        self._seed("Andy Rodrigues", "https://cdn.theporndb.net/x.jpg")
        payload = _jpeg_bytes()

        def respond(request):
            return httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory, \
                httpx.Client(transport=httpx.MockTransport(respond)) as client:
            root = Path(directory).resolve()
            result = performer_artwork_download.download_performer_artwork(
                self.conn, root, "Andy Rodrigues", http_client=client, min_bytes=200,
            )
            self.assertEqual(result["local_state"], "artwork_downloaded")
            stored = self._thumb("Andy Rodrigues")
            self.assertTrue(stored.startswith("performers/"))
            target = root / stored
            self.assertTrue(target.is_file())
            with Image.open(target) as stored_image:
                self.assertEqual(stored_image.format, "JPEG")

    def test_second_run_is_idempotent_and_skips_http(self):
        self._seed("Idempotent Star", "https://stashdb.org/images/x")
        payload = _jpeg_bytes()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = performer_artwork_download.download_performer_artwork(
                self.conn, root, "Idempotent Star",
                http_client=httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=payload))),
                min_bytes=200,
            )
            self.assertEqual(first["local_state"], "artwork_downloaded")
            second = performer_artwork_download.download_performer_artwork(
                self.conn, root, "Idempotent Star", http_client=_NoCallClient(),
            )
            self.assertEqual(second["local_state"], "artwork_already_local")

    def test_black_image_is_rejected_and_row_kept(self):
        self._seed("Dark Star", "https://stashdb.org/images/dark")
        payload = _jpeg_bytes(colour=(0, 0, 0))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = performer_artwork_download.download_performer_artwork(
                self.conn, root, "Dark Star",
                http_client=httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=payload))),
                min_bytes=200,
            )
            self.assertEqual(result["local_state"], "artwork_download_failed")
            self.assertEqual(self._thumb("Dark Star"), "https://stashdb.org/images/dark")
            self.assertFalse(any((root / "performers").glob("*.jpg")))

    def test_untrusted_host_never_reaches_the_network(self):
        self._seed("Untrusted Star", "https://evil.test/x.jpg")
        with tempfile.TemporaryDirectory() as directory:
            result = performer_artwork_download.download_performer_artwork(
                self.conn, Path(directory).resolve(), "Untrusted Star",
                http_client=_NoCallClient(),
            )
            self.assertEqual(result["local_state"], "artwork_not_found")
            self.assertEqual(result["reason"], "host_not_allowed")

    def test_redirect_outside_allowlist_is_rejected(self):
        self._seed("Redirect Star", "https://stashdb.org/images/redirect")
        payload = _jpeg_bytes()

        def respond(request):
            if request.url.host == "stashdb.org":
                return httpx.Response(302, headers={"Location": "https://169.254.169.254/x.jpg"})
            return httpx.Response(200, content=payload)

        with tempfile.TemporaryDirectory() as directory:
            result = performer_artwork_download.download_performer_artwork(
                self.conn, Path(directory).resolve(), "Redirect Star",
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
                min_bytes=200,
            )
            self.assertEqual(result["local_state"], "artwork_download_failed")

    def test_missing_row_reports_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            result = performer_artwork_download.download_performer_artwork(
                self.conn, Path(directory).resolve(), "Nobody Here", http_client=_NoCallClient(),
            )
            self.assertEqual(result["local_state"], "artwork_not_found")
            self.assertEqual(result["reason"], "no_photo_row")


if __name__ == "__main__":
    unittest.main()
