import copy
import ast
import importlib.util
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase


spec = importlib.util.spec_from_file_location('jf_reconcile', Path(__file__).parents[1] / 'sync/jellyfin_db.py')
jf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jf)


class Response:
    def __init__(self, value):
        self.value = value
        self.content = b'json' if value is not None else b''
    def raise_for_status(self):
        pass
    def json(self):
        return copy.deepcopy(self.value)


class Session:
    def __init__(self):
        self.item = {'Id': 'abc', 'Path': '/media/adult/telegram [123].strm',
                     'Name': 'telegram', 'OriginalTitle': None, 'Overview': None,
                     'LockData': True, 'Tags': ['keep'], 'Genres': ['keep'],
                     'ProviderIds': {'telegram': '123'}, 'People': []}
        self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == 'GET' and url.endswith('/Library/VirtualFolders'):
            return Response([{'Locations': ['/media/adult'], 'LibraryOptions': {'MetadataSavers': []}}])
        if method == 'GET' and url.endswith('/Items'):
            return Response({'Items': [self.item], 'TotalRecordCount': 1})
        if method == 'GET' and url.endswith('/Users'):
            return Response([{'Id': 'admin', 'Policy': {'IsAdministrator': True}}])
        if method == 'GET':
            return Response(self.item)
        if url.endswith('/Refresh'):
            self.item.update(Name='Correct', OriginalTitle='Original', Overview='Plot', LockData=True)
        else:
            self.item = copy.deepcopy(kwargs['json'])
        return Response(None)


