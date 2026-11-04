import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pg_helper import PgTestCase
from common import db
from video_persistence import (
    VideoPersistenceServices,
    _merge_video_fields,
    persist_prepared_video,
    publication_pending_ids,
    republish_pending_videos,
    sync_unique_video,
    flush_collected,
    write_media_files,
    reset_channel_hints_warning,
    warn_if_channel_hints_missing,
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
        # Preparing only computes metadata; sidecars stay unwritten until the
        # catalog row is durable, so a rolled-back batch leaves no stray files.
        services.create_nfo_file.assert_not_called()
        services.download_image.assert_not_called()
        services.write_stream_file.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

        write_media_files(services, prepared, self.conn)
        services.create_nfo_file.assert_called_once()
        services.download_image.assert_called_once()
        services.write_stream_file.assert_called_once()
        # Registration is the last step: a file that is on disk is the file the
        # library cleanup is allowed to reason about.
        self.assertEqual(
            self.conn.execute("SELECT file_path FROM synced_files").fetchall(),
            [("testprov_v100.strm",)],
        )

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

    def _insert_matched_scene(self, video_id, catalog_title, override=None):
        self.conn.execute(
            """INSERT INTO videos (id,provider,title,duration,performers,studio,first_seen,last_seen,
                                   scene_match_source,scene_match_external_id,scene_match_url)
               VALUES (%s,'telegram',%s,600,'[]','Example Studio',1000,1000,
                       'stashdb:phash','stash-uuid-1','https://stashdb.org/scenes/stash-uuid-1')""",
            (video_id, catalog_title),
        )
        if override is not None:
            self.conn.execute(
                "INSERT INTO video_metadata_overrides (video_id,fields) VALUES (%s,%s::jsonb)",
                (video_id, json.dumps({"title": override})),
            )
        self.conn.commit()

    def _persist_telegram_listing(self, video_id, provider_title):
        services = self._make_services()
        sync_unique_video(
            video={
                "provider": "telegram",
                "provider_video_id": video_id.split(":", 1)[1],
                "title": provider_title,
                "duration": 600,
                "performers": ["Alice"],
            },
            genres=set(),
            tags=set(),
            target_dir=self.media_dir,
            conn=self.conn,
            commit=True,
            _prepare_only=False,
            services=services,
        )
        return services

    def test_catalog_scene_match_keeps_catalog_title_in_db_and_nfo(self):
        video_id = "telegram:-1001:42:999"
        self._insert_matched_scene(video_id, "Rinse and Rim")

        services = self._persist_telegram_listing(video_id, "Lukayan SHOW #42")

        row = self.conn.execute("SELECT title FROM videos WHERE id=%s", (video_id,)).fetchone()
        self.assertEqual(row[0], "Rinse and Rim")
        nfo_video = services.create_nfo_file.call_args[0][0]
        self.assertEqual(nfo_video["title"], "Rinse and Rim")
        # The provider caption stays available as provenance.
        self.assertEqual(nfo_video["original_title"], "Lukayan SHOW #42")
        # The catalog identity must survive the NFO rewrite.
        self.assertEqual(nfo_video["scene_match_source"], "stashdb:phash")
        self.assertEqual(nfo_video["scene_match_external_id"], "stash-uuid-1")

    def test_operator_title_override_wins_over_catalog_title(self):
        video_id = "telegram:-1001:43:998"
        self._insert_matched_scene(video_id, "Rinse and Rim", override="Operator Title")

        services = self._persist_telegram_listing(video_id, "Lukayan SHOW #43")

        # An operator override must never be replaced by the catalog title.
        row = self.conn.execute("SELECT title FROM videos WHERE id=%s", (video_id,)).fetchone()
        self.assertEqual(row[0], "Lukayan SHOW #43")
        nfo_video = services.create_nfo_file.call_args[0][0]
        self.assertEqual(nfo_video["title"], "Lukayan SHOW #43")
        # The catalog identity still belongs in the NFO.
        self.assertEqual(nfo_video["scene_match_external_id"], "stash-uuid-1")

    def test_listing_without_catalog_match_keeps_provider_title(self):
        video_id = "telegram:-1001:44:997"
        self.conn.execute(
            """INSERT INTO videos (id,provider,title,duration,performers,studio,first_seen,last_seen)
               VALUES (%s,'telegram','Rinse and Rim',600,'[]','Example Studio',1000,1000)""",
            (video_id,),
        )
        self.conn.commit()

        services = self._persist_telegram_listing(video_id, "Lukayan SHOW #44")

        row = self.conn.execute("SELECT title FROM videos WHERE id=%s", (video_id,)).fetchone()
        self.assertEqual(row[0], "Lukayan SHOW #44")
        nfo_video = services.create_nfo_file.call_args[0][0]
        self.assertEqual(nfo_video["title"], "Lukayan SHOW #44")
        self.assertNotIn("original_title", nfo_video)

    def test_catalog_title_reaches_the_written_nfo(self):
        try:
            from test_support import load_flat_sync_module
            sync_module = load_flat_sync_module()
        except ImportError:
            self.skipTest("sync module not available")
        video_id = "telegram:-1001:45:996"
        self._insert_matched_scene(video_id, "Rinse and Rim")

        def real_create_nfo(video, genres, tags, path):
            sync_module.create_nfo_file(video, genres, tags, path, conn=self.conn)

        services = self._make_services(create_nfo_file=real_create_nfo)
        sync_unique_video(
            video={
                "provider": "telegram",
                "provider_video_id": video_id.split(":", 1)[1],
                "title": "Lukayan SHOW #45",
                "duration": 600,
                "performers": ["Alice"],
            },
            genres=set(),
            tags=set(),
            target_dir=self.media_dir,
            conn=self.conn,
            commit=True,
            _prepare_only=False,
            services=services,
        )

        nfo_path = self.media_dir / "telegram_-1001_45_996.nfo"
        self.assertTrue(nfo_path.is_file())
        xml = nfo_path.read_text(encoding="utf-8")
        # Jellyfin imports this sidecar for the locked Telegram entry.
        self.assertIn("<title>Rinse and Rim</title>", xml)
        self.assertIn("<originaltitle>Lukayan SHOW #45</originaltitle>", xml)
        self.assertIn('<uniqueid type="stashdb" default="false">stash-uuid-1</uniqueid>', xml)
        self.assertIn("<lockdata>true</lockdata>", xml)

    def _collect_one(self, video_id="testprov:v9"):
        return {
            video_id: {
                "video": {"provider": "testprov", "provider_video_id": video_id.split(":")[-1],
                          "title": "Unpublished Video", "duration": 600, "performers": ["Alice"]},
                "genres": {"Action"},
                "tags": {"tagA"},
            },
        }

    def _publish_for_real(self, path, video_id):
        path.write_text(f"http://resolver.invalid/admin/play/{video_id}/stream?mode=proxy\n",
                        encoding="utf-8")
        return True

    def _write_nfo_from_catalog(self, video, genres, tags, path):
        path.write_text(f"<movie><title>{video.get('title')}</title>"
                        f"<genre>{','.join(sorted(genres))}</genre></movie>", encoding="utf-8")

    def test_file_write_failure_after_the_commit_keeps_the_entry_unpublished(self):
        """A catalog row that could not be published stays pending, not lost."""
        services = self._make_services(
            write_stream_file=MagicMock(side_effect=OSError("Read-only file system")),
            create_nfo_file=self._write_nfo_from_catalog,
        )
        collected = self._collect_one()

        with self.assertRaisesRegex(RuntimeError, "file write: OSError"):
            flush_collected(collected=collected, conn=self.conn, media_dir=self.media_dir,
                            source_name="TestSource", raise_on_error=True, services=services)

        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 1,
                         "the durable catalog row must survive the failed publication")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM synced_files").fetchone()[0], 0,
                         "an unpublished stream must not be registered")
        self.assertEqual(list(self.media_dir.glob("*.strm")), [])
        self.assertEqual(publication_pending_ids(self.conn, 10), ["testprov:v9"])

    def test_republish_pending_videos_recovers_an_entry_from_an_earlier_run(self):
        """Publication retries from the database, so a restart can still recover."""
        broken = self._make_services(
            write_stream_file=MagicMock(side_effect=OSError("Read-only file system")),
            create_nfo_file=self._write_nfo_from_catalog,
        )
        with self.assertRaises(RuntimeError):
            flush_collected(collected=self._collect_one(), conn=self.conn,
                            media_dir=self.media_dir, raise_on_error=True, services=broken)
        self.conn.close()
        self.conn = db.connect(self.db_url)
        self.addCleanup(self.conn.close)

        recovered = self._make_services(write_stream_file=self._publish_for_real,
                                        create_nfo_file=self._write_nfo_from_catalog)
        result = republish_pending_videos(self.conn, self.media_dir, services=recovered)

        self.assertEqual(result, {"pending": 1, "published": 1, "failed": 0})
        self.assertEqual(
            self.conn.execute("SELECT file_path FROM synced_files").fetchall(),
            [("testprov_v9.strm",)],
        )
        self.assertEqual(publication_pending_ids(self.conn, 10), [])
        self.assertEqual(
            sorted(path.name for path in self.media_dir.glob("*")),
            ["testprov_v9.nfo", "testprov_v9.strm"],
        )
        # The retry rebuilds metadata from the catalog row, not from memory.
        self.assertIn("<title>Unpublished Video</title>",
                      (self.media_dir / "testprov_v9.nfo").read_text(encoding="utf-8"))
        self.assertIn("<genre>Action</genre>",
                      (self.media_dir / "testprov_v9.nfo").read_text(encoding="utf-8"))

    def test_republish_pending_videos_leaves_a_hidden_duplicate_alone(self):
        """Merging a video away is a decision; a retry pass must not undo it."""
        services = self._make_services(write_stream_file=self._publish_for_real)
        flush_collected(collected=self._collect_one(), conn=self.conn,
                        media_dir=self.media_dir, services=services)
        self.conn.execute(
            "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot)"
            " VALUES (%s,%s,'op-1','active',1,'{}')", ("testprov:v9", "testprov:v1"),
        )
        self.conn.execute("DELETE FROM synced_files WHERE video_id = %s", ("testprov:v9",))
        self.conn.commit()

        result = republish_pending_videos(self.conn, self.media_dir, services=services)

        self.assertEqual(result["published"], 0)
        self.assertEqual(publication_pending_ids(self.conn, 10), [],
                         "the active decision is what hides this row from the retry")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM synced_files WHERE video_id = %s", ("testprov:v9",)).fetchone()[0], 0)
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_decisions SET state='undone' WHERE loser_id=%s",
                              ("testprov:v9",))
        self.assertEqual(publication_pending_ids(self.conn, 10), ["testprov:v9"],
                         "the row is otherwise a genuine publication candidate")


