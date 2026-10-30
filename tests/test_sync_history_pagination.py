import json
import tempfile
import unittest
from pathlib import Path

from resolver.admin_data_runtime_files import sync_history


class SyncHistoryPaginationTests(unittest.TestCase):
    def test_history_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_file = root / 'history.json'
            history_file.write_text(json.dumps(
                {'runs': [{'started_at': n, 'sources': []} for n in range(75)]}))
            result = sync_history(history_file, root, 10, 60, channel_labels=lambda _: {},
                                  trim_channel_name=str)
            self.assertEqual([r['started_at'] for r in result['runs']], list(range(60, 70)))
            self.assertEqual(result['total'], 75)
            self.assertTrue(result['truncated'])
            last_page = sync_history(history_file, root, 10, 70, channel_labels=lambda _: {},
                                     trim_channel_name=str)
            self.assertEqual(len(last_page['runs']), 5)


if __name__ == '__main__':
    unittest.main()
