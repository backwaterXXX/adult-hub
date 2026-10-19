import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase
from test_support import load_json_fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nfo
from scene_scrapers import (
    ScrapedScene,
    _clean_query,
    _calculate_match_confidence,
    resolve_studio_engine,
    AlgoliaScraper,
    Project1Scraper,
    HelixScraper,
    BelAmiScraper,
    LucasScraper,
    search_scene,
    backfill_scenes,
    backfill_telegram_fingerprints,
    _telegram_oshash,
)


class TestSceneScrapersParsing(unittest.TestCase):
    def test_clean_query(self):
        raw = "[Falcon Studios] Summer Buddies [1080p] (Aiden Shaw) #gay #muscle - Part 1"
        cleaned = _clean_query(raw)
        self.assertEqual(cleaned, "Summer Buddies Part 1")

    def test_clean_query_empty(self):
        self.assertEqual(_clean_query(""), "")
        self.assertEqual(_clean_query(None), "")

    def test_resolve_studio_engine(self):
        self.assertEqual(
            resolve_studio_engine("Falcon Studios"),
            ("algolia", "falconstudios", "Falcon Studios"),
        )
        self.assertEqual(
            resolve_studio_engine("Raging Stallion Studios"),
            ("algolia", "falconstudios", "Falcon Studios"),
        )
        self.assertEqual(
            resolve_studio_engine("NextDoor Studios"),
            ("algolia", "nextdoorstudios", "NextDoor Studios"),
        )
        self.assertEqual(
            resolve_studio_engine("Sean Cody"),
            ("project1", "seancody", "Sean Cody"),
        )
        self.assertEqual(
            resolve_studio_engine("Helix Studios"),
            ("helix", "helixstudios", "Helix Studios"),
        )
        self.assertEqual(
            resolve_studio_engine("BelAmi"),
            ("belami", "belami", "BelAmi"),
        )
        self.assertEqual(
            resolve_studio_engine("Lucas Entertainment"),
            ("lucas", "lucas", "Lucas Entertainment"),
        )
        self.assertIsNone(resolve_studio_engine("Random Studio"))

    def test_clean_query_advanced(self):
        raw = "🔥 [Lucas Entertainment] Josh Rider Bottoms For Tomas Brand [1080p] HEVC @GayPornChannel 💦 - Full Video"
        cleaned = _clean_query(raw, studio="Lucas Entertainment")
        self.assertEqual(cleaned, "Josh Rider Bottoms For Tomas Brand")

    def test_confidence_exact_match(self):
        conf = _calculate_match_confidence(
            query_title="Summer Buddies",
            target_title="Summer Buddies",
            query_duration=780,
            target_duration=783,
            query_performers=["Aiden Shaw", "Alec Campbell"],
            target_performers=["Aiden Shaw", "Alec Campbell"],
        )
        self.assertAlmostEqual(conf, 1.0, places=2)

    def test_confidence_duration_mismatch_penalty(self):
        conf = _calculate_match_confidence(
            query_title="Summer Buddies",
            target_title="Summer Buddies",
            query_duration=300,
            target_duration=3600,
            query_performers=["Aiden Shaw"],
            target_performers=["Aiden Shaw"],
        )
        # Should be penalized for drastic duration mismatch
        self.assertLess(conf, 0.70)

    def test_algolia_to_scraped_scene(self):
        hit = load_json_fixture("catalog", "algolia_scene.json")
        scene = AlgoliaScraper.to_scraped_scene(hit, default_studio="Falcon Studios")
        self.assertEqual(scene.title, "Summer Buddies")
        self.assertEqual(scene.plot, "Aiden Shaw gives an impressive rim job.")
        self.assertEqual(scene.date, "2006-06-01")
        self.assertEqual(scene.duration, 783)
        self.assertEqual(scene.performers, ["Aiden Shaw", "Alec Campbell"])
        self.assertIn("Anal", scene.tags)
        self.assertIn("https://images03-fame.gammacdn.com/movies/5908/top.jpg", scene.poster_url)

    def test_project1_to_scraped_scene(self):
        hit = {
            "title": "Hunter",
            "description": "Hunter is a handsome guy.",
            "dateReleased": "2016-05-02T12:00:00+00:00",
            "duration": 761,
            "id": 12345,
            "actors": [{"name": "Hunter"}],
            "tags": [{"name": "Solo"}],
            "images": {
                "poster": {
                    "0": {
                        "xx": {"url": "https://media.test/xx.jpg"},
                        "xl": {"url": "https://media.test/xl.jpg"},
                    }
                }
            },
        }
        scene = Project1Scraper.to_scraped_scene(hit, default_studio="Sean Cody")
        self.assertEqual(scene.title, "Hunter")
        self.assertEqual(scene.plot, "Hunter is a handsome guy.")
        self.assertEqual(scene.date, "2016-05-02")
        self.assertEqual(scene.duration, 761)
        self.assertEqual(scene.performers, ["Hunter"])
        self.assertIn("Solo", scene.tags)
        self.assertEqual(scene.poster_url, "https://media.test/xx.jpg")

    def test_helix_to_scraped_scene(self):
        hit = {
            "url": "https://www.helixstudios.com/shop/3008702/blake-mitchell-porn-videos.html",
            "title": "Blake Mitchell in Prague",
            "thumb": "https://imgs4cdn.adultempire.com/movie/2747978?w=500",
        }
        mock_client = MagicMock()
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.text = """
            <html>
                <meta property="og:title" content="Blake Mitchell in Prague streaming at Helix Studios with free previews." />
                <meta property="og:image" content="https://imgs4cdn.adultempire.com/movie/2747978?w=500&h=700&fit=cover" />
                <div class="synopsis"><p>Blake arrives in Prague for adventure.</p></div>
                <a href="/shop/porn-videos/699020/blake-mitchell-pornstars.html">Blake Mitchell</a>
                <a href="/shop/porn-videos/708610/eluan-jeunet-pornstars.html">Eluan Jeunet</a>
                <a href="/shop/246/category/bareback-streaming-porn-videos.html">Bareback</a>
            </html>
        """
        mock_client.get.return_value = mock_res
        scene = HelixScraper.to_scraped_scene(hit, default_studio="Helix Studios", http_client=mock_client)
        self.assertEqual(scene.title, "Blake Mitchell in Prague")
        self.assertEqual(scene.plot, "Blake arrives in Prague for adventure.")
        self.assertEqual(scene.performers, ["Blake Mitchell", "Eluan Jeunet"])
        self.assertIn("Bareback", scene.tags)
        self.assertEqual(scene.poster_url, "https://imgs4cdn.adultempire.com/movie/2747978?w=1200")
        self.assertEqual(scene.studio, "Helix Studios")

    def test_belami_to_scraped_scene(self):
        hit = {
            "video_id": "17640",
            "url": "https://newtour.belamionline.com/playvideo.aspx?VideoID=17640",
        }
        mock_client = MagicMock()
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.text = """
            <html>
                <script type="application/ld+json">
                {
                    "@context": "https://schema.org",
                    "@type": "VideoObject",
                    "name": "Dominic Trailer @ BelAmi Free Tour",
                    "description": "Dominic Case is physically perfect.",
                    "thumbnailUrl": "https://freeassets.belamionline.com/Data/Contents/Content_17640/Thumbnail6.jpg",
                    "uploadDate": "2026-09-13",
                    "duration": "PT14M10S"
                }
                </script>
                <div class="name">Dominic Case</div>
            </html>
        """
        mock_client.get.return_value = mock_res
        scene = BelAmiScraper.to_scraped_scene(hit, default_studio="BelAmi", http_client=mock_client)
        self.assertEqual(scene.title, "Dominic")
        self.assertEqual(scene.plot, "Dominic Case is physically perfect.")
        self.assertEqual(scene.date, "2026-09-13")
        self.assertEqual(scene.duration, 850)
        self.assertEqual(scene.performers, ["Dominic Case"])
        self.assertEqual(scene.poster_url, "https://freeassets.belamionline.com/Data/Contents/Content_17640/Thumbnail6.jpg")
        self.assertEqual(scene.studio, "BelAmi")

    def test_lucas_to_scraped_scene(self):
        hit = {
            "url": "https://www.lucasentertainment.com/scenes/play/josh-rider-bottoms-tomas-brand",
            "title": "Josh Rider Bottoms For Tomas Brand",
            "plot": "Josh Rider gets the atmosphere heated.",
            "poster_url": "https://cdn-mj.lucasentertainment.com/content/LVP241-01.jpg",
            "date": "2024-05-10",
        }
        scene = LucasScraper.to_scraped_scene(hit, default_studio="Lucas Entertainment")
        self.assertEqual(scene.title, "Josh Rider Bottoms For Tomas Brand")
        self.assertEqual(scene.plot, "Josh Rider gets the atmosphere heated.")
        self.assertEqual(scene.performers, ["Josh Rider", "Tomas Brand"])
        self.assertEqual(scene.poster_url, "https://cdn-mj.lucasentertainment.com/content/LVP241-01.jpg")
        self.assertEqual(scene.studio, "Lucas Entertainment")


