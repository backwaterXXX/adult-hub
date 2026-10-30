import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nfo
from scene_scrapers import (
    ScrapedScene,
    backfill_scenes,
    backfill_telegram_fingerprints,
    _telegram_oshash,
)


class TestNfoPlotAndPremiered(unittest.TestCase):
    def test_nfo_custom_plot_and_premiered(self):
        video = {
            "provider": "telegram",
            "provider_video_id": "-1001:42:100",
            "title": "Summer Buddies",
            "plot": "Official synopsis of the scene.",
            "date_released": "2006-06-01",
            "performers": ["Aiden Shaw"],
            "studio": "Falcon Studios",
        }
        with tempfile.TemporaryDirectory() as d:
            nfo_file = Path(d) / "test.nfo"
            nfo.create_nfo_file(
                video,
                genres={"Anal"},
                tags={"Falcon Studios"},
                dest_path=nfo_file,
                get_provider_info_fn=lambda p: {"name": "Telegram", "domain": "t.me", "base_url": "https://t.me"},
                clean_display_title_fn=lambda t, **kw: t,
                normalize_performers_fn=lambda p: p,
                map_categories_fn=lambda c, **kw: c,
            )
            xml_text = nfo_file.read_text(encoding="utf-8")
            self.assertIn("<plot>Official synopsis of the scene.</plot>", xml_text)
            self.assertIn("<premiered>2006-06-01</premiered>", xml_text)
            self.assertIn("<year>2006</year>", xml_text)
            self.assertIn("<studio>Falcon Studios</studio>", xml_text)


