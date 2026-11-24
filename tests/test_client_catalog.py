"""Focused tests for Client API v1 PostgreSQL catalog query and DTO boundary.

Covers:
- Offline unit tests:
  * VideoItem, ChannelItem, PaginatedVideos, PaginatedChannels, PerformerRef DTO contracts
  * Strict allowlisting: absence of all filesystem, upstream, and admin-only fields
  * Deterministic sort tie-breaking with v.id ASC (unseeded random omitted)
  * Paging math and edge cases
  * Channel alias normalization
- PostgreSQL integration fixtures (run via disposable DB / PgTestCase):
  * One visible canonical item with allowlisted safe fields
  * Multiple sources and source-aware channel membership (primary + video_sources)
  * Duplicate merge exclusion and alias resolution to keeper
  * Channel filter, count aggregation, and detail lookups
  * Hidden, unplayable, dead stream (valid schema), and unknown ID rejection
  * Metadata overrides precedence
  * Deterministic pagination ordering
  * Focused real DB regressions:
    - Disabled primary channel with active alternate source visibility and no count leakage
    - Duplicate alias channel count deduplication and canonical DB-only channel totals
    - Stored performer photo_url / upstream URL suppression and same-origin artwork generation
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from common.managed_sources import canonical_channel_id, channel_aliases
from resolver.client_catalog import (
    PaginatedPerformers,
    PaginatedTaxonomy,
    PaginatedVideos,
    PerformerDetail,
    PerformerRef,
    TaxonomyItem,
    VideoItem,
    _CLIENT_SORT_CLAUSES,
    _extract_disabled_channel_ids,
    client_sort_clause,
    query_catalog_channel_detail,
    query_catalog_channels,
    query_catalog_performers,
    query_catalog_video_detail,
    query_catalog_videos,
    serialize_client_video,
)
from tests.pg_helper import PgTestCase


class TestClientCatalogUnit(unittest.TestCase):
    """Offline unit tests verifying contracts, DTO allowlists, and helpers."""

    def test_dto_serialization_strict_allowlist(self):
        """Assert allowlisted fields are present and strictly no admin/filesystem/upstream fields leak."""
        video = VideoItem(
            id="telegram:-100123456789:123:456789",
            title="Test Scene 1",
            duration_seconds=1800,
            release_date="2026-05-01",
            date_added="2026-10-01T12:00:00Z",
            studio="Alpha Studio",
            plot="A test video plot summary.",
            categories=["Action", "Drama"],
            tags=["4k", "HD"],
            performers=[
                PerformerRef(
                    id="stashdb:1111-2222-3333",
                    name="Actor One",
                    photo_url="/api/v1/artwork/performer/stashdb:1111-2222-3333",
                )
            ],
            poster_url="/api/v1/artwork/video/telegram:-100123456789:123:456789/poster",
            backdrop_url="/api/v1/artwork/video/telegram:-100123456789:123:456789/backdrop",
            file_size_bytes=104857600,
            width=1920,
            height=1080,
            is_hd=True,
            user_state=None,
        )

        serialized = video.to_dict()

        # Required & expected allowlisted fields
        expected_keys = {
            "id", "title", "duration_seconds", "release_date", "date_added",
            "studio", "plot", "categories", "tags", "performers",
            "poster_url", "backdrop_url", "file_size_bytes", "width", "height", "is_hd",
        }
        self.assertEqual(set(serialized.keys()), expected_keys)

        # Explicitly verify absence of forbidden admin, filesystem, and private upstream fields
        forbidden_fields = [
            "local_files", "has_local_files", "file_count", "file_categories",
            "telegram_url", "provider", "scene_match_source", "scene_match_external_id",
            "scene_match_url", "scene_match_confidence", "metadata_fields", "metadata_revision",
            "missing_fields", "playable", "dead_stream", "availability", "unavailable_reason",
            "unavailable_detected_at", "source_filename", "source_text", "performer_resolution",
        ]
        for forbidden in forbidden_fields:
            self.assertNotIn(forbidden, serialized, f"Forbidden field '{forbidden}' must not leak in client DTO")

    def test_taxonomy_dtos_serialization(self):
        """Taxonomy DTOs serialize strictly allowlisted fields with deterministic structure."""
        # 1. TaxonomyItem & PaginatedTaxonomy
        tax_item = TaxonomyItem(name="Action", count=42)
        self.assertEqual(tax_item.to_dict(), {"name": "Action", "count": 42})

        paginated_tax = PaginatedTaxonomy(
            items=[tax_item],
            total_count=1,
            offset=0,
            limit=50,
            has_more=False,
        )
        self.assertEqual(
            paginated_tax.to_dict(),
            {
                "items": [{"name": "Action", "count": 42}],
                "total_count": 1,
                "offset": 0,
                "limit": 50,
                "has_more": False,
            },
        )

        # 2. PerformerDetail
        detail = PerformerDetail(
            id="stashdb:8c01d932-4412-4217-91a1-fbf56976f571",
            name="Alex Frost",
            disambiguation="Male performer",
            photo_url="/api/v1/artwork/performer/stashdb%3A8c01d932-4412-4217-91a1-fbf56976f571",
            video_count=15,
            bio="Award-winning star.",
            nationality="US",
            birth="1995-04-12",
            aliases=["Alexander Frost", "A. Frost"],
        )
        d_dict = detail.to_dict()
        expected_keys = {"id", "name", "disambiguation", "photo_url", "video_count", "bio", "nationality", "birth", "aliases"}
        self.assertEqual(set(d_dict.keys()), expected_keys)
        self.assertEqual(d_dict["id"], "stashdb:8c01d932-4412-4217-91a1-fbf56976f571")
        self.assertEqual(d_dict["name"], "Alex Frost")
        self.assertEqual(d_dict["video_count"], 15)
        self.assertEqual(d_dict["aliases"], ["Alexander Frost", "A. Frost"])

        # Forbidden fields check on PerformerDetail
        for forbidden in ["pbc_url", "stashdb_url", "telegram_url", "local_files", "profile_overrides"]:
            self.assertNotIn(forbidden, d_dict)

        # PerformerDetail with photo_url=None includes photo_url=None (nullable in OpenAPI 3.1.0)
        detail_no_photo = PerformerDetail(
            id="stashdb:8c01d932-4412-4217-91a1-fbf56976f571",
            name="Alex Frost",
            photo_url=None,
        )
        self.assertIsNone(detail_no_photo.to_dict()["photo_url"])

        # 3. PaginatedPerformers
        perf_ref = PerformerRef(
            id="stashdb:8c01d932-4412-4217-91a1-fbf56976f571",
            name="Alex Frost",
            photo_url="/api/v1/artwork/performer/stashdb%3A8c01d932-4412-4217-91a1-fbf56976f571",
            video_count=15,
        )
        paginated_perf = PaginatedPerformers(
            items=[perf_ref],
            total_count=1,
            offset=0,
            limit=50,
            has_more=False,
        )
        p_dict = paginated_perf.to_dict()
        self.assertEqual(len(p_dict["items"]), 1)
        self.assertEqual(p_dict["items"][0]["video_count"], 15)
        self.assertEqual(p_dict["total_count"], 1)


    def test_artwork_urls_standardized_paths(self):
        """Artwork URLs must follow Client API v1 paths; only real vetted identifiers generate photo URLs."""
        row = {
            "id": "telegram:-100999:42:100",
            "title": "Sample",
            "duration": 500,
            "first_seen": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "categories": '["Genre"]',
            "tags": '["Tag"]',
            "performers": '[{"id": "stashdb:uuid-999", "name": "Identified Actor"}, "Unmapped Name"]',
        }
        item = serialize_client_video(
            row,
            performers_catalog_map={"stashdb:uuid-999": ("stashdb:uuid-999", "Identified Actor")},
        )
        self.assertEqual(item.poster_url, "/api/v1/artwork/video/telegram%3A-100999%3A42%3A100/poster")
        self.assertEqual(item.backdrop_url, "/api/v1/artwork/video/telegram%3A-100999%3A42%3A100/backdrop")
        self.assertEqual(len(item.performers), 2)

        # Real identifier gets same-origin artwork
        self.assertEqual(item.performers[0].id, "stashdb:uuid-999")
        self.assertEqual(item.performers[0].photo_url, "/api/v1/artwork/performer/stashdb%3Auuid-999")

        # Unmapped performer without real identifier gets photo_url=None
        self.assertEqual(item.performers[1].id, "name:Unmapped Name")
        self.assertIsNone(item.performers[1].photo_url)

    def test_unresolved_raw_performer_id_preserved_as_distinct_identity(self):
        """Unresolved raw IDs are preserved as distinct identities without collapsing to name:<name>."""
        row = {
            "id": "telegram:-100999:42:101",
            "title": "Unvetted Actor Test",
            "duration": 600,
            "performers": [
                {"id": "fake_id_666", "name": "Fake Star"},
                {"id": "only_fake_id_no_name"},
                "Plain String Performer",
            ],
        }
        # Passing empty catalog map (no vetted performers)
        item = serialize_client_video(row, performers_catalog_map={})
        self.assertEqual(len(item.performers), 3)
        # Unresolved raw ID is preserved directly as distinct identity, photo_url is None
        self.assertEqual(item.performers[0].id, "fake_id_666")
        self.assertEqual(item.performers[0].name, "Fake Star")
        self.assertIsNone(item.performers[0].photo_url)
        # Item with only raw ID uses raw ID as both id and name
        self.assertEqual(item.performers[1].id, "only_fake_id_no_name")
        self.assertEqual(item.performers[1].name, "only_fake_id_no_name")
        self.assertIsNone(item.performers[1].photo_url)
        # Plain string performer without raw ID gets explicit name: fallback
        self.assertEqual(item.performers[2].id, "name:Plain String Performer")
        self.assertEqual(item.performers[2].name, "Plain String Performer")
        self.assertIsNone(item.performers[2].photo_url)

    def test_config_disabled_channels_no_fail_open(self):
        """_extract_disabled_channel_ids extracts disabled channels and aliases without fail-open defaults."""
        configured = [
            {"id": "-100111", "enabled": False},
            {"id": "-100222", "enabled": True},
        ]
        disabled = _extract_disabled_channel_ids(configured=configured)
        self.assertIn("-100111", disabled)
        self.assertIn("111", disabled)
        self.assertNotIn("-100222", disabled)
        self.assertNotIn("222", disabled)

        # Explicit disabled items are also merged and expanded to aliases
        explicit = _extract_disabled_channel_ids(configured=configured, explicit_disabled=["-100333"])
        self.assertIn("-100111", explicit)
        self.assertIn("-100333", explicit)
        self.assertIn("333", explicit)

    def test_performer_catalog_mapping_resolution(self):
        """Performer catalog mapping correctly attaches catalog ID and artwork URL."""
        row = {
            "id": "telegram:-100999:42:100",
            "title": "Sample",
            "duration": 500,
            "performers": '["Alex Frost", "Unknown Actor"]',
        }
        mapping = {"alex frost": "stashdb:uuid-1234"}
        item = serialize_client_video(row, performers_catalog_map=mapping)
        self.assertEqual(len(item.performers), 2)
        self.assertEqual(item.performers[0].id, "stashdb:uuid-1234")
        self.assertEqual(item.performers[0].name, "Alex Frost")
        self.assertEqual(item.performers[0].photo_url, "/api/v1/artwork/performer/stashdb%3Auuid-1234")
        self.assertEqual(item.performers[1].id, "name:Unknown Actor")
        self.assertEqual(item.performers[1].name, "Unknown Actor")
        self.assertIsNone(item.performers[1].photo_url)

    def test_paginated_response_paging_math(self):
        """Pagination calculates has_more accurately."""
        paged = PaginatedVideos(items=[], total_count=100, offset=0, limit=50, has_more=True)
        self.assertTrue(paged.has_more)
        self.assertEqual(paged.to_dict()["has_more"], True)

        paged_end = PaginatedVideos(items=[], total_count=100, offset=100, limit=50, has_more=False)
        self.assertFalse(paged_end.has_more)
        self.assertEqual(paged_end.to_dict()["has_more"], False)

    def test_channel_alias_normalization(self):
        """Channel alias helpers normalize -100 prefixes properly."""
        self.assertEqual(canonical_channel_id("-100123456789"), "123456789")
        self.assertEqual(canonical_channel_id("123456789"), "123456789")
        aliases = channel_aliases("-100123456789")
        self.assertIn("-100123456789", aliases)
        self.assertIn("123456789", aliases)

    def test_deterministic_sort_tie_breaker_and_no_random(self):
        """All supported client sort orders include deterministic tie-breaker v.id ASC; unseeded random omitted."""
        self.assertNotIn("random", _CLIENT_SORT_CLAUSES)
        for sort_key, clause in _CLIENT_SORT_CLAUSES.items():
            self.assertTrue(clause.endswith("v.id ASC"), f"Sort '{sort_key}' must have deterministic v.id ASC tie-breaker")

    def test_client_sort_clause_directions_and_canonical_tiebreak(self):
        """client_sort_clause supports asc and desc while maintaining deterministic tie-breaker v.id ASC."""
        for sort_key in ("latest", "title", "duration"):
            for direction in (None, "asc", "desc", "ASC", "DESC"):
                clause = client_sort_clause(sort_key, direction, effective_title="v.title")
                self.assertTrue(
                    clause.endswith("v.id ASC"),
                    f"Sort '{sort_key}' with direction '{direction}' must end with v.id ASC",
                )
        # Default direction checks
        self.assertIn("DESC", client_sort_clause("latest", effective_title="v.title"))
        self.assertIn("DESC", client_sort_clause("duration", effective_title="v.title"))
        self.assertIn("ASC", client_sort_clause("title", effective_title="v.title"))

        # Explicit direction overrides
        self.assertIn("ASC", client_sort_clause("latest", "asc", effective_title="v.title"))
        self.assertIn("DESC", client_sort_clause("title", "desc", effective_title="v.title"))
        self.assertIn("ASC", client_sort_clause("duration", "asc", effective_title="v.title"))

    def test_row_val_record_wrapper_and_json_types(self):
        """_row_val and serialize_client_video work with common.db.Record row wrapper and JSON types."""
        from common.db import Record
        columns = [
            "id", "title", "duration", "date_released", "first_seen",
            "studio", "plot", "categories", "tags", "performers", "file_size_bytes",
        ]
        values = [
            "telegram:-100123:1:100",
            "Wrapped Video Title",
            1200,
            "2026-06-01",
            1760000000,
            "Studio Name",
            "Some plot summary",
            '["Category X", "Category Y"]',
            '["Tag A", "Tag B"]',
            '["Star Performer"]',
            524288000,
        ]
        rec = Record(values, columns)

        item = serialize_client_video(
            rec,
            performers_catalog_map={"star performer": "stashdb:uuid-star"},
        )
        self.assertEqual(item.id, "telegram:-100123:1:100")
        self.assertEqual(item.title, "Wrapped Video Title")
        self.assertEqual(item.duration_seconds, 1200)
        self.assertEqual(item.release_date, "2026-06-01")
        self.assertEqual(item.studio, "Studio Name")
        self.assertEqual(item.plot, "Some plot summary")
        self.assertEqual(item.categories, ["Category X", "Category Y"])
        self.assertEqual(item.tags, ["Tag A", "Tag B"])
        self.assertEqual(len(item.performers), 1)
        self.assertEqual(item.performers[0].id, "stashdb:uuid-star")
        self.assertEqual(item.performers[0].name, "Star Performer")
        self.assertEqual(item.file_size_bytes, 524288000)

    def test_row_val_plain_tuple_fallback(self):
        """_row_val extracts fields from plain tuples via fixed projection mapping."""
        values = (
            "telegram:-100123:2:200",
            "Tuple Video Title",
            600,
            "2026-07-01",
            1760000000,
            "Tuple Studio",
            "Tuple Plot",
            '["Cat1"]',
            '["Tag1"]',
            '["Actor1"]',
            1048576,
        )
        item = serialize_client_video(values)
        self.assertEqual(item.id, "telegram:-100123:2:200")
        self.assertEqual(item.title, "Tuple Video Title")
        self.assertEqual(item.duration_seconds, 600)
        self.assertEqual(item.categories, ["Cat1"])
        self.assertEqual(item.tags, ["Tag1"])
        self.assertEqual(len(item.performers), 1)
        self.assertEqual(item.performers[0].name, "Actor1")


class TestClientCatalogPostgresIntegration(PgTestCase):
    """PostgreSQL integration tests against disposable database using repo wrapper and fixture conventions."""

    def test_one_visible_canonical_item_safe_fields(self):
        """Focused test for one visible canonical item returning allowlisted safe fields."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (
                    id, provider, title, duration, date_released, first_seen,
                    studio, plot, categories, tags, performers, playable, file_size_bytes
                ) VALUES (
                    'telegram:-100123456789:100:1000',
                    'telegram',
                    'Visible Canonical Video',
                    1800,
                    '2026-05-15',
                    '2026-10-01 14:00:00+00',
                    'Main Studio',
                    'Plot text for visible video',
                    '["Category A", "Category B"]'::jsonb,
                    '["Tag 1", "Tag 2"]'::jsonb,
                    '["Performer One"]'::jsonb,
                    1,
                    1500000000
                )
            """)

        # Query catalog videos
        res = query_catalog_videos(self.conn)
        self.assertEqual(res.total_count, 1)
        self.assertEqual(len(res.items), 1)
        self.assertFalse(res.has_more)

        item = res.items[0]
        self.assertEqual(item.id, "telegram:-100123456789:100:1000")
        self.assertEqual(item.title, "Visible Canonical Video")
        self.assertEqual(item.duration_seconds, 1800)
        self.assertEqual(item.release_date, "2026-05-15")
        self.assertEqual(item.date_added, "2026-10-01T14:00:00Z")
        self.assertEqual(item.studio, "Main Studio")
        self.assertEqual(item.plot, "Plot text for visible video")
        self.assertEqual(item.categories, ["Category A", "Category B"])
        self.assertEqual(item.tags, ["Tag 1", "Tag 2"])
        self.assertEqual(len(item.performers), 1)
        self.assertEqual(item.performers[0].name, "Performer One")
        self.assertEqual(item.file_size_bytes, 1500000000)
        self.assertEqual(item.poster_url, "/api/v1/artwork/video/telegram%3A-100123456789%3A100%3A1000/poster")
        self.assertEqual(item.backdrop_url, "/api/v1/artwork/video/telegram%3A-100123456789%3A100%3A1000/backdrop")

        # Serialized dict contains only safe fields
        serialized = item.to_dict()
        self.assertNotIn("provider", serialized)
        self.assertNotIn("local_files", serialized)
        self.assertNotIn("playable", serialized)

    def test_multiple_sources_and_source_aware_channel_membership(self):
        """Test canonical videos with multiple sources and source-aware channel membership."""
        with self.conn:
            # 1. Canonical keeper video in Channel Alpha (-100111)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable, first_seen)
                VALUES ('telegram:-100111:10:1000', 'telegram', 'Canonical Keeper', 1200, 1, '2026-10-01 10:00:00+00')
            """)
            # 2. Retired duplicate loser in Channel Beta (-100222)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable, first_seen)
                VALUES ('telegram:-100222:20:2000', 'telegram', 'Retired Loser', 1200, 1, '2026-10-01 10:00:00+00')
            """)
            # 3. Active duplicate decision: loser -> keeper
            self.conn.execute("""
                INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at)
                VALUES ('telegram:-100222:20:2000', 'telegram:-100111:10:1000', 'op_merge_1', 'active', '2026-10-01 11:00:00+00')
            """)
            # 4. Multi-source entry: Canonical keeper also has Channel Beta as an active source
            self.conn.execute("""
                INSERT INTO video_sources (video_id, source_id, provider, channel_id, priority, is_active, created_at)
                VALUES ('telegram:-100111:10:1000', 'src_beta_1', 'telegram', '-100222', 1, 1, '2026-10-01 11:00:00+00')
            """)

        # (a) Unfiltered browse: returns ONLY canonical keeper, loser is excluded
        browse_all = query_catalog_videos(self.conn)
        self.assertEqual(browse_all.total_count, 1)
        self.assertEqual(browse_all.items[0].id, "telegram:-100111:10:1000")

        # (b) Filter by primary channel Alpha (-100111): returns keeper
        browse_alpha = query_catalog_videos(self.conn, channel_id="-100111")
        self.assertEqual(browse_alpha.total_count, 1)
        self.assertEqual(browse_alpha.items[0].id, "telegram:-100111:10:1000")

        # (c) Filter by secondary source channel Beta (-100222): source-aware membership returns keeper
        browse_beta = query_catalog_videos(self.conn, channel_id="-100222")
        self.assertEqual(browse_beta.total_count, 1)
        self.assertEqual(browse_beta.items[0].id, "telegram:-100111:10:1000")

        # (d) Channel counting reflects membership in both channels
        config_sources = [
            {"id": "-100111", "studio": "Studio Alpha", "enabled": True},
            {"id": "-100222", "studio": "Studio Beta", "enabled": True},
        ]
        channels_res = query_catalog_channels(self.conn, config_sources=config_sources, sort="title")
        self.assertEqual(channels_res.total_count, 2)
        ch_map = {ch.id: ch for ch in channels_res.items}
        self.assertEqual(ch_map["-100111"].video_count, 1)
        self.assertEqual(ch_map["-100222"].video_count, 1)

    def test_channel_filter_count_and_details(self):
        """Channel catalog lists channels with counts and detail query returns accurate record."""
        config_sources = [
            {"id": "-100123", "studio": "Channel Alpha", "enabled": True},
            {"id": "-100456", "studio": "Channel Beta", "enabled": False},
        ]
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES
                    ('telegram:-100123:1:100', 'telegram', 'Alpha 1', 600, 1),
                    ('telegram:-100123:2:200', 'telegram', 'Alpha 2', 700, 1),
                    ('telegram:-100456:1:300', 'telegram', 'Beta 1', 800, 1)
            """)

        # Channel list sorted by video count descending
        res = query_catalog_channels(self.conn, config_sources=config_sources, sort="video_count")
        self.assertEqual(res.total_count, 2)
        self.assertEqual(res.items[0].id, "-100123")
        self.assertEqual(res.items[0].video_count, 2)
        self.assertTrue(res.items[0].is_enabled)
        self.assertEqual(res.items[1].id, "-100456")
        # Disabled channel does not leak count of its invisible videos
        self.assertEqual(res.items[1].video_count, 0)
        self.assertFalse(res.items[1].is_enabled)

        # Single channel detail query
        alpha_detail = query_catalog_channel_detail(self.conn, "-100123", config_sources=config_sources)
        self.assertIsNotNone(alpha_detail)
        self.assertEqual(alpha_detail.id, "-100123")
        self.assertEqual(alpha_detail.video_count, 2)
        self.assertEqual(alpha_detail.icon_url, "/api/v1/artwork/channel/-100123")

        # Nonexistent channel returns None
        none_detail = query_catalog_channel_detail(self.conn, "-100999999", config_sources=config_sources)
        self.assertIsNone(none_detail)

    def test_hidden_unknown_id_and_alias_resolution(self):
        """Unknown, hidden, unplayable, and dead stream videos are not returned; duplicate loser resolves to keeper."""
        with self.conn:
            # Visible keeper
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100100:1:100', 'telegram', 'Keeper Item', 1000, 1)
            """)
            # Retired loser mapped to keeper
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100100:2:200', 'telegram', 'Loser Item', 1000, 1)
            """)
            self.conn.execute("""
                INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at)
                VALUES ('telegram:-100100:2:200', 'telegram:-100100:1:100', 'op_alias', 'active', '2026-10-01 10:00:00+00')
            """)
            # Unplayable video (playable = 0)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100100:3:300', 'telegram', 'Unplayable Item', 1000, 0)
            """)
            # Dead stream video (compatible schema: video_id, reason, detected_at integer)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100100:4:400', 'telegram', 'Dead Item', 1000, 1)
            """)
            self.conn.execute("""
                INSERT INTO dead_streams (video_id, reason, detected_at)
                VALUES ('telegram:-100100:4:400', 'dead stream probe', 1700000000)
            """)
            # Quarantined / Hidden item (loser_id == keeper_id)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100100:5:500', 'telegram', 'Quarantined Item', 1000, 1)
            """)
            self.conn.execute("""
                INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at)
                VALUES ('telegram:-100100:5:500', 'telegram:-100100:5:500', 'op_quarantine', 'active', '2026-10-01 10:00:00+00')
            """)

        # (a) Unknown ID returns None
        self.assertIsNone(query_catalog_video_detail(self.conn, "telegram:-100100:999:999"))

        # (b) Querying retired loser ID resolves cleanly to canonical keeper
        loser_detail = query_catalog_video_detail(self.conn, "telegram:-100100:2:200")
        self.assertIsNotNone(loser_detail)
        self.assertEqual(loser_detail.id, "telegram:-100100:1:100")
        self.assertEqual(loser_detail.title, "Keeper Item")

        # (c) Unplayable video returns None
        self.assertIsNone(query_catalog_video_detail(self.conn, "telegram:-100100:3:300"))

        # (d) Dead stream video returns None
        self.assertIsNone(query_catalog_video_detail(self.conn, "telegram:-100100:4:400"))

        # (e) Quarantined / hidden video returns None
        self.assertIsNone(query_catalog_video_detail(self.conn, "telegram:-100100:5:500"))

    def test_metadata_overrides_semantics(self):
        """Metadata overrides take precedence over underlying source values in browse and detail."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, plot, studio, tags, duration, playable)
                VALUES (
                    'telegram:-100123:50:500',
                    'telegram',
                    'Original Title',
                    'Original Plot',
                    'Original Studio',
                    '["old_tag"]'::jsonb,
                    1200,
                    1
                )
            """)
            self.conn.execute("""
                INSERT INTO video_metadata_overrides (video_id, fields, revision, updated_at)
                VALUES (
                    'telegram:-100123:50:500',
                    '{"title": "Overridden Title", "plot": "Overridden Plot", "studio": "Overridden Studio", "tags": ["new_tag"]}'::jsonb,
                    1,
                    '2026-10-01 10:00:00+00'
                )
            """)

        # Detail query reflects overrides
        detail = query_catalog_video_detail(self.conn, "telegram:-100123:50:500")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.title, "Overridden Title")
        self.assertEqual(detail.plot, "Overridden Plot")
        self.assertEqual(detail.studio, "Overridden Studio")
        self.assertEqual(detail.tags, ["new_tag"])

        # Browse query search against overridden title matches
        search_res = query_catalog_videos(self.conn, q="Overridden Title")
        self.assertEqual(search_res.total_count, 1)
        self.assertEqual(search_res.items[0].title, "Overridden Title")

        # Search against replaced original title does not match
        old_search = query_catalog_videos(self.conn, q="Original Title")
        self.assertEqual(old_search.total_count, 0)

    def test_deterministic_ordering_and_exact_paging(self):
        """Exact offset-based pagination maintains deterministic order across pages."""
        with self.conn:
            for i in range(1, 6):
                self.conn.execute(f"""
                    INSERT INTO videos (id, provider, title, duration, first_seen, playable)
                    VALUES ('telegram:-100123:item_{i}:100', 'telegram', 'Item {i}', 1000, '2026-10-01 12:00:00+00', 1)
                """)

        # Page 1: limit 2, offset 0
        p1 = query_catalog_videos(self.conn, limit=2, offset=0, sort="latest")
        self.assertEqual(p1.total_count, 5)
        self.assertEqual(len(p1.items), 2)
        self.assertTrue(p1.has_more)
        p1_ids = [item.id for item in p1.items]

        # Page 2: limit 2, offset 2
        p2 = query_catalog_videos(self.conn, limit=2, offset=2, sort="latest")
        self.assertEqual(p2.total_count, 5)
        self.assertEqual(len(p2.items), 2)
        self.assertTrue(p2.has_more)
        p2_ids = [item.id for item in p2.items]

        # Page 3: limit 2, offset 4
        p3 = query_catalog_videos(self.conn, limit=2, offset=4, sort="latest")
        self.assertEqual(p3.total_count, 5)
        self.assertEqual(len(p3.items), 1)
        self.assertFalse(p3.has_more)
        p3_ids = [item.id for item in p3.items]

        # All items are unique across page boundaries (no duplicates, no skips)
        all_paged = p1_ids + p2_ids + p3_ids
        self.assertEqual(len(set(all_paged)), 5)
        self.assertEqual(len(all_paged), 5)

    def test_sql_ordering_across_adjacent_offset_pages_both_directions(self):
        """SQL ordering across adjacent offset pages preserves deterministic sequence in both directions."""
        with self.conn:
            # Seed 5 items with distinct titles, durations, and timestamps
            items = [
                ("telegram:-100123:item_a:100", "Apple Video", 600, "2026-01-01 10:00:00+00"),
                ("telegram:-100123:item_b:100", "Banana Video", 700, "2026-02-01 10:00:00+00"),
                ("telegram:-100123:item_c:100", "Cherry Video", 800, "2026-03-01 10:00:00+00"),
                ("telegram:-100123:item_d:100", "Date Video", 900, "2026-04-01 10:00:00+00"),
                ("telegram:-100123:item_e:100", "Elderberry Video", 1000, "2026-05-01 10:00:00+00"),
            ]
            for vid_id, title, duration, first_seen in items:
                self.conn.execute(f"""
                    INSERT INTO videos (id, provider, title, duration, first_seen, playable)
                    VALUES ('{vid_id}', 'telegram', '{title}', {duration}, '{first_seen}', 1)
                """)

        # 1. Title ASC across pages (limit 2, offset 0 then offset 2)
        p1 = query_catalog_videos(self.conn, sort="title", direction="asc", limit=2, offset=0)
        p2 = query_catalog_videos(self.conn, sort="title", direction="asc", limit=2, offset=2)
        self.assertEqual([i.title for i in p1.items], ["Apple Video", "Banana Video"])
        self.assertEqual([i.title for i in p2.items], ["Cherry Video", "Date Video"])
        self.assertTrue(p1.has_more)
        self.assertTrue(p2.has_more)

        # 2. Title DESC across pages
        p1_desc = query_catalog_videos(self.conn, sort="title", direction="desc", limit=2, offset=0)
        p2_desc = query_catalog_videos(self.conn, sort="title", direction="desc", limit=2, offset=2)
        self.assertEqual([i.title for i in p1_desc.items], ["Elderberry Video", "Date Video"])
        self.assertEqual([i.title for i in p2_desc.items], ["Cherry Video", "Banana Video"])

        # 3. Latest (first_seen) DESC across pages
        p1_lat_desc = query_catalog_videos(self.conn, sort="latest", direction="desc", limit=2, offset=0)
        p2_lat_desc = query_catalog_videos(self.conn, sort="latest", direction="desc", limit=2, offset=2)
        self.assertEqual([i.id for i in p1_lat_desc.items], ["telegram:-100123:item_e:100", "telegram:-100123:item_d:100"])
        self.assertEqual([i.id for i in p2_lat_desc.items], ["telegram:-100123:item_c:100", "telegram:-100123:item_b:100"])

        # 4. Latest (first_seen) ASC across pages
        p1_lat_asc = query_catalog_videos(self.conn, sort="latest", direction="asc", limit=2, offset=0)
        p2_lat_asc = query_catalog_videos(self.conn, sort="latest", direction="asc", limit=2, offset=2)
        self.assertEqual([i.id for i in p1_lat_asc.items], ["telegram:-100123:item_a:100", "telegram:-100123:item_b:100"])
        self.assertEqual([i.id for i in p2_lat_asc.items], ["telegram:-100123:item_c:100", "telegram:-100123:item_d:100"])

        # 5. Duration ASC across pages
        p1_dur_asc = query_catalog_videos(self.conn, sort="duration", direction="asc", limit=2, offset=0)
        p2_dur_asc = query_catalog_videos(self.conn, sort="duration", direction="asc", limit=2, offset=2)
        self.assertEqual([i.duration_seconds for i in p1_dur_asc.items], [600, 700])
        self.assertEqual([i.duration_seconds for i in p2_dur_asc.items], [800, 900])

        # 6. Duration DESC across pages
        p1_dur_desc = query_catalog_videos(self.conn, sort="duration", direction="desc", limit=2, offset=0)
        p2_dur_desc = query_catalog_videos(self.conn, sort="duration", direction="desc", limit=2, offset=2)
        self.assertEqual([i.duration_seconds for i in p1_dur_desc.items], [1000, 900])
        self.assertEqual([i.duration_seconds for i in p2_dur_desc.items], [800, 700])

    def test_regression_disabled_channel_with_active_alternate(self):
        """Regression: Disabled primary channel video remains visible if it has an active enabled alternate source;

        conversely, disabled primary videos without active alternate are excluded from browse, detail, and counts.
        """
        with self.conn:
            # Video 1: Primary in disabled channel -100111, NO alternate -> INVISIBLE
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100111:1:100', 'telegram', 'Disabled Only Video', 600, 1)
            """)
            # Video 2: Primary in disabled channel -100111, but has active alternate in enabled channel -100222 -> VISIBLE
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100111:2:200', 'telegram', 'Alternate Enabled Video', 700, 1)
            """)
            self.conn.execute("""
                INSERT INTO video_sources (video_id, source_id, provider, channel_id, priority, is_active, created_at)
                VALUES ('telegram:-100111:2:200', 'src_alt_1', 'telegram', '-100222', 1, 1, '2026-10-01 10:00:00+00')
            """)
            # Video 3: Primary in enabled channel -100222 -> VISIBLE
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100222:3:300', 'telegram', 'Enabled Primary Video', 800, 1)
            """)

        disabled_ids = {"-100111"}

        # 1. Browse: only Video 2 and Video 3 returned; Video 1 excluded
        browse = query_catalog_videos(self.conn, disabled_channel_ids=disabled_ids)
        self.assertEqual(browse.total_count, 2)
        returned_ids = {item.id for item in browse.items}
        self.assertIn("telegram:-100111:2:200", returned_ids)
        self.assertIn("telegram:-100222:3:300", returned_ids)
        self.assertNotIn("telegram:-100111:1:100", returned_ids)

        # 2. Detail: Video 2 is retrievable; Video 1 returns None
        v2_detail = query_catalog_video_detail(self.conn, "telegram:-100111:2:200", disabled_channel_ids=disabled_ids)
        self.assertIsNotNone(v2_detail)
        self.assertEqual(v2_detail.id, "telegram:-100111:2:200")

        v1_detail = query_catalog_video_detail(self.conn, "telegram:-100111:1:100", disabled_channel_ids=disabled_ids)
        self.assertIsNone(v1_detail)

        # 3. Channel counts: Video 1 does not leak into count of disabled channel
        config_sources = [
            {"id": "-100111", "studio": "Disabled Studio", "enabled": False},
            {"id": "-100222", "studio": "Enabled Studio", "enabled": True},
        ]
        channels = query_catalog_channels(self.conn, config_sources=config_sources)
        ch_map = {c.id: c.video_count for c in channels.items}
        self.assertEqual(ch_map["-100111"], 0)
        self.assertEqual(ch_map["-100222"], 2)

    def test_regression_duplicate_alias_channel_counts(self):
        """Regression: Video counts deduplicate canonical videos across channel alias variants;

        DB-only channels use deduplicated canonical totals without inflating counts.
        """
        with self.conn:
            # Video 1: Primary under -100555, and video_sources row under 555 (same channel alias)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100555:10:1000', 'telegram', 'Alias Video 1', 600, 1)
            """)
            self.conn.execute("""
                INSERT INTO video_sources (video_id, source_id, provider, channel_id, priority, is_active, created_at)
                VALUES ('telegram:-100555:10:1000', 'src_alias_1', 'telegram', '555', 1, 1, '2026-10-01 10:00:00+00')
            """)
            # Video 2: Primary under canonical alias 555
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:555:20:2000', 'telegram', 'Alias Video 2', 700, 1)
            """)

        # When channel is DB-only (not in config_sources), it appears under canonical ID '555'
        channels_res = query_catalog_channels(self.conn, config_sources=[])
        self.assertEqual(channels_res.total_count, 1)
        item = channels_res.items[0]
        self.assertEqual(item.id, "555")
        # Exactly 2 distinct canonical videos, not inflated to 3
        self.assertEqual(item.video_count, 2)

        # Single detail query handles both -100555 and 555
        detail_dash = query_catalog_channel_detail(self.conn, "-100555", config_sources=[])
        self.assertIsNotNone(detail_dash)
        self.assertEqual(detail_dash.video_count, 2)

        detail_canon = query_catalog_channel_detail(self.conn, "555", config_sources=[])
        self.assertIsNotNone(detail_canon)
        self.assertEqual(detail_canon.video_count, 2)

    def test_performer_list_exposes_artwork_only_when_a_photo_is_stored(self):
        """Artwork is advertised only when the artwork route can actually serve it: a performer
        with a local thumbnail gets the URL, a performer with an upstream URL gets None."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO performer_catalog (id, source, source_id, name, active)
                VALUES ('stashdb:uuid-photo', 'stashdb', 'photo', 'Photo Star', true),
                       ('stashdb:uuid-nophoto', 'stashdb', 'nophoto', 'No Photo Star', true)
            """)
            self.conn.execute("""
                INSERT INTO performer_photos (name, thumb)
                VALUES ('Photo Star', 'thumbs/photo-star.jpg'),
                       ('No Photo Star', 'https://cdn.example.test/no-photo-star.webp')
            """)
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable, performers)
                VALUES
                    ('telegram:-100123:91:901', 'telegram', 'Photo Star Video', 900, 1, '["Photo Star"]'::jsonb),
                    ('telegram:-100123:92:902', 'telegram', 'No Photo Video', 900, 1, '["No Photo Star"]'::jsonb)
            """)

        page = query_catalog_performers(self.conn, limit=50)
        by_name = {item.name: item for item in page.items}
        self.assertEqual(
            by_name["Photo Star"].photo_url,
            "/api/v1/artwork/performer/stashdb%3Auuid-photo",
        )
        self.assertIsNone(by_name["No Photo Star"].photo_url)

    def test_regression_photo_url_leakage_and_strict_origin(self):
        """Regression: Stored performer photo_url or upstream URL is never passed into client DTO;

        only generated same-origin artwork when a real identifier exists.
        """
        with self.conn:
            # Insert performer into catalog
            self.conn.execute("""
                INSERT INTO performer_catalog (id, source, source_id, name, active)
                VALUES ('stashdb:uuid-777', 'stashdb', '777', 'Vetted Star', true)
            """)
            # Schema-valid performer text array in PostgreSQL videos table
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable, performers)
                VALUES (
                    'telegram:-100123:90:900',
                    'telegram',
                    'Security Test Video',
                    900,
                    1,
                    '["Vetted Star", "Unvetted Extra"]'::jsonb
                )
            """)

        # Query video detail
        detail = query_catalog_video_detail(self.conn, "telegram:-100123:90:900")
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail.performers), 2)

        p1 = detail.performers[0]
        self.assertEqual(p1.id, "stashdb:uuid-777")
        self.assertEqual(p1.name, "Vetted Star")
        # Must be same-origin generated artwork URL
        self.assertEqual(p1.photo_url, "/api/v1/artwork/performer/stashdb%3Auuid-777")

        p2 = detail.performers[1]
        self.assertEqual(p2.id, "name:Unvetted Extra")
        self.assertEqual(p2.name, "Unvetted Extra")
        # No real identifier -> photo_url is None
        self.assertIsNone(p2.photo_url)

        # Check raw serialized dictionary
        raw_dict = detail.to_dict()
        dict_str = str(raw_dict)
        self.assertNotIn("https://leaked-upstream-cdn.org", dict_str)
        self.assertNotIn("file:///var/secret", dict_str)

        # Direct serialization with risky synthetic inputs (upstream URLs and file paths)
        synthetic_row = {
            "id": "telegram:-100123:90:900",
            "title": "Security Test Video",
            "duration": 900,
            "performers": [
                {"id": "stashdb:uuid-777", "name": "Vetted Star", "photo_url": "https://leaked-upstream-cdn.org/actor.jpg"},
                {"id": "", "name": "Unvetted Extra", "photo_url": "file:///var/secret/path.jpg"},
            ],
        }
        # Mirror _lookup_performers_catalog: vetted performers are keyed by canonical id and lowercased name.
        synthetic_dto = serialize_client_video(
            synthetic_row,
            performers_catalog_map={
                "stashdb:uuid-777": ("stashdb:uuid-777", "Vetted Star"),
                "vetted star": ("stashdb:uuid-777", "Vetted Star"),
            },
        )
        self.assertEqual(len(synthetic_dto.performers), 2)
        self.assertEqual(synthetic_dto.performers[0].photo_url, "/api/v1/artwork/performer/stashdb%3Auuid-777")
        self.assertIsNone(synthetic_dto.performers[1].photo_url)
        synthetic_dict_str = str(synthetic_dto.to_dict())
        self.assertNotIn("https://leaked-upstream-cdn.org", synthetic_dict_str)
        self.assertNotIn("file:///var/secret", synthetic_dict_str)

    def test_regression_secondary_source_telegram_provider_only(self):
        """Regression: Secondary source membership strictly requires Telegram provider (no other providers)."""
        with self.conn:
            # Video 1: Primary in channel -100111, secondary source in -100222 but provider is 'other' (not 'telegram')
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100111:1:100', 'telegram', 'Non-Telegram Source Video', 600, 1)
            """)
            self.conn.execute("""
                INSERT INTO video_sources (video_id, source_id, provider, channel_id, priority, is_active, created_at)
                VALUES ('telegram:-100111:1:100', 'src_other_1', 'other_provider', '-100222', 1, 1, '2026-10-01 10:00:00+00')
            """)

        # Channel count for -100222 must be 0 because the source is not Telegram
        config_sources = [
            {"id": "-100111", "studio": "Studio Alpha", "enabled": True},
            {"id": "-100222", "studio": "Studio Beta", "enabled": True},
        ]
        channels = query_catalog_channels(self.conn, config_sources=config_sources)
        ch_map = {c.id: c.video_count for c in channels.items}
        self.assertEqual(ch_map["-100222"], 0)
        self.assertEqual(ch_map["-100111"], 1)

        # Filtering by channel -100222 does not return the video
        browse_beta = query_catalog_videos(self.conn, channel_id="-100222", config_sources=config_sources)
        self.assertEqual(browse_beta.total_count, 0)

    def test_regression_exact_studio_matching(self):
        """Regression: Studio filter uses exact matching (case-insensitive), not substring matching."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, studio, duration, playable)
                VALUES
                    ('telegram:-100123:1:100', 'telegram', 'Alpha Video', 'Studio Alpha', 600, 1),
                    ('telegram:-100123:2:200', 'telegram', 'Alpha Deluxe Video', 'Studio Alpha Deluxe', 600, 1),
                    ('telegram:-100123:3:300', 'telegram', 'Prefix Video', 'The Studio Alpha', 600, 1)
            """)

        # Filtering for "Studio Alpha" must only return the exact match, not substring variations
        res = query_catalog_videos(self.conn, studio="Studio Alpha")
        self.assertEqual(res.total_count, 1)
        self.assertEqual(res.items[0].id, "telegram:-100123:1:100")

        # Case-insensitive exact match
        res_lower = query_catalog_videos(self.conn, studio="studio alpha")
        self.assertEqual(res_lower.total_count, 1)
        self.assertEqual(res_lower.items[0].id, "telegram:-100123:1:100")

    def test_regression_search_does_not_match_raw_performer_or_tag_json(self):
        """Regression: Search q operates only on public text fields, never matching raw performer or tag JSON."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, plot, studio, performers, tags, duration, playable)
                VALUES (
                    'telegram:-100123:10:100',
                    'telegram',
                    'Public Title',
                    'Public Plot Text',
                    'Public Studio',
                    '["secret_performer_marker_12345"]'::jsonb,
                    '["secret_internal_tag_98765"]'::jsonb,
                    600,
                    1
                )
            """)

        # Searching for performer stored in performer array must NOT match
        performer_res = query_catalog_videos(self.conn, q="secret_performer_marker_12345")
        self.assertEqual(performer_res.total_count, 0)

        # Searching for tag value in raw JSON must NOT match
        tag_res = query_catalog_videos(self.conn, q="secret_internal_tag_98765")
        self.assertEqual(tag_res.total_count, 0)

        # Searching public text matches correctly
        title_res = query_catalog_videos(self.conn, q="Public Title")
        self.assertEqual(title_res.total_count, 1)

    def test_regression_configured_channel_alias_deduplication_and_stable_id(self):
        """Regression: Duplicate configured channel aliases are deduplicated with stable list and detail IDs."""
        config_sources = [
            {"id": "-100888", "studio": "Studio Eight", "enabled": True},
            {"id": "888", "studio": "Studio Eight Alias", "enabled": True},
        ]
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, playable)
                VALUES ('telegram:-100888:1:100', 'telegram', 'Eight Video 1', 600, 1)
            """)

        # List channels: only 1 channel returned, not 2
        channels_res = query_catalog_channels(self.conn, config_sources=config_sources)
        self.assertEqual(channels_res.total_count, 1)
        list_channel = channels_res.items[0]
        self.assertEqual(list_channel.id, "-100888")

        # Detail query via alias "888" returns stable ID "-100888" matching list
        detail_via_alias = query_catalog_channel_detail(self.conn, "888", config_sources=config_sources)
        self.assertIsNotNone(detail_via_alias)
        self.assertEqual(detail_via_alias.id, "-100888")
        self.assertEqual(detail_via_alias.id, list_channel.id)
