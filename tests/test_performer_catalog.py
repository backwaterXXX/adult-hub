import unittest

from common.performer_catalog import match_catalog, normalize_name


class PerformerCatalogTests(unittest.TestCase):
    def test_normalizes_accents_punctuation_and_joined_names(self):
        self.assertEqual(normalize_name("Nicolás Ríos"), "nicolasrios")
        records = [{"id": "p1", "name": "Nicolás Ríos", "aliases": ["Nicolas-Rios"]}]
        for spelling in ("Nicolas Rios", "nicolas.rios", "NICOLÁSRÍOS"):
            matched, proposals = match_catalog([("caption", spelling)], records)
            self.assertEqual([person["id"] for person in matched], ["p1"])
            self.assertEqual(proposals, [])

    def test_prefers_longest_span_and_does_not_match_inside_words(self):
        records = [
            {"id": "short", "name": "James", "aliases": []},
            {"id": "long", "name": "James Cassidy", "aliases": []},
        ]
        matched, _ = match_catalog([("caption", "James Cassidy and Jamestown")], records)
        self.assertEqual([person["id"] for person in matched], ["long"])

    def test_one_character_typo_becomes_review_proposal(self):
        records = [{"id": "p1", "name": "Alejandro Martinez", "aliases": []}]
        matched, proposals = match_catalog([("caption", "Alejandor Martinez & Ryan Black")], records)
        self.assertEqual(matched, [])
        self.assertTrue(any(p["reason"] == "possible_typo" and p["performer_id"] == "p1" for p in proposals))
        self.assertTrue(any(p["reason"] == "uncatalogued_credit" and p["matched_text"] == "Ryan Black" for p in proposals))

    def test_legacy_bio_typo_proposal_has_no_catalog_foreign_key(self):
        records = [{"id": "local:alejandro martinez", "name": "Alejandro Martinez",
                    "aliases": [], "_persisted": False}]
        _, proposals = match_catalog([("caption", "Alejandor Martinez & Ryan Black")], records)
        typo = next(p for p in proposals if p["reason"] == "possible_typo")
        self.assertEqual(typo["candidate"], "Alejandro Martinez")
        self.assertIsNone(typo["performer_id"])

    def test_persisted_local_id_keeps_its_foreign_key(self):
        records = [{"id": "local:alejandro martinez", "name": "Alejandro Martinez", "aliases": []}]
        _, proposals = match_catalog([("caption", "Alejandor Martinez & Ryan Black")], records)
        typo = next(p for p in proposals if p["reason"] == "possible_typo")
        self.assertEqual(typo["performer_id"], "local:alejandro martinez")

    def test_ambiguous_alias_does_not_guess(self):
        records = [
            {"id": "p1", "name": "Alex One", "aliases": ["Alex"]},
            {"id": "p2", "name": "Alex Two", "aliases": ["Alex"]},
        ]
        matched, proposals = match_catalog([("caption", "Alex")], records)
        self.assertEqual(matched, [])
        self.assertEqual(proposals[0]["reason"], "ambiguous_catalog_alias")

    def test_short_unreviewed_alias_is_not_an_automatic_match(self):
        records = [{"id": "p1", "name": "Ryan Rose", "aliases": ["Pierce"]}]
        matched, proposals = match_catalog([("caption", "Pierce bareback")], records)
        self.assertEqual(matched, [])
        self.assertEqual(proposals, [])

    def test_short_reviewed_alias_matches_exactly(self):
        records = [{"id": "p1", "name": "Ryan Rose", "aliases": ["Pierce"],
                    "trusted_aliases": ["Pierce"]}]
        matched, proposals = match_catalog([("caption", "Pierce bareback")], records)
        self.assertEqual([person["id"] for person in matched], ["p1"])
        self.assertEqual(proposals, [])

    def test_strips_aggregators_from_proposals(self):
        records = []
        matched, proposals = match_catalog(
            [("caption", "[OnlyFans] ViktorOnee & Performer Two, OF Dom King & Only Fans BritishTwunk")],
            records,
        )
        self.assertEqual(matched, [])
        candidates = {p["candidate"] for p in proposals if p["reason"] == "uncatalogued_credit"}
        self.assertIn("ViktorOnee", candidates)
        self.assertIn("Performer Two", candidates)
        self.assertIn("Dom King", candidates)
        self.assertIn("BritishTwunk", candidates)
        self.assertFalse(any("onlyfans" in c.lower() or "only fans" in c.lower() or c.startswith("OF ") for c in candidates))

    def test_strips_studio_prefixes_from_proposals(self):
        records = []
        matched, proposals = match_catalog(
            [("caption", "[Tim Tales] Ethan Metz & TimTales - Tim Kruger, Tim Tales] Franklin Acevedo")],
            records,
        )
        candidates = {p["candidate"] for p in proposals if p["reason"] == "uncatalogued_credit"}
        self.assertIn("Ethan Metz", candidates)
        self.assertIn("Tim Kruger", candidates)
        self.assertIn("Franklin Acevedo", candidates)
        self.assertFalse(any("timtales" in c.lower() or "tim tales" in c.lower() for c in candidates))

    def test_discards_pure_studio_and_aggregator_names(self):
        records = []
        matched, proposals = match_catalog(
            [("caption", "Tim Tales & Corbin Fisher, Falcon Studios & OnlyFans")],
            records,
        )
        self.assertEqual(matched, [])
        self.assertEqual(proposals, [])

    def test_strips_nationality_prefixes_from_proposals(self):
        records = []
        matched, proposals = match_catalog(
            [("caption", "Brasileiros Peralta Silva & Cubano Alexandro Cabrera")],
            records,
        )
        candidates = {p["candidate"] for p in proposals if p["reason"] == "uncatalogued_credit"}
        self.assertIn("Peralta Silva", candidates)
        self.assertIn("Alexandro Cabrera", candidates)

    def test_discards_nationality_demonym_descriptors(self):
        records = []
        matched, proposals = match_catalog(
            [("caption", "Korean Twunk & Asian Hunks")],
            records,
        )
        self.assertEqual(matched, [])
        self.assertEqual(proposals, [])

    def test_strips_multiline_studio_and_acronym_prefixes(self):
        records = []
        matched, proposals = match_catalog(
            [("caption", "Eric Videos \nJustin Jett, LE American Sergeant Miles and RFC Paddy O'Brian, TT Damien Crosse")],
            records,
        )
        self.assertEqual(matched, [])
        candidates = {p["candidate"] for p in proposals if p["reason"] == "uncatalogued_credit"}
        self.assertIn("Justin Jett", candidates)
        self.assertIn("Sergeant Miles", candidates)
        self.assertIn("Paddy O'Brian", candidates)
        self.assertIn("Damien Crosse", candidates)
        self.assertFalse(any("eric" in c.lower() or c.startswith("LE ") or c.startswith("RFC ") or c.startswith("TT ") for c in candidates))

    def test_canonical_persisted_match_preferred_over_synthetic_legacy_alias(self):
        records = [
            {"id": "stashdb:1", "name": "Malik Delgaty", "aliases": [], "_persisted": True},
            {"id": "local:menmalikdelgaty", "name": "MEN Malik Delgaty", "aliases": ["Malik Delgaty"], "_persisted": False},
        ]
        matched, proposals = match_catalog([("caption", "Malik Delgaty & Ryan Black")], records)
        self.assertEqual([person["id"] for person in matched], ["stashdb:1"])
        self.assertEqual(proposals, [{"candidate": "Ryan Black", "matched_text": "Ryan Black", "source_field": "caption", "reason": "uncatalogued_credit", "performer_id": None}])

    def test_strips_channel_suffixes_and_acronym_prefixes(self):
        records = [
            {"id": "stashdb:1", "name": "Tom Storm", "aliases": [], "_persisted": True},
            {"id": "stashdb:2", "name": "Carter Collins", "aliases": [], "_persisted": True},
            {"id": "stashdb:3", "name": "Diego Mineiro", "aliases": [], "_persisted": True},
            {"id": "stashdb:4", "name": "Rhyheim Shabazz", "aliases": [], "_persisted": True},
        ]
        matched, proposals = match_catalog(
            [("caption", "RFCC Russian Tom Storm, Carter Collins Putinho, MOL Brasileiros Diego Mineiro, JFF VOYR Rhyheim Shabazz and Drew Skylar Putinho Vídeos")],
            records,
        )
        matched_ids = [m["id"] for m in matched]
        self.assertIn("stashdb:1", matched_ids)
        self.assertIn("stashdb:2", matched_ids)
        self.assertIn("stashdb:3", matched_ids)
        self.assertIn("stashdb:4", matched_ids)
        candidates = {p["candidate"] for p in proposals}
        self.assertIn("Drew Skylar", candidates)
        self.assertFalse(any("putinho" in c.lower() or "vídeo" in c.lower() for c in candidates))


if __name__ == "__main__":
    unittest.main()
