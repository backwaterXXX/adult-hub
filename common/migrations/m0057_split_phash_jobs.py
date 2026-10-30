"""Keep historical combined jobs while supporting independent outputs."""
from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("ALTER TABLE video_phash_jobs ADD COLUMN IF NOT EXISTS job_kind TEXT NOT NULL DEFAULT 'combined'")
    conn.execute("ALTER TABLE video_phash_jobs DROP CONSTRAINT IF EXISTS video_phash_jobs_video_id_algorithm_version_decoder_profile_key")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS video_phash_jobs_identity_kind_idx ON video_phash_jobs (video_id, algorithm_version, decoder_profile, job_kind)")
    conn.execute("ALTER TABLE video_phash_jobs DROP CONSTRAINT IF EXISTS video_phash_jobs_kind_check")
    conn.execute("ALTER TABLE video_phash_jobs ADD CONSTRAINT video_phash_jobs_kind_check CHECK (job_kind IN ('combined','phash','thumbnail'))")


migration = Migration(57, '0057_split_phash_jobs', 'Separate fingerprint and thumbnail jobs', apply)
current_migration = Migration(9, '0009_split_phash_jobs', 'Separate fingerprint and thumbnail jobs', apply)
