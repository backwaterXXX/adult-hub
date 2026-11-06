"""Historical migrations kept only to upgrade databases that already used them.

`LEGACY_MIGRATIONS` is the pre-baseline chain (versions 1-8). The current
baseline (version 1, `0001_baseline`) creates the same schema from
`schema_baseline.sql`, so these run only for databases whose recorded history
proves they started before the baseline existed.

`POST_BASELINE_MIGRATIONS` is the compatibility chain for databases that
recorded the historical post-baseline upgrades (versions 10-62). They still
upgrade through it; a database built by the current baseline never sees them.
The migrations stay in their own files for exactly that reason, and ordering
is load-bearing (m0039 drops the table m0035 creates).
"""

from common.migrations.base import Migration
from common.migrations.m0001_initial_schema import migration as m0001
from common.migrations.m0002_duplicate_jobs_and_sources import migration as m0002
from common.migrations.m0003_telegram_channel_state import migration as m0003
from common.migrations.m0004_performer_enrichment import migration as m0004
from common.migrations.m0005_stream_cache_and_diagnostics import migration as m0005
from common.migrations.m0006_data_normalizations import migration as m0006
from common.migrations.m0007_relational_metadata import migration as m0007
from common.migrations.m0008_timestamptz import migration as m0008
from common.migrations.m0010_admin_operation_plans import migration as admin_operation_plans
from common.migrations.m0011_video_metadata_overrides import migration as video_metadata_overrides
from common.migrations.m0012_video_availability_checks import migration as video_availability_checks
from common.migrations.m0013_admin_audit_items import migration as admin_audit_items
from common.migrations.m0014_admin_schedules import migration as admin_schedules
from common.migrations.m0015_admin_audit_nfo_findings import migration as admin_audit_nfo_findings
from common.migrations.m0016_admin_audit_jellyfin_state import migration as admin_audit_jellyfin_state
from common.migrations.m0017_admin_audit_artwork import migration as admin_audit_artwork
from common.migrations.m0018_admin_action_events import migration as admin_action_events
from common.migrations.m0019_admin_change_events import migration as admin_change_events
from common.migrations.m0020_admin_audit_operations_schedules import migration as admin_audit_operations_schedules
from common.migrations.m0021_performer_catalog import migration as performer_catalog
from common.migrations.m0022_telegram_performer_rebuild_backup import migration as performer_rebuild_backup
from common.migrations.m0023_admin_quality_center import migration as admin_quality_center
from common.migrations.m0024_admin_orphan_quarantine import migration as admin_orphan_quarantine
from common.migrations.m0025_admin_quality_acknowledgements import migration as admin_quality_acknowledgements
from common.migrations.m0026_external_metadata_attempts import migration as external_metadata_attempts
from common.migrations.m0027_video_fingerprints import migration as video_fingerprints
from common.migrations.m0028_phash_provenance import migration as phash_provenance
from common.migrations.m0029_oshash_media_probe import migration as oshash_media_probe
from common.migrations.m0030_video_duplicate_decision_operation_index import migration as video_duplicate_decision_operation_index
from common.migrations.m0031_video_scene_match_provenance import migration as video_scene_match_provenance
from common.migrations.m0032_history_retention_indexes import migration as history_retention_indexes
from common.migrations.m0033_video_duplicate_rules_provenance import migration as video_duplicate_rules_provenance
from common.migrations.m0034_video_phash_thumbnails import migration as video_phash_thumbnails
from common.migrations.m0035_fuzzy_performer_reviews import migration as fuzzy_performer_reviews
from common.migrations.m0036_metadata_review_proposals import migration as metadata_review_proposals
from common.migrations.m0037_video_phash_jobs import migration as video_phash_jobs
from common.migrations.m0038_video_phash_job_processing_time import migration as video_phash_job_processing_time
from common.migrations.m0039_drop_fuzzy_performer_reviews import migration as drop_fuzzy_performer_reviews
from common.migrations.m0040_performer_profile_overrides import migration as performer_profile_overrides
from common.migrations.m0041_performer_external_links import migration as performer_external_links
from common.migrations.m0042_telegram_performer_resolution import migration as telegram_performer_resolution
from common.migrations.m0043_telegram_unavailable_quarantine import migration as telegram_unavailable_quarantine
from common.migrations.m0044_minimum_video_duration import migration as minimum_video_duration
from common.migrations.m0045_external_metadata_error_detail import migration as external_metadata_error_detail
from common.migrations.m0046_drop_stream_probe_columns import migration as drop_stream_probe_columns
from common.migrations.m0047_video_file_size import migration as video_file_size
from common.migrations.m0048_video_phash_job_error_detail import migration as video_phash_job_error_detail
from common.migrations.m0049_catalog_automation import migration as catalog_automation
from common.migrations.m0050_duplicate_reconcile_schedule import migration as duplicate_reconcile_schedule
from common.migrations.m0051_video_visual_duplicates import migration as video_visual_duplicates
from common.migrations.m0052_video_visual_timestamps import migration as video_visual_timestamps
from common.migrations.m0053_visual_history import migration as video_visual_history
from common.migrations.m0054_telegram_latest_cursor import migration as telegram_latest_cursor
from common.migrations.m0055_duplicate_reconcile_opt_in import migration as duplicate_reconcile_opt_in
from common.migrations.m0056_performer_alias_canonical_index import (
    migration as performer_alias_canonical_index,
)
from common.migrations.m0057_split_phash_jobs import migration as split_phash_jobs
from common.migrations.m0058_cron_schedules import migration as cron_schedules
from common.migrations.m0059_metadata_proposal_retries import migration as metadata_proposal_retries
from common.migrations.m0060_video_job_lease_token import migration as video_job_lease_token
from common.migrations.m0061_schedule_stages import migration as schedule_stages
from common.migrations.m0062_artwork_generation_schedule import migration as artwork_generation_schedule
from common.migrations.m0063_source_removal_indexes import migration as source_removal_indexes
from common.migrations.m0064_video_duplicate_decisions_evidence import (
    migration as video_duplicate_decisions_evidence,
)

