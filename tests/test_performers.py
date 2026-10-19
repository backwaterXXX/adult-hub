"""Performer extraction (listing cards + video pages) and NFO/DB passthrough."""
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase, TEST_URL, reset_db

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


try:
    from providers.base import (
        normalize_performers,
        parse_performer_links,
        parse_ldjson_actor_names,
        parse_title_performers,
        is_valid_performer_name,
        clean_display_title,
        is_performer_segment,
        register_slug_token_hook,
        get_slug_token_hook,
    )
    from common.performers import is_safe_alias, strip_nationality_prefix, strip_scene_suffix
    PROVIDERS_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    PROVIDERS_AVAILABLE = False

try:
    import sync as sync_module
    SYNC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    SYNC_AVAILABLE = False


EXAMPLE_VIDEO_PAGE = """
<script type="application/ld+json">{"@context":"https://schema.org","@type":"VideoObject","name":"Some video",
"actor":[{"@type":"Person","name":"Example Performer","url":"https://example.com/performer/example-performer"},
{"@type":"Person","name":"Second Performer","url":"https://example.com/performer/second-performer"}],
"publisher":{"@type":"Organization","name":"Example"}}}</script>
<div data-video-performers class="grid"><a href="https://example.com/performer/example-performer"><span class="truncate">Example Performer</span></a></div>
"""


