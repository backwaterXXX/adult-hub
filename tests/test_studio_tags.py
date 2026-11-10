"""Unit tests for production studio detection into tags."""
import unittest


try:
    import sync.studio_tags as studio_tags
    STUDIO_TAGS_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    STUDIO_TAGS_AVAILABLE = False


class TestStudioTags(unittest.TestCase):
    def setUp(self):
        if not STUDIO_TAGS_AVAILABLE:
            self.skipTest("studio_tags module not available")

    def test_detect_belami_from_title(self):
        tags = studio_tags.detect_production_studios("BelAmi - Summer Days Scene 1")
        self.assertEqual(tags, ["Studio: BelAmi"])

    def test_detect_sean_cody_variant(self):
        tags = studio_tags.detect_production_studios("Hunky guys in SeanCody scene")
        self.assertEqual(tags, ["Studio: Sean Cody"])

    def test_detect_cockyboys_and_corbin_fisher(self):
        tags = studio_tags.detect_production_studios("CockyBoys meets Corbin Fisher stars")
        self.assertEqual(tags, ["Studio: CockyBoys", "Studio: Corbin Fisher"])

    def test_detect_from_existing_tags(self):
        tags = studio_tags.detect_production_studios("Hot Afternoon Scene", existing_tags=["Men.com", "Bareback"])
        self.assertEqual(tags, ["Studio: Men.com"])

    def test_no_studio_detected(self):
        tags = studio_tags.detect_production_studios("Random amateur home video", existing_tags=["Amateur", "Solo"])
        self.assertEqual(tags, [])

    def test_detect_dragon_media(self):
        self.assertEqual(
            studio_tags.detect_production_studios("Dragon – Camping Cum Buddies s2"),
            ["Studio: Dragon Media"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("Dragon Media – Dad Gets Into Trouble"),
            ["Studio: Dragon Media"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("[Dragon] Girth"),
            ["Studio: Dragon Media"],
        )
        # Should not falsely match Dragon Rojo
        self.assertEqual(
            studio_tags.detect_production_studios("Markin Wolf fucks Argentino Dragon Rojo"),
            [],
        )

    def test_detect_menatplay(self):
        self.assertEqual(
            studio_tags.detect_production_studios("MenAtPlay - Office Encounter"),
            ["Studio: MenAtPlay"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("Men at play (retro video)"),
            ["Studio: MenAtPlay"],
        )

    def test_detect_raging_stallion_and_rs(self):
        self.assertEqual(
            studio_tags.detect_production_studios("Raging Stallion Aggro 2024 Sc3"),
            ["Studio: Raging Stallion"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("[RS] Hardcore Action"),
            ["Studio: Raging Stallion"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("RS - Summer Camp"),
            ["Studio: Raging Stallion"],
        )

    def test_detect_expanded_studios(self):
        self.assertEqual(
            studio_tags.detect_production_studios("🎞 Produtora: Bi Latin Men - Hot Guys"),
            ["Studio: Bi Latin Men"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("[Tim Tales] Summer Encounter"),
            ["Studio: Tim Tales"],
        )
        self.assertEqual(
            studio_tags.detect_production_studios("[Cutler's Den] Leather Scene"),
            ["Studio: Cutler's Den"],
        )


if __name__ == "__main__":
    unittest.main()
