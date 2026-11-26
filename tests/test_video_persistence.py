import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


from pg_helper import PgTestCase
from common import db
from sync.video_persistence import (
    VideoPersistenceServices,
    _merge_video_fields,
    persist_prepared_video,
    publication_pending_ids,
    republish_pending_videos,
    sync_unique_video,
    flush_collected,
    write_media_files,
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

    def test_resolution_is_stored_and_never_cleared(self):
        """Telegram reports the encoded size with every document; keep the last one."""
        video = {
            "provider": "telegram",
            "provider_video_id": "chan:1:100",
            "title": "Resolved Scene",
            "duration": 600,
            "performers": [],
            "thumbnail": "http://img/thumb.jpg",
            "width": 3840,
            "height": 2160,
        }
        prepared = {"comp_id": "telegram:chan:1:100", "provider": "telegram",
                    "title": "Resolved Scene", "video": video,
                    "genres": set(), "tags": set(), "studio": None}
        with self.conn:
            persist_prepared_video(self.conn, prepared, time_fn=lambda: 1700000000.0)
        row = self.conn.execute(
            "SELECT width, height, quality_tier FROM videos WHERE id = %s",
            ("telegram:chan:1:100",),
        ).fetchone()
        self.assertEqual(row, (3840, 2160, "high"))

        # A later message without the attribute must not wipe the resolution.
        prepared["video"] = {**video, "width": None, "height": None}
        with self.conn:
            persist_prepared_video(self.conn, prepared, time_fn=lambda: 1700000100.0)
        self.assertEqual(
            self.conn.execute(
                "SELECT width, height, quality_tier FROM videos WHERE id = %s",
                ("telegram:chan:1:100",),
            ).fetchone(),
            (3840, 2160, "high"),
        )

        # The resolution beats the bitrate: 1080p at a low bitrate is HD, not SD.
        prepared["video"] = {**video, "width": 1920, "height": 1080}
        with self.conn:
            persist_prepared_video(self.conn, prepared, time_fn=lambda: 1700000200.0)
            self.conn.execute(
                "UPDATE videos SET file_size_bytes = 200000000 WHERE id = %s",
                ("telegram:chan:1:100",),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT bitrate_kbps, quality_tier FROM videos WHERE id = %s",
                ("telegram:chan:1:100",),
            ).fetchone(),
            (2667, "medium"),
        )

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
        services.download_image.assert_not_called()
        services.write_stream_file.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

        write_media_files(services, prepared, self.conn)
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

    def test_catalog_scene_match_keeps_catalog_title_and_identity_in_db(self):
        video_id = "telegram:-1001:42:999"
        self._insert_matched_scene(video_id, "Rinse and Rim")

        self._persist_telegram_listing(video_id, "Lukayan SHOW #42")

        row = self.conn.execute(
            "SELECT title, scene_match_source, scene_match_external_id"
            " FROM videos WHERE id=%s", (video_id,)).fetchone()
        self.assertEqual(row[0], "Rinse and Rim")
        # The catalog identity is durable.
        self.assertEqual(row[1], "stashdb:phash")
        self.assertEqual(row[2], "stash-uuid-1")

    def test_operator_title_override_wins_over_catalog_title(self):
        video_id = "telegram:-1001:43:998"
        self._insert_matched_scene(video_id, "Rinse and Rim", override="Operator Title")

        self._persist_telegram_listing(video_id, "Lukayan SHOW #43")

        # An operator override must never be replaced by the catalog title.
        row = self.conn.execute(
            "SELECT title, scene_match_external_id FROM videos WHERE id=%s", (video_id,)).fetchone()
        self.assertEqual(row[0], "Lukayan SHOW #43")
        # The catalog identity is still recorded.
        self.assertEqual(row[1], "stash-uuid-1")

    def test_listing_without_catalog_match_keeps_provider_title(self):
        video_id = "telegram:-1001:44:997"
        self.conn.execute(
            """INSERT INTO videos (id,provider,title,duration,performers,studio,first_seen,last_seen)
               VALUES (%s,'telegram','Rinse and Rim',600,'[]','Example Studio',1000,1000)""",
            (video_id,),
        )
        self.conn.commit()

        self._persist_telegram_listing(video_id, "Lukayan SHOW #44")

        row = self.conn.execute(
            "SELECT title, scene_match_source FROM videos WHERE id=%s", (video_id,)).fetchone()
        self.assertEqual(row[0], "Lukayan SHOW #44")
        self.assertIsNone(row[1])

    def test_catalog_title_reaches_the_catalog_row_and_writes_no_sidecar_metadata(self):
        video_id = "telegram:-1001:45:996"
        self._insert_matched_scene(video_id, "Rinse and Rim")

        services = self._make_services()
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

        row = self.conn.execute(
            "SELECT title, scene_match_external_id FROM videos WHERE id=%s",
            (video_id,)).fetchone()
        self.assertEqual(row[0], "Rinse and Rim")
        self.assertEqual(row[1], "stash-uuid-1")
        # The library keeps only the stream pointer and artwork; Kodi metadata is gone.
        self.assertEqual(list(self.media_dir.glob("*.nfo")), [])

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

    def test_file_write_failure_after_the_commit_keeps_the_entry_unpublished(self):
        """A catalog row that could not be published stays pending, not lost."""
        services = self._make_services(
            write_stream_file=MagicMock(side_effect=OSError("Read-only file system")),
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
        )
        with self.assertRaises(RuntimeError):
            flush_collected(collected=self._collect_one(), conn=self.conn,
                            media_dir=self.media_dir, raise_on_error=True, services=broken)
        self.conn.close()
        self.conn = db.connect(self.db_url)
        self.addCleanup(self.conn.close)

        recovered = self._make_services(write_stream_file=self._publish_for_real)
        result = republish_pending_videos(self.conn, self.media_dir, services=recovered)

        self.assertEqual(result, {"pending": 1, "published": 1, "failed": 0})
        self.assertEqual(
            self.conn.execute("SELECT file_path FROM synced_files").fetchall(),
            [("testprov_v9.strm",)],
        )
        self.assertEqual(publication_pending_ids(self.conn, 10), [])
        self.assertEqual(
            sorted(path.name for path in self.media_dir.glob("*")),
            ["testprov_v9.strm"],
        )

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

    def test_telegram_video_records_primary_playable_source_in_same_transaction(self):
        services = self._make_services()
        video = {
            "provider": "telegram",
            "provider_video_id": "-100123:42:1",
            "title": "Telegram Scene 42",
            "duration": 600,
            "performers": ["Alice"],
        }
        sync_unique_video(
            video=video,
            genres={"Action"},
            tags={"tag1"},
            target_dir=self.media_dir,
            conn=self.conn,
            commit=True,
            _prepare_only=False,
            services=services,
        )
        vid_id = "telegram:-100123:42:1"
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos WHERE id=%s", (vid_id,)).fetchone()[0], 1)
        src_row = self.conn.execute(
            "SELECT video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active "
            "FROM video_sources WHERE video_id=%s",
            (vid_id,),
        ).fetchone()
        self.assertIsNotNone(src_row)
        self.assertEqual(src_row, (
            vid_id, vid_id, "telegram", vid_id, "https://t.me/c/123/42", "-100123", 1, 1
        ))

    def test_telegram_video_unsupported_channel_id_leaves_source_url_blank(self):
        services = self._make_services()
        video = {
            "provider": "telegram",
            "provider_video_id": "-5:99:1",
            "title": "Small Group Video",
            "duration": 600,
        }
        sync_unique_video(
            video=video,
            genres={"Action"},
            tags={"tag1"},
            target_dir=self.media_dir,
            conn=self.conn,
            commit=True,
            _prepare_only=False,
            services=services,
        )
        vid_id = "telegram:-5:99:1"
        src_row = self.conn.execute(
            "SELECT video_id, source_id, source_url, channel_id, priority, is_active "
            "FROM video_sources WHERE video_id=%s",
            (vid_id,),
        ).fetchone()
        self.assertIsNotNone(src_row)
        self.assertEqual(src_row, (vid_id, vid_id, "", "-5", 1, 1))

    def test_non_telegram_video_does_not_record_video_source(self):
        services = self._make_services()
        video = {
            "provider": "testprov",
            "provider_video_id": "v200",
            "title": "Web Scene 200",
            "duration": 600,
            "performers": ["Bob"],
        }
        sync_unique_video(
            video=video,
            genres={"Action"},
            tags={"tag1"},
            target_dir=self.media_dir,
            conn=self.conn,
            commit=True,
            _prepare_only=False,
            services=services,
        )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos WHERE id='testprov:v200'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_sources WHERE video_id='testprov:v200'").fetchone()[0], 0)

    def test_telegram_primary_source_survives_sidecar_file_write_failure(self):
        services = self._make_services(
            write_stream_file=MagicMock(side_effect=OSError("Disk full")),
        )
        vid_id = "telegram:-100123:55:1"
        collected = {
            vid_id: {
                "video": {"provider": "telegram", "provider_video_id": "-100123:55:1",
                          "title": "Unpublished Telegram Video", "duration": 600, "performers": ["Alice"]},
                "genres": {"Action"},
                "tags": {"tagA"},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "file write: OSError"):
            flush_collected(collected=collected, conn=self.conn, media_dir=self.media_dir,
                            source_name="Telegram", raise_on_error=True, services=services)

        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos WHERE id=%s", (vid_id,)).fetchone()[0], 1)
        src_row = self.conn.execute(
            "SELECT video_id, source_id, priority, is_active FROM video_sources WHERE video_id=%s",
            (vid_id,),
        ).fetchone()
        self.assertEqual(src_row, (vid_id, vid_id, 1, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM synced_files WHERE video_id=%s", (vid_id,)).fetchone()[0], 0)

    def test_batch_flush_collected_records_primary_sources_and_rolls_back_on_failure(self):
        calls = 0
        def fail_on_second(conn, prep):
            nonlocal calls
            calls += 1
            persist_prepared_video(conn, prep)
            if calls == 2:
                raise OSError("simulated persist failure")

        services = self._make_services(persist_prepared_video=fail_on_second)
        collected = {
            "telegram:-1001:1": {
                "video": {"provider": "telegram", "provider_video_id": "-1001:1", "title": "Vid 1", "duration": 600},
                "genres": {"Action"},
                "tags": set(),
            },
            "telegram:-1001:2": {
                "video": {"provider": "telegram", "provider_video_id": "-1001:2", "title": "Vid 2", "duration": 600},
                "genres": {"Comedy"},
                "tags": set(),
            },
        }
        written = flush_collected(collected=collected, conn=self.conn, media_dir=self.media_dir, services=services)
        self.assertEqual(written, 0)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos WHERE id LIKE 'telegram:-1001:%'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_sources WHERE video_id LIKE 'telegram:-1001:%'").fetchone()[0], 0)

    def test_telegram_primary_source_idempotent_and_protects_other_canonical_owner(self):
        services = self._make_services()
        keeper_id = "telegram:-1001:99:keeper"
        loser_id = "telegram:-1002:88:loser"

        sync_unique_video(
            video={"provider": "telegram", "provider_video_id": "-1001:99:keeper", "title": "Keeper", "duration": 600},
            genres=set(), tags=set(), target_dir=self.media_dir, conn=self.conn, commit=True, services=services,
        )
        self.conn.execute(
            "INSERT INTO video_sources (video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active, created_at) "
            "VALUES (%s, %s, 'telegram', %s, 'https://t.me/c/2/88', '-1002', 2, 1, 1000)",
            (keeper_id, loser_id, loser_id),
        )
        self.conn.commit()

        # Re-syncing loser video must RAISE and rollback: video/source pair cannot partially commit
        with self.assertRaisesRegex(RuntimeError, "already owned by another video"):
            sync_unique_video(
                video={"provider": "telegram", "provider_video_id": "-1002:88:loser", "title": "Loser", "duration": 600},
                genres=set(), tags=set(), target_dir=self.media_dir, conn=self.conn, commit=True, services=services,
            )
        # Loser video row must not exist in videos (transaction rolled back)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM videos WHERE id=%s", (loser_id,)).fetchone()[0],
            0,
            "Video persistence must be rolled back on source conflict",
        )
        # Source row remains owned by keeper
        src = self.conn.execute(
            "SELECT video_id, source_id, priority FROM video_sources WHERE source_id=%s",
            (loser_id,),
        ).fetchone()
        self.assertEqual(src, (keeper_id, loser_id, 2), "Must not overwrite source owned by different canonical video")

        # Demote and deactivate keeper to verify re-sync does not reset priority or is_active
        self.conn.execute(
            "UPDATE video_sources SET priority = 5, is_active = 0 WHERE source_id=%s",
            (keeper_id,),
        )
        self.conn.commit()

        # Repeated sync of keeper is idempotent and does not reset priority or is_active
        sync_unique_video(
            video={"provider": "telegram", "provider_video_id": "-1001:99:keeper", "title": "Keeper Updated", "duration": 600},
            genres=set(), tags=set(), target_dir=self.media_dir, conn=self.conn, commit=True, services=services,
        )
        keeper_src = self.conn.execute(
            "SELECT video_id, source_id, priority, is_active FROM video_sources WHERE source_id=%s",
            (keeper_id,),
        ).fetchone()
        self.assertEqual(keeper_src, (keeper_id, keeper_id, 5, 0), "Re-sync must not reset priority or is_active")


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

    def test_a_studio_equal_to_the_items_channel_is_a_channel_name(self):
        services = self._make_services()
        video = {
            "provider": "telegram",
            "provider_video_id": "-100123:42:1",
            "title": "Scene with channel title",
            "studio": "CustomChannel",
            "channel_name": "CustomChannel",
            "duration": 600,
            "performers": [],
        }

        prepared = sync_unique_video(
            video=dict(video),
            genres=set(),
            tags=set(),
            target_dir=self.media_dir,
            conn=self.conn,
            commit=False,
            _prepare_only=True,
            services=services,
        )

        self.assertEqual(prepared["video"].get("channel"), "CustomChannel")
        self.assertEqual(prepared["video"].get("studio"), "")
        self.assertIsNone(prepared.get("studio"))

    def test_the_title_studio_wins_over_the_channel_label(self):
        services = self._make_services()
        video = {
            "provider": "telegram",
            "provider_video_id": "-100123:42:2",
            "title": "[BelAmi] Scene Title",
            "studio": "MyTelegramSource",
            "channel_name": "MyTelegramSource",
            "duration": 600,
            "performers": [],
        }

        prepared = sync_unique_video(
            video=dict(video),
            genres=set(),
            tags=set(),
            target_dir=self.media_dir,
            conn=self.conn,
            commit=False,
            _prepare_only=True,
            services=services,
        )

        self.assertEqual(prepared["video"].get("channel"), "MyTelegramSource")
        self.assertEqual(prepared["video"].get("studio"), "BelAmi")
        self.assertEqual(prepared.get("studio"), "BelAmi")

    def test_a_custom_studio_without_a_channel_match_survives(self):
        services = self._make_services()
        video = {
            "provider": "telegram",
            "provider_video_id": "-100123:42:3",
            "title": "Scene Title",
            "studio": "CustomChannel",
            "duration": 600,
            "performers": [],
        }

        prepared = sync_unique_video(
            video=dict(video),
            genres=set(),
            tags=set(),
            target_dir=self.media_dir,
            conn=self.conn,
            commit=False,
            _prepare_only=True,
            services=services,
        )

        self.assertIsNone(prepared["video"].get("channel"))
        self.assertEqual(prepared["video"].get("studio"), "CustomChannel")
        self.assertEqual(prepared.get("studio"), "CustomChannel")


class TestVideoSourcesPersistenceUnit(unittest.TestCase):
    def test_record_primary_video_source_skips_non_telegram(self):
        from sync.video_persistence import _record_primary_video_source
        conn = MagicMock()
        _record_primary_video_source(conn, "testprov:123", "testprov", {"duration": 600})
        conn.execute.assert_not_called()

    def test_record_primary_video_source_executes_expected_sql_for_telegram(self):
        from sync.video_persistence import _record_primary_video_source
        conn = MagicMock()
        _record_primary_video_source(
            conn,
            "telegram:-100123:42:1",
            "telegram",
            {"duration": 600},
            time_fn=lambda: 1700000000.0,
        )
        self.assertEqual(conn.execute.call_count, 1)
        sql, params = conn.execute.call_args[0]
        self.assertIn("INSERT INTO video_sources", sql)
        self.assertIn("WHERE video_sources.video_id = EXCLUDED.video_id", sql)
        self.assertIn("RETURNING video_sources.video_id", sql)
        update_clause = sql.split("DO UPDATE SET")[1].split("WHERE")[0]
        self.assertNotIn("priority", update_clause)
        self.assertNotIn("is_active", update_clause)
        self.assertEqual(params, (
            "telegram:-100123:42:1",
            "telegram:-100123:42:1",
            "telegram",
            "telegram:-100123:42:1",
            "https://t.me/c/123/42",
            "-100123",
            1700000000,
        ))

    def test_record_primary_video_source_unsupported_small_group_id(self):
        from sync.video_persistence import _record_primary_video_source
        conn = MagicMock()
        _record_primary_video_source(
            conn,
            "telegram:-5:42",
            "telegram",
            {"duration": 600},
            time_fn=lambda: 1700000000.0,
        )
        self.assertEqual(conn.execute.call_count, 1)
        _, params = conn.execute.call_args[0]
        self.assertEqual(params[4], "", "Small-group channel must leave source_url blank")
        self.assertEqual(params[5], "-5")

    def test_record_primary_video_source_unsupported_missing_message_id(self):
        from sync.video_persistence import _record_primary_video_source
        conn = MagicMock()
        _record_primary_video_source(
            conn,
            "telegram:-100123",
            "telegram",
            {"duration": 600},
            time_fn=lambda: 1700000000.0,
        )
        self.assertEqual(conn.execute.call_count, 1)
        _, params = conn.execute.call_args[0]
        self.assertEqual(params[4], "", "Missing message ID must leave source_url blank")
        self.assertEqual(params[5], "-100123")

    def test_record_primary_video_source_custom_channel_and_source_url(self):
        from sync.video_persistence import _record_primary_video_source
        conn = MagicMock()
        _record_primary_video_source(
            conn,
            "telegram:fallback:99",
            "telegram",
            {
                "channel_id": "-1009999",
                "source_url": "https://t.me/c/custom/99",
                "provider_ref": "custom_ref",
                "duration": 600,
            },
            time_fn=lambda: 1700000000.0,
        )
        self.assertEqual(conn.execute.call_count, 1)
        sql, params = conn.execute.call_args[0]
        self.assertEqual(params, (
            "telegram:fallback:99",
            "telegram:fallback:99",
            "telegram",
            "custom_ref",
            "https://t.me/c/custom/99",
            "-1009999",
            1700000000,
        ))

    def test_record_primary_video_source_raises_on_conflict_with_different_video(self):
        from sync.video_persistence import _record_primary_video_source
        conn = MagicMock()
        # Simulate DO UPDATE WHERE false no-op (no rows returned by RETURNING)
        conn.execute.return_value.fetchone.return_value = None
        with self.assertRaisesRegex(RuntimeError, "already owned by another video"):
            _record_primary_video_source(
                conn,
                "telegram:-100123:42:1",
                "telegram",
                {"duration": 600},
                time_fn=lambda: 1700000000.0,
            )

    def test_persist_prepared_video_calls_record_source_in_same_conn(self):
        from sync.video_persistence import persist_prepared_video
        conn = MagicMock()
        prepared = {
            "comp_id": "telegram:-100123:42",
            "provider": "telegram",
            "title": "Title",
            "video": {"duration": 600},
            "genres": set(),
            "tags": [],
            "studio": "",
        }
        persist_prepared_video(conn, prepared, time_fn=lambda: 1700000000.0)
        calls = [c[0][0] for c in conn.execute.call_args_list]
        video_indexes = [i for i, c in enumerate(calls) if "INSERT INTO videos" in c]
        source_indexes = [i for i, c in enumerate(calls) if "INSERT INTO video_sources" in c]
        self.assertEqual(len(video_indexes), 1, "Exactly one INSERT INTO videos must be performed")
        self.assertEqual(len(source_indexes), 1, "Exactly one INSERT INTO video_sources must be performed")
        self.assertLess(
            video_indexes[0],
            source_indexes[0],
            "Video persistence must precede source recording on the same connection",
        )
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()

    def test_sync_unique_video_rolls_back_on_source_conflict(self):
        from sync.video_persistence import sync_unique_video

        conn = MagicMock()
        # Simulate fetchone returning None for video_sources RETURNING query
        def fake_execute(sql, params=None):
            cur = MagicMock()
            if "INSERT INTO video_sources" in sql:
                cur.fetchone.return_value = None
            else:
                cur.fetchone.return_value = ("2026-01-01 00:00:00",)
            return cur

        conn.execute.side_effect = fake_execute

        services = MagicMock()
        services.stable_media_basename.return_value = "tg_1"
        services.map_categories.return_value = set()
        services.normalize_performers = lambda p: list(p)
        services.time_fn.return_value = 1700000000.0
        services.thumbnail_urls.return_value = ("", "")

        from sync.video_persistence import persist_prepared_video
        services.persist_prepared_video = lambda c, p: persist_prepared_video(c, p)

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "already owned by another video"):
                sync_unique_video(
                    video={"provider": "telegram", "provider_video_id": "-100123:42:1", "duration": 600},
                    genres=set(),
                    tags=set(),
                    target_dir=Path(tmpdir),
                    conn=conn,
                    commit=True,
                    services=services,
                )
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