LEGACY_MIGRATIONS: tuple[Migration, ...] = (
    m0001,
    m0002,
    m0003,
    m0004,
    m0005,
    m0006,
    m0007,
    m0008,
)

POST_BASELINE_MIGRATIONS: tuple[Migration, ...] = (
    admin_operation_plans,
    video_metadata_overrides,
    video_availability_checks,
    admin_audit_items,
    admin_schedules,
    admin_audit_nfo_findings,
    admin_audit_jellyfin_state,
    admin_audit_artwork,
    admin_action_events,
    admin_change_events,
    admin_audit_operations_schedules,
    performer_catalog,
    performer_rebuild_backup,
    admin_quality_center,
    admin_orphan_quarantine,
    admin_quality_acknowledgements,
    external_metadata_attempts,
    video_fingerprints,
    phash_provenance,
    oshash_media_probe,
    video_duplicate_decision_operation_index,
    video_scene_match_provenance,
    history_retention_indexes,
    video_duplicate_rules_provenance,
    video_phash_thumbnails,
    fuzzy_performer_reviews,
    metadata_review_proposals,
    video_phash_jobs,
    video_phash_job_processing_time,
    drop_fuzzy_performer_reviews,
    performer_profile_overrides,
    performer_external_links,
    telegram_performer_resolution,
    telegram_unavailable_quarantine,
    minimum_video_duration,
    external_metadata_error_detail,
    drop_stream_probe_columns,
    video_file_size,
    video_phash_job_error_detail,
    catalog_automation,
    duplicate_reconcile_schedule,
    video_visual_duplicates,
    video_visual_timestamps,
    video_visual_history,
    telegram_latest_cursor,
    duplicate_reconcile_opt_in,
    performer_alias_canonical_index,
    split_phash_jobs,
    cron_schedules,
    metadata_proposal_retries,
    video_job_lease_token,
    schedule_stages,
    artwork_generation_schedule,
    source_removal_indexes,
    video_duplicate_decisions_evidence,
)
