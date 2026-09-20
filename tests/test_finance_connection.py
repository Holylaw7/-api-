"""Dedicated finance setup uses temporary credentials and fake HTTP only."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import config
from app.config import DEFAULT
from app.provider import APIError
from app.server import LocalServer
from app.service import Service, SH


NOW = datetime(2026, 9, 20, 17, 0, tzinfo=SH)
FIXTURE_KEY = 'local-fixture-finance-token'


class Response:
    status = 200
    headers = {}

    def __init__(self, envelope):
        self.envelope = envelope

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _):
        return json.dumps(self.envelope).encode()


class FinanceConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credentials = self.root / 'outside-project-credentials'
        self.patches = [
            patch('app.config.credential_dir', return_value=self.credentials),
            patch('app.config._windows_finance_key', return_value=''),
            patch('app.ai_gateway.credential_dir', return_value=self.credentials),
            patch.dict(os.environ, {'HITHINK_FINANCE_API_KEY':'', 'DEEPSEEK_API_KEY':'',
                                    'OPENAI_API_KEY':'', 'CUSTOM_AI_API_KEY':''}),
            patch('app.service.load_config', return_value=copy.deepcopy(DEFAULT)),
            patch('app.service.now_sh', return_value=NOW),
        ]
        for item in self.patches:
            item.start()
        self.service = Service(self.root / 'project-data')
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def save_key(self):
        return self.service.save_finance_config(FIXTURE_KEY)

    def run_test_worker(self):
        workers = []
        with patch.object(self.service, '_job', side_effect=lambda name, work: workers.append(work) or True):
            self.assertTrue(self.service.run_finance_test())
        workers[0]()

    @contextmanager
    def http(self):
        server = LocalServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, body=None, marked=True):
            headers = {'Content-Type':'application/json'}
            if marked:
                headers['X-Local-App'] = 'auction-lab'
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,
                data=None if body is None else json.dumps(body).encode(), headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=3)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            yield request
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_first_run_has_visible_unconfigured_status_without_network(self):
        with patch('app.service.HiThinkProvider') as provider:
            status = self.service.finance_status()
            snapshot = self.service.snapshot()
        self.assertFalse(status['configured'])
        self.assertEqual(status['credential_source'], 'missing')
        self.assertTrue(status['can_save'])
        self.assertFalse(status['can_test'])
        self.assertEqual(status['test']['status'], 'not_tested')
        self.assertEqual(snapshot['finance'], status)
        provider.assert_not_called()

    def test_save_only_persists_outside_project_and_clears_old_clients(self):
        self.service.provider = object()
        self.service.sector_catalog = [{'old':'cached'}]
        self.service.sector_catalog_at = 7
        self.service.finance_connection_test = {'status':'success', 'ok':True}
        with patch.object(self.service, 'start') as start, patch('app.service.HiThinkProvider') as provider:
            status = self.save_key()
        self.assertTrue(status['configured'])
        self.assertEqual(status['credential_source'], 'user_file')
        self.assertTrue(status['persisted'])
        self.assertEqual(status['test']['status'], 'not_tested')
        self.assertFalse(self.service.running)
        self.assertIsNone(self.service.provider)
        self.assertEqual(self.service.sector_catalog, [])
        self.assertEqual(self.service.sector_catalog_at, 0)
        self.assertTrue((self.credentials / 'credentials.env').exists())
        self.assertFalse((self.root / 'project-data' / 'credentials.env').exists())
        self.assertNotIn(FIXTURE_KEY, json.dumps(status))
        self.assertEqual(os.environ['HITHINK_FINANCE_API_KEY'], '')
        start.assert_not_called()
        provider.assert_not_called()

    def test_saved_file_precedes_old_environment_without_modifying_it(self):
        os.environ['HITHINK_FINANCE_API_KEY'] = 'old-process-fixture'
        with patch('app.config._windows_finance_key', return_value='old-user-fixture'):
            self.assertEqual(config.finance_key(), 'old-process-fixture')
            self.save_key()
            self.assertEqual(config.finance_key(), FIXTURE_KEY)
            self.assertEqual(config.credential_status()['credential_source'], 'user_file')
        self.assertEqual(os.environ['HITHINK_FINANCE_API_KEY'], 'old-process-fixture')

    def test_environment_fallback_sources_are_explicit(self):
        with patch('app.config._windows_finance_key', return_value='user-fixture'):
            self.assertEqual(config.finance_key(), 'user-fixture')
            self.assertEqual(config.credential_status(), {'credential_source':'user_environment', 'credential_persisted':True})
            os.environ['HITHINK_FINANCE_API_KEY'] = 'process-fixture'
            self.assertEqual(config.finance_key(), 'process-fixture')
            self.assertEqual(config.credential_status(), {'credential_source':'process', 'credential_persisted':False})

    def test_rejects_nontext_whitespace_controls_and_oversize_without_writing(self):
        for value in (None, 42, True, {}, [], '', ' ', 'x y', 'x\ny', 'x\x00y', 'x\x7fy', 'x'*513, '"key"', '密钥'):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.service.save_finance_config(value)
        self.assertFalse(self.credentials.exists())

    def test_preserves_unrelated_credentials_lines(self):
        self.credentials.mkdir()
        path = self.credentials / 'credentials.env'
        path.write_text('OTHER_SETTING=keep\nHITHINK_FINANCE_API_KEY=old-fixture\n', encoding='utf-8')
        self.save_key()
        content = path.read_text(encoding='utf-8')
        self.assertIn('OTHER_SETTING=keep', content)
        self.assertEqual(content.count('HITHINK_FINANCE_API_KEY='), 1)

    def test_save_requires_stopped_monitor_and_completed_jobs(self):
        self.service.running = True
        self.assertFalse(self.service.finance_status()['can_save'])
        with self.assertRaisesRegex(ValueError, '停止'):
            self.save_key()
        self.service.running = False
        self.service.jobs['review'] = {'status':'running'}
        with self.assertRaisesRegex(ValueError, '任务'):
            self.save_key()
        self.service.jobs.clear()
        with self.service.sector_catalog_lock:
            with self.assertRaisesRegex(ValueError, '任务'):
                self.save_key()

    def test_demo_and_whole_protected_minute_reject_save_and_test(self):
        self.save_key()
        self.service.mode = 'demo'
        with self.assertRaisesRegex(ValueError, '演示'):
            self.save_key()
        with self.assertRaisesRegex(ValueError, '演示'):
            self.service.run_finance_test()
        self.service.mode = 'live'
        for hour, minute, second in ((9,10,0),(9,26,59)):
            with patch('app.service.now_sh', return_value=NOW.replace(hour=hour,minute=minute,second=second)):
                status = self.service.finance_status()
                self.assertFalse(status['can_save'])
                self.assertFalse(status['can_test'])
                with self.assertRaisesRegex(ValueError, '09:10'):
                    self.save_key()
                with self.assertRaisesRegex(ValueError, '09:10'):
                    self.service.run_finance_test()

    def test_success_is_one_official_request_without_collecting_or_generating(self):
        self.save_key()
        reply = Response({'code':0, 'data':{'item':[{'date':'20260917'}, {'date':'20260918'}]}})
        with patch('app.provider.urlopen', return_value=reply) as remote, \
                patch.object(self.service, 'start') as start, patch.object(self.service, 'prepare') as prepare, \
                patch.object(self.service, 'run_review') as review, patch('app.llm.analyze') as llm:
            self.run_test_worker()
        self.assertEqual(remote.call_count, 1)
        request = remote.call_args.args[0]
        self.assertEqual(request.full_url, 'https://fuyao.aicubes.cn/api/a-share/calendar/trading-days')
        self.assertEqual(request.get_header('X-api-key'), FIXTURE_KEY)
        self.assertNotIn(FIXTURE_KEY, request.full_url)
        status = self.service.finance_status()
        self.assertEqual(status['test']['status'], 'success')
        self.assertEqual(status['test']['calendar_count'], 2)
        self.assertEqual(status['test']['latest_trade_date'], '2026-09-18')
        self.assertIsNotNone(status['test']['checked_at'])
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.days, [])
        for action in (start, prepare, review, llm):
            action.assert_not_called()
        self.assertNotIn(FIXTURE_KEY, json.dumps(self.service.snapshot()))

    def test_business_failure_is_safe_and_does_not_retry(self):
        self.save_key()
        reply = Response({'code':5001, 'message':FIXTURE_KEY, 'request_id':FIXTURE_KEY})
        with patch('app.provider.urlopen', return_value=reply) as remote:
            with self.assertRaisesRegex(ValueError, '验证失败'):
                self.run_test_worker()
        self.assertEqual(remote.call_count, 1)
        status = self.service.finance_status()
        self.assertEqual(status['test']['status'], 'error')
        self.assertFalse(status['test']['ok'])
        self.assertNotIn(FIXTURE_KEY, json.dumps(status))

    def test_http_non200_cannot_be_success_even_with_zero_business_code(self):
        self.save_key()
        reply = Response({'code':0, 'data':{'item':[{'date':'20260918'}]}})
        reply.status = 503
        with patch('app.provider.urlopen', return_value=reply) as remote:
            with self.assertRaises(ValueError):
                self.run_test_worker()
        self.assertEqual(remote.call_count, 1)
        self.assertFalse(self.service.finance_status()['test']['ok'])

    def test_empty_invalid_calendar_and_unexpected_errors_do_not_pass(self):
        self.save_key()
        for payload in ([], ['2026-02-30'], ['2026-9-18'], [123], None):
            with self.subTest(payload=payload), patch('app.service.HiThinkProvider') as provider:
                provider.return_value.calendar.return_value = payload
                with self.assertRaises(ValueError):
                    self.run_test_worker()
                self.assertFalse(self.service.finance_status()['test']['ok'])
        with patch('app.service.HiThinkProvider', side_effect=RuntimeError(FIXTURE_KEY)):
            with self.assertRaises(ValueError) as error:
                self.run_test_worker()
        self.assertNotIn(FIXTURE_KEY, str(error.exception))

    def test_key_change_invalidates_queued_test_and_previous_success(self):
        self.save_key()
        workers = []
        with patch.object(self.service, '_job', side_effect=lambda name, work: workers.append(work) or True):
            self.service.run_finance_test()
        config.save_finance_key('replacement-fixture')
        with patch('app.service.HiThinkProvider') as provider, self.assertRaises(ValueError):
            workers[0]()
        provider.assert_not_called()
        self.assertEqual(self.service.finance_status()['test']['status'], 'not_tested')

    def test_entering_protection_before_request_cancels_without_network(self):
        self.save_key()
        workers = []
        with patch.object(self.service, '_job', side_effect=lambda name, work: workers.append(work) or True):
            self.service.run_finance_test()
        with patch('app.service.now_sh', return_value=NOW.replace(hour=9,minute=10)), \
                patch('app.service.HiThinkProvider') as provider, self.assertRaises(ValueError):
            workers[0]()
        provider.assert_not_called()

    def test_one_test_at_a_time_and_configuration_is_locked_until_done(self):
        self.save_key()
        entered, release = threading.Event(), threading.Event()

        def calendar():
            entered.set()
            release.wait(2)
            return ['2026-09-18']

        with patch('app.service.HiThinkProvider') as provider:
            provider.return_value.calendar.side_effect = calendar
            self.assertTrue(self.service.run_finance_test())
            self.assertTrue(entered.wait(2))
            try:
                self.assertFalse(self.service.run_finance_test())
                status = self.service.finance_status()
                self.assertFalse(status['can_save'])
                self.assertFalse(status['can_test'])
                with self.assertRaises(ValueError):
                    self.service.save_finance_config('another-fixture')
            finally:
                release.set()
            deadline = time.monotonic()+2
            while self.service.jobs['finance_test']['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.01)
        provider.assert_called_once()
        self.assertEqual(provider.call_args.kwargs['max_retries'], 0)
        self.assertEqual(self.service.jobs['finance_test']['status'], 'done')

    def test_http_setup_routes_are_local_and_do_not_expose_or_start(self):
        with self.http() as request, patch.object(self.service, 'start') as start, \
                patch('app.service.HiThinkProvider') as provider:
            code, status = request('/api/finance/status')
            self.assertEqual(code, 200)
            self.assertFalse(status['configured'])
            code, _ = request('/api/finance/config', {'api_key':FIXTURE_KEY}, marked=False)
            self.assertEqual(code, 403)
            code, result = request('/api/finance/config', {'api_key':FIXTURE_KEY})
            self.assertEqual(code, 200)
            self.assertTrue(result['finance']['configured'])
            self.assertNotIn(FIXTURE_KEY, json.dumps(result))
            code, _ = request('/api/finance/config', {'api_key':FIXTURE_KEY,'base_url':'https://other.invalid'})
            self.assertEqual(code, 400)
            code, _ = request('/api/finance/config', {'api_key':True})
            self.assertEqual(code, 400)
            code, _ = request('/api/finance/test', {'api_key':FIXTURE_KEY})
            self.assertEqual(code, 400)
        start.assert_not_called()
        provider.assert_not_called()

    def test_legacy_save_endpoint_still_starts_after_save(self):
        with self.http() as request, patch.object(self.service, 'start') as start:
            code, result = request('/api/credentials', {'api_key':FIXTURE_KEY})
        self.assertEqual(code, 200)
        self.assertTrue(result['ok'])
        start.assert_called_once()
        self.assertEqual(config.finance_key(), FIXTURE_KEY)

    def test_http_test_queues_job_using_saved_key_only(self):
        self.save_key()
        with self.http() as request, patch.object(self.service, 'run_finance_test', return_value=True) as run:
            code, result = request('/api/finance/test', {})
        self.assertEqual(code, 200)
        self.assertTrue(result['started'])
        self.assertIn('finance', result)
        run.assert_called_once_with()

    def test_exit_demo_without_key_restores_only_live_report_and_stays_stopped(self):
        real_report = {'date':'2026-09-18', 'mode':'live', 'status':'partial', 'generated_at':NOW.isoformat()}
        demo_report = {'date':'2026-09-19', 'mode':'demo', 'status':'ready', 'generated_at':NOW.isoformat()}
        self.service.store.report(real_report['date'], 'live', real_report)
        self.service.store.report(demo_report['date'], 'demo', demo_report)
        self.service.mode = 'demo'
        self.service.review = demo_report
        self.service.codes = ['000001.SZ']
        self.service.session_date = '2026-01-05'
        self.service.prepared_date = '2026-01-05'
        self.service.stock_analysis = {'demo':True}
        with patch('app.service.HiThinkProvider') as provider, patch.object(self.service, 'start') as start:
            self.service.exit_demo()
        self.assertEqual(self.service.mode, 'live')
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.review, real_report)
        self.assertEqual(self.service.latest_review, real_report)
        self.assertEqual(self.service.codes, [])
        self.assertIsNone(self.service.session_date)
        self.assertIsNone(self.service.stock_analysis)
        self.assertIsNone(self.service.prepared_date)
        self.assertEqual(self.service.engine.summary()['symbol_count'], 0)
        self.assertTrue(self.service.finance_status()['can_save'])
        self.save_key()
        provider.assert_not_called()
        start.assert_not_called()

    def test_queued_demo_cannot_write_after_exit(self):
        workers = []
        with patch('app.service.threading.Thread') as thread:
            thread.side_effect = lambda **kwargs: workers.append(kwargs['target']) or type('Queued', (), {'start':lambda self:None})()
            self.service.demo()
        self.assertEqual(self.service.mode, 'demo')
        self.service.exit_demo()
        with patch.object(self.service.store, 'batch') as batch:
            workers[0]()
        batch.assert_not_called()
        self.assertEqual(self.service.mode, 'live')
        self.assertEqual(self.service.engine.summary()['symbol_count'], 0)

    def test_exit_demo_in_live_mode_is_noop_and_http_needs_no_key(self):
        self.service.codes = ['600519.SH']
        self.service.running = True
        self.service.exit_demo()
        self.assertTrue(self.service.running)
        self.assertEqual(self.service.codes, ['600519.SH'])
        self.service.mode = 'demo'
        with self.http() as request, patch('app.service.HiThinkProvider') as provider:
            code, result = request('/api/demo/exit', {})
        self.assertEqual(code, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(self.service.mode, 'live')
        self.assertFalse(self.service.running)
        provider.assert_not_called()

    def test_server_can_open_without_autostart_or_credential_read(self):
        from app.server import serve
        with patch('app.server.Service') as service, patch('app.server.LocalServer'), \
                patch('app.server.finance_key') as read_key:
            serve(0, auto_start=False)
        read_key.assert_not_called()
        service.return_value.start.assert_not_called()
        service.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
