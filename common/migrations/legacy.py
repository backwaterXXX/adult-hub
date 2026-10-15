"""Pre-baseline migrations kept only to upgrade databases already using them."""

from common.migrations.m0001_initial_schema import migration as m0001
from common.migrations.m0002_duplicate_jobs_and_sources import migration as m0002
from common.migrations.m0003_telegram_channel_state import migration as m0003
from common.migrations.m0004_performer_enrichment import migration as m0004
from common.migrations.m0005_stream_cache_and_diagnostics import migration as m0005
from common.migrations.m0006_data_normalizations import migration as m0006
from common.migrations.m0007_relational_metadata import migration as m0007
from common.migrations.m0008_timestamptz import migration as m0008

LEGACY_MIGRATIONS = (
    m0001,
    m0002,
    m0003,
    m0004,
    m0005,
    m0006,
    m0007,
    m0008,
)
