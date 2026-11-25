"""Tests for the durable videos.file_size_bytes column and its fill paths."""

import unittest


from pg_helper import PgTestCase

import sync.scene_scrapers as scene_scrapers


class VideoFileSizeTests(PgTestCase):
    def _video(self, video_id: str = "telegram:123"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration) VALUES (%s, 'telegram', 'T', 600)",
                (video_id,))
        return video_id

    def _add_probe(self, video_id: str, size, algorithm: str = "OSHASH"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_fingerprints (video_id, algorithm, fingerprint, media_probe_data) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (video_id, algorithm, "0123456789abcdef",
                 f'{{"file_size_bytes": {size}}}' if isinstance(size, int) else '{}'),
            )

    def test_store_fingerprint_propagates_size_to_videos(self):
        video_id = self._video()
        scene_scrapers._store_video_fingerprint(
            self.conn, video_id, "OSHASH", "0123456789abcdef",
            media_probe={"status": "complete", "bytes": 262144,
                         "data": {"file_size_bytes": 4200000}})
        self.assertEqual(
            self.conn.execute("SELECT file_size_bytes FROM videos WHERE id=%s",
                              (video_id,)).fetchone()[0], 4200000)

    def test_store_fingerprint_without_probe_leaves_size_null(self):
        video_id = self._video()
        scene_scrapers._store_video_fingerprint(
            self.conn, video_id, "OSHASH", "0123456789abcdef")
        self.assertIsNone(
            self.conn.execute("SELECT file_size_bytes FROM videos WHERE id=%s",
                              (video_id,)).fetchone()[0])

    def test_phash_size_sink_fills_only_when_missing(self):
        video_id = self._video()
        self._video("telegram:456")
        # Mirrors _record_phash_size in scene_scraper_fingerprint_backfill.
        sink_sql = ("UPDATE videos SET file_size_bytes=%s "
                    "WHERE id=%s AND file_size_bytes IS NULL")
        with self.conn:
            self.conn.execute(sink_sql, (99887766, video_id))
        self.assertEqual(
            self.conn.execute("SELECT file_size_bytes FROM videos WHERE id=%s",
                              (video_id,)).fetchone()[0], 99887766)
        with self.conn:
            self.conn.execute(sink_sql, (111, video_id))  # must not overwrite
        self.assertEqual(
            self.conn.execute("SELECT file_size_bytes FROM videos WHERE id=%s",
                              (video_id,)).fetchone()[0], 99887766)

    def test_store_fingerprint_rejects_unusable_sizes(self):
        for bad in (-1, True, "12345", None):
            video_id = self._video(f"telegram:bad{bad}")
            scene_scrapers._store_video_fingerprint(
                self.conn, video_id, "OSHASH", "0123456789abcdef",
                media_probe={"status": "complete", "bytes": 262144,
                             "data": {"file_size_bytes": bad}})
            self.assertIsNone(
                self.conn.execute("SELECT file_size_bytes FROM videos WHERE id=%s",
                                  (video_id,)).fetchone()[0], msg=str(bad))


if __name__ == "__main__":
    unittest.main()
