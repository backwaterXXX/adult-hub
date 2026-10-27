import unittest

from common.duplicate_evidence import pair_evidence
from sync.video_visual import reevaluate_evidence


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class Conn:
    def __init__(self, hashes, *, dismissed=False, parts=('', ''), oshashes=('', '')):
        self.hashes, self.dismissed, self.parts, self.oshashes = hashes, dismissed, parts, oshashes
    def execute(self, sql, params):
        if 'FROM videos v' in sql:
            return Result([(name, 100, 1, self.parts[i], '', '', '', self.oshashes[i], self.hashes[i])
                           for i, name in enumerate(('a', 'b'))])
        if 'video_duplicate_dismissals' in sql:
            return Result([(1,)] if self.dismissed else [])
        return Result([])


class EvidencePolicyTest(unittest.TestCase):
    def test_phash_boundaries_and_protections(self):
        for bits in (0, 10):
            conn = Conn(('0000000000000000', f'{(1 << bits)-1:016x}'))
            self.assertEqual(pair_evidence(conn, 'a', 'b')['distance'], bits)
        self.assertIsNone(pair_evidence(Conn(('0000000000000000', '00000000000007ff')), 'a', 'b'))
        self.assertIsNone(pair_evidence(Conn(('0', '0')), 'a', 'b'))
        self.assertIsNone(pair_evidence(Conn(('0000000000000000',)*2, dismissed=True), 'a', 'b'))
        self.assertIsNone(pair_evidence(Conn(('0000000000000000',)*2, parts=('part 1','part 2')), 'a', 'b'))
        self.assertEqual(pair_evidence(Conn(('', ''), oshashes=('same', 'same')), 'a', 'b')['type'], 'oshash')

    def test_saved_measurements_policy(self):
        strict = {'informative': True, 'phash': 6, 'dhash': 6, 'ssim': .90}
        loose = {'informative': True, 'phash': 12, 'dhash': 10, 'ssim': .85}
        bad = {'informative': True, 'phash': 30, 'dhash': 30, 'ssim': .5}
        evidence = {'matches': [strict]*5 + [loose] + [bad]*2,
                    'strict_sections': {'beginning': True, 'middle': True, 'end': True},
                    'sections': {'beginning': True, 'middle': True, 'end': True},
                    'offset_fit': {'max_residual_seconds': .5, 'predicted': [0, 2]},
                    'aspect_ratio_auto_safe': True}
        self.assertEqual(reevaluate_evidence(evidence), 'confirmed')
        evidence['offset_fit']['max_residual_seconds'] = .51
        self.assertNotEqual(reevaluate_evidence(evidence), 'confirmed')
        evidence['matches'] = []
        self.assertIsNone(reevaluate_evidence(evidence))


if __name__ == '__main__': unittest.main()

try:
    from pg_helper import PgTestCase
except ImportError:
    from tests.pg_helper import PgTestCase


class VisualHistoryTest(PgTestCase):
    def test_archives_signature_updates_and_deletes(self):
        from psycopg.types.json import Jsonb
        with self.conn:
            self.conn.execute("INSERT INTO videos (id,provider,title,duration) VALUES ('history-test','telegram','Test',600)")
            self.conn.execute("INSERT INTO video_visual_signatures (video_id,algorithm_version,source_key,duration_seconds,signature,contact_sheet) VALUES ('history-test','v1','old',600,%s,%s)", (Jsonb({'samples': [1]}), b'image'))
            self.conn.execute("UPDATE video_visual_signatures SET source_key='new',signature=%s WHERE video_id='history-test'", (Jsonb({'samples': [2]}),))
            self.conn.execute("DELETE FROM videos WHERE id='history-test'")
        rows = self.conn.execute("SELECT record->>'source_key',image FROM video_visual_history WHERE kind='signature' ORDER BY id").fetchall()
        self.assertEqual([(row[0], bytes(row[1])) for row in rows], [('old', b'image'), ('new', b'image')])

    def test_only_candidates_receive_signature_jobs(self):
        from sync.video_duplicate_automation import _reconcile_visual
        with self.conn:
            for name in ('candidate-a', 'candidate-b', 'unrelated'):
                self.conn.execute("INSERT INTO videos (id,provider,title,duration) VALUES (%s,'telegram',%s,600)", (name,name))
        result = _reconcile_visual(self.db_url, [{'left': {'id': 'candidate-a'}, 'right': {'id': 'candidate-b'}}])
        self.assertEqual(result['visual_signatures_queued'], 2)
        ids = {row[0] for row in self.conn.execute("SELECT video_id FROM video_visual_jobs WHERE state='queued'").fetchall()}
        self.assertEqual(ids, {'candidate-a', 'candidate-b'})
        again = _reconcile_visual(self.db_url, [{'left': {'id': 'candidate-a'}, 'right': {'id': 'candidate-b'}}])
        self.assertEqual(again['visual_signatures_queued'], 0)
