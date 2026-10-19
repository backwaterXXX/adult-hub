"""Reject catalog rows for videos shorter than seven minutes."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        ALTER TABLE videos
        ADD CONSTRAINT videos_minimum_duration_seconds
        CHECK (duration IS NULL OR duration <= 0 OR duration >= 420)
    """)
    conn.execute("""
        CREATE OR REPLACE FUNCTION reject_unverified_short_video_duration()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.duration IS NULL OR NEW.duration < 420 THEN
                    RAISE EXCEPTION 'video duration must be known and at least 420 seconds'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.duration IS DISTINCT FROM OLD.duration THEN
                IF NEW.duration IS NULL OR NEW.duration < 420 THEN
                    RAISE EXCEPTION 'video duration must be known and at least 420 seconds'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    conn.execute("""
        CREATE TRIGGER videos_minimum_duration_insert
        BEFORE INSERT ON videos
        FOR EACH ROW EXECUTE FUNCTION reject_unverified_short_video_duration()
    """)
    conn.execute("""
        CREATE TRIGGER videos_minimum_duration_update
        BEFORE UPDATE OF duration ON videos
        FOR EACH ROW EXECUTE FUNCTION reject_unverified_short_video_duration()
    """)


migration = Migration(
    version=44,
    name="0044_minimum_video_duration",
    description="Enforce a 420-second minimum duration for known video lengths",
    apply_fn=apply,
)
