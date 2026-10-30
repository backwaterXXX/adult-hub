import json
import unittest

from common.fuzzy_merge import performer_merge_revision


class AliasConnection:
    def __init__(self, aliases):
        self.aliases = aliases

    def execute(self, query):
        assert 'FROM performer_aliases' in query
        return self

    def fetchall(self):
        return sorted(self.aliases, key=lambda row: (row[0].lower(), row[0]))


class PerformerMergeRevisionScopeTests(unittest.TestCase):
    def test_unrelated_alias_changes_do_not_invalidate_new_revision(self):
        core = {'table_names': lambda _: {'performer_aliases'}, 'json': json}
        conn = AliasConnection([('Alias', 'Old'), ('Other', 'Unrelated')])
        revision = performer_merge_revision(conn, 'Alias', 'Canonical', _core=core)
        legacy = performer_merge_revision(conn, 'Alias', 'Canonical', _core=core, legacy=True)
        conn.aliases.append(('Someone', 'Else'))
        self.assertEqual(revision, performer_merge_revision(conn, 'Alias', 'Canonical', _core=core))
        self.assertNotEqual(legacy, performer_merge_revision(conn, 'Alias', 'Canonical', _core=core, legacy=True))
        conn.aliases.append(('Dependent', 'Alias'))
        self.assertNotEqual(revision, performer_merge_revision(conn, 'Alias', 'Canonical', _core=core))

    def test_target_alias_change_invalidates_revision(self):
        core = {'table_names': lambda _: {'performer_aliases'}, 'json': json}
        conn = AliasConnection([])
        revision = performer_merge_revision(conn, 'Alias', 'Canonical', _core=core)
        conn.aliases.append(('Canonical', 'Another'))
        self.assertNotEqual(revision, performer_merge_revision(conn, 'Alias', 'Canonical', _core=core))


if __name__ == '__main__':
    unittest.main()
