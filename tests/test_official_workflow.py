"""Official observations in the local report workflow, never real credentials."""
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import DEFAULT
from app.provider import APIError, HiThinkProvider
from app.reporting import report_identity
from app.sentiment import build_sentiment
from app.server import LocalServer
from app.service import Service, SH


DATE = '2026-09-18'
PAST = '2026-09-17'
NOW = datetime(2026, 9, 20, 12, tzinfo=SH)


def report(date=DATE):
    row = {'thscode':'000001.SZ', 'name':'测试证券', 'score':75,
           'consecutive_days':1, 'consecutive_lower_bound':False,
           'continue_day_text':'首板', 'seal_money':25, 'max_seal_money':100,
           'limit_up_reason':'测试官方原因', 'factors':{'seal_retention':25}}
    return {'date':date, 'mode':'live', 'status':'ready',
            'generated_at':date+'T15:30:00+08:00', 'warnings':[],
            'market':{'limit_up_count':1},
            'limit_up':{'count':1, 'status':'ready', 'rows':[row]},
            'raw':{'calendar':[PAST,DATE], 'pools_by_date':{date:[copy.deepcopy(row)]}}}


def context(status='ready'):
    return {'date':DATE, 'mode':'live', 'status':status, 'cancelled':False,
            'generated_at':'2026-09-20T12:00:00+08:00', 'requests':{'attempted':4,'max':4},
            'warnings':[], 'errors':[], 'definition':'测试官方样本口径，不等于全市场资金流',
            'benchmark':{'date':DATE, 'status':'ready', 'sample_count':1,
                         'valid_auction_count':1, 'mean_auction_pct':2.5,
                         'median_auction_pct':2.5, 'positive_ratio_pct':100,
                         'rows':[{'thscode':'000001.SZ','name':'风向样本测试','auction_pct':2.5,'tags':['样本标签']}],
                         'definition':'auction_pct百分数原值', 'warnings':[]},
            'dragon_tiger':{board:{'status':'ready','board_type':board,'trade_date':DATE,
                                  'groups':{'one_day':[], 'three_day':[], 'other':[]},
                                  'group_counts':{'one_day':0,'three_day':0,'other':0},
                                  'reported_count':0,'reported_stock_count':0,'received_rows':0,
                                  'shown_count':0,'definition':'三类榜单不相加','warnings':[]}
                            for board in ('all','org','hot_money')},
            'raw':{'private_upstream_field':'raw-observation-must-not-export'}}


class OfficialWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch('app.service.load_config',return_value=copy.deepcopy(DEFAULT)),
                        patch('app.service.finance_key',return_value=''),
                        patch('app.service.credential_status',return_value={}),
                        patch('app.ai_gateway.credential_dir',return_value=self.root/'credentials'),
                        patch.dict('os.environ',{'DEEPSEEK_API_KEY':'','OPENAI_API_KEY':'','CUSTOM_AI_API_KEY':''}),
                        patch('app.service.now_sh',return_value=NOW)]
        for item in self.patches:
            item.start()
        self.service = Service(self.root)
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.original, self.older = report(), report(PAST)
        for value in (self.older, self.original):
            self.service.store.report(value['date'],'live',value)
        self.service.review = self.original
        self.service.latest_review = self.original
        self.queued = {}

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def queue(self):
        def enqueue(name, worker):
            self.queued[name] = worker
            return True
        with patch.object(self.service,'_job',side_effect=enqueue):
            self.assertTrue(self.service.enrich_report(DATE,report_identity(self.service.saved_report(DATE))))
        return self.queued['evidence']

    def run_automatic_review(self, provider, official_call, date=DATE):
        """Queue and run one automatic review with the four-item fetch controlled."""

        def enqueue(name, worker):
            self.queued[name] = worker
            return True

        with patch.object(self.service, '_provider', return_value=provider), \
                patch.object(self.service, '_job', side_effect=enqueue), \
                patch('app.service.build_review', return_value=report(date)), \
                patch.object(self.service.daily_validation, 'label', return_value={'status': 'unavailable'}), \
                patch.object(self.service, '_save_markdown'), \
                patch.object(self.service, 'run_research'), \
                official_call:
            self.service.run_review(date, automatic=True)
            self.queued['review']()

    def test_automatic_review_reads_the_four_official_sections(self):
        provider = Mock()
        provider.calendar.return_value = [PAST, DATE]
        official = Mock(return_value=context('ready'))
        self.run_automatic_review(provider, patch('app.service.build_official_context', official))
        official.assert_called_once()
        self.assertEqual(official.call_args.args[1], DATE)
        self.assertTrue(callable(official.call_args.kwargs['should_stop']))
        stored = self.service.store.get_report(DATE, 'live')
        self.assertEqual(stored['official_context']['status'], 'ready')
        self.assertFalse([warning for warning in stored['warnings'] if '官方补充观察' in warning])

    def test_partial_official_observations_attach_with_a_warning(self):
        provider = Mock()
        provider.calendar.return_value = [PAST, DATE]
        self.run_automatic_review(provider, patch('app.service.build_official_context',
                                                  Mock(return_value=context('partial'))))
        stored = self.service.store.get_report(DATE, 'live')
        self.assertEqual(stored['official_context']['status'], 'partial')
        self.assertTrue(any('官方补充观察部分缺失' in warning for warning in stored['warnings']))

    def test_official_observation_failure_only_warns_and_keeps_the_report(self):
        provider = Mock()
        provider.calendar.return_value = [PAST, DATE]
        self.run_automatic_review(provider, patch('app.service.build_official_context',
                                                  Mock(side_effect=RuntimeError('boom'))))
        stored = self.service.store.get_report(DATE, 'live')
        self.assertNotIn('official_context', stored)
        self.assertTrue(any('官方补充观察读取异常' in warning for warning in stored['warnings']))
        self.assertEqual(stored['date'], DATE)

    def assert_original_saved(self):
        self.assertEqual(self.service.report_library.get(DATE,'live'),self.original)
        self.assertFalse((self.root/'reports'/f'{DATE}.json').exists())
        self.assertFalse((self.root/'reports'/f'{DATE}.md').exists())

    def test_guards_reject_before_provider_or_worker_creation(self):
        cases = ['demo','priority','review_running','future','not_closed','stale_id','missing_calendar','wrong_calendar']
        for case in cases:
            with self.subTest(case=case):
                self.service.mode = 'live'
                self.service.jobs = {}
                self.service.review = copy.deepcopy(self.original)
                date, identity, now = DATE, report_identity(self.original), NOW
                if case == 'demo': self.service.mode = 'demo'
                if case == 'priority': now = datetime(2026,9,20,9,20,tzinfo=SH)
                if case == 'review_running': self.service.jobs['review'] = {'status':'running'}
                if case == 'future': date = '2026-09-21'
                if case == 'not_closed': now = datetime(2026,9,18,14,59,tzinfo=SH)
                if case == 'stale_id': identity = 'stale'
                if case == 'missing_calendar': self.service.review['raw'].pop('calendar')
                if case == 'wrong_calendar': self.service.review['raw']['calendar'] = [PAST]
                if case in ('missing_calendar','wrong_calendar'):
                    identity = report_identity(self.service.review)
                with patch('app.service.now_sh',return_value=now), \
                        patch.object(self.service,'_provider') as provider, \
                        patch.object(self.service,'_job') as job, \
                        self.assertRaises(ValueError):
                    self.service.enrich_report(date,identity)
                provider.assert_not_called()
                job.assert_not_called()
        self.assert_original_saved()

    def test_duplicate_evidence_job_returns_false_without_provider(self):
        self.service.jobs['evidence'] = {'status':'running'}
        with patch.object(self.service,'_provider') as provider, patch.object(self.service,'_job') as job:
            self.assertFalse(self.service.enrich_report(DATE,report_identity(self.original)))
        provider.assert_not_called()
        job.assert_not_called()

    def test_success_creates_new_identity_markdown_and_preserves_market_scores(self):
        worker = self.queue()
        self.service.llm_results = {'deepseek':{'review_id':report_identity(self.original),'text':'old analysis'}}
        self.service.llm_result = 'old analysis'
        with patch.object(self.service,'_provider',return_value=Mock()) as provider, \
                patch('app.service.build_official_context',return_value=context()) as build:
            worker()
        provider.assert_called_once()
        self.assertEqual(build.call_args.args[1],DATE)
        self.assertTrue(callable(build.call_args.kwargs['should_stop']))
        updated = self.service.report_library.get(DATE,'live')
        self.assertNotEqual(report_identity(updated),report_identity(self.original))
        self.assertEqual(updated['market'],self.original['market'])
        self.assertEqual(updated['limit_up'],self.original['limit_up'])
        self.assertEqual(updated['raw'],self.original['raw'])
        self.assertEqual(updated['official_context'],context())
        self.assertIn('sentiment',updated)
        self.assertIn('enriched_at',updated)
        self.assertEqual(self.service.latest_review,updated)
        self.assertEqual(self.service.review,updated)
        self.assertFalse(self.service.llm_results)
        self.assertIsNone(self.service.llm_result)
        self.assertEqual(json.loads((self.root/'reports'/f'{DATE}.json').read_text(encoding='utf-8')),updated)
        markdown = (self.root/'reports'/f'{DATE}.md').read_text(encoding='utf-8')
        self.assertIn('风向样本测试',markdown)
        self.assertIn('封单',markdown)
        self.assertNotIn('raw-observation-must-not-export',markdown)

    def test_switching_history_during_enrichment_keeps_selected_view(self):
        worker = self.queue()
        self.service.load_report(PAST)
        with patch.object(self.service,'_provider',return_value=Mock()), \
                patch('app.service.build_official_context',return_value=context()):
            worker()
        self.assertEqual(self.service.review['date'],PAST)
        self.assertEqual(self.service.latest_review['date'],DATE)
        self.assertIn('official_context',self.service.latest_review)
        self.assertTrue(self.service.viewing_archive)

    def test_markdown_io_failure_keeps_new_identity_and_can_retry_export_locally(self):
        old_id = self.service.snapshot()['review_id']
        old_export = self.service.markdown_report(DATE,review_id=old_id,save=True)
        path = Path(old_export['path'])
        old_markdown = path.read_bytes()
        self.service.llm_results = {'deepseek':{'review_id':old_id,'text':'old analysis'}}
        self.service.llm_result = 'old analysis'
        worker = self.queue()
        with patch.object(self.service,'_provider',return_value=Mock()), \
                patch('app.service.build_official_context',return_value=context()), \
                patch.object(self.service,'_save_markdown',side_effect=OSError('fixture file locked')), \
                self.assertRaisesRegex(ValueError,'已保存且报告版本已更新.*Markdown.*重试'):
            worker()
        stored = self.service.report_library.get(DATE,'live')
        new_id = report_identity(stored)
        self.assertNotEqual(old_id,new_id)
        self.assertEqual(report_identity(self.service.review),new_id)
        self.assertEqual(report_identity(self.service.latest_review),new_id)
        self.assertEqual(self.service.snapshot()['review_id'],new_id)
        self.assertFalse(self.service.llm_results)
        self.assertIsNone(self.service.llm_result)
        self.assertEqual(json.loads((self.root/'reports'/f'{DATE}.json').read_text(encoding='utf-8')),stored)
        self.assertEqual(path.read_bytes(),old_markdown)
        with patch.object(self.service,'_provider',side_effect=AssertionError('export must not fetch')):
            saved = self.service.markdown_report(DATE,review_id=new_id,save=True)
        self.assertEqual(saved['review_id'],new_id)
        self.assertIn('风向样本测试',path.read_text(encoding='utf-8'))
        self.assertNotEqual(path.read_bytes(),old_markdown)
        self.assertEqual(self.service.report_library.read_markdown(saved['filename'],'live',saved['sha256']),path.read_bytes())

    def test_markdown_preflight_failure_leaves_all_report_evidence_unchanged(self):
        old_id = self.service.snapshot()['review_id']
        old_ai = {'deepseek':{'review_id':old_id,'text':'preserved analysis'}}
        self.service.llm_results = copy.deepcopy(old_ai)
        worker = self.queue()
        with patch.object(self.service,'_provider',return_value=Mock()), \
                patch('app.service.build_official_context',return_value=context()), \
                patch.object(self.service.report_library,'markdown',side_effect=ValueError('fixture render error')), \
                self.assertRaisesRegex(ValueError,'fixture render error'):
            worker()
        self.assert_original_saved()
        self.assertEqual(self.service.snapshot()['review_id'],old_id)
        self.assertEqual(self.service.review,self.original)
        self.assertEqual(self.service.latest_review,self.original)
        self.assertEqual(self.service.llm_results,old_ai)

    def test_storage_revision_during_request_cannot_be_overwritten(self):
        worker = self.queue()
        newer = copy.deepcopy(self.original)
        newer['generated_at'] = DATE+'T16:30:00+08:00'
        def finish(*args, **kwargs):
            self.service.store.report(DATE,'live',newer)
            return context()
        with patch.object(self.service,'_provider',return_value=Mock()), \
                patch('app.service.build_official_context',side_effect=finish), \
                self.assertRaisesRegex(ValueError,'已更新'):
            worker()
        self.assertEqual(self.service.report_library.get(DATE,'live'),newer)
        self.assertFalse((self.root/'reports').exists())

    def test_cancelled_or_unavailable_context_never_writes_reports(self):
        for state in ('cancelled','unavailable','mode_changed','generation_changed','shutdown','priority'):
            with self.subTest(state=state):
                self.service.mode = 'live'
                self.service.jobs = {}
                self.service.shutdown.clear()
                worker = self.queue()
                payload = context('unavailable' if state == 'unavailable' else 'ready')
                if state == 'cancelled': payload['cancelled'] = True
                def finish(*args, **kwargs):
                    if state == 'mode_changed': self.service.mode = 'demo'
                    if state == 'generation_changed': self.service.demo_generation += 1
                    if state == 'shutdown': self.service.shutdown.set()
                    return payload
                with patch.object(self.service,'_provider',return_value=Mock()), \
                        patch('app.service.build_official_context',side_effect=finish), \
                        patch.object(self.service,'_auction_priority',return_value=state == 'priority'), \
                        self.assertRaises(ValueError):
                    worker()
                self.assert_original_saved()

    def test_new_official_fields_are_escaped_and_raw_stays_out_of_public_state(self):
        payload = context()
        payload['benchmark']['rows'][0]['name'] = '<script>unsafe()</script>|第二列\n## 假标题'
        worker = self.queue()
        with patch.object(self.service,'_provider',return_value=Mock()), \
                patch('app.service.build_official_context',return_value=payload):
            worker()
        markdown = (self.root/'reports'/f'{DATE}.md').read_text(encoding='utf-8')
        self.assertNotIn('<script>',markdown)
        self.assertNotIn('\n## 假标题',markdown)
        public = self.service.snapshot()['review']['official_context']
        self.assertNotIn('raw',public)
        self.assertNotIn('raw-observation-must-not-export',json.dumps(public))

    def test_partial_retry_does_not_replace_ready_official_context(self):
        self.original['official_context'] = context()
        self.service.store.report(DATE,'live',self.original)
        worker = self.queue()
        with patch.object(self.service,'_provider',return_value=Mock()), \
                patch('app.service.build_official_context',return_value=context('partial')), \
                self.assertRaisesRegex(ValueError,'保留上一份完整'):
            worker()
        self.assert_original_saved()

    def test_legacy_sentiment_is_cached_without_mutating_report_or_identity(self):
        before = copy.deepcopy(self.original)
        identity = report_identity(before)
        with patch('app.service.build_sentiment',wraps=build_sentiment) as build:
            first, second = self.service.snapshot(), self.service.snapshot()
            self.assertEqual(build.call_count,1)
        self.assertEqual(first['review_sentiment'],second['review_sentiment'])
        self.assertEqual(first['review_id'],identity)
        self.assertNotIn('sentiment',first['review'])
        self.assertEqual(self.service.review,before)
        self.assertEqual(report_identity(self.service.review),identity)
        first['review_sentiment']['status'] = 'caller mutation'
        self.assertNotEqual(self.service.snapshot()['review_sentiment']['status'],'caller mutation')

    def test_existing_sentiment_not_recalculated_and_changes_with_new_report(self):
        enriched = copy.deepcopy(self.original)
        enriched['sentiment'] = build_sentiment(enriched)
        self.service.review = enriched
        with patch('app.service.build_sentiment',wraps=build_sentiment) as build:
            self.service.snapshot()
            build.assert_not_called()
            self.service.review = self.older
            state = self.service.snapshot()
            self.assertEqual(build.call_count,1)
        self.assertEqual(state['review_sentiment']['date'],PAST)

    def test_shared_provider_cooldown_visible_in_snapshot_and_readiness_without_request(self):
        first = HiThinkProvider('offline-workflow-cooldown-fixture',min_interval=0)
        second = HiThinkProvider('offline-workflow-cooldown-fixture',min_interval=0)
        try:
            with patch('app.provider.time.monotonic',return_value=100):
                first._note_rate_limit(90)
                self.service.provider = second
                with patch('app.provider.urlopen',side_effect=AssertionError('no network')):
                    state = self.service.snapshot()
                    diagnostics = self.service.diagnostics()
            self.assertEqual(state['api'],{'rate_limited':True,'cooldown_seconds':90})
            relevant = [row for row in diagnostics['checks'] if '冷却' in row['message'] or '限流' in row['message']]
            self.assertTrue(relevant)
            self.assertTrue(any(row['status']=='warn' and '90' in row['message'] for row in relevant))
            self.service.mode = 'demo'
            self.assertEqual(self.service.snapshot()['api'],{'rate_limited':False,'cooldown_seconds':0})
        finally:
            with HiThinkProvider._limit_lock:
                HiThinkProvider._cooldown_until.pop(first._limiter_id,None)
                HiThinkProvider._next_request.pop(first._limiter_id,None)

    def test_auction_429_keeps_full_ninety_second_retry_deadline(self):
        clock = datetime(2026,9,18,9,16,tzinfo=SH)
        self.service.running = True
        self.service.codes = ['000001.SZ']
        provider = Mock()
        provider.auction.side_effect = APIError('限流',code=429,retry_after_seconds=90)
        with patch.object(self.service,'_provider',return_value=provider), \
                patch('app.service.time.monotonic',return_value=100):
            self.service.collect_cycle('live',clock=lambda:clock)
            state = self.service.snapshot()
        self.assertEqual(provider.auction.call_count,1)
        self.assertEqual(self.service.api_backoff_until,190)
        self.assertEqual(state['api']['cooldown_seconds'],90)

    def test_scheduler_does_not_poll_before_recorded_retry_deadline(self):
        self.service.running = True
        self.service.prepared_date = DATE
        self.service.days = [PAST,DATE]
        self.service.config.update(universe='watchlist',auto_review=False)
        self.service.api_backoff_until = 190
        times = iter([100,189.9,190.1])
        tick = [0]
        def wait(_):
            tick[0] = next(times,None)
            return tick[0] is None
        with patch.object(self.service.shutdown,'wait',side_effect=wait), \
                patch('app.service.now_sh',return_value=datetime(2026,9,18,9,16,tzinfo=SH)), \
                patch('app.service.time.monotonic',side_effect=lambda:tick[0]), \
                patch.object(self.service,'collect_cycle') as collect:
            self.service._loop()
        collect.assert_called_once_with('live')

    def test_http_enrichment_requires_matching_report_id_and_exposes_started(self):
        server = LocalServer(('127.0.0.1',0),self.service)
        worker = threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{server.server_port}/api/reports/enrich'
        def request(identity):
            return urllib.request.Request(url,data=json.dumps({'date':DATE,'review_id':identity}).encode(),
                                          headers={'Content-Type':'application/json','X-Local-App':'auction-lab'})
        try:
            with patch.object(self.service,'_job',return_value=True) as job:
                with opener.open(request(report_identity(self.original)),timeout=3) as response:
                    result = json.load(response)
                self.assertTrue(result['ok'])
                self.assertTrue(result['started'])
                self.assertEqual(job.call_args.args[0],'evidence')
            with patch.object(self.service,'_provider') as provider, self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(request('stale'),timeout=3)
            self.assertEqual(error.exception.code,400)
            provider.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