class TestPerformerHelpers(unittest.TestCase):
    def setUp(self):
        if not PROVIDERS_AVAILABLE:
            self.skipTest("providers module not available")

    def test_normalize_dedupes_and_preserves_order(self):
        self.assertEqual(
            normalize_performers(["B", "A", "B", "  ", "", None, "A ", "C"]),
            ["B", "A", "C"],
        )
        self.assertEqual(normalize_performers(None), [])
        self.assertEqual(normalize_performers([]), [])

    def test_normalize_unescapes_entities(self):
        self.assertEqual(normalize_performers(["Tom &amp; Jerry"]), ["Tom & Jerry"])

    def test_parse_performer_links(self):
        html = ('<a href="/performer/fallandarkofc">FallandarkOFC</a>'
                '<a href="https://example.com/performer/ace-carter" rel="tag">Ace Carter</a>'
                '<a href="/162504/category/solo">Solo</a>')
        self.assertEqual(parse_performer_links(html), ["FallandarkOFC", "Ace Carter"])
        self.assertEqual(parse_performer_links(""), [])
        self.assertEqual(parse_performer_links(None), [])

    def test_normalize_strips_parenthesized_handles(self):
        self.assertEqual(
            normalize_performers(["Adam Snow (AdamSnowXXX)", "Joey Mills (JoeyMillsXXX)"]),
            ["Adam Snow", "Joey Mills"],
        )
        self.assertEqual(
            normalize_performers(["Andy Catlin(Catlintheanimal)"]),
            ["Andy Catlin"],
        )

    def test_normalize_with_alias_map(self):
        alias_map = {"johnny b": "John Black", "jb": "John Black"}
        self.assertEqual(
            normalize_performers(["Johnny B", "Other Guy"], alias_map=alias_map),
            ["John Black", "Other Guy"],
        )
        # Deduplication when alias maps to an already present canonical name
        self.assertEqual(
            normalize_performers(["Johnny B", "John Black", "JB"], alias_map=alias_map),
            ["John Black"],
        )

    def test_strip_scene_suffix(self):
        self.assertEqual(strip_scene_suffix("Adam s1"), "Adam")
        self.assertEqual(strip_scene_suffix("Adam s2"), "Adam")
        self.assertEqual(strip_scene_suffix("Chris Blades s4"), "Chris Blades")
        self.assertEqual(strip_scene_suffix("Chris Blades s4 pt1"), "Chris Blades")
        self.assertEqual(strip_scene_suffix("Czech Jim Durden s4"), "Czech Jim Durden")
        self.assertEqual(strip_scene_suffix("Performer - s2"), "Performer")
        self.assertEqual(strip_scene_suffix("Performer.s01"), "Performer")
        self.assertEqual(strip_scene_suffix("Performer Scene 1"), "Performer")
        self.assertEqual(strip_scene_suffix("Performer Part 2"), "Performer")
        self.assertEqual(strip_scene_suffix("Performer Ep 3"), "Performer")
        self.assertEqual(strip_scene_suffix("James 2"), "James 2")
        self.assertEqual(strip_scene_suffix("Boy9"), "Boy9")
        self.assertEqual(strip_scene_suffix("Alexxxx95"), "Alexxxx95")

    def test_normalize_strips_scene_suffixes(self):
        self.assertEqual(
            normalize_performers(["Chris Blades s4", "Dante Martin"]),
            ["Chris Blades", "Dante Martin"],
        )
        self.assertEqual(
            normalize_performers(["Adam s1", "Adam s2"]),
            ["Adam"],
        )
        self.assertEqual(
            normalize_performers(["Adam", "Adam s1", "Adam s2"]),
            ["Adam"],
        )
        self.assertEqual(
            normalize_performers(["s1", "s2", "sc1", "scene 1", "part 2"]),
            [],
        )

    def test_strip_nationality_prefix(self):
        self.assertEqual(strip_nationality_prefix("Peruvians JeffreyDair"), "JeffreyDair")
        self.assertEqual(strip_nationality_prefix("Peruvian Jeffreydair"), "Jeffreydair")
        self.assertEqual(strip_nationality_prefix("Vietnames hunk Kenvin"), "Kenvin")
        self.assertEqual(strip_nationality_prefix("Vietnamese hunk Kenvin"), "Kenvin")
        self.assertEqual(strip_nationality_prefix("Asian Hunk Alex Chou"), "Alex Chou")
        self.assertEqual(strip_nationality_prefix("Canadian Latino Kenzo Alvarez"), "Kenzo Alvarez")
        self.assertEqual(strip_nationality_prefix("Slovak-Hungarian Kyle Brady"), "Kyle Brady")
        self.assertEqual(strip_nationality_prefix("Englsih Paddy O'Brian"), "Paddy O'Brian")
        self.assertEqual(strip_nationality_prefix("Czech Bruce Harrelson"), "Bruce Harrelson")
        self.assertEqual(strip_nationality_prefix("Colombian Bastian Karim"), "Bastian Karim")
        # Legitimate first name 'Dane' is preserved
        self.assertEqual(strip_nationality_prefix("Dane Cross"), "Dane Cross")

    def test_normalize_strips_nationality_prefixes(self):
        self.assertEqual(
            normalize_performers(["Peruvians JeffreyDair", "Peruvian Jeffreydair"]),
            ["JeffreyDair"],
        )
        self.assertEqual(
            normalize_performers(["Vietnames hunk Kenvin", "Vietnamese hunk Kenvin"]),
            ["Kenvin"],
        )
        self.assertEqual(
            normalize_performers(["Asian Hunk Alex Chou", "Alex Chou"]),
            ["Alex Chou"],
        )
        self.assertEqual(
            normalize_performers(["Korean Twunk", "Mexican Republic", "Portugues twink FHD", "Asian Gay Sex"]),
            [],
        )

    def test_parse_ldjson_actor_names(self):
        self.assertEqual(
            parse_ldjson_actor_names(EXAMPLE_VIDEO_PAGE),
            ["Example Performer", "Second Performer"],
        )
        self.assertEqual(parse_ldjson_actor_names("<html>no actors</html>"), [])

    def test_parse_title_performers_multi_name_asset(self):
        title = "Alex Ink, Alexandro Cabrera & Pietro – A Hot Surprise When He Gets Home"
        self.assertEqual(
            parse_title_performers(title),
            ["Alex Ink", "Alexandro Cabrera", "Pietro"],
        )

    def test_parse_title_performers_patterns(self):
        # Cast before dash
        self.assertEqual(
            parse_title_performers("Martin Hlozek & Yuri Kacena – Full Contact"),
            ["Martin Hlozek", "Yuri Kacena"],
        )
        # Cast after dash
        self.assertEqual(
            parse_title_performers("Wank Party #203 Part 1 – David Six, Radek Pozer & Yuri Kacena"),
            ["David Six", "Radek Pozer", "Yuri Kacena"],
        )
        # Series with colon
        self.assertEqual(
            parse_title_performers("ASG Auditions: Duke Daly, Tommy Parker & Leo Fox"),
            ["Duke Daly", "Tommy Parker", "Leo Fox"],
        )
        # Fucks action pattern
        self.assertEqual(
            parse_title_performers("Alexandro Cabrera fucks Leo Redwood"),
            ["Alexandro Cabrera", "Leo Redwood"],
        )
        # Single performer with known dictionary
        self.assertEqual(
            parse_title_performers("Repoed Car Leads To Fuck Sesh – Johnny Wood", known_performers={"johnny wood"}),
            ["Johnny Wood"],
        )
        # Particle in performer name ("The")
        self.assertEqual(
            parse_title_performers("Andrew Akito, Beau Butler, Wolfie Wildes & Jimmy The Inked Pig – Fresh Meat Day"),
            ["Andrew Akito", "Beau Butler", "Wolfie Wildes", "Jimmy The Inked Pig"],
        )
        # Parenthesized handles and action verbs
        self.assertEqual(
            parse_title_performers("Louis Ricaute (louis_ricaute) fucks Matthew Ellis (atasteofmatt)"),
            ["Louis Ricaute", "Matthew Ellis"],
        )
        self.assertEqual(
            parse_title_performers("James Cassidy (jamestoplease) and Bryce Jax (brycejaxxx) fuck Lachie Neil (urboylachie)"),
            ["James Cassidy", "Bryce Jax", "Lachie Neil"],
        )
        self.assertEqual(
            parse_title_performers("Panterino Bottoms For Kosta Viking – Stuffing The Butt"),
            ["Panterino", "Kosta Viking"],
        )
        # Dictionary matching in descriptive title
        self.assertEqual(
            parse_title_performers("Tiny Austin Young Barebacked By Daddies", known_performers={"Austin Young"}),
            ["Austin Young"],
        )
        # Trailing scene suffixes (s1, s2, s3, etc.) stripped from titles
        self.assertEqual(
            parse_title_performers("Next Door Raw - Grooming Roommates - Dante Martin, Chris Blades s4"),
            ["Dante Martin", "Chris Blades"],
        )
        self.assertEqual(
            parse_title_performers("Next Door Raw - Fresh Dick - Gabriel Cross & Chris Blades s2"),
            ["Gabriel Cross", "Chris Blades"],
        )
        self.assertEqual(
            parse_title_performers("Next Door Studios - Eddie Patrick And Andrew Delta s2"),
            ["Eddie Patrick", "Andrew Delta"],
        )
        # Nationality prefixes and descriptors stripped from titles
        self.assertEqual(
            parse_title_performers("Slovak Kayden Gray bb fucks Asian Luke Truong"),
            ["Kayden Gray", "Luke Truong"],
        )
        self.assertEqual(
            parse_title_performers(
                "Peruvian Jeffreydair & Vietnames hunk Kenvin – Double Fun",
                known_performers={"Peruvian Jeffreydair", "Vietnamese hunk Kenvin"},
            ),
            ["Jeffreydair", "Kenvin"],
        )
        self.assertEqual(
            parse_title_performers("Peruvian Jeffrey Dair & Vietnames hunk Kenvin – Double Fun"),
            ["Jeffrey Dair", "Kenvin"],
        )
        self.assertEqual(
            parse_title_performers("Canadian Latino Kenzo Alvarez fucks Michael Vente"),
            ["Kenzo Alvarez", "Michael Vente"],
        )

        # Space-separated CamelCase performers list
        self.assertEqual(
            parse_title_performers("OF JayAlexander BryceEvans OliverHunt 720p"),
            ["Jay Alexander", "Bryce Evans", "Oliver Hunt"],
        )

    def test_parse_title_performers_rejects_non_performer_titles(self):
        self.assertEqual(parse_title_performers("Dragon – Camping Cum Buddies s2"), [])
        self.assertEqual(parse_title_performers("Stud Loads Vol. 8 – Stable Master"), [])
        self.assertEqual(parse_title_performers("File 52 – Our Little Secret 10:30"), [])
        self.assertEqual(parse_title_performers("Torso Sucking"), [])
        self.assertEqual(parse_title_performers(""), [])
        self.assertEqual(parse_title_performers(None), [])
        self.assertEqual(parse_title_performers("MenAtPlay - Campaign Erection, Editor's Cut.mp4"), [])
        self.assertEqual(parse_title_performers("MenAtPlay -Pure Suit - Revival, Editor's Cut.mp4"), [])
        self.assertEqual(parse_title_performers("MenAtPlay -Flip, Editor's Cut.mp4"), [])
        self.assertEqual(parse_title_performers("MenAtPlay -2 Wet, Editor's Cut.mp4"), [])

    def test_action_verbs_fucked(self):
        self.assertEqual(
            parse_title_performers("Jack fucked Florian"),
            ["Jack", "Florian"],
        )

    def test_is_valid_performer_name_rejects_noise_and_stopwords(self):
        for invalid in (
            "Editor's Cut.mp4",
            "Editor‘s Cut.mp4",
            "Editor’s Cut.mp4",
            "Editor's Cut",
            "Cut.mp4",
            "fucked",
            "Fucked",
            "fuck",
            "fucks",
            "fucking",
            "fucker",
            "Director's Cut",
            "Extended Cut.mkv",
            "Bareback.mp4",
            "RS",
            "rs",
            "XL",
            "xl",
            "XXL",
            "Dad",
            "Hard",
            "Wet",
            "Out",
            "Mix",
            "Pup",
            "Bred",
            "DJ",
            "JC",
            "JD",
            "TJ",
            "OK",
            "Chris Blades s4",
            "Adam s1",
            "Boris s2",
            "s1",
            "sc1",
            "scene 1",
            "part 2",
            "Dragon",
            "Dragon Media",
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(is_valid_performer_name(invalid))

        for valid in (
            "Alex Ink",
            "Johnny Wood",
            "Artyóm Ivanov",
            "Avi Jacobs",
            "Dragon Rojo",
            "23CM",
            "XXX",
            "FallandarkOFC",
            "Gio Akros",
            "Alonzo Jordon",
            "Lance Bennett",
            "Leon Fox",
            "Leon Reddz",
        ):
            with self.subTest(valid=valid):
                self.assertTrue(is_valid_performer_name(valid))

    def test_is_safe_alias(self):
        # Unsafe aliases: generic single words or first names
        self.assertFalse(is_safe_alias("leon", "Paddy O'Brian"))
        self.assertFalse(is_safe_alias("chris", "Darius"))
        self.assertFalse(is_safe_alias("dave", "Bareback Cocks"))
        self.assertFalse(is_safe_alias("matt", "Beaux Matthews"))
        self.assertFalse(is_safe_alias("lance", "Lance Bennett"))
        self.assertFalse(is_safe_alias("rs", "Raging Stallion"))
        self.assertFalse(is_safe_alias("xl", "Men of Montreal"))
        self.assertFalse(is_safe_alias("adam sky", "Adam s2"))

        # Safe aliases: multi-word or distinctive handles or concatenated full names
        self.assertTrue(is_safe_alias("bostonjock", "Boston Jock"))
        self.assertTrue(is_safe_alias("lancebennett", "Lance Bennett"))
        self.assertTrue(is_safe_alias("leonfox", "Leon Fox"))
        self.assertTrue(is_safe_alias("paddy_obrian", "Paddy O'Brian"))
        self.assertTrue(is_safe_alias("dillon diaz vip", "Dillon Diaz"))
        self.assertTrue(is_safe_alias("afroblackxxx", "Afro Black"))

    def test_title_parsing_with_studio_tags_and_complex_actions(self):
        # Test case: [Only Fans] prefix and whole-string performer list with &
        p1 = parse_title_performers("[Only Fans] Rhyheim Shabazz, Cade Maddox & Alonzo Jordon")
        self.assertEqual(p1, ["Rhyheim Shabazz", "Cade Maddox", "Alonzo Jordon"])

        # Test case: [Just For Fans] - prefix with compound action verbs (fuck & DP)
        p2 = parse_title_performers("[Just For Fans] - Rhyheim Shabazz, Krave, Trapp, Fame & Leon Reddz fuck & DP Adrian Hart")
        self.assertEqual(p2, ["Rhyheim Shabazz", "Krave", "Trapp", "Fame", "Leon Reddz", "Adrian Hart"])

        # Test case: Two performers joined by & without dashes
        p3 = parse_title_performers("Lance Bennett & Leon Fox")
        self.assertEqual(p3, ["Lance Bennett", "Leon Fox"])

        # Test case: Title prefix before comma-separated performers without dash
        p4 = parse_title_performers("men The Straight Man Part 4 Connor Halstead, Eli Hunter, Jared Summers")
        self.assertEqual(p4, ["Connor Halstead", "Eli Hunter", "Jared Summers"])

        # Test case: Trailing parenthesized performer lists
        p5 = parse_title_performers("[MenAtPlay] Stripper (Jason Vario, Skyy Knox) 1080p")
        self.assertEqual(p5, ["Jason Vario", "Skyy Knox"])

        p6 = parse_title_performers("3-Way Shoot [1080p] - Helix Studios - 2015 (Troy Ryan, Evan Parker, Brad Chase).avi")
        self.assertEqual(p6, ["Troy Ryan", "Evan Parker", "Brad Chase"])

        p7 = parse_title_performers("[E9] The Breakdown (Ram Soto & Turk Mason).mp4")
        self.assertEqual(p7, ["Ram Soto", "Turk Mason"])

        p8 = parse_title_performers("#ANAL The Retreat (Steve Cruz & Heath Halo, 2024)")
        self.assertEqual(p8, ["Steve Cruz", "Heath Halo"])

        # Test case: Scene prefixes
        p9 = parse_title_performers("SC1-Ethan & Tommy.mp4", known_performers=["Ethan", "Tommy"])
        self.assertEqual(p9, ["Ethan", "Tommy"])

        p10 = parse_title_performers("MO30  Eddie Patrick, Brody Meyer - It s Been Too Long HD.mp4")
        self.assertEqual(p10, ["Eddie Patrick", "Brody Meyer"])

        # Test case: Dash parts with action verbs and Corbin Fisher codes
        p11 = parse_title_performers("Corbin Fisher – ACM3130 (ACM0804, ACM9026) – Dawson Fucks Aaron Remastered")
        self.assertEqual(p11, ["Dawson", "Aaron"])

        # Test case: Inline handle in parentheses
        p12 = parse_title_performers("Big Boy Joey (Michael Joseph Massetti) and Brock Boulder")
        self.assertEqual(p12, ["Big Boy Joey", "Brock Boulder"])

        # Test case: Studio titles without performers should return empty list
        self.assertEqual(parse_title_performers("[Corbin Fisher] Down On The Farm"), [])
        self.assertEqual(parse_title_performers("[Sean Cody] Winter Getaway"), [])
        self.assertEqual(parse_title_performers("Lucas Entertainment"), [])
        self.assertEqual(parse_title_performers("Naked Sword"), [])

    def test_shared_title_parser_preserves_resolver_and_sync_mapping_policies(self):
        # Resolver treats a mapping as an iterable of names; sync accepts its
        # lowercase-name to canonical-display-name map from the backfill path.
        title = "A scene with Johnny Wood tonight"
        names = {"johnny wood": "Canonical Johnny"}
        self.assertEqual(parse_title_performers(title, known_performers=names), ["johnny wood"])
        self.assertEqual(sync_module.parse_title_performers(title, known_performers=names), ["Canonical Johnny"])

    def test_is_performer_segment(self):
        perfs = ["Alex Ink", "Alexandro Cabrera", "Pietro"]
        self.assertTrue(is_performer_segment("Alex Ink, Alexandro Cabrera & Pietro", perfs))
        self.assertTrue(is_performer_segment("Alex Ink and Pietro", perfs))
        self.assertFalse(is_performer_segment("Alex Ink & John Doe", perfs))
        self.assertFalse(is_performer_segment("", perfs))

    def test_shared_performer_helpers_keep_sync_aliases_in_step(self):
        from common import performers as shared

        samples = ["Adam Snow (AdamSnowXXX)", "Colombiano Flaquito Vergon", "Flaquito Vergon", "A & B"]
        self.assertIs(sync_module.normalize_performers, shared.normalize_performers)
        self.assertEqual(normalize_performers(samples), ["Adam Snow", "Flaquito Vergon", "A & B"])
        self.assertEqual(sync_module.normalize_performers(samples), normalize_performers(samples))
        for name in ("Alex Ink", "fucks", "123", "A"):
            with self.subTest(name=name):
                self.assertEqual(sync_module.is_valid_performer_name(name), is_valid_performer_name(name))
        self.assertEqual(
            sync_module.is_performer_segment("Alex Ink and Pietro", ["Alex Ink", "Pietro"]),
            is_performer_segment("Alex Ink and Pietro", ["Alex Ink", "Pietro"]),
        )

    def test_clean_display_title_scenarios(self):
        perfs = ["Alex Ink", "Alexandro Cabrera", "Pietro"]
        # Performer prefix stripped
        self.assertEqual(
            clean_display_title("Alex Ink, Alexandro Cabrera & Pietro – A Hot Surprise When He Gets Home", perfs),
            "A Hot Surprise When He Gets Home",
        )
        # Performer suffix stripped
        self.assertEqual(
            clean_display_title("Wank Party #203 Part 1 – David Six, Radek Pozer & Yuri Kacena",
                                ["David Six", "Radek Pozer", "Yuri Kacena"]),
            "Wank Party #203 Part 1",
        )
        # Colon separator with series prefix
        self.assertEqual(
            clean_display_title("ASG Auditions: Duke Daly, Tommy Parker & Leo Fox",
                                ["Duke Daly", "Tommy Parker", "Leo Fox"]),
            "ASG Auditions",
        )
        # Performer-only title preserved
        self.assertEqual(
            clean_display_title("Martin Hlozek & Yuri Kacena", ["Martin Hlozek", "Yuri Kacena"]),
            "Martin Hlozek & Yuri Kacena",
        )
        # Action phrase preserved
        self.assertEqual(
            clean_display_title("Alexandro Cabrera fucks Leo Redwood", ["Alexandro Cabrera", "Leo Redwood"]),
            "Alexandro Cabrera fucks Leo Redwood",
        )
        # Handles in parentheses stripped
        self.assertEqual(
            clean_display_title("Louis Ricaute (louis_ricaute) fucks Matthew Ellis (atasteofmatt)",
                                ["Louis Ricaute", "Matthew Ellis"]),
            "Louis Ricaute fucks Matthew Ellis",
        )
        # Slug-token rules are web-specific and live in the web extension
        # (neutral hook, default passthrough): without a hook the title is
        # kept as-is even for provider="secondweb".
        self.assertEqual(
            clean_display_title("Berkcan34 fucks victor vivone 3ckce",
                                ["Berk_Can34", "Victor Vivone"],
                                provider="secondweb", slug="berkcan34-fucks-victor-vivone-3ckce"),
            "Berkcan34 fucks victor vivone 3ckce",
        )
        self.assertEqual(
            clean_display_title("Aaron hugese fucks sam holister round 1 0guve",
                                ["Aaron Hugese", "Sam Holister"],
                                provider="secondweb", slug="aaron-hugese-fucks-sam-holister-round-1-0guve"),
            "Aaron hugese fucks sam holister round 1 0guve",
        )
        # Title without performers
        self.assertEqual(clean_display_title("Torso Sucking", []), "Torso Sucking")
        self.assertEqual(clean_display_title("", []), "")
        # Leading category hashtags stripped
        self.assertEqual(
            clean_display_title("#ANAL #MEN - Arno Antino, Enzo Muller",
                                ["Arno Antino", "Enzo Muller"]),
            "Arno Antino, Enzo Muller",
        )
        self.assertEqual(
            clean_display_title("🔥 #ANAL   #MEN - French Canadian Malik Delgaty",
                                ["Malik Delgaty"]),
            "French Canadian Malik Delgaty",
        )
        self.assertEqual(
            clean_display_title("#ANAL  PrideStudios - The Furry Ass s2", []),
            "PrideStudios - The Furry Ass s2",
        )
        # Leading aggregator and .mp4 extension stripped
        self.assertEqual(
            clean_display_title("Only Fans – Kev Inspiring, Mark Lax, Girth Brooks.mp4",
                                ["Kev Inspiring", "Mark Lax", "Girth Brooks"]),
            "Kev Inspiring, Mark Lax, Girth Brooks",
        )

    def test_slug_token_hook_mechanism(self):
        # ETAP C: provider-specific slug-token rules arrive via neutral hook.
        import providers.base as _base
        snapshot = dict(_base._SLUG_TOKEN_HOOKS)
        try:
            self.assertIsNone(get_slug_token_hook("dummyx"))
            register_slug_token_hook("dummyx", lambda text, slug: text + "!")
            self.assertEqual(
                clean_display_title("Some title", ["A B"], provider="dummyx", slug="s-t"),
                "Some title!",
            )
            # Other providers unaffected.
            self.assertEqual(
                clean_display_title("Some title", ["A B"], provider="telegram", slug="s-t"),
                "Some title",
            )
            with self.assertRaises(ValueError):
                register_slug_token_hook("", lambda t, s: t)
            with self.assertRaises(TypeError):
                register_slug_token_hook("dummyx", "not-callable")
        finally:
            _base._SLUG_TOKEN_HOOKS.clear()
            _base._SLUG_TOKEN_HOOKS.update(snapshot)


# NOTE (ETAP D): TestProviderPerformerParsing moved to the private
# suite (private-web-checkout/tests/test_provider_performers.py)
# with the five web provider parsers.


class TestPerformerSyncPassthrough(PgTestCase):
    def setUp(self):
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")
        reset_db(self.db_url)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.conn = sync_module.init_db(TEST_URL)
        self.addCleanup(self.conn.close)

    def test_nfo_contains_actor_entries(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "51003/slug",
            "title": "T & Co",
            "slug": "slug",
            "url": "https://exampleweb.example.com/video/51003/slug",
            "thumbnail": "",
            "duration": 600,
            "performers": ["FallandarkOFC", "Gio Akros", "Gio Akros", "  "],
        }
        sync_module.sync_unique_video(video, {"Solo & Masturbation"}, {"Secondweb"}, self.media, self.conn)
        nfo = next(self.media.glob("*.nfo")).read_text(encoding="utf-8")
        root = ET.fromstring(nfo)
        actors = [(a.findtext("name"), a.findtext("role")) for a in root.findall("actor")]
        self.assertEqual(
            actors,
            [("FallandarkOFC", "Performer"), ("Gio Akros", "Performer")],
        )

    def test_nfo_escapes_special_chars(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "abc",
            "title": "T",
            "slug": "s",
            "url": "",
            "thumbnail": "",
            "duration": 600,
            "performers": ["Tom & Jerry"],
        }
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        nfo = next(self.media.glob("*.nfo")).read_text(encoding="utf-8")
        self.assertIn("<name>Tom &amp; Jerry</name>", nfo)
        self.assertEqual(ET.fromstring(nfo).find("actor/name").text, "Tom & Jerry")

    def test_performers_persisted_and_updated_in_db(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "1/s",
            "title": "T",
            "slug": "s",
            "url": "",
            "thumbnail": "",
            "duration": 600,
            "performers": ["First Guy"],
        }
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        row = self.conn.execute(
            "SELECT performers FROM videos WHERE id = %s", ("exampleweb:1/s",)).fetchone()
        self.assertEqual(json.loads(row[0]), ["First Guy"])
        # Re-sync with new performers updates the row (no stale data).
        video["performers"] = ["First Guy", "Second Guy"]
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        row = self.conn.execute(
            "SELECT performers FROM videos WHERE id = %s", ("exampleweb:1/s",)).fetchone()
        self.assertEqual(json.loads(row[0]), ["First Guy", "Second Guy"])
        nfo = next(self.media.glob("*.nfo")).read_text(encoding="utf-8")
        self.assertIn("<name>Second Guy</name>", nfo)

    def test_collect_merges_performers_across_sightings(self):
        from unittest.mock import patch
        first = {
            "provider": "exampleweb", "provider_video_id": "1/s", "title": "T",
            "performers": ["First Guy"], "categories": [],
        }
        second = {
            "provider": "exampleweb", "provider_video_id": "1/s", "title": "T",
            "performers": ["Second Guy", "First Guy"], "categories": [],
        }
        collected = {"exampleweb:1/s": {"video": first, "genres": set(), "tags": set()}}
        with patch.object(sync_module, "fetch_provider_categories",
                           return_value=[{"id": "solo", "title": "Solo"}]), \
             patch.object(sync_module, "fetch_provider_category_videos",
                           return_value=[second]), \
             patch.object(sync_module.time, "sleep"):
            sync_module.collect_provider_categories(collected, "exampleweb", 1)
        self.assertEqual(
            collected["exampleweb:1/s"]["video"]["performers"],
            ["First Guy", "Second Guy"],
        )

    def test_backfill_performers_from_titles(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "69769",
            "title": "Alex Ink, Alexandro Cabrera & Pietro – A Hot Surprise When He Gets Home",
            "slug": "alex-ink",
            "url": "https://example.com/video/69769",
            "thumbnail": "",
            "duration": 1680,
            "performers": [],
        }
        sync_module.sync_unique_video(video, {"Anal"}, {"Exampleweb"}, self.media, self.conn)
        nfo_before = next(self.media.glob("*.nfo")).read_text(encoding="utf-8")
        self.assertNotIn("<actor>", nfo_before)

        updated = sync_module.backfill_performers_from_titles(self.conn, self.media)
        self.assertEqual(updated, 1)

        row = self.conn.execute("SELECT performers FROM videos WHERE id = 'exampleweb:69769'").fetchone()
        self.assertEqual(json.loads(row[0]), ["Alex Ink", "Alexandro Cabrera", "Pietro"])

        nfo_after = next(self.media.glob("*.nfo")).read_text(encoding="utf-8")
        self.assertIn("<actor>", nfo_after)
        self.assertIn("<name>Alex Ink</name>", nfo_after)
        self.assertIn("<name>Alexandro Cabrera</name>", nfo_after)
        self.assertIn("<name>Pietro</name>", nfo_after)
        self.assertIn("<set>", nfo_after)
        # Check display title vs original title in NFO
        self.assertIn("<title>A Hot Surprise When He Gets Home</title>", nfo_after)
        self.assertIn("<originaltitle>Alex Ink, Alexandro Cabrera &amp; Pietro – A Hot Surprise When He Gets Home</originaltitle>", nfo_after)

    def test_migrate_display_titles(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "12345",
            "title": "Cofran XXX & Sagi Trd – Show Your Girlfriend What She Is Missing: Take a Picture When I Suck Your Cock",
            "slug": "cofran-xxx",
            "url": "https://example.com/video/12345",
            "thumbnail": "",
            "duration": 1200,
            "performers": ["Cofran XXX", "Sagi Trd"],
        }
        sync_module.sync_unique_video(video, {"Anal"}, {"Exampleweb"}, self.media, self.conn)
        nfo_file = next(self.media.glob("*.nfo"))
        # Force uncleaned title into NFO to simulate legacy file
        old_content = nfo_file.read_text(encoding="utf-8")
        raw_nfo = old_content.replace(
            "<title>Show Your Girlfriend",
            "<title>Cofran XXX &amp; Sagi Trd – Show Your Girlfriend",
        )
        nfo_file.write_text(raw_nfo, encoding="utf-8")

        # Clear meta flag so migration executes
        self.conn.execute("DELETE FROM meta WHERE key = 'display_title_version'")
        self.conn.commit()

        # Run migration function
        updated = sync_module.migrate_display_titles(self.conn, self.media)
        self.assertEqual(updated, 1)

        migrated_content = nfo_file.read_text(encoding="utf-8")
        self.assertIn(
            "<title>Show Your Girlfriend What She Is Missing: Take a Picture When I Suck Your Cock</title>",
            migrated_content,
        )
        self.assertIn(
            "<originaltitle>Cofran XXX &amp; Sagi Trd – Show Your Girlfriend What She Is Missing: Take a Picture When I Suck Your Cock</originaltitle>",
            migrated_content,
        )

        # Second run should skip (idempotent, version stored in meta)
        self.assertEqual(sync_module.migrate_display_titles(self.conn, self.media), 0)



    def test_multi_part_title_prefers_full_names(self):
        from common.performer_titles import parse_title_performers_with_known_map
        known_map = {'claude': 'Claude', 'malik': 'Malik'}

        # Full names in last part should win over first names / summary in first part
        perfs = parse_title_performers_with_known_map(
            'Claude and Tim - 8906 - Claude Sorel, Tim Campbell.mp4', known_map
        )
        self.assertEqual(perfs, ['Claude Sorel', 'Tim Campbell'])

        # Multi-part with .part001.mp4 extension
        perfs = parse_title_performers_with_known_map(
            'Todd and Claude - 9742 - Claude Sorel, Todd Rosset.part001.mp4', known_map
        )
        self.assertEqual(perfs, ['Claude Sorel', 'Todd Rosset'])

        # Title words in first part vs full names in second part
        perfs = parse_title_performers_with_known_map(
            '[Bro Network] Recruit & Tackle - Marcus McNeill & Enzo Muller', known_map
        )
        self.assertEqual(perfs, ['Marcus McNeill', 'Enzo Muller'])

    def test_shared_title_parser_regressions(self):
        from common.performer_titles import (
            parse_title_performers_with_known_map,
            strip_performer_credits,
        )

        self.assertEqual(
            parse_title_performers_with_known_map('Alex Ink and John Doe - Morning Scene'),
            ['Alex Ink', 'John Doe'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map(
                'John Doe meets Christopher Reynolds',
                {'john doe': 'John Doe', 'christopher reynolds': 'Christopher Reynolds'},
            ),
            ['John Doe', 'Christopher Reynolds'],
        )
        overlapping_names = {'felipe eduardo': 'Felipe Eduardo', 'felipe': 'Felipe'}
        self.assertEqual(
            parse_title_performers_with_known_map(
                '#ANAL #OF - Venezolano Felipe Eduardo & texasgymjock',
                overlapping_names,
            ),
            ['Felipe Eduardo'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map(
                'Felipe Eduardo meets Felipe at the gym', overlapping_names
            ),
            ['Felipe Eduardo', 'Felipe'],
        )
        telegram_known = {
            **overlapping_names,
            'venezolano felipe eduardo': 'Venezolano Felipe Eduardo',
            'texasgymjock': 'Texas Gym Jock',
            'cj delco': 'CJ Delco',
        }
        self.assertEqual(
            parse_title_performers_with_known_map(
                '#ANAL #OF - Venezolano Felipe Eduardo & texasgymjock',
                telegram_known,
            ),
            ['Felipe Eduardo', 'Texas Gym Jock'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map(
                '#OF #ANAL Only Fans – CJ Delco And Felipe Eduardo',
                telegram_known,
            ),
            ['CJ Delco', 'Felipe Eduardo'],
        )
        known_name = {'first second': 'First Second'}
        self.assertEqual(
            parse_title_performers_with_known_map('FirstSecond and Third_Fourth - Scene'),
            ['First Second', 'Third Fourth'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map('FirstSecond fucks Third_Fourth'),
            ['First Second', 'Third Fourth'],
        )
        for title in (
            'Scene-FirstSecond',
            'Scene-First_Second',
            'Scene_First_Second',
            'Scene-First-Second',
            'Scene-First Second',
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    parse_title_performers_with_known_map(title, known_name),
                    ['First Second'],
                )
        self.assertEqual(
            parse_title_performers_with_known_map(
                'Scene-FirstSecondary', known_name
            ),
            [],
        )
        self.assertEqual(
            strip_performer_credits(
                'Alex Ink - Foo — Bar', ['Alex Ink'], is_performer_segment
            ),
            'Foo — Bar',
        )
        self.assertEqual(
            strip_performer_credits(
                'Alex Ink - John Doe - Scene',
                ['Alex Ink', 'John Doe'],
                is_performer_segment,
            ),
            'Scene',
        )

    def test_lucas_not_treated_as_single_word_performer(self):
        self.assertFalse(is_valid_performer_name("Lucas"))
        self.assertFalse(is_valid_performer_name("Falcon"))
        self.assertTrue(is_valid_performer_name("Michael Lucas"))
        self.assertTrue(is_valid_performer_name("Lucas Dias"))
        self.assertFalse(is_performer_segment("[Lucas Raunch]", ["Lucas"]))
        self.assertFalse(is_performer_segment("[Lucas Entertainment]", ["Lucas"]))

    def test_black_and_jordan_not_treated_as_single_word_performers(self):
        self.assertFalse(is_valid_performer_name("Black"))
        self.assertFalse(is_valid_performer_name("black"))
        self.assertFalse(is_valid_performer_name("Jordan"))
        self.assertFalse(is_valid_performer_name("jordan"))
        self.assertTrue(is_valid_performer_name("Morgan Black"))
        self.assertTrue(is_valid_performer_name("Heath Jordan"))
        self.assertTrue(is_valid_performer_name("Jordan Starr"))
        self.assertTrue(is_valid_performer_name("Jordan Neo"))
        self.assertTrue(is_valid_performer_name("Damian Black"))
        self.assertTrue(is_valid_performer_name("Conner Habib"))

    def test_nationality_prefixes_stripped_from_performers(self):
        from common.performer_titles import parse_title_performers_with_known_map

        self.assertFalse(is_valid_performer_name("Brasileiro"))
        self.assertFalse(is_valid_performer_name("Brasileiros"))
        self.assertFalse(is_valid_performer_name("Argentino"))
        self.assertFalse(is_valid_performer_name("Colombiano"))
        self.assertTrue(is_valid_performer_name("That Switch"))

        self.assertEqual(
            normalize_performers(["Brasileiro That Switch"]),
            ["That Switch"],
        )
        self.assertEqual(
            normalize_performers(["Brasileiros That Switch"]),
            ["That Switch"],
        )
        self.assertEqual(
            normalize_performers(["Colombiano Flaquito Vergon"]),
            ["Flaquito Vergon"],
        )

        title1 = "💕  #OF  #GROUP  OnlyFans - Argentinos ViktorOnee, Emiliano Terra, Federico Martin, & NakedYogi, Brasileiro That Switch"
        self.assertEqual(
            parse_title_performers_with_known_map(title1, {"viktoronee": "Viktor Onee"}),
            ["Viktor Onee", "Emiliano Terra", "Federico Martin", "Naked Yogi", "That Switch"],
        )

        title2 = "🔥  #GROUP  RFC  - Brasileiros That Switch and Jeff Carvalho, Argentino CraptusLion"
        self.assertEqual(
            parse_title_performers_with_known_map(title2),
            ["That Switch", "Jeff Carvalho", "Craptus Lion"],
        )

        title3 = "💕  #GROUP  #OF - Orgy in Brazil - Brasileiro Victor Vivone, Argentinos Felipe Hervera, Emiliano Terra & Julian Oxilia, V"
        self.assertEqual(
            parse_title_performers_with_known_map(title3),
            ["Victor Vivone", "Felipe Hervera", "Emiliano Terra", "Julian Oxilia"],
        )


if __name__ == "__main__":
    unittest.main()
