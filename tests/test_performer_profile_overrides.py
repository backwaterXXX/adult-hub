"""PostgreSQL contracts for manual performer profiles, revisions and reset semantics."""

import json

from common.performer_profiles import (
    PerformerProfileConflict,
    get_override_fields,
    merge_override_names,
    read_profile,
    update_profile,
)
from tests.pg_helper import PgTestCase


class TestPerformerProfileOverrides(PgTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO performer_bios(name,bio,aliases) VALUES (%s,%s,%s)",
            ("Canonical Name", "Provider bio", json.dumps(["Provider Alias"])),
        )
        self.conn.execute(
            "INSERT INTO performer_photos(name,thumb) VALUES (%s,%s)",
            ("Canonical Name", "https://provider.example/art.jpg"),
        )
        self.conn.execute(
            "INSERT INTO performer_aliases(alias,canonical) VALUES (%s,%s)",
            ("provider alias", "Canonical Name"),
        )

    def test_edit_is_revisioned_audited_and_provider_locks_cover_all_editable_fields(self):
        profile = update_profile(
            self.conn, name="Canonical Name", expected_revision=0,
            profile={"bio": "Manual bio", "photo": "https://manual.example/art.jpg",
                     "aliases": ["Manual Alias"]},
        )
        self.assertEqual(profile["revision"], 1)
        self.assertEqual(profile["overrides"], ["aliases", "bio", "photo"])
        self.assertEqual(get_override_fields(self.conn, "canonical name"), {"bio", "photo", "aliases"})
        self.assertEqual(read_profile(self.conn, "Canonical Name")["bio"], "Manual bio")
        self.assertEqual(read_profile(self.conn, "Canonical Name")["photo"], "https://manual.example/art.jpg")
        self.assertEqual(read_profile(self.conn, "Canonical Name")["aliases"], ["Manual Alias"])
        audit = self.conn.execute(
            "SELECT event_type,object_type,changes FROM admin_audit_events WHERE object_id=%s",
            ("Canonical Name",),
        ).fetchone()
        self.assertEqual(audit[0:2], ("performer_profile_update", "performer"))
        changes = audit[2] if isinstance(audit[2], dict) else json.loads(audit[2])
        self.assertNotIn("manual.example", json.dumps(changes), "signed/artwork URLs are never retained in audit data")
        self.assertEqual(merge_override_names(self.conn, "Canonical Name", "Other Name"), ["Canonical Name"])

    def test_stale_revision_alias_conflict_and_reset_restore_saved_provider_values(self):
        self.conn.execute("INSERT INTO videos(id,title) VALUES ('profile-test-other','Other performer fixture')")
        self.conn.execute(
            "INSERT INTO video_performers(video_id,performer_name,position) VALUES (%s,%s,%s)",
            ("profile-test-other", "Another Person", 0),
        )
        update_profile(
            self.conn, name="Canonical Name", expected_revision=0,
            profile={"bio": "Manual bio", "photo": "https://manual.example/art.jpg",
                     "aliases": ["Manual Alias"]},
        )
        with self.assertRaises(PerformerProfileConflict):
            update_profile(
                self.conn, name="Canonical Name", expected_revision=0,
                profile={"bio": "Stale", "photo": "", "aliases": []},
            )
        with self.assertRaises(PerformerProfileConflict):
            update_profile(
                self.conn, name="Canonical Name", expected_revision=1,
                profile={"bio": "Manual bio", "photo": "https://manual.example/art.jpg",
                         "aliases": ["Another Person"]},
            )
        restored = update_profile(
            self.conn, name="Canonical Name", expected_revision=1,
            profile={"bio": "Manual bio", "photo": "https://manual.example/art.jpg",
                     "aliases": ["Manual Alias"]},
            reset_fields=["bio", "photo", "aliases"],
        )
        self.assertEqual(restored["revision"], 2)
        self.assertEqual(restored["overrides"], [])
        self.assertEqual(restored["bio"], "Provider bio")
        self.assertEqual(restored["photo"], "https://provider.example/art.jpg")
        self.assertCountEqual(restored["aliases"], ["Provider Alias"])
        self.assertEqual(get_override_fields(self.conn, "Canonical Name"), set())

