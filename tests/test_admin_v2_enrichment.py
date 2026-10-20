"""Admin v2 external-metadata progress projection tests."""

import sys
import time
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))
sys.path.insert(0, str(ROOT / "tests"))

import admin_v2_enrichment
from pg_helper import PgTestCase


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


def request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/admin/api/v2/metadata/enrichment", "raw_path": b"/admin/api/v2/metadata/enrichment",
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
        "server": ("test", 80), "root_path": "", "app": SimpleNamespace(state=SimpleNamespace()),
    })


class TestAdminV2Enrichment(PgTestCase):
    def test_aggregates_catalog_and_attempts_without_entity_keys(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos(id,provider,title,performers) VALUES "
                "(%s,'telegram','First',%s::jsonb),(%s,'telegram','Second',%s::jsonb),"
                "(%s,'other','Outside',%s::jsonb)",
                ("private-video-one", json.dumps(["Alex Star", "Jamie North"]),
                 "private-video-two", json.dumps(["Alex Star", "Taylor West"]),
                 "private-video-three", json.dumps(["Outside Actor"])),
            )
            self.conn.execute("""
                INSERT INTO external_metadata_attempts
                    (entity_type,entity_key,source,outcome,attempt_count,last_attempt_at,next_attempt_at)
                VALUES
                    ('video','private-video-one','studio_scene','no_match',1,to_timestamp(%s),to_timestamp(%s)),
                    ('performer','alex star','pbc','found',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + interval '180 days'),
                    ('performer','jamie north','pbc','error',2,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + interval '1 hour')
            """, (int(time.time()) - 40 * 86400, int(time.time()) - 10 * 86400))

        admin = SimpleNamespace(
            _admin_blocked=lambda _request: False,
            JSONResponse=JSONResponse,
            asyncio=InlineAsyncio,
            HUB_DB_URL=self.db_url,
            CONFIG_PATH=ROOT / "tests/fixtures/config_telegram_only.yaml",
        )
        endpoint = next(route.endpoint for route in admin_v2_enrichment.build_router(admin).routes
                        if route.path == "/api/v2/metadata/enrichment")
        config = SimpleNamespace(sync=SimpleNamespace(
            scene_backfill_per_run=101,
            pbc_backfill_per_run=7,
            tpdb_backfill_per_run=9,
            daily_at="03:30",
            interval_minutes=60,
            run_on_startup=False,
        ))
        with patch("common.config_loader.load_config", return_value=config):
            payload = self.asyncio_run(endpoint(request()))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["batch_limits"], {
            "videos_per_run": 101,
            "pbc_actors_per_run": 7,
            "theporndb_actors_per_run": 9,
            "stashdb_actors_per_run": 9,
        })
        self.assertEqual(payload["sync_schedule"], {
            "mode": "daily", "daily_at": "03:30", "interval_minutes": None,
            "run_on_startup": False,
        })
        self.assertEqual(payload["catalog"], {"videos": 2, "performers": 4})
        scene = next(row for row in payload["sources"] if row["source"] == "studio_scene")
        self.assertEqual((scene["attempts"], scene["no_match"], scene["due"]), (1, 1, 1))
        pbc = next(row for row in payload["sources"] if row["source"] == "pbc")
        self.assertEqual((pbc["attempts"], pbc["found"], pbc["errors"], pbc["remaining"]), (2, 1, 1, 2))
        self.assertNotIn("private-video-one", str(payload))
        self.assertNotIn("Alex Star", str(payload))

    def test_rejects_public_request(self):
        admin = SimpleNamespace(
            _admin_blocked=lambda _request: True,
            JSONResponse=JSONResponse,
            asyncio=InlineAsyncio,
            HUB_DB_URL=self.db_url,
        )
        endpoint = next(route.endpoint for route in admin_v2_enrichment.build_router(admin).routes
                        if route.path == "/api/v2/metadata/enrichment")
        response = self.asyncio_run(endpoint(request()))
        self.assertEqual(response.status_code, 403)

    @staticmethod
    def asyncio_run(coro):
        import asyncio
        return asyncio.run(coro)
