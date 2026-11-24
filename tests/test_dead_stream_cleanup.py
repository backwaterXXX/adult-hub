"""Tests for the dead-stream cleanup helpers."""

from __future__ import annotations

from pg_helper import PgTestCase

from tools.cleanup_dead_streams import (
    collect_dead_stream_report,
    drop_orphan_dead_streams,
    mark_dead_streams_unplayable,
)


class DeadStreamCleanupTests(PgTestCase):
    def _video(self, video_id: str, *, playable: int | None = 1) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration, playable) "
                "VALUES (%s, 'telegram', %s, 600, %s)",
                (video_id, video_id, playable),
            )

    def _dead(self, video_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO dead_streams (video_id, reason, detected_at) "
                "VALUES (%s, 'probe', now())",
                (video_id,),
            )

    def test_report_counts_orphans_and_playable_drift(self):
        self._video("telegram:a")
        self._video("telegram:b", playable=0)
        self._dead("telegram:a")      # dead but still playable
        self._dead("telegram:b")      # dead and already off
        self._dead("telegram:gone")   # orphan row

        report = collect_dead_stream_report(self.conn)

        self.assertEqual(report["dead_streams"], 3)
        self.assertEqual(report["orphans"], 1)
        self.assertEqual(report["dead_but_playable"], 1)
        self.assertEqual(report["unplayable_without_row"], 0)

    def test_dry_run_reports_without_writing(self):
        self._video("telegram:a")
        self._dead("telegram:a")
        self._dead("telegram:gone")

        self.assertEqual(drop_orphan_dead_streams(self.conn), 1)
        self.assertEqual(mark_dead_streams_unplayable(self.conn), 1)

        report = collect_dead_stream_report(self.conn)
        self.assertEqual(report["orphans"], 1)
        self.assertEqual(report["dead_but_playable"], 1)

    def test_apply_drops_orphans_and_marks_videos_unplayable(self):
        self._video("telegram:a")
        self._dead("telegram:a")
        self._dead("telegram:gone")
        self._video("telegram:manual", playable=0)  # unplayable without a dead row

        self.assertEqual(drop_orphan_dead_streams(self.conn, apply=True), 1)
        self.assertEqual(mark_dead_streams_unplayable(self.conn, apply=True), 1)

        report = collect_dead_stream_report(self.conn)
        self.assertEqual(report["dead_streams"], 1)
        self.assertEqual(report["orphans"], 0)
        self.assertEqual(report["dead_but_playable"], 0)
        self.assertEqual(report["unplayable_without_row"], 1)

        playable = self.conn.execute(
            "SELECT playable FROM videos WHERE id = %s", ("telegram:a",)
        ).fetchone()[0]
        self.assertEqual(playable, 0)
