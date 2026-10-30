import json
from resolver.admin_data_runtime_files import sync_history

def test_history_page(tmp_path):
    f = tmp_path / 'history.json'
    f.write_text(json.dumps({'runs': [{'started_at': n, 'sources': []} for n in range(75)]}))
    result = sync_history(f, tmp_path, 10, 60, channel_labels=lambda _: {}, trim_channel_name=str)
    assert [r['started_at'] for r in result['runs']] == list(range(60, 70))
    assert result['total'] == 75
    assert result['truncated']
    assert len(sync_history(f, tmp_path, 10, 70, channel_labels=lambda _: {}, trim_channel_name=str)['runs']) == 5