class TestTelegramImportStateAudit(unittest.TestCase):
    def test_audit_import_state_matches_jellyfin_ids_to_nfo_hashes_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            nfo = folder / 'telegram [123].nfo'
            nfo.write_text('<movie><title>Correct</title></movie>')
            digest = hashlib.sha256(nfo.read_bytes()).hexdigest()
            session = Session()
            state = folder / 'state.json'
            state.write_text(json.dumps({'abc': digest}))
            progress = []
            result = jf.audit_telegram_import_state('http://jellyfin', 'secret', folder,
                                                    state_path=state, session=session,
                                                    progress=lambda completed, total: progress.append((completed, total)))
            self.assertEqual(result, {'status': 'checked', 'findings': []})
            self.assertEqual(progress, [(1, 1)])
            self.assertEqual([call[0] for call in session.calls], ['GET'])
            self.assertEqual(session.calls[0][2]['timeout'], 20)

            state.write_text(json.dumps({'abc': 'old-digest'}))
            result = jf.audit_telegram_import_state('http://jellyfin', 'secret', folder,
                                                    state_path=state, session=session)
            self.assertEqual(result, {'status': 'checked', 'findings': [
                ('telegram [123].strm', 'nfo_import_state_mismatch')
            ]})
            state.write_text('{}')
            result = jf.audit_telegram_import_state('http://jellyfin', 'secret', folder,
                                                    state_path=state, session=session)
            self.assertEqual(result['findings'][0][1], 'nfo_import_state_missing')

    def test_audit_import_state_reports_unknown_without_private_errors(self):
        class BrokenSession:
            def request(self, *args, **kwargs):
                raise RuntimeError('PRIVATE_API_KEY in request URL')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{}')
            self.assertEqual(jf.audit_telegram_import_state(
                'http://jellyfin', 'secret', directory, state_path=state,
                session=BrokenSession()), {'status': 'unknown', 'findings': []})

    def test_audit_import_state_does_not_accept_an_empty_page_before_inventory_end(self):
        class TruncatedSession:
            def __init__(self):
                self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append(kwargs['params'])
                if len(self.calls) == 1:
                    return Response({'Items': [
                        {'Id': str(index), 'Path': f'/other/movie-{index}.strm'}
                        for index in range(500)
                    ], 'TotalRecordCount': 501})
                return Response({'Items': [], 'TotalRecordCount': 501})

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{}')
            session = TruncatedSession()
            result = jf.audit_telegram_import_state(
                'http://jellyfin', 'secret', directory, state_path=state, session=session)

            self.assertEqual(result, {'status': 'unknown', 'findings': []})
            self.assertEqual([call['StartIndex'] for call in session.calls], [0, 500])
            self.assertEqual([call['Limit'] for call in session.calls], [500, 500])

    def test_audit_import_state_rejects_inventory_drift_and_repeated_pages(self):
        class ChangingSession:
            def __init__(self, second_item, second_total):
                self.calls = []
                self.second_item = second_item
                self.second_total = second_total
            def request(self, method, url, **kwargs):
                self.calls.append(kwargs['params'])
                if len(self.calls) == 1:
                    return Response({'Items': [{'Id': 'first', 'Path': '/other/first.strm'}],
                                     'TotalRecordCount': 2})
                return Response({'Items': [self.second_item], 'TotalRecordCount': self.second_total})

        cases = (
            ('changed total', {'Id': 'second', 'Path': '/other/second.strm'}, 3),
            ('repeated item', {'Id': 'first', 'Path': '/other/first.strm'}, 2),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{}')
            for label, item, total in cases:
                with self.subTest(label=label):
                    session = ChangingSession(item, total)
                    result = jf.audit_telegram_import_state(
                        'http://jellyfin', 'secret', directory, state_path=state, session=session)
                    self.assertEqual(result, {'status': 'unknown', 'findings': []})
                    self.assertEqual([call['StartIndex'] for call in session.calls], [0, 1])

    def test_audit_import_state_rejects_malformed_items(self):
        class MalformedSession:
            def __init__(self, item):
                self.item = item
            def request(self, method, url, **kwargs):
                return Response({'Items': [self.item], 'TotalRecordCount': 1})

        items = (
            ('non-object item', None),
            ('missing item ID', {'Path': '/media/adult/telegram [1].strm'}),
            ('missing item path', {'Id': 'item-1'}),
            ('invalid item path', {'Id': 'item-1', 'Path': 123}),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{}')
            for label, item in items:
                with self.subTest(label=label):
                    result = jf.audit_telegram_import_state(
                        'http://jellyfin', 'secret', directory, state_path=state,
                        session=MalformedSession(item))
                    self.assertEqual(result, {'status': 'unknown', 'findings': []})


class TestTelegramMetadata(PgTestCase):
    def test_sync_wrapper_confirms_completed_job_after_jellyfin_refresh_and_hash_match(self):
        sys.path.insert(0, str(Path(__file__).parents[1] / 'sync'))
        import sync

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            nfo = folder / 'telegram [123].nfo'
            nfo.write_text(
                '<movie><title>Correct</title><originaltitle>Original</originaltitle><plot>Plot</plot>'
                '<lockdata>true</lockdata><genre>keep</genre><tag>keep</tag>'
                '<uniqueid type="telegram">123</uniqueid></movie>')
            digest = hashlib.sha256(nfo.read_bytes()).hexdigest()
            job_id = 'metadata-refresh-integration'
            with self.conn:
                self.conn.execute(
                    "INSERT INTO video_duplicate_jobs(id,action,state,payload,result,created_at,updated_at) "
                    "VALUES (%s,'metadata_update','done',%s,%s,1,1)",
                    (job_id, json.dumps({'video_id': 'telegram:-1001:123'}), json.dumps({
                        'local_state': 'metadata_updated', 'nfo_written': True,
                        'jellyfin_import': 'not_confirmed',
                        'nfo_content_hashes': {nfo.name: digest},
                    })))

            session = Session()

            def reconcile(url, key, media_dir, **kwargs):
                return jf.sync_jellyfin_telegram_metadata(
                    url, key, media_dir, session=session, **kwargs)

            with patch.object(sync, 'JELLYFIN_URL', 'http://jellyfin.test'), \
                    patch.object(sync, 'JELLYFIN_API_KEY', 'test-key'), \
                    patch.object(sync, 'MEDIA_DIR', folder), \
                    patch.object(sync, 'DATA_DIR', folder), \
                    patch.object(sync, 'DB_URL', self.db_url), \
                    patch.object(sync, '_sync_jellyfin_telegram_metadata', side_effect=reconcile):
                result = sync.sync_jellyfin_telegram_metadata()

            self.assertEqual(result['updated'], 1)
            self.assertEqual(result['failed'], 0)
            self.assertTrue(any(method == 'POST' and url.endswith('/Refresh')
                                for method, url, _ in session.calls))
            job = self.conn.execute(
                'SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
            outcome = json.loads(job[0])
            self.assertEqual(outcome['jellyfin_import'], 'confirmed')
            self.assertIsInstance(outcome['jellyfin_import_confirmed_at'], int)

    def test_adopt_matching_corrects_only_ids_while_retaining_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><originaltitle>Original</originaltitle><plot>Plot</plot>'
                '<lockdata>true</lockdata><genre>keep</genre><tag>keep</tag>'
                '<uniqueid type="telegram">123</uniqueid></movie>')
            session = Session()
            session.item.update(Name='Correct', OriginalTitle='Original', Overview='Plot',
                ProviderIds={'telegram': '123', 'Imdb': 'wrong'}, UserData={'Played': True})
            state = folder / 'state.json'
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                state_path=state, session=session, adopt_matching=True)
            self.assertEqual(result['updated'], 1)
            self.assertEqual(result['failed'], 0)
            posts = [(url, kwargs) for method, url, kwargs in session.calls if method == 'POST']
            self.assertEqual(len(posts), 1)
            self.assertFalse(posts[0][0].endswith('/Refresh'))
            self.assertTrue(posts[0][1]['json']['LockData'])
            self.assertEqual(session.item['ProviderIds'], {'telegram': '123'})
            self.assertEqual(session.item['Id'], 'abc')
            self.assertEqual(session.item['UserData'], {'Played': True})
            self.assertEqual(json.loads(state.read_text()).keys(), {'abc'})
            # An older content hash must still import changed tags/cast, even
            # when the visible core fields and ID-only predicate would match.
            state.write_text(json.dumps({'abc': 'older changed NFO'}))
            session.item['ProviderIds']['Imdb'] = 'wrong'
            session.calls.clear()
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                state_path=state, session=session, adopt_matching=True)
            self.assertEqual(result['updated'], 1)
            self.assertTrue(any(url.endswith('/Refresh') for method, url, _ in session.calls if method == 'POST'))

    def test_nfo_provider_ids_replace_foreign_match_without_changing_item_or_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><originaltitle>Original</originaltitle><plot>Plot</plot>'
                '<lockdata>true</lockdata><uniqueid type="telegram">123</uniqueid></movie>')
            session = Session()
            session.item.update(LockData=False, ProviderIds={'telegram': '123', 'Imdb': 'wrong', 'Tmdb': 'wrong'},
                                UserData={'Played': True, 'PlaybackPositionTicks': 123456})
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                state_path=folder / 'state.json', session=session)
            self.assertEqual(result['updated'], 1)
            self.assertEqual(session.item['ProviderIds'], {'telegram': '123'})
            self.assertEqual(session.item['Id'], 'abc')
            self.assertEqual(session.item['UserData'], {'Played': True, 'PlaybackPositionTicks': 123456})
            self.assertEqual(session.item['Tags'], ['keep'])
            update = next(kw['json'] for method, url, kw in session.calls
                          if method == 'POST' and not url.endswith('/Refresh'))
            self.assertEqual(update['ProviderIds'], {'telegram': '123'})

    def test_refresh_waits_for_unlock_to_be_visible(self):
        class DelayedUnlockSession(Session):
            def __init__(self):
                super().__init__()
                self.unlock_posted = False
                self.stale_reads = 0
                self.unlock_seen = False
            def request(self, method, url, **kwargs):
                if method == 'POST' and not url.endswith('/Refresh') and not kwargs['json']['LockData']:
                    self.unlock_posted = True
                if method == 'GET' and '/Users/admin/Items/' in url and self.unlock_posted and not self.unlock_seen:
                    self.stale_reads += 1
                    if self.stale_reads == 1:
                        stale = copy.deepcopy(self.item)
                        stale['LockData'] = True
                        return Response(stale)
                    self.unlock_seen = True
                if url.endswith('/Refresh'):
                    assert self.unlock_seen, 'Refresh queued before unlock was visible'
                return super().request(method, url, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><originaltitle>Original</originaltitle><plot>Plot</plot>'
                '<lockdata>true</lockdata></movie>')
            session = DelayedUnlockSession()
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                state_path=folder / 'state.json', session=session)
            self.assertEqual(result['updated'], 1)
            self.assertEqual(session.stale_reads, 2)

    def test_verification_timeout_logs_reason_without_private_values(self):
        class StalledSession(Session):
            def request(self, method, url, **kwargs):
                if url.endswith('/Refresh'):
                    self.item['ProviderIds'] = {'Imdb': 'PRIVATE_WRONG_PROVIDER_ID'}
                    return Response(None)
                return super().request(method, url, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>PRIVATE_EXPECTED_TITLE</title><originaltitle>PRIVATE_ORIGINAL</originaltitle>'
                '<plot>PRIVATE_DESCRIPTION</plot><lockdata>true</lockdata></movie>')
            session = StalledSession()
            session.item['Name'] = 'PRIVATE_CURRENT_TITLE'
            logger = logging.getLogger('telegram_metadata_observability')
            with self.assertLogs(logger, level='WARNING') as logs:
                result = jf.sync_jellyfin_telegram_metadata(
                    'http://jellyfin', 'PRIVATE_API_KEY', folder, state_path=folder / 'state.json',
                    session=session, logger=logger, poll_timeout=0)
            self.assertEqual(result['failed'], 1)
            output = '\n'.join(logs.output)
            self.assertIn('last_poll_available=True LockData=False', output)
            self.assertIn("mismatched_keys=['Name', 'OriginalTitle', 'Overview', 'ProviderIds']", output)
            for private_value in ('PRIVATE_EXPECTED_TITLE', 'PRIVATE_ORIGINAL',
                                  'PRIVATE_DESCRIPTION', 'PRIVATE_CURRENT_TITLE', 'PRIVATE_API_KEY',
                                  'PRIVATE_WRONG_PROVIDER_ID'):
                self.assertNotIn(private_value, output)

    def test_cli_poll_timeout_default_override_validation_and_failure_exit(self):
        source = Path(jf.__file__).read_text()
        tree = ast.parse(source)
        entrypoint = next(node for node in tree.body if isinstance(node, ast.If)
                          and "__name__" in ast.unparse(node.test))
        code = compile(ast.Module(body=entrypoint.body, type_ignores=[]), jf.__file__, "exec")
        for arguments, failures, expected_timeout, exit_code in (
            ([], 0, 120, 0),
            (["--poll-timeout", "150"], 1, 150, 1),
            (["--poll-timeout", "0"], 0, None, 2),
            (["--poll-timeout", "-1"], 0, None, 2),
        ):
            with self.subTest(arguments=arguments, failures=failures):
                reconcile = Mock(return_value={"failed": failures})
                with patch.object(sys, "argv", ["jellyfin_db.py", *arguments]), \
                     patch("builtins.print"), patch("sys.stderr"), \
                     self.assertRaises(SystemExit) as error:
                    exec(code, {"json": json, "sync_jellyfin_telegram_metadata": reconcile})
                self.assertEqual(error.exception.code, exit_code)
                if expected_timeout is None:
                    reconcile.assert_not_called()
                else:
                    self.assertEqual(reconcile.call_args.kwargs["poll_timeout"], expected_timeout)

    def test_nfo_strings_normalized_like_jellyfin_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            nfo = Path(directory) / 'whitespace.nfo'
            nfo.write_text('<movie><title> Correct </title><originaltitle> Original\n </originaltitle>'
                           '<plot>\n Plot \n</plot><lockdata>true</lockdata></movie>')
            _, expected = jf._telegram_nfo(nfo)
            self.assertEqual(expected, {'Name': 'Correct', 'OriginalTitle': 'Original', 'Overview': 'Plot', 'ProviderIds': {}})

    def test_runtime_adoption_respects_generated_minute_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            nfo = Path(directory) / 'runtime.nfo'
            nfo.write_text('<movie><runtime>2</runtime></movie>')
            self.assertTrue(jf._matches_complete_nfo({'RunTimeTicks': 1250000000}, nfo))
            self.assertFalse(jf._matches_complete_nfo({'RunTimeTicks': 1800000000}, nfo))
            self.assertFalse(jf._matches_complete_nfo({'RunTimeTicks': 1190000000}, nfo))
            nfo.write_text('<movie><runtime>1</runtime></movie>')
            self.assertTrue(jf._matches_complete_nfo({'RunTimeTicks': 250000000}, nfo))
            self.assertFalse(jf._matches_complete_nfo({'RunTimeTicks': None}, nfo))

    def test_adopt_only_comprehensive_match_and_no_previous_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><originaltitle>Original</originaltitle><plot>Plot</plot>'
                '<lockdata>true</lockdata><genre>keep</genre><tag>keep</tag><runtime>2</runtime>'
                '<uniqueid type="telegram">123</uniqueid></movie>')
            session = Session()
            session.item.update(Name='Correct', OriginalTitle='Original', Overview='Plot', RunTimeTicks=1200000000)
            state = folder / 'state.json'
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                state_path=state, session=session, adopt_matching=True)
            self.assertEqual(result['adopted'], 1)
            self.assertEqual(result['needed'], 0)
            self.assertFalse(any(call[0] == 'POST' for call in session.calls))
            state.write_text(json.dumps({'abc': 'previous different NFO'}))
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                state_path=state, session=session, adopt_matching=True)
            self.assertEqual(result['adopted'], 0)
            self.assertEqual(result['updated'], 1)

    def test_worker_uses_authenticated_api_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><originaltitle>Original</originaltitle><plot>Plot</plot>'
                '<lockdata>true</lockdata></movie>')
            session = Session()
            with patch('requests.Session', return_value=session):
                result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                    state_path=folder / 'state.json', session=session, workers=4)
            self.assertEqual(result['updated'], 1)
            self.assertTrue((folder / 'state.json').exists())

    def test_enabled_nfo_saver_never_unlocked(self):
        class SaverSession(Session):
            def request(self, method, url, **kwargs):
                if url.endswith('/Library/VirtualFolders'):
                    return Response([{'Locations': ['/media/adult'], 'LibraryOptions': {'MetadataSavers': ['Nfo']}}])
                return super().request(method, url, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><lockdata>true</lockdata></movie>')
            session = SaverSession()
            with self.assertRaisesRegex(ValueError, 'MetadataSavers'):
                jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                                                  state_path=folder / 'state', session=session)
            self.assertFalse(any(call[0] == 'POST' for call in session.calls))

    def test_failed_refresh_restores_lock_and_does_not_checkpoint(self):
        class BrokenSession(Session):
            def request(self, method, url, **kwargs):
                if url.endswith('/Refresh'):
                    raise TimeoutError('no refresh')
                return super().request(method, url, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><title>Correct</title><lockdata>true</lockdata></movie>')
            session = BrokenSession()
            state = folder / 'state.json'
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                                                       state_path=state, session=session)
            self.assertEqual(result['failed'], 1)
            self.assertTrue(session.item['LockData'])
            self.assertEqual(session.item['Tags'], ['keep'])
            self.assertFalse(state.exists())

    def test_unlock_refresh_verify_and_hash_skips_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            nfo = folder / 'telegram [123].nfo'
            nfo.write_text('<movie><title>Correct</title><originaltitle>Original</originaltitle>'
                           '<plot>Plot</plot><lockdata>true</lockdata></movie>')
            session = Session()
            state = folder / 'state.json'
            args = ('http://jellyfin', 'secret', folder)
            result = jf.sync_jellyfin_telegram_metadata(*args, state_path=state, session=session)
            self.assertEqual(result['updated'], 1)
            self.assertEqual(result['_confirmed_file_hashes'], {
                'telegram [123].strm': hashlib.sha256(nfo.read_bytes()).hexdigest()
            })
            self.assertIn('Token="secret"', session.calls[0][2]['headers']['Authorization'])
            posts = [(url, kw) for method, url, kw in session.calls if method == 'POST']
            self.assertFalse(posts[0][1]['json']['LockData'])
            self.assertEqual(posts[0][1]['json']['Tags'], ['keep'])
            self.assertEqual(posts[1][1]['params']['imageRefreshMode'], 'None')
            self.assertEqual(posts[1][1]['params']['replaceAllImages'], 'false')
            session.calls.clear()
            result = jf.sync_jellyfin_telegram_metadata(*args, state_path=state, session=session)
            self.assertEqual(result['needed'], 0)
            self.assertFalse(any(call[0] == 'POST' for call in session.calls))
            nfo.write_text(nfo.read_text().replace('</movie>', '<tag>new</tag></movie>'))
            result = jf.sync_jellyfin_telegram_metadata(*args, state_path=state, session=session)
            self.assertEqual(result['updated'], 1)

    def test_network_artwork_never_unlocked(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'telegram [123].nfo').write_text(
                '<movie><lockdata>true</lockdata><thumb>https://bad/thumb</thumb></movie>')
            session = Session()
            result = jf.sync_jellyfin_telegram_metadata('http://jellyfin', 'secret', folder,
                                                       state_path=folder / 'state', session=session)
            self.assertEqual(result['failed'], 1)
            self.assertFalse(any(call[0] == 'POST' for call in session.calls))

    def test_title_sync_uses_api_and_preserves_unrelated_item_fields(self):
        self.conn.execute(
            'INSERT INTO videos (id, provider, title, slug, duration, performers)'
            ' VALUES (%s, %s, %s, %s, %s, %s)',
            ('exampleweb:123', 'exampleweb', 'Original title', 's', 600, '[]'),
        )
        self.conn.execute(
            'INSERT INTO synced_files (file_path, video_id) VALUES (%s, %s)',
            ('exampleweb [123].strm', 'exampleweb:123'),
        )
        self.conn.commit()

        class ApiSession:
            def __init__(self):
                self.calls = []
                self.dto = {'Id': 'item-1', 'Path': '/media/adult/exampleweb [123].strm',
                            'Name': 'Old', 'OriginalTitle': 'Old', 'Overview': 'Preserve me',
                            'LockData': True, 'Tags': ['keep']}

            def request(inner, method, url, **kwargs):
                inner.calls.append((method, url, kwargs))
                if method == 'GET' and url.endswith('/Items'):
                    return Response({'Items': [dict(inner.dto)], 'TotalRecordCount': 1})
                if method == 'GET' and url.endswith('/Items/item-1'):
                    return Response(inner.dto)
                if method == 'POST' and url.endswith('/Items/item-1'):
                    inner.dto = kwargs['json']
                    return Response(None)
                raise AssertionError((method, url))

        session = ApiSession()
        result = jf.sync_jellyfin_titles(
            hub_db_url=self.db_url,
            clean_title=lambda title, **kwargs: 'Clean title',
            url='http://jellyfin', api_key='secret', session=session,
            media_dir='/media/adult',
        )
        self.assertEqual(result, {'checked': 1, 'updated': 1, 'failed': 0})
        self.assertEqual(session.dto['Name'], 'Clean title')
        self.assertEqual(session.dto['OriginalTitle'], 'Original title')
        self.assertEqual(session.dto['Overview'], 'Preserve me')
        self.assertTrue(session.dto['LockData'])
        self.assertEqual(session.dto['Tags'], ['keep'])
