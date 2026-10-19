import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pg_helper import TEST_URL, init_test_db, require_url

# PostgreSQL-only: the resolver provider caches (load_known_performers /
# load_alias_map) read DATABASE_URL. Point it at the test database so
# auto-load paths in message_to_video never touch a dev/prod database.
if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL

from test_support import optional_import_failed

try:
    from providers import telegram as tg_module  # noqa: F401
    from providers.telegram import (
        channel_to_category,
        extract_file_ref,
        is_channel_authorized,
        load_known_performers,
        match_known_performers,
        message_to_video,
        parse_caption_title,
        passes_duration_filter,
        thumb_api_path,
        TelegramProvider,
    )
    TELEGRAM_AVAILABLE = True
    _IMPORT_ERROR = None
except ImportError as e:
    from test_support import optional_import_failed
    optional_import_failed(e)
    tg_module = None
    parse_caption_title = None
    extract_file_ref = None
    is_channel_authorized = None
    channel_to_category = None
    message_to_video = None
    load_known_performers = None
    match_known_performers = None
    passes_duration_filter = None
    thumb_api_path = None
    TelegramProvider = None
    TELEGRAM_AVAILABLE = False
    _IMPORT_ERROR = e

try:
    from providers.telegram import extract_hashtags
except ImportError as exc:
    optional_import_failed(exc)
    extract_hashtags = None

try:
    from providers.base import CategoryItem, VideoItem
    BASE_AVAILABLE = True
except ImportError as exc:
    optional_import_failed(exc)
    CategoryItem = None
    VideoItem = None
    BASE_AVAILABLE = False


def _require_telegram(testcase):
    if not TELEGRAM_AVAILABLE:
        testcase.skipTest(f"telegram provider not available: {_IMPORT_ERROR}")


def _video_message(channel_id=-1001234567890, message_id=42,
                   caption="First Title\nSecond line\nThird",
                   unique_id="AQADabc123unique"):
    """Ksztalt message_dict tolerancyjny wobec implementacji.

    Zawiera file_unique_id zarowno plasko jak i zagniezdzone pod
    kluczami video/document/file, zeby sensowna implementacja
    (czytajaca dowolna z tych sciezek) znalazla wlasciwy ref.
    """
    return {
        "channel_id": channel_id,
        "chat_id": channel_id,
        "message_id": message_id,
        "id": message_id,
        "caption": caption,
        "video": {"file_unique_id": unique_id},
        "document": {"file_unique_id": unique_id},
        "file": {"file_unique_id": unique_id},
        "file_unique_id": unique_id,
    }


