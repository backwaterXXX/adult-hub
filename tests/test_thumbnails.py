import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thumbnails import (
    ThumbnailServices,
    _thumb_fail_marker,
    _valid_thumbnail_bytes,
    download_image,
    download_image_if_missing,
    thumbnail_urls,
)


class TestThumbnails(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.dest = Path(self.temp_dir.name) / "thumb.jpg"
        self.mock_logger = MagicMock()
        self.mock_time = MagicMock()
        self.mock_time.time.return_value = 1700000000.0

    def test_thumb_fail_marker_path(self):
        marker = _thumb_fail_marker(self.dest)
        self.assertEqual(marker.name, "thumb.thumb_fail")

    def test_thumbnail_urls_standard_and_telegram(self):
        # Regular external URL
        pub, dl = thumbnail_urls("https://cdn.example.org/pic.jpg")
        self.assertEqual(pub, "https://cdn.example.org/pic.jpg")
        self.assertEqual(dl, "https://cdn.example.org/pic.jpg")

        # Telegram thumb URL rewrite
        tg_url = "/admin/api/thumb?provider=telegram&ref=-1001%3A123%3A456"
        pub, dl = thumbnail_urls(
            tg_url,
            resolver_public_url="https://public.resolver.org",
            resolver_api_url="http://internal-resolver:8090",
        )
        self.assertEqual(pub, "https://public.resolver.org/admin/api/thumb?provider=telegram&ref=-1001%3A123%3A456")
        self.assertEqual(dl, "http://internal-resolver:8090/admin/api/thumb?provider=telegram&ref=-1001%3A123%3A456")

    def test_valid_thumbnail_bytes(self):
        # Large payload
        self.assertTrue(_valid_thumbnail_bytes(b"x" * 1500, "https://example.com/p.jpg"))
        # Small payload not telegram
        self.assertFalse(_valid_thumbnail_bytes(b"x" * 500, "https://example.com/p.jpg"))
        # Small valid telegram JPEG
        valid_tg_jpg = b"\xff\xd8" + (b"0" * 200) + b"\xff\xd9"
        self.assertTrue(_valid_thumbnail_bytes(valid_tg_jpg, "/admin/api/thumb?provider=telegram"))

        # Black JPEG is rejected
        import io
        from PIL import Image
        black_img = Image.new("RGB", (100, 100), color=(0, 0, 0))
        buf = io.BytesIO()
        black_img.save(buf, format="JPEG")
        self.assertFalse(_valid_thumbnail_bytes(buf.getvalue(), "/admin/api/thumb?provider=telegram"))
        self.assertFalse(_valid_thumbnail_bytes(buf.getvalue(), "https://example.com/thumb.jpg"))

    def test_download_image_success(self):
        image_bytes = b"x" * 2000
        services = ThumbnailServices(
            http_get=lambda url, timeout: image_bytes,
            logger=self.mock_logger,
            time=self.mock_time,
            fail_ttl_seconds=3600,
        )
        download_image("https://example.com/pic.jpg", self.dest, services=services)
        self.assertTrue(self.dest.exists())
        self.assertEqual(self.dest.read_bytes(), image_bytes)

    def test_download_image_if_missing_creates_atomically_and_never_overwrites(self):
        image_bytes = b"x" * 2000
        calls = []
        services = ThumbnailServices(
            http_get=lambda url, timeout: calls.append(url) or image_bytes,
            logger=self.mock_logger,
        )
        self.assertTrue(download_image_if_missing("https://example.com/pic.jpg", self.dest, services=services))
        self.assertEqual(self.dest.read_bytes(), image_bytes)
        self.dest.write_bytes(b"existing")
        self.assertFalse(download_image_if_missing("https://example.com/pic.jpg", self.dest, services=services))
        self.assertEqual(self.dest.read_bytes(), b"existing")
        self.assertEqual(calls, ["https://example.com/pic.jpg"])

    def test_download_image_if_missing_refuses_symlink(self):
        target = Path(self.temp_dir.name) / "existing.jpg"
        target.write_bytes(b"x" * 2000)
        self.dest.symlink_to(target)
        services = ThumbnailServices(http_get=lambda *_args, **_kwargs: b"x" * 2000,
                                     logger=self.mock_logger)
        self.assertFalse(download_image_if_missing("https://example.com/pic.jpg", self.dest, services=services))
        self.assertEqual(target.read_bytes(), b"x" * 2000)

    def test_download_image_404_creates_fail_marker(self):
        def raise_404(url, timeout):
            raise Exception("HTTP 404: Not Found")

        services = ThumbnailServices(
            http_get=raise_404,
            logger=self.mock_logger,
            time=self.mock_time,
            fail_ttl_seconds=3600,
        )
        download_image("https://example.com/pic.jpg", self.dest, services=services)
        self.assertFalse(self.dest.exists())
        marker = _thumb_fail_marker(self.dest)
        self.assertTrue(marker.exists())

    def test_download_image_skips_when_recent_fail_marker_exists(self):
        marker = _thumb_fail_marker(self.dest)
        marker.touch()
        # Mock time within TTL
        self.mock_time.time.return_value = marker.stat().st_mtime + 10.0
        called = []
        services = ThumbnailServices(
            http_get=lambda u, t: called.append(u),
            logger=self.mock_logger,
            time=self.mock_time,
            fail_ttl_seconds=3600,
        )
        download_image("https://example.com/pic.jpg", self.dest, services=services)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
