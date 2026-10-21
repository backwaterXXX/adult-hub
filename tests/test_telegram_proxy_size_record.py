"""Playback-time file-size recording through the Telegram range proxy."""

import asyncio
import inspect as inspect_module
import sys
import time as time_module
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "resolver"))
sys.path.insert(0, str(ROOT / "tests"))

import telegram_proxy
from telegram_proxy import TelegramProxyServices, proxy_telegram_response

REF = "-1001140295673:4613:5325555053841380087"


class _Provider:
    async def file_size(self, ref):
        return 9_876_543

    async def download_range(self, ref, offset, length):
        for chunk in (b"abc", b"def"):
            yield chunk

    def is_channel_authorized(self, channel_ref):
        return True


def _services(**overrides):
    fields = {
        "diagnostics": SimpleNamespace(begin=lambda *a: None),
        "get_provider": lambda name: _Provider(),
        "parse_range_request": lambda raw, total: None,
        "asyncio": asyncio,
        "inspect": inspect_module,
        "time": time_module,
    }
    fields.update(overrides)
    return TelegramProxyServices(**fields)


def _request(method="GET", headers=None):
    return SimpleNamespace(
        method=method,
        headers=headers or {},
        client=SimpleNamespace(host="192.168.1.10"),
    )


class PlaybackSizeRecordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(telegram_proxy.telegram_playback_activity, "begin_playback",
                               return_value="handle")
        patcher2 = patch.object(telegram_proxy.telegram_playback_activity, "end_playback",
                                return_value=None)
        self.begin = patcher.start()
        patcher2.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(patcher2.stop)

    async def _drain(self, request, services):
        response = await proxy_telegram_response(request, f"tg://{REF}", services=services)
        iterator = getattr(response, "body_iterator", None)
        if iterator is not None:
            async for _ in iterator:
                pass
        if telegram_proxy._pending_size_records:
            await asyncio.gather(*list(telegram_proxy._pending_size_records))

    async def test_playback_records_known_size(self):
        records = []
        services = _services(record_file_size=lambda ref, total: records.append((ref, total)))
        await self._drain(_request(), services)
        self.assertEqual(records, [(REF, 9_876_543)])

    async def test_head_and_worker_requests_do_not_record(self):
        records = []
        services = _services(record_file_size=lambda ref, total: records.append((ref, total)))
        await self._drain(_request(method="HEAD"), services)
        worker_request = SimpleNamespace(
            method="GET",
            headers={"x-adult-hub-transfer": "phash"},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        await self._drain(worker_request, services)
        self.assertEqual(records, [])

    async def test_missing_sink_is_harmless(self):
        await self._drain(_request(), _services(record_file_size=None))

    async def test_record_failure_does_not_break_playback(self):
        def _boom(ref, total):
            raise RuntimeError("db down")

        services = _services(record_file_size=_boom)
        response = await proxy_telegram_response(_request(), f"tg://{REF}", services=services)
        chunks = [chunk async for chunk in response.body_iterator]
        if telegram_proxy._pending_size_records:
            await asyncio.gather(*list(telegram_proxy._pending_size_records))
        self.assertEqual(b"".join(chunks), b"abcdef")


if __name__ == "__main__":
    unittest.main()
