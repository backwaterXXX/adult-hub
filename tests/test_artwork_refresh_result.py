import json

from tools.generate_missing_artwork import update_job_jellyfin_result


class FakeConnection:
    def __init__(self):
        self.writes = []
        self.commits = 0

    def execute(self, sql, params):
        if sql.startswith('SELECT'):
            class Row:
                def fetchone(self):
                    return (json.dumps({'events': [], 'jellyfin_refresh': 'pending',
                                        'jellyfin_library_refresh': 'pending'}),)
            return Row()
        self.writes.append((sql, params))

    def commit(self):
        self.commits += 1


def test_refresh_result_records_both_outcomes_once():
    conn = FakeConnection()
    update_job_jellyfin_result(conn, 'job-1', item_status='updated', library_status='failed')
    payload = json.loads(conn.writes[0][1][0])
    assert payload['jellyfin_refresh'] == 'updated'
    assert payload['jellyfin_library_refresh'] == 'failed'
    assert len(payload['events']) == 1
    assert conn.commits == 1