class TestOshashAlgorithm(unittest.TestCase):
    def test_keeps_full_tail_block_when_blocks_overlap(self):
        class Response:
            status_code = 206

            def __init__(self, start, end, total):
                self.headers = {"content-range": f"bytes {start}-{end}/{total}"}
                self.data = bytes([1]) * (end - start + 1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_bytes(self):
                yield self.data

        class Client:
            def __init__(self):
                self.ranges = []

            def stream(self, method, url, *, headers, timeout):
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                self.ranges.append((start, end))
                return Response(start, end, 86_193)

        client = Client()
        fingerprint, bytes_read = _telegram_oshash(
            "telegram:-1001234567890:42:999", http_client=client,
        )
        self.assertEqual(client.ranges, [(0, 65535), (20_657, 86_192)])
        self.assertEqual(bytes_read, 131_072)
        expected = (86_193 + int.from_bytes(bytes([1]) * 8, "little") * 16_384) & 0xFFFFFFFFFFFFFFFF
        self.assertEqual(fingerprint, f"{expected:016x}")


class TestBackfillScenes(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.media_dir = Path(self.temp_dir.name)

    def test_trusted_proposals_are_applied_once_and_preserve_overrides(self):
        from common.metadata_review import store_metadata_review_proposal
        from scene_scraper_backfill import apply_pending_fingerprint_proposals
        video_id = 'telegram:-1001:99:999'
        self.conn.execute("INSERT INTO videos (id,provider,title,plot,studio,first_seen,last_seen,scene_match_source,scene_match_external_id) VALUES (%s,'telegram','Old','Old plot','Old studio',1,1,'stashdb:phash','scene-1')", (video_id,))
        self.conn.execute("INSERT INTO video_metadata_overrides (video_id,fields,revision) VALUES (%s,%s::jsonb,1)", (video_id, json.dumps({'studio': 'My studio'})))
        store_metadata_review_proposal(self.conn, video_id, {'plot': 'New plot', 'studio': 'New studio'}, source='stashdb:phash', external_id='scene-1', source_url='', confidence=0.9)
        self.conn.commit()
        with patch('scene_scrapers._update_media_files'):
            self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (1, 1))
            self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (0, 0))
        row = self.conn.execute('SELECT plot,studio FROM videos WHERE id=%s', (video_id,)).fetchone()
        self.assertEqual(tuple(row), ('New plot', 'Old studio'))
        self.assertEqual(self.conn.execute('SELECT status FROM metadata_review_proposals WHERE video_id=%s', (video_id,)).fetchone()[0], 'accepted')

    def test_text_proposals_apply_but_do_not_replace_hash_matches(self):
        from common.metadata_review import store_metadata_review_proposal
        from scene_scraper_backfill import apply_pending_fingerprint_proposals
        for suffix, source in [('101', 'stashdb:title_exact'), ('102', 'stashdb:phash')]:
            video_id = f'telegram:-1001:{suffix}:999'
            self.conn.execute("INSERT INTO videos (id,provider,title,plot,first_seen,last_seen,scene_match_source,scene_match_external_id) VALUES (%s,'telegram','Old','Old plot',1,1,%s,'scene-1')", (video_id, source))
            store_metadata_review_proposal(self.conn, video_id, {'plot': 'New plot'}, source='stashdb:title_exact', external_id='scene-1', source_url='', confidence=0.9)
        self.conn.commit()
        with patch('scene_scrapers._update_media_files'):
            self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (2, 1))
        rows = self.conn.execute("SELECT v.plot,p.status FROM videos v JOIN metadata_review_proposals p ON p.video_id=v.id ORDER BY v.id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [('New plot', 'accepted'), ('Old plot', 'rejected')])

    def test_pending_proposal_without_registered_media_is_deferred(self):
        from common.metadata_review import store_metadata_review_proposal
        from scene_scraper_backfill import apply_pending_fingerprint_proposals
        video_id = 'telegram:-1001:104:999'
        self.conn.execute("INSERT INTO videos (id,provider,title,plot,first_seen,last_seen,scene_match_source,scene_match_external_id) VALUES (%s,'telegram','Old','Old plot',1,1,'stashdb:title_exact','scene-2')", (video_id,))
        store_metadata_review_proposal(self.conn, video_id, {'plot': 'New plot'}, source='stashdb:title_exact', external_id='scene-2', source_url='', confidence=0.9)
        self.conn.commit()
        self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (0, 0))
        proposal = self.conn.execute('SELECT status,attempt_count FROM metadata_review_proposals WHERE video_id=%s', (video_id,)).fetchone()
        self.assertEqual(tuple(proposal), ('pending', 1))
        self.assertEqual(self.conn.execute('SELECT plot FROM videos WHERE id=%s', (video_id,)).fetchone()[0], 'Old plot')

    def test_pending_proposal_updates_every_registered_nfo_and_retries_missing(self):
        from common.metadata_review import store_metadata_review_proposal
        from scene_scraper_backfill import apply_pending_fingerprint_proposals
        video_id = 'telegram:-1001:103:999'
        self.conn.execute("INSERT INTO videos (id,provider,title,plot,first_seen,last_seen,scene_match_source,scene_match_external_id) VALUES (%s,'telegram','Old','Old plot',1,1,'stashdb:title_exact','scene-1')", (video_id,))
        for name in ('first', 'second'):
            self.conn.execute('INSERT INTO synced_files (file_path,video_id) VALUES (%s,%s)', (name + '.strm', video_id))
        (self.media_dir / 'first.nfo').write_text('<movie><plot>Old plot</plot></movie>')
        store_metadata_review_proposal(self.conn, video_id, {'plot': 'New plot'}, source='stashdb:title_exact', external_id='scene-1', source_url='', confidence=0.9)
        self.conn.commit()
        self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (0, 0))
        self.assertEqual(self.conn.execute('SELECT status FROM metadata_review_proposals WHERE video_id=%s', (video_id,)).fetchone()[0], 'pending')
        (self.media_dir / 'second.nfo').write_text('<movie><plot>Old plot</plot></movie>')
        self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (0, 0),
                         'failed proposals wait until their retry time')
        self.conn.execute("UPDATE metadata_review_proposals SET next_attempt_at=CURRENT_TIMESTAMP WHERE video_id=%s", (video_id,))
        self.conn.commit()
        self.assertEqual(apply_pending_fingerprint_proposals(self.conn, self.media_dir), (1, 1))
        for name in ('first', 'second'):
            self.assertIn('<plot>New plot</plot>', (self.media_dir / (name + '.nfo')).read_text())

    def test_backfill_scenes_enriches_db_and_nfo(self):
        vid_id = "telegram:-1001:42:999"
        self.conn.execute(
            """
            INSERT INTO videos (id, provider, title, slug, duration, performers, studio, first_seen, last_seen, source_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (vid_id, "telegram", "Summer Buddies", "999", 780, json.dumps(["Aiden Shaw"]), "Falcon Studios", 1000, 1000,
             "Title: Summer Buddies\nCast: Aiden Shaw"),
        )
        self.conn.commit()

        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, updated_at, stashdb_url, pbc_url) VALUES (%s, %s, %s, %s, %s)",
            ("Aiden Shaw", "bio", 100, "https://stashdb.org/performers/aiden", "https://pbc.xxx/wiki/Aiden_Shaw"),
        )
        self.conn.commit()

        # Create initial dummy NFO
        nfo_path = self.media_dir / "telegram [-1001_42_999-abcdef123456].nfo"
        nfo_path.write_text(
            "<movie><title>Summer Buddies</title><plot></plot><actor><name>Aiden Shaw</name><role>Performer</role></actor></movie>",
            encoding="utf-8",
        )

        mock_scene = ScrapedScene(
            title="Summer Buddies",
            plot="Sweaty gym jocks action.",
            date="2006-06-01",
            duration=783,
            poster_url="",
            performers=["Aiden Shaw", "Alec Campbell"],
            tags=["Anal"],
            studio="Falcon Studios",
            confidence=0.95,
            source="stashdb",
            external_id="stash-uuid-12345",
            url="https://stashdb.org/scenes/stash-uuid-12345",
        )

        with patch("scene_scraper_text_sources.search_scene_by_exact_title", return_value=mock_scene), \
             patch("scene_scrapers.backfill_telegram_fingerprints", return_value=(0, 0)), \
             patch("scene_scrapers.backfill_missing_telegram_oshashes", return_value=(0, 0)):
            checked, updated = backfill_scenes(self.conn, self.media_dir, limit=10, sleep_sec=0)

        # The title lookup and official performer-alias pass both inspect this row.
        self.assertEqual((checked, updated), (2, 1))

        # Check DB update
        row = self.conn.execute("SELECT plot, date_released, performers, scene_matched_at FROM videos WHERE id = %s", (vid_id,)).fetchone()
        self.assertEqual(row[0], "Sweaty gym jocks action.")
        self.assertEqual(row[1], "2006-06-01")
        self.assertEqual(json.loads(row[2]), ["Aiden Shaw", "Alec Campbell"])
        self.assertGreater(row[3], 0)
        proposal = self.conn.execute(
            "SELECT changes FROM metadata_review_proposals WHERE video_id=%s AND status='pending'", (vid_id,),
        ).fetchone()
        self.assertIsNone(proposal)

        # Check NFO update
        updated_nfo = nfo_path.read_text(encoding="utf-8")
        self.assertIn("<plot>Sweaty gym jocks action.</plot>", updated_nfo)
        self.assertIn("<premiered>2006-06-01</premiered>", updated_nfo)
        self.assertIn("<year>2006</year>", updated_nfo)
        self.assertIn('<uniqueid type="stashdb" default="false">stash-uuid-12345</uniqueid>', updated_nfo)
        self.assertIn("<website>https://stashdb.org/scenes/stash-uuid-12345</website>", updated_nfo)
        self.assertIn("<profile>https://stashdb.org/performers/aiden</profile>", updated_nfo)
        self.assertIn("<profile>https://pbc.xxx/wiki/Aiden_Shaw</profile>", updated_nfo)

    def test_telegram_fingerprint_match_fills_blanks_and_stages_existing_values(self):
        vid_id = "telegram:-1001234567890:42:999"
        other_id = "telegram:-1001234567890:43:998"
        self.conn.execute(
            """INSERT INTO videos (id,provider,title,duration,performers,studio,first_seen,last_seen)
               VALUES (%s,'telegram','Example Scene #42',600,'[]','Example Studio',1000,1000)""",
            (vid_id,),
        )
        self.conn.execute(
            """INSERT INTO videos (id,provider,title,duration,performers,studio,first_seen,last_seen)
               VALUES (%s,'telegram','Different upload',600,'[]','Example Studio',1000,1000)""",
            (other_id,),
        )
        self.conn.execute(
            "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) VALUES (%s,'OSHASH',%s)",
            (other_id, "0123456789abcdef"),
        )
        self.conn.commit()
        nfo_path = self.media_dir / "Example 42.nfo"
        nfo_path.write_text("<movie><title>Example Scene #42</title><plot></plot></movie>", encoding="utf-8")
        scene = ScrapedScene(
            title="Scene from exact hash", plot="Scene description", date="2020-01-02",
            performers=["Performer One"], tags=["Drama"], studio="Known Studio",
            poster_url="", external_id="scene-1",
        )
        with patch.dict(os.environ, {"TELEGRAM_FINGERPRINTS_ENABLED": "true"}), \
             patch("scene_scrapers._telegram_oshash", return_value=("0123456789abcdef", 131072)), \
             patch("scene_scrapers._search_tpdb_scene_by_oshash", return_value=scene):
            self.assertEqual(backfill_telegram_fingerprints(
                self.conn, self.media_dir, limit=1, sleep_sec=0, video_ids=[vid_id],
            ), (1, 1))
        row = self.conn.execute(
            "SELECT title,plot,date_released,performers,tags,studio FROM videos WHERE id=%s", (vid_id,)
        ).fetchone()
        self.assertEqual(row[0:3], ("Example Scene #42", "Scene description", "2020-01-02"))
        self.assertEqual(json.loads(row[3]), ["Performer One"])
        self.assertEqual(json.loads(row[4]), ["Drama"])
        self.assertEqual(row[5], "Example Studio")
        updated_nfo = nfo_path.read_text(encoding="utf-8")
        self.assertIn("<title>Example Scene #42</title>", updated_nfo)
        self.assertIn("<plot>Scene description</plot>", updated_nfo)
        proposal = self.conn.execute(
            "SELECT changes FROM metadata_review_proposals WHERE video_id=%s AND status='pending'", (vid_id,),
        ).fetchone()
        changes = json.loads(proposal[0])
        self.assertEqual(changes["title"], "Scene from exact hash")
        self.assertEqual(changes["studio"], "Known Studio")
        state = self.conn.execute(
            "SELECT outcome FROM external_metadata_attempts WHERE entity_key=%s AND source='telegram_fingerprint'",
            (vid_id,),
        ).fetchone()
        self.assertEqual(state[0], "found")
        cached_hash = self.conn.execute(
            "SELECT fingerprint FROM video_fingerprints WHERE video_id=%s AND algorithm='OSHASH'",
            (vid_id,),
        ).fetchone()
        self.assertEqual(cached_hash[0], "0123456789abcdef")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='hide'",
        ).fetchone()[0], 0)

    def test_catalog_scene_match_replaces_provider_title_in_nfo(self):
        vid_id = "telegram:-1001234567890:42:999"
        self.conn.execute(
            """INSERT INTO videos (id,provider,title,duration,performers,studio,first_seen,last_seen)
               VALUES (%s,'telegram','Provider Caption #42',600,'[]','Example Studio',1000,1000)""",
            (vid_id,),
        )
        self.conn.commit()
        self.conn.execute("INSERT INTO synced_files (file_path,video_id) VALUES (%s,%s)", ("Example 42.strm", vid_id))
        self.conn.commit()
        nfo_path = self.media_dir / "Example 42.nfo"
        nfo_path.write_text(
            "<movie><title>Provider Caption #42</title><plot></plot></movie>", encoding="utf-8",
        )
        scene = ScrapedScene(
            title="Catalog Scene Title", plot="Scene description", date="2020-01-02",
            performers=["Performer One"], tags=["Drama"], studio="Known Studio",
            poster_url="", external_id="stash-uuid-1",
            url="https://stashdb.org/scenes/stash-uuid-1", source="stashdb:phash",
        )
        with patch.dict(os.environ, {"TELEGRAM_FINGERPRINTS_ENABLED": "true"}), \
             patch("scene_scrapers._telegram_oshash", return_value=("0123456789abcdef", 131072)), \
             patch("scene_scrapers._search_stashdb_scene_by_fingerprint", return_value=scene):
            self.assertEqual(backfill_telegram_fingerprints(
                self.conn, self.media_dir, limit=1, sleep_sec=0, video_ids=[vid_id],
            ), (1, 1))
        row = self.conn.execute(
            "SELECT title,scene_match_source,scene_match_external_id FROM videos WHERE id=%s", (vid_id,)
        ).fetchone()
        self.assertEqual(row[0], "Catalog Scene Title")
        self.assertEqual(row[1], "stashdb:phash")
        self.assertEqual(row[2], "stash-uuid-1")
        # Jellyfin imports the locked NFO, so the catalog title must be there.
        updated_nfo = nfo_path.read_text(encoding="utf-8")
        self.assertIn("<title>Catalog Scene Title</title>", updated_nfo)
        self.assertNotIn("<title>Provider Caption #42</title>", updated_nfo)

    def test_telegram_fingerprint_backfill_covers_all_channels(self):
        video_ids = [
            "telegram:-1001234567890:420:999",
            "telegram:-1001234567891:421:998",
        ]
        for index, video_id in enumerate(video_ids):
            self.conn.execute(
                """INSERT INTO videos (id,provider,title,duration,performers,first_seen,last_seen)
                   VALUES (%s,'telegram',%s,600,'[]',1000,1000)""",
                (video_id, f"Untitled upload {index}"),
            )
            self.conn.execute(
                "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) VALUES (%s,'OSHASH',%s)",
                (video_id, f"{index + 1:016x}"),
            )
        self.conn.commit()

        with patch.dict(os.environ, {"TELEGRAM_FINGERPRINTS_ENABLED": "true", "TELEGRAM_PHASH_ENABLED": "false"}), \
             patch("scene_scrapers.search_scene_by_oshash", return_value=None):
            self.assertEqual(backfill_telegram_fingerprints(
                self.conn, self.media_dir, limit=10, sleep_sec=0,
            ), (2, 0))

        attempts = self.conn.execute(
            """SELECT entity_key,source,outcome FROM external_metadata_attempts
               WHERE entity_key = ANY(%s) ORDER BY entity_key""",
            (video_ids,),
        ).fetchall()
        self.assertEqual(attempts, [
            (video_ids[0], "telegram_fingerprint", "no_match"),
            (video_ids[1], "telegram_fingerprint", "no_match"),
        ])


    def test_uses_only_exact_head_and_tail_ranges(self):
        class Response:
            def __init__(self, start, end, total):
                self.status_code = 206
                self.headers = {"content-range": f"bytes {start}-{end}/{total}"}
                self.data = bytes([1]) * (end - start + 1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_bytes(self):
                yield self.data

        class Client:
            def __init__(self):
                self.ranges = []

            def stream(self, method, url, *, headers, timeout):
                self.assertions = (method, url, timeout)
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                self.ranges.append((start, end))
                return Response(start, end, 300_000)

        client = Client()
        fingerprint, bytes_read = _telegram_oshash(
            "telegram:-1001234567890:42:999", http_client=client,
        )
        self.assertEqual(client.ranges, [(0, 65535), (234464, 299999)])
        self.assertEqual(bytes_read, 131072)
        expected = (300_000 + int.from_bytes(bytes([1]) * 8, "little") * (65536 // 8) * 2) & 0xFFFFFFFFFFFFFFFF
        self.assertEqual(fingerprint, f"{expected:016x}")

    def test_no_match_advances_the_bounded_daily_queue(self):
        for suffix, title, seen in (("1", "No listing for Alice", 1000),
                                    ("2", "No listing for Bob", 2000),
                                    ("3", "No listing for Carol", 3000)):
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,studio,performers,first_seen,last_seen,source_text) "
                "VALUES (%s,'telegram',%s,'Unknown Studio','[]',%s,%s,%s)",
                (f"telegram:-1001:{suffix}:unique", title, seen, seen, f"Title: {title}"))
        self.conn.commit()
        titles = []

        def no_match(title, **_kwargs):
            titles.append(title)
            return None

        with patch("scene_scraper_text_sources.search_scene_by_exact_title", side_effect=no_match), \
             patch("scene_scrapers.backfill_telegram_fingerprints", return_value=(0, 0)), \
             patch("scene_scrapers.backfill_missing_telegram_oshashes", return_value=(0, 0)):
            self.assertEqual(backfill_scenes(self.conn, self.media_dir, limit=1, sleep_sec=0), (1, 0))
            self.assertEqual(backfill_scenes(self.conn, self.media_dir, limit=1, sleep_sec=0), (1, 0))
        self.assertEqual(titles, ["No listing for Alice", "No listing for Bob"])
        states = self.conn.execute(
            "SELECT entity_key,outcome FROM external_metadata_attempts "
            "WHERE entity_type='video' AND source='telegram_caption_title_exact_v1' ORDER BY entity_key"
        ).fetchall()
        self.assertEqual(len(states), 2)
        self.assertTrue(all(row[1] == "no_match" for row in states))


if __name__ == "__main__":
    unittest.main()