class TestSearchSceneRouting(unittest.TestCase):
    def test_tpdb_fallback_matches_catalog_scene(self):
        response = MagicMock()
        response.json.return_value = {"data": [{
            "id": "scene-123", "title": "A Night at the Studio", "description": "Official synopsis",
            "date": "2024-03-18", "duration": 1200,
            "poster_image": "https://cdn.test/poster.jpg", "url": "https://studio.test/scene",
            "site": {"name": "Example Studio"},
            "performers": [{"name": "Alex Star"}, {"name": "Jamie North"}],
        }]}
        client = MagicMock()
        client.get.return_value = response
        with patch.dict("os.environ", {"THEPORNDB_API_TOKEN": "test-token"}):
            result = search_scene(
                title="A Night at the Studio", studio="Example Studio", duration=1200,
                performers=["Alex Star"], http_client=client,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.external_id, "scene-123")
        self.assertEqual(result.plot, "Official synopsis")
        self.assertEqual(result.performers, ["Alex Star", "Jamie North"])
        self.assertEqual(result.poster_url, "https://cdn.test/poster.jpg")
        self.assertEqual(client.get.call_args.kwargs["params"]["q"], "A Night at the Studio")

    def test_tpdb_fallback_rejects_low_confidence_match(self):
        response = MagicMock()
        response.json.return_value = {"data": [{
            "id": "wrong", "title": "An Entirely Different Scene", "duration": 1200,
            "performers": [{"name": "Someone Else"}], "site": {"name": "Other Studio"},
        }]}
        client = MagicMock()
        client.get.return_value = response
        with patch.dict("os.environ", {"THEPORNDB_API_TOKEN": "test-token"}):
            result = search_scene(
                title="A Night at the Studio", studio="Unknown Studio", duration=1200,
                performers=["Alex Star"], http_client=client,
            )
        self.assertIsNone(result)

    def test_tpdb_fallback_is_skipped_without_api_token(self):
        client = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            result = search_scene("A Night at the Studio", studio="Unknown Studio", http_client=client)
        self.assertIsNone(result)
        client.get.assert_not_called()

    @patch.object(AlgoliaScraper, "search")
    def test_search_scene_algolia_match(self, mock_search):
        mock_search.return_value = [
            {
                "title": "Summer Buddies",
                "description": "Plot summary",
                "release_date": "2006-06-01",
                "length": 783,
                "clip_id": 21302,
                "studio_name": "Falcon Studios",
                "actors": [{"name": "Aiden Shaw"}, {"name": "Alec Campbell"}],
            }
        ]
        result = search_scene(
            title="Summer Buddies",
            studio="Falcon Studios",
            duration=780,
            performers=["Aiden Shaw"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Summer Buddies")
        self.assertGreaterEqual(result.confidence, 0.85)

    @patch.object(AlgoliaScraper, "search")
    def test_search_scene_below_threshold_returns_none(self, mock_search):
        mock_search.return_value = [
            {
                "title": "Completely Unrelated Film",
                "description": "Something else",
                "release_date": "2020-01-01",
                "length": 3600,
                "clip_id": 99999,
                "studio_name": "Falcon Studios",
                "actors": [{"name": "Someone Else"}],
            }
        ]
        result = search_scene(
            title="Summer Buddies",
            studio="Falcon Studios",
            duration=780,
            performers=["Aiden Shaw"],
        )
        self.assertIsNone(result)

    @patch.object(HelixScraper, "search")
    @patch.object(HelixScraper, "to_scraped_scene")
    def test_search_scene_helix_routing(self, mock_to_scraped, mock_search):
        mock_search.return_value = [{"title": "Blake Mitchell in Prague"}]
        mock_to_scraped.return_value = ScrapedScene(
            title="Blake Mitchell in Prague",
            plot="Description",
            performers=["Blake Mitchell"],
            studio="Helix Studios",
        )
        res = search_scene(title="Blake Mitchell in Prague", studio="Helix Studios")
        self.assertIsNotNone(res)
        self.assertEqual(res.title, "Blake Mitchell in Prague")

    @patch.object(BelAmiScraper, "search")
    @patch.object(BelAmiScraper, "to_scraped_scene")
    def test_search_scene_belami_routing(self, mock_to_scraped, mock_search):
        mock_search.return_value = [{"video_id": "17640"}]
        mock_to_scraped.return_value = ScrapedScene(
            title="Dominic",
            plot="BelAmi Dominic scene",
            performers=["Dominic Case"],
            studio="BelAmi",
        )
        res = search_scene(title="Dominic", studio="BelAmi")
        self.assertIsNotNone(res)
        self.assertEqual(res.title, "Dominic")

    @patch.object(LucasScraper, "search")
    @patch.object(LucasScraper, "to_scraped_scene")
    def test_search_scene_lucas_routing(self, mock_to_scraped, mock_search):
        mock_search.return_value = [{"url": "https://www.lucasentertainment.com/scenes/play/josh-rider"}]
        mock_to_scraped.return_value = ScrapedScene(
            title="Josh Rider Bottoms",
            plot="Lucas plot",
            performers=["Josh Rider"],
            studio="Lucas Entertainment",
        )
        res = search_scene(title="Josh Rider Bottoms", studio="Lucas Entertainment")
        self.assertIsNotNone(res)
        self.assertEqual(res.title, "Josh Rider Bottoms")


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
        self.assertEqual(json.loads(row[2]), ["Aiden Shaw"])
        self.assertGreater(row[3], 0)
        proposal = self.conn.execute(
            "SELECT changes FROM metadata_review_proposals WHERE video_id=%s AND status='pending'", (vid_id,),
        ).fetchone()
        self.assertEqual(json.loads(proposal[0])["performers"], ["Aiden Shaw", "Alec Campbell"])

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
