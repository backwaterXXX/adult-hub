"""Progress counts must distinguish current catalog entries from historical attempts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase
from common.admin_external_metadata import enrichment_status
from common.external_metadata import record_attempt


class EnrichmentCatalogProgressTests(PgTestCase):
    def test_historical_records_do_not_inflate_current_catalog_progress(self):
        with self.conn:
            self.conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags) VALUES "
                              "('telegram:current','telegram','Current','[\"Actor\"]','[]','[]'),"
                              "('telegram:other','telegram','Other','[]','[]','[]')")
        record_attempt(self.conn, 'video', 'telegram:current', 'studio_scene', 'found')
        record_attempt(self.conn, 'video', 'telegram:deleted', 'studio_scene', 'no_match')
        record_attempt(self.conn, 'performer', 'actor', 'pbc', 'found')
        record_attempt(self.conn, 'performer', 'old name', 'pbc', 'no_match')
        result = enrichment_status(self.db_url)
        rows = {row['source']: row for row in result['sources']}
        self.assertEqual((rows['studio_scene']['attempts'], rows['studio_scene']['catalog_checked'],
                          rows['studio_scene']['catalog_total'], rows['studio_scene']['remaining']), (2, 1, 2, 1))
        self.assertEqual((rows['pbc']['attempts'], rows['pbc']['catalog_checked'],
                          rows['pbc']['catalog_total'], rows['pbc']['remaining']), (2, 1, 1, 0))
