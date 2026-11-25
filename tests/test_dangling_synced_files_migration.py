"""Migration 0077 removes library file records whose catalog item is gone."""

from __future__ import annotations

import unittest

from common.migrations.m0077_remove_dangling_synced_files import apply
from pg_helper import PgTestCase


class DanglingSyncedFilesMigrationTests(PgTestCase):
    def test_removes_files_without_a_catalog_item(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, title, duration) VALUES ('keep-video', 'Keep', 600)"
            )
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id) VALUES ('keep.strm', 'keep-video')"
            )
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id) VALUES ('dangling.strm', 'gone-video')"
            )

        apply(self.conn)

        rows = {row[0] for row in self.conn.execute(
            "SELECT file_path FROM synced_files"
        ).fetchall()}
        self.assertEqual(rows, {"keep.strm"})

    def test_leaves_rows_without_a_video_reference_alone(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id) VALUES ('unassigned.strm', NULL)"
            )

        apply(self.conn)

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM synced_files WHERE file_path = 'unassigned.strm'"
        ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
