"""Canonical maintenance-job stage names shared by the sync worker and resolver API."""

WORKING_STAGES = frozenset({
    "starting", "validating", "preparing_files", "moving_files", "saving_catalog",
    "restoring_files", "merging_performers", "refreshing_collections", "refreshing_jellyfin",
    "applying_metadata", "checking_availability", "auditing_library", "auditing_metadata",
    "checking_jellyfin_import_state", "creating_backup", "testing_backup_restore",
    "repairing_nfo", "writing_nfo", "scanning_metadata", "downloading_artwork", "generating_artwork",
    "metadata_pbc", "metadata_photos", "metadata_tpdb", "metadata_stashdb", "metadata_scenes",
})

# Job states that may appear in persisted progress as final markers; they are not
# recordable stages for _record_job_progress.
TERMINAL_STAGES = frozenset({"completed", "cancelled"})

ALL_STAGES = WORKING_STAGES | TERMINAL_STAGES
