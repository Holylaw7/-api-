"""Selected-sector workflows use temporary data and mocked external services."""
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import DEFAULT
from app.reporting import report_identity
from app.server import LocalServer
from app.service import SH, Service


DATE = '2026-09-18'
NOW = datetime(2026, 9, 20, 12, tzinfo=SH)
CODES = ['885001.TI']
CATALOG = [
    {'thscode': '885001.TI', 'name': 'PCB', 'category': 'cn_concept'},
    {'thscode': '881001.TI', 'name': '测试行业', 'category': 'industry'},
]


def report():
    row = {'thscode': '000001.SZ', 'name': '测试证券', 'score': 70,
           'consecutive_days': 2, 'consecutive_lower_bound': False}
    return {'date': DATE, 'mode': 'live', 'status': 'ready',
            'generated_at': DATE + 'T15:30:00+08:00', 'warnings': [],
            'market': {'limit_up_count': 1},
            'limit_up': {'count': 1, 'status': 'ready', 'rows': [row]},
            'raw': {'calendar': ['2026-09-17', DATE],
                    'pools_by_date': {DATE: [copy.deepcopy(row)]}}}


def evidence(status='ready'):
    return {'date': DATE, 'mode': 'live', 'status': status,
            'generated_at': NOW.isoformat(), 'warnings': [],
            'definition': '当前成分对应所选日期的有限同行观察，不是历史成分',
            'boards': [{'thscode': CODES[0], 'name': 'PCB', 'category': 'cn_concept',
                        'status': status, 'member_count': 1,
                        'members': [{'thscode': '000001.SZ', 'name': '测试证券',
                                     'price_change_ratio_pct': 3.0, 'turnover': 100.0,
                                     'limit_up': True}],
                        'warnings': [], 'net_flow': None,
                        'raw': {'private': 'excluded-upstream-evidence'}}],
            'raw': {'private': 'excluded-raw-evidence'}}


class SectorWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch('app.service.load_config', return_value=copy.deepcopy(DEFAULT)),
            patch('app.service.finance_key', return_value=''),
            patch('app.service.credential_status', return_value={}),
            patch('app.ai_gateway.credential_dir', return_value=self.root / 'credentials'),
            patch.dict('os.environ', {'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': '',
                                      'CUSTOM_AI_API_KEY': ''}),
            patch('app.service.now_sh', return_value=NOW),
            patch('app.provider.urlopen', side_effect=AssertionError('external network forbidden')),
        ]
        for item in self.patches:
            item.start()
        self.service = Service(self.root)
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.original = report()
        self.service.store.report(DATE, 'live', self.original)
        self.service.review = self.original
        self.service.latest_review = self.original
        self.identity = report_identity(self.original)

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def queued(self, method, *args, **kwargs):
        jobs = {}
        def capture(name, worker):
            jobs[name] = worker
            return True
        with patch.object(self.service, '_job', side_effect=capture):
            self.assertTrue(method(*args, **kwargs))
        self.assertEqual(len(jobs), 1)
        return next(iter(jobs.values()))

    def queue_ai(self, config=None, **kwargs):
        config = config if config is not None else {
            'provider': 'deepseek', 'api_key': 'offline-llm-placeholder',
            'model': 'fixture-model', 'label': 'Fixture provider'}
        with patch('app.service.load_llm', return_value=config):
            return self.queued(self.service.run_llm, '研究 PCB 同行', 'deepseek',
                               DATE, self.identity, sector_codes=CODES,
                               fetch_sectors=True, **kwargs)

    @contextmanager
    def local_http(self):
        server = LocalServer(('127.0.0.1', 0), self.service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path, body=None, headers=None):
            payload = None if body is None else json.dumps(body).encode('utf-8')
            request_headers = {'Content-Type': 'application/json'}
            if body is not None:
                request_headers['X-Local-App'] = 'auction-lab'
            if headers:
                request_headers.update(headers)
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}' + path,
                                         data=payload, headers=request_headers)
            with opener.open(req, timeout=3) as response:
                return json.load(response), dict(response.headers)
        try:
            yield request
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_search_reads_both_directories_once_and_caches_for_other_queries(self):
        provider = Mock()
        provider.catalog.side_effect = lambda category: [
            {k: v for k, v in row.items() if k != 'category'}
            for row in CATALOG if row['category'] == category]
        with patch.object(self.service, '_provider', return_value=provider):
            found = self.service.search_sectors('pcb')
            absent = self.service.search_sectors('MLCC')
            by_code = self.service.search_sectors(CODES[0])
        self.assertEqual(provider.catalog.call_count, 2)
        self.assertEqual([call.args[0] for call in provider.catalog.call_args_list],
                         ['industry', 'cn_concept'])
        self.assertEqual(found['exact'], CODES)
        self.assertEqual(by_code['matches'], [CATALOG[0]])
        self.assertEqual(absent['matches'], [])
        self.assertIn('未找到匹配', absent['unmatched_note'])
        found['matches'][0]['name'] = 'caller changed result'
        self.assertEqual(self.service.search_sectors('PCB')['matches'][0]['name'], 'PCB')

    def test_invalid_search_never_requests_provider(self):
        with patch.object(self.service, '_provider') as provider:
            for value in (None, '', '   ', ['PCB'], 'x' * 81):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.service.search_sectors(value)
        provider.assert_not_called()

    def test_demo_and_whole_0926_minute_block_search_and_research(self):
        for case in ('demo', 'priority'):
            with self.subTest(case=case):
                self.service.mode = 'demo' if case == 'demo' else 'live'
                current = datetime(2026, 9, 21, 9, 26, 59, tzinfo=SH) if case == 'priority' else NOW
                with patch('app.service.now_sh', return_value=current), \
                        patch.object(self.service, '_provider') as provider, \
                        patch.object(self.service, '_job') as job:
                    with self.assertRaises(ValueError):
                        self.service.search_sectors('PCB')
                    with self.assertRaises(ValueError):
                        self.service.run_sector_research(CODES, DATE, self.identity)
                    with self.assertRaises(ValueError):
                        self.service.run_llm('query', 'deepseek', DATE, self.identity,
                                             sector_codes=CODES, fetch_sectors=True)
                provider.assert_not_called()
                job.assert_not_called()

    def test_stale_report_binding_and_invalid_codes_reject_before_queue(self):
        with patch.object(self.service, '_provider') as provider, \
                patch.object(self.service, '_job') as job:
            for codes, identity in ((CODES, 'stale'), (CODES, None),
                                    ([], self.identity), ([CODES[0]] * 2, self.identity),
                                    (['885001'], self.identity)):
                with self.subTest(codes=codes, identity=identity), self.assertRaises(ValueError):
                    self.service.run_sector_research(codes, DATE, identity)
        provider.assert_not_called()
        job.assert_not_called()

    def test_cached_read_is_local_during_protection_and_excludes_raw(self):
        saved = self.service.sector_library.save(dict(evidence(), review_id=self.identity))
        with patch.object(self.service, '_provider', side_effect=AssertionError('read must stay local')), \
                patch('app.service.now_sh', return_value=datetime(2026, 9, 21, 9, 26, 59, tzinfo=SH)):
            cached = self.service.sector_research_result(DATE, self.identity)
            snapshot = self.service.snapshot()
        self.assertEqual(cached['evidence_id'], saved['evidence_id'])
        self.assertNotIn('raw', cached)
        self.assertNotIn('raw', cached['boards'][0])
        self.assertEqual(set(snapshot['sector_research']),
                         {'date', 'review_id', 'evidence_id', 'status', 'generated_at'})
        self.assertNotIn('boards', snapshot['sector_research'])

    def test_missing_cache_is_not_run_without_reading_financial_api(self):
        with patch.object(self.service, '_provider') as provider:
            result = self.service.sector_research_result(DATE, self.identity)
        self.assertEqual(result, {'status': 'not_run', 'date': DATE, 'review_id': self.identity})
        provider.assert_not_called()

    def test_supplement_does_not_change_report_version_scores_or_stock_pool(self):
        original_codes = list(self.service.codes)
        original_config = copy.deepcopy(self.service.config)
        worker = self.queued(self.service.run_sector_research, CODES, DATE, self.identity)
        with patch.object(self.service, '_provider', return_value=Mock()), \
                patch('app.service.load_catalog', return_value=CATALOG), \
                patch('app.service.build_sector_research', return_value=evidence()) as build:
            worker()
        self.assertEqual(build.call_args.args[1:3], (CODES, DATE))
        self.assertTrue(callable(build.call_args.kwargs['should_stop']))
        self.assertEqual(self.service.review, self.original)
        self.assertEqual(self.service.latest_review, self.original)
        self.assertEqual(self.service.saved_report(DATE), self.original)
        self.assertEqual(self.service.snapshot()['review_id'], self.identity)
        self.assertEqual(self.service.codes, original_codes)
        self.assertEqual(self.service.config, original_config)
        self.assertTrue(self.service.sector_research_result(DATE, self.identity)['evidence_id'])

    def test_ai_fetches_before_analysis_and_captures_provider_config_and_evidence(self):
        config = {'provider': 'deepseek', 'api_key': 'offline-llm-placeholder',
                  'model': 'original-fixture-model', 'label': 'Fixture provider'}
        worker = self.queue_ai(config)
        config.update(provider='openai', model='changed-after-submit', api_key='different-fixture')
        order = []
        def fetch(*args, **kwargs):
            order.append('fetch')
            return evidence()
        def analyze(captured, state, question):
            order.append('ai')
            self.assertEqual(captured['provider'], 'deepseek')
            self.assertEqual(captured['model'], 'original-fixture-model')
            self.assertEqual(state['review']['date'], DATE)
            self.assertEqual(state['auction']['rows'], [])
            self.assertEqual(state['sector_research']['review_id'], self.identity)
            self.assertNotIn('raw', state['sector_research'])
            self.assertNotIn('api_key', state)
            self.assertEqual(question, '研究 PCB 同行')
            return 'mocked analysis, not a real model call'
        with patch.object(self.service, '_provider', return_value=Mock()), \
                patch('app.service.load_catalog', return_value=CATALOG), \
                patch('app.service.build_sector_research', side_effect=fetch), \
                patch('app.llm.analyze', side_effect=analyze) as model:
            worker()
        self.assertEqual(order, ['fetch', 'ai'])
        model.assert_called_once()
        result = self.service.llm_results['deepseek']
        self.assertEqual(result['sector_codes'], CODES)
        self.assertEqual(result['question'], '研究 PCB 同行')
        self.assertEqual(result['review_id'], self.identity)
        self.assertRegex(result['sector_evidence_id'], r'^[a-f0-9]{24}$')
        archived = self.service.report_library.load_ai(self.original)['deepseek']
        self.assertEqual(archived['sector_evidence_id'], result['sector_evidence_id'])
        self.assertEqual(archived['sector_codes'], CODES)
        self.assertNotIn('api_key', archived)
        self.assertNotIn('offline-llm-placeholder', json.dumps(archived))

    def test_all_unavailable_evidence_is_saved_but_does_not_invoke_ai(self):
        worker = self.queue_ai()
        with patch.object(self.service, '_provider', return_value=Mock()), \
                patch('app.service.load_catalog', return_value=CATALOG), \
                patch('app.service.build_sector_research', return_value=evidence('unavailable')), \
                patch('app.llm.analyze') as model, self.assertRaisesRegex(ValueError, '未调用 AI'):
            worker()
        model.assert_not_called()
        self.assertEqual(self.service.sector_research_result(DATE, self.identity)['status'], 'unavailable')
        self.assertFalse(self.service.llm_results)

    def test_no_model_key_or_ambiguous_fetch_flag_reject_before_finance_requests(self):
        with patch.object(self.service, '_provider') as provider, \
                patch.object(self.service, '_job') as job, \
                patch('app.service.load_llm', return_value={'provider': 'deepseek'}):
            for arguments in ({'sector_codes': CODES, 'fetch_sectors': True},
                              {'sector_codes': CODES, 'fetch_sectors': False},
                              {'sector_codes': CODES, 'fetch_sectors': 'true'}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    self.service.run_llm('query', 'deepseek', DATE, self.identity, **arguments)
        provider.assert_not_called()
        job.assert_not_called()

    def test_report_revision_during_fetch_prevents_model_call(self):
        worker = self.queue_ai()
        revised = copy.deepcopy(self.original)
        revised['generated_at'] = DATE + 'T16:30:00+08:00'
        def fetch(*args, **kwargs):
            self.service.store.report(DATE, 'live', revised)
            self.service.review = revised
            return evidence()
        with patch.object(self.service, '_provider', return_value=Mock()), \
                patch('app.service.load_catalog', return_value=CATALOG), \
                patch('app.service.build_sector_research', side_effect=fetch), \
                patch('app.llm.analyze') as model, self.assertRaisesRegex(ValueError, '已经更新'):
            worker()
        model.assert_not_called()
        self.assertEqual(self.service.saved_report(DATE), revised)
        self.assertEqual(self.service.sector_research_result(DATE, report_identity(revised))['status'], 'not_run')

    def test_pending_fetch_cancelled_on_mode_generation_or_protection(self):
        for case in ('mode', 'generation', 'shutdown', 'priority'):
            with self.subTest(case=case):
                self.service.mode = 'live'
                self.service.shutdown.clear()
                worker = self.queued(self.service.run_sector_research, CODES, DATE, self.identity)
                if case == 'mode':
                    self.service.mode = 'demo'
                elif case == 'generation':
                    self.service.demo_generation += 1
                elif case == 'shutdown':
                    self.service.shutdown.set()
                with patch.object(self.service, '_provider') as provider, \
                        patch.object(self.service, '_auction_priority', return_value=case == 'priority'), \
                        self.assertRaises(ValueError):
                    worker()
                provider.assert_not_called()
        self.service.shutdown.clear()

    def test_fetch_failure_releases_lock_and_preserves_previous_cache(self):
        prior = self.service.sector_library.save(dict(evidence(), review_id=self.identity))
        worker = self.queued(self.service.run_sector_research, CODES, DATE, self.identity)
        with patch.object(self.service, '_provider', return_value=Mock()), \
                patch('app.service.load_catalog', return_value=CATALOG), \
                patch('app.service.build_sector_research', side_effect=ValueError('fixture failure')), \
                self.assertRaisesRegex(ValueError, 'fixture failure'):
            worker()
        self.assertFalse(self.service.sector_fetch_lock.locked())
        self.assertEqual(self.service.sector_research_result(DATE, self.identity)['evidence_id'], prior['evidence_id'])

    def test_http_search_post_research_read_export_and_stale_binding(self):
        saved = self.service.sector_library.save(dict(evidence(), review_id=self.identity))
        query = urllib.parse.urlencode({'date': DATE, 'review_id': self.identity})
        with self.local_http() as request, patch.object(self.service, '_provider') as provider:
            with patch.object(self.service, '_sector_catalog', return_value=CATALOG):
                result, _ = request('/api/sectors/search', {'query': 'PCB'})
            self.assertTrue(result['ok'])
            self.assertEqual(result['exact'], CODES)
            with patch.object(self.service, '_job', return_value=True) as job:
                result, _ = request('/api/sectors/research',
                                    {'codes': CODES, 'date': DATE, 'review_id': self.identity})
            self.assertTrue(result['ok'])
            self.assertTrue(result['started'])
            self.assertEqual(job.call_args.args[0], 'sectors')
            cached, _ = request('/api/sectors/research?' + query)
            self.assertEqual(cached['evidence_id'], saved['evidence_id'])
            exported, headers = request('/api/sectors/export?evidence_id=' + saved['evidence_id'])
            self.assertNotIn('raw', exported)
            self.assertNotIn('raw', exported['boards'][0])
            self.assertIn(saved['evidence_id'] + '.json', headers['Content-Disposition'])
            self.assertEqual(headers['Cache-Control'], 'no-store')
            with self.assertRaises(urllib.error.HTTPError) as failure:
                request('/api/sectors/research', {'codes': CODES, 'date': DATE, 'review_id': 'stale'})
            self.assertEqual(failure.exception.code, 400)
        provider.assert_not_called()

    def test_http_blocks_unmarked_mutations_and_arbitrary_export_paths(self):
        with self.local_http() as request, patch.object(self.service, '_provider') as provider:
            for identifier in ('../credentials', 'a' * 64, 'x' * 24, '', 'C:\\private\\credentials'):
                with self.subTest(identifier=identifier), self.assertRaises(urllib.error.HTTPError) as failure:
                    request('/api/sectors/export?' + urllib.parse.urlencode({'evidence_id': identifier}))
                self.assertEqual(failure.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                request('/api/sectors/search', {'query': 'PCB'}, {'X-Local-App': ''})
            self.assertEqual(failure.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                request('/api/sectors/research?date=' + DATE + '&review_id=stale')
            self.assertEqual(failure.exception.code, 400)
        provider.assert_not_called()

    def test_evidence_digest_rejects_modified_archive(self):
        saved = self.service.sector_library.save(dict(evidence(), review_id=self.identity))
        path = self.root / 'sector-research' / 'evidence' / (saved['evidence_id'] + '.json')
        changed = json.loads(path.read_text(encoding='utf-8'))
        changed['boards'][0]['member_count'] = 99
        path.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            self.service.sector_library.get(saved['evidence_id'])
        with self.assertRaises(ValueError):
            self.service.sector_research_result(DATE, self.identity)

    def test_new_report_version_has_separate_pointer_and_old_evidence_is_retained(self):
        first = self.service.sector_library.save(dict(evidence(), review_id=self.identity))
        replacement = dict(self.original, generated_at=DATE + 'T16:30:00+08:00')
        other_id = report_identity(replacement)
        self.assertEqual(self.service.sector_library.latest(DATE, other_id)['status'], 'not_run')
        second = self.service.sector_library.save(dict(evidence(), review_id=other_id))
        self.assertNotEqual(first['evidence_id'], second['evidence_id'])
        self.assertEqual(self.service.sector_library.get(first['evidence_id']), first)
        self.assertEqual(self.service.sector_library.latest(DATE, self.identity), first)
        self.assertEqual(self.service.sector_library.latest(DATE, other_id), second)

    def test_http_forwards_sector_request_with_ai_binding_without_calling_model(self):
        with self.local_http() as request, \
                patch.object(self.service, 'run_llm', return_value=True) as run:
            result, _ = request('/api/llm', {'question': 'PCB 比较', 'provider': 'deepseek',
                'review_date': DATE, 'review_id': self.identity,
                'sector_codes': CODES, 'fetch_sectors': True})
        self.assertTrue(result['ok'])
        self.assertTrue(result['started'])
        run.assert_called_once_with('PCB 比较', 'deepseek', DATE, self.identity,
                                    sector_codes=CODES, fetch_sectors=True)


if __name__ == '__main__':
    unittest.main()