class TestVideoPersistenceChannelHints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.media_dir = Path(self.temp_dir.name) / "media"
        self.media_dir.mkdir()
        self.conn = MagicMock()
        self.conn.execute.side_effect = Exception("No DB in mock")

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
            "sync_unique_video": None,
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

    def test_telegram_channel_name_hints_distinguishes_channel_from_studio(self):
        services = self._make_services()
        video_with_hint = {
            "provider": "telegram",
            "provider_video_id": "-100123:42:1",
            "title": "Scene with channel title",
            "studio": "CustomChannel",
            "duration": 600,
            "performers": [],
        }
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": "CustomChannel,OtherChannel"}):
            prep_hint = sync_unique_video(
                video=dict(video_with_hint),
                genres=set(),
                tags=set(),
                target_dir=self.media_dir,
                conn=self.conn,
                commit=False,
                _prepare_only=True,
                services=services,
            )
            self.assertEqual(prep_hint["video"].get("channel"), "CustomChannel")
            self.assertEqual(prep_hint["video"].get("studio"), "")
            self.assertIsNone(prep_hint.get("studio"))

        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": ""}):
            prep_no_hint = sync_unique_video(
                video=dict(video_with_hint),
                genres=set(),
                tags=set(),
                target_dir=self.media_dir,
                conn=self.conn,
                commit=False,
                _prepare_only=True,
                services=services,
            )
            self.assertIsNone(prep_no_hint["video"].get("channel"))
            self.assertEqual(prep_no_hint["video"].get("studio"), "CustomChannel")
            self.assertEqual(prep_no_hint.get("studio"), "CustomChannel")

    def test_telegram_channel_name_hints_downstream_nfo_behavior(self):
        from nfo import create_nfo_file
        services = self._make_services()
        video_base = {
            "provider": "telegram",
            "provider_video_id": "-100123:42:2",
            "title": "[BelAmi] Scene Title",
            "studio": "MyTelegramSource",
            "duration": 600,
            "performers": [],
        }

        # With hints configured: channel is recognized as channel, and BelAmi as production studio
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": "MyTelegramSource"}):
            prep_hint = sync_unique_video(
                video=dict(video_base),
                genres=set(),
                tags=set(),
                target_dir=self.media_dir,
                conn=self.conn,
                commit=False,
                _prepare_only=True,
                services=services,
            )
            self.assertEqual(prep_hint["video"].get("channel"), "MyTelegramSource")
            self.assertEqual(prep_hint["video"].get("studio"), "BelAmi")
            self.assertEqual(prep_hint.get("studio"), "BelAmi")
            nfo_path_hint = self.media_dir / "hint_test.nfo"
            create_nfo_file(
                prep_hint["video"],
                set(),
                set(),
                nfo_path_hint,
                get_provider_info_fn=lambda _: {"name": "Telegram", "domain": "t.me"},
                clean_display_title_fn=lambda t, **_: t,
                normalize_performers_fn=lambda p: p,
                map_categories_fn=lambda c, **_: c,
            )
            content_hint = nfo_path_hint.read_text(encoding="utf-8")
            self.assertIn("<tagline>Kanał: MyTelegramSource</tagline>", content_hint)
            self.assertIn("<studio>BelAmi</studio>", content_hint)
            self.assertNotIn("<studio>MyTelegramSource</studio>", content_hint)

        # Without hints: channel is not recognized, so production studio falls back to channel_name in NFO
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": ""}):
            prep_no_hint = sync_unique_video(
                video=dict(video_base),
                genres=set(),
                tags=set(),
                target_dir=self.media_dir,
                conn=self.conn,
                commit=False,
                _prepare_only=True,
                services=services,
            )
            self.assertIsNone(prep_no_hint["video"].get("channel"))
            self.assertEqual(prep_no_hint["video"].get("studio"), "BelAmi")
            nfo_path_no_hint = self.media_dir / "no_hint_test.nfo"
            create_nfo_file(
                prep_no_hint["video"],
                set(),
                set(),
                nfo_path_no_hint,
                get_provider_info_fn=lambda _: {"name": "Telegram", "domain": "t.me"},
                clean_display_title_fn=lambda t, **_: t,
                normalize_performers_fn=lambda p: p,
                map_categories_fn=lambda c, **_: c,
            )
            content_no_hint = nfo_path_no_hint.read_text(encoding="utf-8")
            # Without hints, MyTelegramSource channel was lost; BelAmi became the channel_name tagline
            self.assertNotIn("Kanał: MyTelegramSource", content_no_hint)
            self.assertIn("<tagline>Kanał: BelAmi</tagline>", content_no_hint)

    def test_startup_warning_when_telegram_configured_and_hints_empty(self):
        reset_channel_hints_warning()
        mock_logger = MagicMock()
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": "", "TELEGRAM_API_ID": "12345", "SYNC_TELEGRAM": "true"}):
            warned = warn_if_channel_hints_missing(mock_logger)
            self.assertTrue(warned)
            mock_logger.warning.assert_called_once()
            warning_text = mock_logger.warning.call_args[0][0]
            self.assertIn("TELEGRAM_CHANNEL_NAME_HINTS is empty", warning_text)
            # Ensure no private channel identifiers or secrets are logged
            self.assertNotIn("12345", warning_text)

            # Subsequent call does NOT warn again (once-per-startup / not repeated per video)
            mock_logger.reset_mock()
            warned_second = warn_if_channel_hints_missing(mock_logger)
            self.assertFalse(warned_second)
            mock_logger.warning.assert_not_called()

    def test_startup_warning_not_repeated_across_videos_in_sync_unique_video(self):
        reset_channel_hints_warning()
        mock_logger = MagicMock()
        services = self._make_services(logger=mock_logger)
        video = {
            "provider": "telegram",
            "provider_video_id": "tg-vid-1",
            "title": "Video 1",
            "studio": "SomeChan",
            "duration": 600,
        }
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": ""}):
            # Processing first video triggers once-per-startup check
            sync_unique_video(
                video=dict(video),
                genres=set(),
                tags=set(),
                target_dir=self.media_dir,
                conn=self.conn,
                services=services,
                _prepare_only=True,
            )
            self.assertEqual(mock_logger.warning.call_count, 1)

            # Processing second video must NOT repeat warning
            video2 = dict(video, provider_video_id="tg-vid-2", title="Video 2")
            sync_unique_video(
                video=video2,
                genres=set(),
                tags=set(),
                target_dir=self.media_dir,
                conn=self.conn,
                services=services,
                _prepare_only=True,
            )
            self.assertEqual(mock_logger.warning.call_count, 1, "Warning must not be repeated per video")

    def test_no_startup_warning_when_hints_are_set(self):
        reset_channel_hints_warning()
        mock_logger = MagicMock()
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": "ExplicitHint", "TELEGRAM_API_ID": "12345", "SYNC_TELEGRAM": "true"}):
            warned = warn_if_channel_hints_missing(mock_logger)
            self.assertFalse(warned)
            mock_logger.warning.assert_not_called()

    def test_no_startup_warning_when_telegram_not_configured(self):
        reset_channel_hints_warning()
        mock_logger = MagicMock()
        with patch.dict(os.environ, {"TELEGRAM_CHANNEL_NAME_HINTS": "", "TELEGRAM_API_ID": "", "SYNC_TELEGRAM": "false", "TELEGRAM_CHANNELS": ""}, clear=True):
            with patch("video_persistence._is_telegram_configured", return_value=False):
                warned = warn_if_channel_hints_missing(mock_logger)
                self.assertFalse(warned)
                mock_logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