class TestParseCaptionTitle(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if parse_caption_title is None:
            self.fail("required Telegram function missing: parse_caption_title")

    def test_first_line_of_caption(self):
        self.assertEqual(
            parse_caption_title("Hello World\nsecond line\nthird", "fb"),
            "Hello World")

    def test_first_line_stripped(self):
        self.assertEqual(
            parse_caption_title("  Spaced Title  \nrest", "fb"),
            "Spaced Title")

    def test_fallback_on_empty(self):
        self.assertEqual(parse_caption_title("", "fb"), "fb")
        self.assertEqual(parse_caption_title("   \n  \n", "fb"), "fb")

    def test_fallback_on_none(self):
        self.assertEqual(parse_caption_title(None, "fb"), "fb")

    def test_truncated_to_120_chars(self):
        long_title = "x" * 200
        result = parse_caption_title(long_title, "fb")
        self.assertLessEqual(len(result), 120)
        self.assertTrue(result)  # nie pusty po obcieciu

    def test_multiline_long_first_line_truncated(self):
        caption = "y" * 150 + "\nsecond"
        result = parse_caption_title(caption, "fb")
        self.assertLessEqual(len(result), 120)
        self.assertNotIn("second", result)

    def test_skips_hashtag_only_first_line(self):
        """A line consisting solely of hashtags should be skipped and leading hashtags stripped."""
        caption = "#ANAL #MEN\n#ANAL #MEN - Arno Antino, Enzo Muller"
        self.assertEqual(
            parse_caption_title(caption, "fb"),
            "Arno Antino, Enzo Muller")

    def test_strips_leading_hashtags_and_separators(self):
        caption = "🔥 #ANAL   #MEN - French Canadian Malik Delgaty"
        self.assertEqual(
            parse_caption_title(caption, "fb"),
            "French Canadian Malik Delgaty")

    def test_skips_multiple_hashtag_only_lines(self):
        caption = "#GROUP\n#ANAL\n#MEN\nArno Antino, Enzo Muller"
        self.assertEqual(
            parse_caption_title(caption, "fb"),
            "Arno Antino, Enzo Muller")

    def test_hashtag_only_caption_uses_first_hashtag_line(self):
        """When every line is hashtag-only, return the first one."""
        caption = "#onlyfans"
        self.assertEqual(parse_caption_title(caption, "fb"), "#onlyfans")

    def test_mixed_hashtag_then_content(self):
        caption = "#OF #ANAK\nOnlyFans - Craig Marks with Papi Kocic"
        self.assertEqual(
            parse_caption_title(caption, "fb"),
            "OnlyFans - Craig Marks with Papi Kocic")


class TestExtractFileRef(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if extract_file_ref is None:
            self.fail("required Telegram function missing: extract_file_ref")

    def test_video_message_ref_format(self):
        msg = _video_message()
        ref = extract_file_ref(msg)
        self.assertIsNotNone(ref)
        self.assertEqual(
            ref, f"{msg['channel_id']}:{msg['message_id']}:AQADabc123unique")
        self.assertRegex(ref, r"^.+:\d+:.+$")

    def test_ignores_text_only(self):
        msg = {"channel_id": -1001, "message_id": 7, "text": "hello world"}
        self.assertIsNone(extract_file_ref(msg))

    def test_ignores_photo_only(self):
        msg = {"channel_id": -1001, "message_id": 8,
               "photo": {"file_unique_id": "photo-only-123"},
               "caption": "just a pic"}
        self.assertIsNone(extract_file_ref(msg))

    def test_video_preferred_over_photo(self):
        msg = _video_message()
        msg["photo"] = {"file_unique_id": "photo-only-123"}
        ref = extract_file_ref(msg)
        self.assertIsNotNone(ref)
        self.assertIn("AQADabc123unique", ref)
        self.assertNotIn("photo-only-123", ref)

    def test_empty_dict_returns_none(self):
        self.assertIsNone(extract_file_ref({}))


class TestChannelToCategory(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if channel_to_category is None:
            self.fail("required Telegram function missing: channel_to_category")

    def _as_dict(self, cat):
        if cat is None:
            return {}
        if isinstance(cat, dict):
            return cat
        if isinstance(cat, (tuple, list)) and len(cat) >= 2:
            return {"id": cat[0], "title": cat[1]}
        return {"id": getattr(cat, "id", None),
                "title": getattr(cat, "title", None)}

    def test_maps_channel(self):
        cat = channel_to_category(-1001234567890, "Some Channel")
        d = self._as_dict(cat)
        self.assertIsNotNone(cat)
        self.assertIn("1001234567890", str(d.get("id")))
        self.assertTrue(d.get("title"))

    def test_title_preserved(self):
        cat = channel_to_category(-10099, "My Cool Channel")
        d = self._as_dict(cat)
        self.assertIn("My Cool Channel", str(d.get("title")))


class TestMessageToVideo(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")

    def test_builds_video_item(self):
        msg = _video_message()
        item = message_to_video(msg, "Some Channel")
        self.assertIsNotNone(item)
        if BASE_AVAILABLE:
            self.assertIsInstance(item, VideoItem)
        self.assertEqual(item.id, "telegram:AQADabc123unique")
        self.assertEqual(item.provider, "telegram")

    def test_url_points_to_tme(self):
        msg = _video_message()
        item = message_to_video(msg, "Some Channel")
        self.assertRegex(item.url, r"t\.me")
        self.assertIn("42", item.url)  # message_id w linku

    def test_title_from_caption_first_line(self):
        msg = _video_message(caption="Nice Clip\nmore text here")
        item = message_to_video(msg, "Some Channel")
        self.assertEqual(item.title, "Nice Clip")

    def test_title_fallback_without_caption(self):
        msg = _video_message(caption="")
        item = message_to_video(msg, "Some Channel")
        self.assertTrue(item.title)

    def test_text_only_returns_none_or_raises(self):
        msg = {"channel_id": -1001, "message_id": 7, "text": "hello"}
        try:
            result = message_to_video(msg, "Some Channel")
        except (ValueError, TypeError, AttributeError, KeyError):
            return  # dopuszczalne: brak materialu video
        self.assertIsNone(result)


class TestTelegramProvider(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if TelegramProvider is None:
            self.fail("required Telegram class missing: TelegramProvider")
        self._orig_channels = os.environ.get("TELEGRAM_CHANNELS")
        os.environ["TELEGRAM_CHANNELS"] = "-1001234567890,@testchannel"

    def tearDown(self):
        if self._orig_channels is not None:
            os.environ["TELEGRAM_CHANNELS"] = self._orig_channels
        else:
            os.environ.pop("TELEGRAM_CHANNELS", None)

    def test_name_is_telegram(self):
        self.assertEqual(TelegramProvider.name, "telegram")

    def test_channel_authorization_whitelist(self):
        provider = TelegramProvider()
        self.assertTrue(provider.is_channel_authorized("-1001234567890"))
        self.assertTrue(provider.is_channel_authorized("1234567890"))
        self.assertTrue(provider.is_channel_authorized("-1234567890"))
        self.assertTrue(provider.is_channel_authorized("@testchannel"))
        self.assertTrue(provider.is_channel_authorized("testchannel"))
        self.assertFalse(provider.is_channel_authorized("me"))
        self.assertFalse(provider.is_channel_authorized("self"))
        self.assertFalse(provider.is_channel_authorized("-1009999999999"))
        self.assertFalse(provider.is_channel_authorized("@unknownchannel"))
        self.assertFalse(provider.is_channel_authorized(""))
        self.assertFalse(provider.is_channel_authorized(None))

    def test_access_check_resolves_only_the_entity_with_existing_session(self):
        provider = TelegramProvider()
        client = Mock()
        entity = object()
        client.get_entity = AsyncMock(return_value=entity)
        client.get_messages = AsyncMock(return_value=[])
        with patch.object(tg_module, "_require_client", AsyncMock(return_value=client)), \
             patch.object(tg_module, "_entity_ref", return_value=-1001234567890):
            asyncio.run(provider.check_configured_channel_access("-1001234567890"))
        client.get_entity.assert_awaited_once_with(-1001234567890)
        client.get_messages.assert_awaited_once_with(entity, limit=1)
        client.iter_messages.assert_not_called()
        client.download_media.assert_not_called()

    def test_missing_session_check_does_not_create_a_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_session = Path(directory) / "not-logged-in.session"
            telethon_stub = types.ModuleType("telethon")
            telethon_stub.TelegramClient = Mock()
            with patch.object(tg_module, "_client", None), \
                 patch.object(tg_module, "_client_lock", None), \
                 patch.object(tg_module, "_get_session_path", return_value=str(missing_session)), \
                 patch.object(tg_module, "_get_api_id", return_value=123), \
                 patch.object(tg_module, "_get_api_hash", return_value="test-hash"), \
                 patch.dict(sys.modules, {"telethon": telethon_stub}):
                with self.assertRaisesRegex(RuntimeError, "session file is missing"):
                    asyncio.run(tg_module._require_client())
            self.assertFalse(missing_session.exists())
            telethon_stub.TelegramClient.assert_not_called()

    def test_resolve_stream_returns_tg_marker(self):
        provider = TelegramProvider()
        result = asyncio.run(provider.resolve_stream(
            "-1001234567890:42:AQADabc123unique"))
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("tg://"),
                        f"expected tg:// marker, got: {result!r}")
        self.assertRegex(result, r"^tg://.+")

    def test_resolve_stream_rejects_unauthorized_channel(self):
        provider = TelegramProvider()
        with self.assertRaises(ValueError):
            asyncio.run(provider.resolve_stream("me:42:unique"))
        with self.assertRaises(ValueError):
            asyncio.run(provider.resolve_stream("-1009999999999:42:unique"))

    def test_file_size_reads_direct_document_media(self):
        provider = TelegramProvider()
        message = types.SimpleNamespace(
            id=42,
            message="",
            media=types.SimpleNamespace(id=777, size=987654),
        )
        with patch.object(tg_module, "_require_client", AsyncMock(return_value=object())), \
             patch.object(tg_module, "_get_cached_message", AsyncMock(return_value=message)):
            size = asyncio.run(provider.file_size("-1001234567890:42:777"))
        self.assertEqual(size, 987654)

    def test_get_category_videos_rejects_unauthorized_channel(self):
        provider = TelegramProvider()
        with self.assertRaises(ValueError):
            asyncio.run(provider.get_category_videos("me"))
        with self.assertRaises(ValueError):
            asyncio.run(provider.get_category_videos("-1009999999999"))

    def test_get_history_batch_rejects_unauthorized_channel(self):
        provider = TelegramProvider()
        with self.assertRaises(ValueError):
            asyncio.run(provider.get_history_batch("me"))
        with self.assertRaises(ValueError):
            asyncio.run(provider.get_history_batch("-1009999999999"))

class TestDurationFilter(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if passes_duration_filter is None:
            self.fail("required Telegram function missing: passes_duration_filter")

    def test_shorts_rejected_default_420(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_MIN_DURATION_SECONDS", None)
            self.assertFalse(passes_duration_filter(60))
            self.assertFalse(passes_duration_filter(419))
            self.assertTrue(passes_duration_filter(420))
            self.assertTrue(passes_duration_filter(3600))

    def test_unknown_duration_passes(self):
        self.assertTrue(passes_duration_filter(0))
        self.assertTrue(passes_duration_filter(None))

    def test_custom_threshold(self):
        with patch.dict(os.environ, {"TELEGRAM_MIN_DURATION_SECONDS": "600"}):
            self.assertFalse(passes_duration_filter(599))
            self.assertTrue(passes_duration_filter(600))

    def test_invalid_env_falls_back(self):
        with patch.dict(os.environ, {"TELEGRAM_MIN_DURATION_SECONDS": "nonsense"}):
            self.assertFalse(passes_duration_filter(60))
            self.assertTrue(passes_duration_filter(420))


class TestThumbPath(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if thumb_api_path is None:
            self.fail("required Telegram function missing: thumb_api_path")

    def test_path_format(self):
        p = thumb_api_path("-1001:42:UNIQ")
        self.assertTrue(p.startswith("/admin/api/thumb?provider=telegram&ref="))
        self.assertIn("-1001%3A42%3AUNIQ", p)
        self.assertNotIn(" ", p)

    def test_video_carries_thumb_path(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        item = message_to_video(_video_message(), "Some Channel")
        self.assertTrue(str(item.thumbnail).startswith("/admin/api/thumb?"))


class TestCyrillicTitle(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")

    def test_multiword_cyrillic_falls_back(self):
        msg = _video_message(caption="🔥 В нашем приватном канале новинки")
        item = message_to_video(msg, "Kanal", known_performers=[])
        self.assertEqual(item.title, "Kanal #42")

    def test_repeated_russian_channel_ad_is_not_used_as_video_title(self):
        msg = _video_message(caption=(
            '🔥 В нашем приватном канале "ЗАКРЫТЫЙ КИНОЗАЛ" ежедневно выходят '
            'новинки и эксклюзивные ролики разных порно студий с удобной навигацией по хештегам.\n\n'
            '🔐 Вступай в приватный канал : @closhollbot со скидкой.\n\n'
            'ВСЕ ТОПОВЫЕ ПРОЕКТЫ | ВИДЕОЧАТ С ПАРНЯМИ | VPN'
        ))
        item = message_to_video(msg, "Kanal", known_performers=[])
        self.assertEqual(item.title, "Kanal #42")

    def test_single_cyrillic_word_kept(self):
        msg = _video_message(caption="Привет")
        item = message_to_video(msg, "Kanal", known_performers=[])
        self.assertEqual(item.title, "Привет")

    def test_latin_untouched(self):
        msg = _video_message(caption="Hot video title")
        item = message_to_video(msg, "Kanal", known_performers=[])
        self.assertEqual(item.title, "Hot video title")

    def test_cyrillic_spam_prefers_filename(self):
        msg = _video_message(caption="🔥 Новинки каждый день")
        msg["filename"] = "hot_scene_2024.mp4"
        item = message_to_video(msg, "Kanal", known_performers=[])
        self.assertEqual(item.title, "hot scene 2024")


class TestPerformerMatching(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if match_known_performers is None:
            self.fail("required Telegram function missing: match_known_performers")

    @staticmethod
    def _catalog(names, aliases=None):
        aliases = aliases or {}
        return [
            {"id": f"stashdb:{index}", "name": name,
             "aliases": [alias for alias, canonical in aliases.items() if canonical == name],
             "trusted_aliases": [alias for alias, canonical in aliases.items() if canonical == name]}
            for index, name in enumerate(names, 1)
        ]

    def test_full_name_match(self):
        out = match_known_performers("OF Andres Milan Rico Marlon 720p",
                                     ["Rico Marlon", "Andres Milan"])
        self.assertIn("Rico Marlon", out)
        self.assertIn("Andres Milan", out)

    def test_concatenated_name_no_match(self):
        # Krotka sklejka (< 8 liter po sklejeniu) nie lapie sie — brak zgadywania.
        out = match_known_performers("AlBo show", ["Al Bo"])
        self.assertEqual(out, [])

    def test_glued_filename_match(self):
        # Dlugie wielowyrazowe sklejki z nazw plikow lapia sie wariantem bezspacjowym.
        out = match_known_performers("OF AndresMilan RicoMarlon 720p",
                                     ["Rico Marlon", "Andres Milan"])
        self.assertIn("Rico Marlon", out)
        self.assertIn("Andres Milan", out)

    def test_short_glued_no_match(self):
        out = match_known_performers("AlPacino show", ["Al Pacino"])
        self.assertEqual(out, ["Al Pacino"])
        # "alpacino" ma 8 liter -> prog zaliczony; to celowy trade-off,
        # ale krotkie sklejki ponizej 8 liter odpadaja:
        out2 = match_known_performers("JoDoe show", ["Jo Doe"])
        self.assertEqual(out2, [])

    def test_alias_maps_to_canonical(self):
        out = match_known_performers("Pierce bareback", ["Ryan Rose"],
                                     aliases={"pierce": "Ryan Rose"})
        self.assertEqual(out, ["Ryan Rose"])

    def test_alias_only_dictionary(self):
        out = match_known_performers("Pierce bareback", [],
                                     aliases={"pierce": "Ryan Rose"})
        self.assertEqual(out, ["Ryan Rose"])

    def test_alias_no_double_count(self):
        out = match_known_performers("Ryan Rose aka Pierce", ["Ryan Rose"],
                                     aliases={"pierce": "Ryan Rose"})
        self.assertEqual(out, ["Ryan Rose"])

    def test_alias_unknown_ignored(self):
        out = match_known_performers("Some Guy show", ["Ryan Rose"],
                                     aliases={"": "X", "  ": "Y"})
        self.assertEqual(out, [])

    def test_case_insensitive(self):
        out = match_known_performers("RICO MARLON show", ["Rico Marlon"])
        self.assertEqual(out, ["Rico Marlon"])

    def test_no_partial_word_match(self):
        out = match_known_performers("Marlonetti show", ["Marlon"])
        self.assertEqual(out, [])

    def test_empty_dict_means_empty(self):
        self.assertEqual(match_known_performers("Rico Marlon", []), [])
        self.assertEqual(match_known_performers("Rico Marlon", None), [])

    def test_max_five(self):
        names = [f"Actor Number{i}" for i in range(10)]
        text = " ".join(names)
        self.assertEqual(len(match_known_performers(text, names)), 5)

    def test_subname_collision_prevented(self):
        # Mononyms like 'Bryce' and 'James' must not match inside full names like 'Bryce Jax' or 'James Cassidy'
        text = "🔥 Only Fans - James Cassidy, Bryce Jax & Matthew Ellis"
        names = ["Matthew Ellis", "James Cassidy", "Bryce Jax", "Bryce", "James"]
        out = match_known_performers(text, names)
        self.assertEqual(sorted(out), ["Bryce Jax", "James Cassidy", "Matthew Ellis"])

    def test_separate_mononym_occurrence_is_preserved(self):
        out = match_known_performers("James Cassidy and James", ["James Cassidy", "James"])
        self.assertEqual(out, ["James Cassidy", "James"])

    def test_invalid_names_like_editors_cut_and_fucked_are_ignored(self):
        out = match_known_performers(
            "MenAtPlay - Campaign Erection, Editor's Cut.mp4 - Gael Kriok fucked by Ronny",
            ["Editor's Cut.mp4", "Fucked", "fucked", "Gael Kriok", "Ronny", "Campaign Erection"]
        )
        self.assertIn("Gael Kriok", out)
        self.assertIn("Ronny", out)
        self.assertNotIn("Editor's Cut.mp4", out)
        self.assertNotIn("Fucked", out)
        self.assertNotIn("fucked", out)
        self.assertNotIn("Campaign Erection", out)

    def test_user_cases_no_false_alias_and_first_name_truncation(self):
        # 1. Lance Bennett & Leon Fox:
        # 'Lance' must not match inside 'Lance Bennett'
        # 'leon' alias (e.g. Paddy O'Brian) must not match inside 'Leon Fox'
        out = match_known_performers(
            "Lance Bennett & Leon Fox",
            ["Lance", "Leon Fox"],
            aliases={"leon": "Paddy O'Brian"}
        )
        self.assertNotIn("Lance", out)
        self.assertNotIn("Paddy O'Brian", out)
        self.assertIn("Leon Fox", out)

    def test_message_to_video_uses_aliases_and_suppresses_name_overlap(self):
        msg = _video_message(
            caption="#ANAL #OF - Venezolano Felipe Eduardo & texasgymjock"
        )
        catalog = self._catalog(["Felipe Eduardo", "Texas Gym Jock"], {
            "Venezolano Felipe Eduardo": "Felipe Eduardo", "texasgymjock": "Texas Gym Jock",
        })
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["Felipe Eduardo", "Texas Gym Jock"])

        msg = _video_message(
            caption="#OF #ANAL Only Fans – CJ Delco And Felipe Eduardo"
        )
        catalog = self._catalog(["Felipe Eduardo", "CJ Delco"])
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["CJ Delco", "Felipe Eduardo"])

    def test_user_cases_message_to_video_title_syntax(self):
        # 1. [Only Fans] Rhyheim Shabazz, Cade Maddox & Alonzo Jordon
        # Uncatalogued credits are proposed for review rather than auto-added.
        msg1 = _video_message(caption="[Only Fans] Rhyheim Shabazz, Cade Maddox & Alonzo Jordon")
        catalog = self._catalog(["Rhyheim Shabazz", "Cade Maddox", "Leon Reddz", "Adrian Hart", "Leon Fox"])
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item1 = message_to_video(msg1, "Kanal")
        self.assertEqual(list(item1.performers), ["Rhyheim Shabazz", "Cade Maddox"])
        self.assertTrue(any(p["matched_text"] == "Alonzo Jordon" for p in item1.performer_proposals))

        # 2. [Just For Fans] - Rhyheim Shabazz, Krave, Trapp, Fame & Leon Reddz fuck & DP Adrian Hart
        # Must not have Paddy O'Brian
        msg2 = _video_message(
            caption="[Just For Fans] - Rhyheim Shabazz, Krave, Trapp, Fame & Leon Reddz fuck & DP Adrian Hart"
        )
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item2 = message_to_video(msg2, "Kanal")
        self.assertNotIn("Paddy O'Brian", item2.performers)
        self.assertIn("Leon Reddz", item2.performers)
        self.assertIn("Adrian Hart", item2.performers)
        self.assertIn("Rhyheim Shabazz", item2.performers)

        # 3. Lance Bennett & Leon Fox
        msg3 = _video_message(caption="Lance Bennett & Leon Fox")
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item3 = message_to_video(msg3, "Kanal")
        self.assertNotIn("Paddy O'Brian", item3.performers)
        self.assertNotIn("Lance", item3.performers)
        self.assertEqual(list(item3.performers), ["Leon Fox"])
        self.assertTrue(any(p["matched_text"] == "Lance Bennett" for p in item3.performer_proposals))

    def test_auto_loaded_aliases(self):
        msg = _video_message(caption="Pierce bareback")
        catalog = self._catalog(["Ryan Rose"], {"pierce": "Ryan Rose"})
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["Ryan Rose"])

    def test_video_gets_performers_from_dict(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        msg = _video_message(caption="Rico Marlon bareback")
        with patch.object(tg_module, "load_performer_catalog",
                          return_value=self._catalog(["Rico Marlon"])):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["Rico Marlon"])

    def test_load_known_performers_missing_db(self):
        if load_known_performers is None:
            self.fail("required Telegram function missing: load_known_performers")
        require_url()
        init_test_db().close()
        tg_module._KNOWN_PERFORMERS_CACHE.update({"mtime": 0.0, "names": []})
        tg_module._ALIAS_MAP_CACHE.update({"mtime": 0.0, "entries": {}})
        with patch.dict(os.environ, {"DATABASE_URL": TEST_URL}):
            self.assertEqual(load_known_performers(), [])


class TestNoSecretLeak(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)

    def test_no_api_hash_in_video_or_url(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        sentinel = "deadbeefcafe00secret"
        with patch.dict(os.environ, {"TELEGRAM_API_HASH": sentinel,
                                     "TELEGRAM_API_ID": "12345"}):
            msg = _video_message()
            item = message_to_video(msg, "Some Channel")
            blob = repr(item) + str(getattr(item, "url", "")) \
                + str(getattr(item, "id", ""))
            self.assertNotIn(sentinel, blob)
            self.assertNotIn("api_hash", blob.lower())

    def test_no_api_hash_in_resolve_marker(self):
        if TelegramProvider is None:
            self.fail("required Telegram class missing: TelegramProvider")
        sentinel = "deadbeefcafe00secret"
        provider = TelegramProvider()
        with patch.dict(os.environ, {"TELEGRAM_API_HASH": sentinel,
                                     "TELEGRAM_API_ID": "12345",
                                     "TELEGRAM_CHANNELS": "-1001234567890"}):
            result = asyncio.run(provider.resolve_stream(
                "-1001234567890:42:AQADabc123unique"))
        self.assertNotIn(sentinel, str(result))


class TestHashtags(unittest.TestCase):
    """Hashtagi Telegramu (etap 2): extract + categories z prefixem #."""

    def setUp(self):
        _require_telegram(self)

    def _require_extract(self):
        if extract_hashtags is None:
            self.fail("required Telegram function missing: extract_hashtags")

    @staticmethod
    def _norm(tags):
        return [str(t).lstrip("#").strip() for t in (tags or [])]

    @staticmethod
    def _norm_lower(tags):
        return [str(t).lstrip("#").strip().lower() for t in (tags or [])]

    def test_extract_basic(self):
        self._require_extract()
        tags = extract_hashtags("Hot clip #anal #twink extra")
        norm = self._norm_lower(tags)
        self.assertIn("anal", norm)
        self.assertIn("twink", norm)

    def test_extract_unicode(self):
        self._require_extract()
        tags = extract_hashtags("клип #гей горячо")
        norm = self._norm_lower(tags)
        self.assertIn("гей", norm)

    def test_extract_duplicates(self):
        self._require_extract()
        tags = extract_hashtags("#anal some #anal text #twink #anal")
        norm = self._norm_lower(tags)
        self.assertEqual(norm.count("anal"), 1)
        self.assertEqual(len(norm), 2)

    def test_extract_trailing_punctuation(self):
        self._require_extract()
        tags = extract_hashtags("hot #anal! next")
        norm = self._norm(tags)
        self.assertIn("anal", norm)
        for t in tags:
            self.assertFalse(str(t).endswith("!"),
                             f"trailing punctuation not stripped: {t!r}")
            self.assertNotIn("!", str(t))

    def test_extract_max_15(self):
        self._require_extract()
        caption = " ".join(f"#tag{i}" for i in range(20))
        tags = extract_hashtags(caption)
        self.assertLessEqual(len(tags), 15)
        self.assertEqual(len(tags), 15)

    def test_extract_empty(self):
        self._require_extract()
        self.assertEqual(extract_hashtags(""), [])
        self.assertEqual(extract_hashtags("plain text without tags"), [])
        try:
            result = extract_hashtags(None)
        except (TypeError, AttributeError):
            self.fail("extract_hashtags(None) should return an empty list")
        self.assertEqual(result, [])

    def test_message_categories_prefixed(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        msg = _video_message(caption="Nice clip #anal #twink extra")
        item = message_to_video(msg, "Some Channel")
        self.assertIsNotNone(item)
        cats = list(getattr(item, "categories", []) or [])
        if len(cats) <= 1 or not any(
                isinstance(c, str) and c.startswith("#") for c in cats[1:]):
            self.fail("hashtags in Telegram captions must become # prefixed categories")
        self.assertEqual(cats[0], "Some Channel")
        for c in cats[1:]:
            self.assertTrue(isinstance(c, str) and c.startswith("#"),
                            f"expected # prefix, got: {c!r}")

    def test_title_fallback_mention_only(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        msg = _video_message(channel_id=-1001234567890, message_id=123,
                             caption="@GayBunker")
        item = message_to_video(msg, "Kanał")
        self.assertIsNotNone(item)
        title = (item.title or "").strip()
        self.assertTrue(title)
        if title == "@GayBunker" or title.lower().startswith("telegram video"):
            self.fail("mention-only captions need a useful title fallback")
        self.assertNotEqual(title, "@GayBunker")
        self.assertFalse(title.lower().startswith("telegram video"),
                         f"generic fallback not allowed, got: {title!r}")
        self.assertTrue("123" in title or "Kanał" in title or "#" in title,
                        f"fallback should reference channel/message, got: {title!r}")

    def test_title_from_filename_when_caption_empty(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        msg = _video_message(caption="")
        msg["filename"] = "my_hot_clip.mp4"
        msg["file_name"] = "my_hot_clip.mp4"
        for key in ("video", "document", "file"):
            if isinstance(msg.get(key), dict):
                msg[key]["filename"] = "my_hot_clip.mp4"
                msg[key]["file_name"] = "my_hot_clip.mp4"
                msg[key]["file_path"] = "my_hot_clip.mp4"
        item = message_to_video(msg, "Some Channel")
        self.assertIsNotNone(item)
        title = (item.title or "")
        norm = title.lower().replace("_", " ").replace("-", " ").replace(".", " ")
        if "my hot clip" not in norm:
            self.fail("empty Telegram caption should fall back to the filename")
        self.assertIn("my hot clip", norm)

    def test_performers_extracted_from_multiline_caption_and_filename(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        caption = "Cock Sure Men\nMorgan Black, Heath Jordan and Conner Habib\n@freegaypornhd"
        msg = _video_message(caption=caption)
        msg["filename"] = "Heath Jordan & Morgan Black, Conner Habib.mp4"
        item = message_to_video(msg, "Gay Porn Full Videos HD", known_performers=[])
        self.assertIsNotNone(item)
        self.assertEqual(item.title, "Cock Sure Men")
        self.assertEqual(item.performers, [])
        self.assertEqual({p["matched_text"] for p in item.performer_proposals},
                         {"Morgan Black", "Heath Jordan", "Conner Habib"})


if __name__ == "__main__":
    unittest.main()
