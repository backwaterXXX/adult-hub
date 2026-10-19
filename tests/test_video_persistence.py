import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pg_helper import PgTestCase
from video_persistence import (
    VideoPersistenceServices,
    _merge_video_fields,
    persist_prepared_video,
    sync_unique_video,
    flush_collected,
)
from common.performer_catalog import load_catalog, match_catalog


class TestVideoPersistence(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media_dir = self.root / "media"
        self.media_dir.mkdir()

    def _make_services(self, **overrides):
        defaults = {
            "stable_media_basename": lambda cid: cid.replace(":", "_"),
            "map_categories": lambda cats, title="": set(cats),
            "normalize_performers": lambda perfs: list(perfs),
            "thumbnail_urls": lambda url: (url or "", url or ""),
            "load_performer_thumbs": lambda conn, perfs: {},
            "format_dateadded": lambda ts: "2026-01-01 00:00:00",
            "create_nfo_file": MagicMock(),
            "download_image": MagicMock(),
            "write_stream_file": MagicMock(return_value=True),
            "sync_unique_video": None,  # assigned below if not overridden
            "persist_prepared_video": None,
            "logger": logging.getLogger("test"),
            "time_fn": lambda: 1700000000.0,
        }
        defaults.update(overrides)
        if defaults["persist_prepared_video"] is None:
            defaults["persist_prepared_video"] = lambda conn, prep: persist_prepared_video(conn, prep, time_fn=defaults["time_fn"])
        if defaults["sync_unique_video"] is None:
            defaults["sync_unique_video"] = lambda **kwargs: sync_unique_video(services=services_obj, **kwargs)
        services_obj = VideoPersistenceServices(**defaults)
        return services_obj

    def test_synthetic_legacy_profile_proposal_persists_without_catalog_fk(self):
        self.conn.execute(
            "INSERT INTO performer_bios(name,bio,aliases,updated_at) VALUES (%s,%s,%s,1)",
            ("Alejandro Martinez", "Legacy bio", json.dumps([])),
        )
        self.conn.execute(
            "INSERT INTO performer_aliases(alias,canonical) VALUES (%s,%s)",
            ("Mitch J", "Mitchell Jordan"),
        )
        self.conn.commit()
        catalog = load_catalog(self.conn)
        self.assertEqual(next(row for row in catalog if row["id"] == "local:alejandromartinez")["_persisted"], False)
        self.assertEqual(next(row for row in catalog if row["id"] == "local:mitchelljordan")["_persisted"], False)
        _, bio_proposals = match_catalog(
            [("caption", "Alejandor Martinez & Ryan Black")], catalog,
        )
        _, alias_proposals = match_catalog(
            [("caption", "Mitchel Jordan & Gavin Smith")], catalog,
        )
        proposals = bio_proposals + alias_proposals
        typos = [proposal for proposal in proposals if proposal["reason"] == "possible_typo"]
        self.assertEqual({proposal["candidate"] for proposal in typos},
                         {"Alejandro Martinez", "Mitchell Jordan"})
        self.assertTrue(all(proposal["performer_id"] is None for proposal in typos))

        prepared = {
            "comp_id": "telegram:-100123:42:999",
            "provider": "telegram",
            "video": {"title": "Legacy performer video", "duration": 600, "performer_proposals": proposals},
            "genres": set(),
            "tags": [],
            "title": "Legacy performer video",
            "studio": "",
            "strm_file": Path("legacy-performer.strm"),
        }
        persist_prepared_video(self.conn, prepared)
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT candidate,performer_id FROM telegram_performer_proposals "
            "WHERE video_id=%s AND reason='possible_typo' ORDER BY candidate",
            (prepared["comp_id"],),
        ).fetchall()
        self.assertEqual(rows, [("Alejandro Martinez", None), ("Mitchell Jordan", None)])

    def test_promoted_local_profile_proposal_keeps_catalog_fk(self):
        performer_id = "local:alejandromartinez"
        self.conn.execute(
            "INSERT INTO performer_catalog(id,source,source_id,name) VALUES (%s,'manual',%s,%s)",
            (performer_id, performer_id, "Alejandro Martinez"),
        )
        self.conn.commit()
        catalog = load_catalog(self.conn)
        self.assertEqual(next(row for row in catalog if row["id"] == performer_id)["_persisted"], True)
        _, proposals = match_catalog(
            [("caption", "Alejandor Martinez & Ryan Black")], catalog,
        )
        typo = next(proposal for proposal in proposals if proposal["reason"] == "possible_typo")
        self.assertEqual(typo["performer_id"], performer_id)
        prepared = {
            "comp_id": "telegram:-100123:43:998",
            "provider": "telegram",
            "video": {"title": "Promoted performer video", "duration": 600, "performer_proposals": proposals},
            "genres": set(),
            "tags": [],
            "title": "Promoted performer video",
            "studio": "",
            "strm_file": Path("promoted-performer.strm"),
        }
        persist_prepared_video(self.conn, prepared)
        self.conn.commit()
        saved = self.conn.execute(
            "SELECT performer_id FROM telegram_performer_proposals "
            "WHERE video_id=%s AND reason='possible_typo'",
            (prepared["comp_id"],),
        ).fetchone()
        self.assertEqual(saved, (performer_id,))

    def test_merge_video_fields(self):
        existing = {"performers": ["Alice"], "categories": ["Cat1"]}
        new = {"performers": ["Alice", "Bob"], "categories": ["Cat2"]}
        _merge_video_fields(existing, new)
        self.assertEqual(existing["performers"], ["Alice", "Bob"])
        self.assertEqual(existing["categories"], ["Cat1", "Cat2"])

    def test_sync_unique_video_prepare_only(self):
        services = self._make_services()
        video = {
            "provider": "testprov",
            "provider_video_id": "v100",
            "title": "Sample Video",
            "duration": 600,
            "performers": ["Alice"],
            "thumbnail": "http://img/thumb.jpg",
        }
        prepared = sync_unique_video(
            video=video,
            genres={"Action"},
            tags={"tag1"},
            target_dir=self.media_dir,
            conn=self.conn,
            commit=False,
            _prepare_only=True,
            services=services,
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["comp_id"], "testprov:v100")
        self.assertEqual(prepared["title"], "Sample Video")
        services.create_nfo_file.assert_called_once()
        services.download_image.assert_called_once()
        services.write_stream_file.assert_called_once()
        # Verify not yet in DB
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

    def test_sync_unique_video_commit(self):
        services = self._make_services()
        video = {
            "provider": "testprov",
            "provider_video_id": "v100",
            "title": "Sample Video",
            "duration": 600,
            "performers": ["Alice"],
            "thumbnail": "http://img/thumb.jpg",
        }
        res = sync_unique_video(
            video=video,
            genres={"Action"},
            tags={"tag1"},
            target_dir=self.media_dir,
            conn=self.conn,
            commit=True,
            _prepare_only=False,
            services=services,
        )
        self.assertIsNone(res)
        row = self.conn.execute("SELECT title, performers, tags FROM videos WHERE id = %s", ("testprov:v100",)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "Sample Video")
        self.assertEqual(json.loads(row[1]), ["Alice"])

    def test_flush_collected_success_and_clearing(self):
        services = self._make_services()
        collected = {
            "testprov:v1": {
                "video": {"provider": "testprov", "provider_video_id": "v1", "title": "Vid 1", "duration": 600},
                "genres": {"Action"},
                "tags": {"tagA"},
            },
            "testprov:v2": {
                "video": {"provider": "testprov", "provider_video_id": "v2", "title": "Vid 2", "duration": 600},
                "genres": {"Comedy"},
                "tags": {"tagB"},
            },
        }
        written = flush_collected(
            collected=collected,
            conn=self.conn,
            media_dir=self.media_dir,
            source_name="TestSource",
            services=services,
        )
        self.assertEqual(written, 2)
        self.assertEqual(len(collected), 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 2)

    def test_flush_collected_rolls_back_on_persist_failure(self):
        calls = 0
        def fail_on_second(conn, prep):
            nonlocal calls
            calls += 1
            persist_prepared_video(conn, prep)
            if calls == 2:
                raise OSError("simulated disk/db error")

        services = self._make_services(persist_prepared_video=fail_on_second)
        collected = {
            "testprov:v1": {
                "video": {"provider": "testprov", "provider_video_id": "v1", "title": "Vid 1", "duration": 600},
                "genres": {"Action"},
                "tags": {"tagA"},
            },
            "testprov:v2": {
                "video": {"provider": "testprov", "provider_video_id": "v2", "title": "Vid 2", "duration": 600},
                "genres": {"Comedy"},
                "tags": {"tagB"},
            },
        }
        written = flush_collected(
            collected=collected,
            conn=self.conn,
            media_dir=self.media_dir,
            services=services,
        )
        self.assertEqual(written, 0)
        # NOTE (PG port): psycopg keeps an idle transaction open after any
        # statement (even SELECT), unlike sqlite3, so the no-leak check must
        # run before the next SELECT on this connection.
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
