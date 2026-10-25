"""Comprehensive tests for Telegram video variants (alt_documents) support.

Verifies:
- Variant detection with multiple video qualities
- Filtering of HLS playlists (.m3u8), storyboards, previews, and thumbnails
- Truthful metadata extraction without invented parameters
- Calculation of estimated_average_bitrate vs null bitrate
- Single original video detection (4K and materials with no alternatives)
- Unfinished variant recognition (video_processing_pending)
- Error handling on missing metadata or invalid refs
- Configuration and MTProto flag video_ignore_alt_documents
- Range requests, seeking, and Content-Range headers on selected variants
- Cache key isolation between variants
- Strict rejection of invalid variant IDs (never silently falling back to original)
- Backwards compatibility when no variant parameter is passed
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl import types

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.telegram_variants import (
    extract_message_variants,
    extract_variant_info,
    find_variant_document,
    is_full_video_document,
    parse_tl_json,
    should_ignore_alt_documents,
)
from providers.telegram import TelegramProvider
import app as resolver_app
from fastapi.testclient import TestClient


class MockDocument:
    def __init__(self, id, size, mime_type, attributes=None):
        self.id = id
        self.size = size
        self.mime_type = mime_type
        self.attributes = attributes or []


class MockMedia:
    def __init__(self, document, alt_documents=None):
        self.document = document
        self.alt_documents = alt_documents or []


class MockMessage:
    def __init__(self, id, media, video_processing_pending=False):
        self.id = id
        self.media = media
        self.video_processing_pending = video_processing_pending


class TestTelegramVariantsUnit(unittest.TestCase):
    """Unit tests for telegram_variants logic and filtering."""

    def test_parse_tl_json(self):
        obj = types.JsonObject(value=[
            types.JsonObjectValue(key="str_val", value=types.JsonString(value="hello")),
            types.JsonObjectValue(key="num_val", value=types.JsonNumber(value=42.0)),
            types.JsonObjectValue(key="bool_val", value=types.JsonBool(value=True)),
            types.JsonObjectValue(key="null_val", value=types.JsonNull()),
            types.JsonObjectValue(key="arr_val", value=types.JsonArray(value=[
                types.JsonString(value="item")
            ]))
        ])
        parsed = parse_tl_json(obj)
        self.assertEqual(parsed, {
            "str_val": "hello",
            "num_val": 42.0,
            "bool_val": True,
            "null_val": None,
            "arr_val": ["item"],
        })

    def test_filter_out_non_video_documents(self):
        # Full video document
        vdoc = MockDocument(
            1, 5000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=30.0, w=1280, h=720, video_codec="h264")]
        )
        self.assertTrue(is_full_video_document(vdoc))

        # HLS mpegurl playlist
        hls_doc = MockDocument(2, 600, "application/x-mpegurl", [])
        self.assertFalse(is_full_video_document(hls_doc))

        # Storyboard
        sb_doc = MockDocument(3, 25000, "application/x-tgstoryboard", [])
        self.assertFalse(is_full_video_document(sb_doc))

        # Storyboard map
        sbm_doc = MockDocument(4, 200, "application/x-tgstoryboardmap", [])
        self.assertFalse(is_full_video_document(sbm_doc))

        # Round video note (must not be treated as a regular movie variant)
        round_doc = MockDocument(
            5, 100000, "video/mp4",
            [types.DocumentAttributeVideo(duration=5.0, w=320, h=320, round_message=True)]
        )
        self.assertFalse(is_full_video_document(round_doc))

        # Audio or sticker
        audio_doc = MockDocument(
            6, 100000, "audio/mp4",
            [types.DocumentAttributeAudio(duration=120)]
        )
        self.assertFalse(is_full_video_document(audio_doc))

    def test_extract_variant_info_honest_bitrate(self):
        doc = MockDocument(
            101, 3000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=20.0, w=1280, h=720, video_codec="av01")]
        )
        info = extract_variant_info(doc, is_original=False)
        self.assertIsNotNone(info)
        self.assertEqual(info["id"], "alt_101")
        self.assertFalse(info["is_original"])
        self.assertEqual(info["width"], 1280)
        self.assertEqual(info["height"], 720)
        self.assertEqual(info["codec"], "av01")
        self.assertEqual(info["mime_type"], "video/mp4")
        self.assertEqual(info["file_size"], 3000000)
        self.assertEqual(info["duration"], 20.0)
        # Bitrate is reliably known in MTProto only when present, so null
        self.assertIsNone(info["bitrate"])
        # Estimated average bitrate: (3000000 * 8) / 20 = 1200000 bps
        self.assertEqual(info["estimated_average_bitrate"], 1200000)

    def test_extract_variant_info_missing_parameters_not_invented(self):
        doc = MockDocument(102, None, "video/mp4", [types.DocumentAttributeVideo(duration=0.0, w=0, h=0)])
        info = extract_variant_info(doc, is_original=True)
        self.assertIsNotNone(info)
        self.assertEqual(info["id"], "original")
        self.assertTrue(info["is_original"])
        self.assertIsNone(info["width"])
        self.assertIsNone(info["height"])
        self.assertIsNone(info["codec"])
        self.assertIsNone(info["file_size"])
        self.assertIsNone(info["duration"])
        self.assertIsNone(info["bitrate"])
        self.assertIsNone(info["estimated_average_bitrate"])

    def test_extract_message_variants_multiple(self):
        orig = MockDocument(
            1001, 2000000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=1000.0, w=1920, h=1080, video_codec="h264")]
        )
        alt720 = MockDocument(
            2001, 500000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=1000.0, w=1280, h=720, video_codec="h264")]
        )
        alt480 = MockDocument(
            2002, 200000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=1000.0, w=854, h=480, video_codec="av01")]
        )
        hls = MockDocument(3001, 500, "application/x-mpegurl")
        sb = MockDocument(3002, 15000, "application/x-tgstoryboard")

        media = MockMedia(orig, [alt720, hls, alt480, sb])
        msg = MockMessage(50, media, video_processing_pending=False)

        res = extract_message_variants(msg)
        self.assertEqual(res["status"], "ready")
        self.assertTrue(res["has_alternatives"])
        self.assertFalse(res["video_processing_pending"])
        self.assertEqual(len(res["variants"]), 3)

        self.assertEqual(res["variants"][0]["id"], "original")
        self.assertTrue(res["variants"][0]["is_original"])
        self.assertEqual(res["variants"][0]["height"], 1080)

        self.assertEqual(res["variants"][1]["id"], "alt_2001")
        self.assertFalse(res["variants"][1]["is_original"])
        self.assertEqual(res["variants"][1]["height"], 720)

        self.assertEqual(res["variants"][2]["id"], "alt_2002")
        self.assertFalse(res["variants"][2]["is_original"])
        self.assertEqual(res["variants"][2]["height"], 480)

    def test_extract_message_variants_original_only(self):
        orig_4k = MockDocument(
            9999, 4193615013, "video/mp4",
            [types.DocumentAttributeVideo(duration=2329.91, w=3840, h=2160, video_codec=None)]
        )
        media = MockMedia(orig_4k, [])
        msg = MockMessage(13598, media, video_processing_pending=False)

        res = extract_message_variants(msg)
        self.assertEqual(res["status"], "original_only")
        self.assertFalse(res["has_alternatives"])
        self.assertFalse(res["video_processing_pending"])
        self.assertEqual(len(res["variants"]), 1)
        self.assertEqual(res["variants"][0]["id"], "original")
        self.assertTrue(res["variants"][0]["is_original"])
        self.assertEqual(res["variants"][0]["width"], 3840)
        self.assertEqual(res["variants"][0]["height"], 2160)
        self.assertEqual(res["variants"][0]["file_size"], 4193615013)

    def test_extract_message_variants_processing_pending(self):
        orig = MockDocument(
            1001, 100000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=60.0, w=1920, h=1080, video_codec="h264")]
        )
        media = MockMedia(orig, [])
        msg = MockMessage(55, media, video_processing_pending=True)

        res = extract_message_variants(msg)
        self.assertEqual(res["status"], "processing")
        self.assertTrue(res["video_processing_pending"])
        self.assertFalse(res["has_alternatives"])
        self.assertEqual(len(res["variants"]), 1)

    def test_find_variant_document(self):
        orig = MockDocument(1001, 1000, "video/mp4")
        alt1 = MockDocument(2001, 500, "video/mp4")
        media = MockMedia(orig, [alt1])

        self.assertIs(find_variant_document(media, "original"), orig)
        self.assertIs(find_variant_document(media, "1001"), orig)
        self.assertIs(find_variant_document(media, "alt_2001"), alt1)
        self.assertIs(find_variant_document(media, "2001"), alt1)
        self.assertIsNone(find_variant_document(media, "nonexistent"))
        self.assertIsNone(find_variant_document(media, "alt_9999"))

    def test_video_ignore_alt_documents_env_and_app_config(self):
        with patch.dict(os.environ, {"TELEGRAM_VIDEO_IGNORE_ALT_DOCUMENTS": "true"}):
            self.assertTrue(asyncio.run(should_ignore_alt_documents()))

        with patch.dict(os.environ, {"TELEGRAM_VIDEO_IGNORE_ALT_DOCUMENTS": "false"}):
            self.assertFalse(asyncio.run(should_ignore_alt_documents()))

        # MTProto help.getAppConfig
        fake_client = AsyncMock()
        fake_resp = MagicMock()
        fake_resp.config = types.JsonObject(value=[
            types.JsonObjectValue(key="video_ignore_alt_documents", value=types.JsonBool(value=True))
        ])
        fake_client.return_value = fake_resp

        with patch.dict(os.environ, {}, clear=True):
            with patch("providers.telegram_variants._APP_CONFIG_CACHE", None):
                self.assertTrue(asyncio.run(should_ignore_alt_documents(fake_client)))


class TestTelegramProviderVariantsIntegration(unittest.TestCase):
    """Integration tests for TelegramProvider methods with variant support."""

    def setUp(self):
        self.prov = TelegramProvider()
        self.channel_id = "-1001"
        self.patcher = patch("providers.telegram.is_channel_authorized", return_value=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.orig_doc = MockDocument(
            5001, 10000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=100.0, w=1920, h=1080, video_codec="h264")]
        )
        self.alt_doc = MockDocument(
            5002, 3000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=100.0, w=1280, h=720, video_codec="h264")]
        )
        self.media = MockMedia(self.orig_doc, [self.alt_doc])
        self.message = MockMessage(42, self.media, video_processing_pending=False)

    def test_get_video_variants_success(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                ref = f"{self.channel_id}:42:5001"
                variants_data = asyncio.run(self.prov.get_video_variants(ref))

                self.assertEqual(variants_data["status"], "ready")
                self.assertTrue(variants_data["has_alternatives"])
                self.assertEqual(len(variants_data["variants"]), 2)
                self.assertEqual(variants_data["variants"][0]["id"], "original")
                self.assertEqual(variants_data["variants"][1]["id"], "alt_5002")

    def test_get_video_variants_unauthorized_channel_raises(self):
        with patch("providers.telegram.is_channel_authorized", return_value=False):
            with self.assertRaises(ValueError):
                asyncio.run(self.prov.get_video_variants("-1009999999999:42:5001"))

    def test_resolve_stream_variants(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                base_id = f"{self.channel_id}:42:5001"

                # No variant -> original
                orig_stream = asyncio.run(self.prov.resolve_stream(base_id, variant=None))
                self.assertEqual(orig_stream, f"tg://{base_id}")

                # Explicit original -> original
                orig_stream2 = asyncio.run(self.prov.resolve_stream(base_id, variant="original"))
                self.assertEqual(orig_stream2, f"tg://{base_id}")

                # Valid variant -> alt document ID
                alt_stream = asyncio.run(self.prov.resolve_stream(base_id, variant="alt_5002"))
                self.assertEqual(alt_stream, f"tg://{self.channel_id}:42:5002")

                # Invalid variant -> raises ValueError, NEVER original!
                with self.assertRaises(ValueError):
                    asyncio.run(self.prov.resolve_stream(base_id, variant="alt_9999"))

    def test_file_size_for_variant(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                # Original file size
                orig_size = asyncio.run(self.prov.file_size(f"{self.channel_id}:42:5001"))
                self.assertEqual(orig_size, 10000000)

                # Variant file size
                alt_size = asyncio.run(self.prov.file_size(f"{self.channel_id}:42:5002"))
                self.assertEqual(alt_size, 3000000)

                # Nonexistent doc id
                none_size = asyncio.run(self.prov.file_size(f"{self.channel_id}:42:9999"))
                self.assertIsNone(none_size)

    def test_check_message_metadata_for_variant(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                self.assertEqual(
                    asyncio.run(self.prov.check_message_metadata(self.channel_id, 42, "5001")),
                    "available"
                )
                self.assertEqual(
                    asyncio.run(self.prov.check_message_metadata(self.channel_id, 42, "5002")),
                    "available"
                )
                self.assertEqual(
                    asyncio.run(self.prov.check_message_metadata(self.channel_id, 42, "9999")),
                    "mismatch"
                )


class TestHttpVariantsEndpoints(unittest.TestCase):
    """Test HTTP API endpoints for /play/{video_id}/variants and /play/{video_id}/stream."""

    def setUp(self):
        self.client = TestClient(resolver_app.app)
        self.channel_id = "-1001"
        self.patcher = patch("providers.telegram.is_channel_authorized", return_value=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.video_ref = f"{self.channel_id}:42:5001"
        self.composite_id = f"telegram:{self.video_ref}"

        self.orig_doc = MockDocument(
            5001, 10000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=100.0, w=1920, h=1080, video_codec="h264")]
        )
        self.alt_doc = MockDocument(
            5002, 3000000, "video/mp4",
            [types.DocumentAttributeVideo(duration=100.0, w=1280, h=720, video_codec="h264")]
        )
        self.media = MockMedia(self.orig_doc, [self.alt_doc])
        self.message = MockMessage(42, self.media, video_processing_pending=False)

    def test_get_variants_http(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                resp = self.client.get(f"/admin/play/{self.composite_id}/variants")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()

                self.assertEqual(data["video_id"], self.composite_id)
                self.assertEqual(data["status"], "ready")
                self.assertTrue(data["has_alternatives"])
                self.assertFalse(data["video_processing_pending"])
                self.assertEqual(len(data["variants"]), 2)

                orig = data["variants"][0]
                self.assertEqual(orig["id"], "original")
                self.assertTrue(orig["is_original"])
                self.assertEqual(orig["width"], 1920)
                self.assertEqual(orig["height"], 1080)
                self.assertEqual(orig["file_size"], 10000000)
                self.assertEqual(orig["estimated_average_bitrate"], 800000)

                alt = data["variants"][1]
                self.assertEqual(alt["id"], "alt_5002")
                self.assertFalse(alt["is_original"])
                self.assertEqual(alt["width"], 1280)
                self.assertEqual(alt["height"], 720)
                self.assertEqual(alt["file_size"], 3000000)
                self.assertEqual(alt["estimated_average_bitrate"], 240000)

    def test_get_variants_redirect_from_legacy_play(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                resp = self.client.get(f"/play/{self.composite_id}/variants", follow_redirects=True)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["status"], "ready")

    def test_play_stream_with_variant_range_request(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                # Mock download_range in provider to return bytes
                prov = resolver_app.get_provider("telegram")

                async def fake_dl(ref, offset, length):
                    yield b"X" * (length or 100)

                with patch.object(prov, "download_range", side_effect=fake_dl):
                    # Request Range for variant alt_5002 (size 3,000,000)
                    headers = {"Range": "bytes=0-999"}
                    resp = self.client.get(
                        f"/admin/play/{self.composite_id}/stream?variant=alt_5002",
                        headers=headers
                    )
                    self.assertEqual(resp.status_code, 206)
                    self.assertEqual(resp.headers["Content-Range"], "bytes 0-999/3000000")
                    self.assertEqual(resp.headers["Content-Length"], "1000")
                    self.assertEqual(resp.headers["Accept-Ranges"], "bytes")

    def test_play_stream_invalid_variant_returns_404(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                resp = self.client.get(
                    f"/admin/play/{self.composite_id}/stream?variant=alt_nonexistent"
                )
                self.assertEqual(resp.status_code, 404)
                self.assertIn("not found", resp.json()["detail"].lower())

    def test_play_stream_without_variant_preserves_original(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                prov = resolver_app.get_provider("telegram")

                async def fake_dl(ref, offset, length):
                    yield b"O" * (length or 100)

                with patch.object(prov, "download_range", side_effect=fake_dl):
                    headers = {"Range": "bytes=0-999"}
                    resp = self.client.get(
                        f"/admin/play/{self.composite_id}/stream",
                        headers=headers
                    )
                    self.assertEqual(resp.status_code, 206)
                    # Content-Range must reflect original size (10,000,000)
                    self.assertEqual(resp.headers["Content-Range"], "bytes 0-999/10000000")

    def test_cache_separation_between_variants(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                prov = resolver_app.get_provider("telegram")

                async def fake_dl(ref, offset, length):
                    yield b"D" * (length or 50)

                with patch.object(prov, "download_range", side_effect=fake_dl):
                    with patch("app.proxy_fetch_response", wraps=resolver_app.proxy_fetch_response) as mock_pfr:
                        # Play original
                        self.client.get(f"/admin/play/{self.composite_id}/stream")

                        # Play variant
                        self.client.get(f"/admin/play/{self.composite_id}/stream?variant=alt_5002")

                        self.assertEqual(mock_pfr.call_count, 2)
                        orig_call = mock_pfr.call_args_list[0]
                        alt_call = mock_pfr.call_args_list[1]

                        # 1. URLs are distinct tg:// refs
                        self.assertEqual(orig_call.args[1], f"tg://{self.video_ref}")
                        self.assertEqual(alt_call.args[1], f"tg://{self.channel_id}:42:5002")

                        # 2. Active cache keys are distinct
                        self.assertEqual(orig_call.args[2], f"telegram:{self.video_ref}")
                        self.assertEqual(alt_call.args[2], f"telegram:{self.video_ref}:variant:alt_5002")


    def test_file_reference_refresh_on_variant_download(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                # First attempt raises FileReferenceExpiredError, second succeeds with refreshed doc
                refreshed_alt_doc = MockDocument(5002, 3000000, "video/mp4")
                refreshed_media = MockMedia(self.orig_doc, [refreshed_alt_doc])
                refreshed_message = MockMessage(42, refreshed_media)

                mock_get_msg.side_effect = [self.message, refreshed_message]

                prov = TelegramProvider()

                class FileReferenceExpiredError(Exception):
                    pass

                attempts = 0

                async def fake_seq_dl(client, target, offset, length):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise FileReferenceExpiredError("file reference expired")
                    yield b"REFRESHED_CHUNK"

                with patch("providers.telegram._download_sequential", side_effect=fake_seq_dl):
                    with patch("providers.telegram._evict_cached_message") as mock_evict:
                        chunks = []
                        async def run_dl():
                            async for chunk in prov.download_range(f"{self.channel_id}:42:5002", 0, 100):
                                chunks.append(chunk)

                        asyncio.run(run_dl())
                        self.assertEqual(chunks, [b"REFRESHED_CHUNK"])
                        self.assertEqual(attempts, 2)
                        mock_evict.assert_called_with(self.channel_id, 42)

    def test_variant_seeking_range_request(self):
        with patch("providers.telegram._require_client", new_callable=AsyncMock):
            with patch("providers.telegram._get_cached_message", new_callable=AsyncMock) as mock_get_msg:
                mock_get_msg.return_value = self.message

                prov = resolver_app.get_provider("telegram")

                async def fake_dl(ref, offset, length):
                    self.assertEqual(offset, 500000)
                    self.assertEqual(length, 1000)
                    yield b"S" * length

                with patch.object(prov, "download_range", side_effect=fake_dl):
                    headers = {"Range": "bytes=500000-500999"}
                    resp = self.client.get(
                        f"/admin/play/{self.composite_id}/stream?variant=alt_5002",
                        headers=headers
                    )
                    self.assertEqual(resp.status_code, 206)
                    self.assertEqual(resp.headers["Content-Range"], "bytes 500000-500999/3000000")
                    self.assertEqual(resp.headers["Content-Length"], "1000")
                    self.assertEqual(len(resp.content), 1000)


if __name__ == "__main__":
    unittest.main()
