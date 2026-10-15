"""Shared allowlist for Admin v2 catalog quality projections."""

QUALITY_KINDS = (
    "missing_metadata", "missing_nfo", "invalid_nfo", "nfo_metadata_mismatch", "missing_artwork",
)
QUALITY_FIELDS = ("title", "performers", "studio", "plot", "thumbnail")
QUALITY_REVIEW_FIELDS = ("title", "performers", "studio", "plot", "tags", "date_released")
QUALITY_DIFF_FIELDS = (
    "Title", "Original title", "Studio", "Release date", "Year", "Description",
    "Runtime", "Genres", "Tags", "Collections", "Cast",
)
