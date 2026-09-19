import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ai_studio import AIStudio, AIServer, load_market_context, _read_local
import launch_ai


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = 'deepseek'
        self.configs = {p: {'provider': p, 'label': p, 'base_url': 'https://example.test',
                             'model': p + '-model', 'api_key': 'fake-test-key'}
                        for p in ('deepseek', 'openai', 'custom')}
        self.public_patch = patch('app.ai_studio.ai_gateway.profiles_state', side_effect=self.public)
        self.config_patch = patch('app.ai_studio.ai_gateway.active_config', side_effect=lambda provider=None: self.configs[provider or self.provider])
        self.public_patch.start()
        self.config_patch.start()
        self.addCleanup(self.public_patch.stop)
        self.addCleanup(self.config_patch.stop)
        self.studio = AIStudio(self.temp.name)

    def public(self):
        return {'active_provider': self.provider,
                'profiles': [{'id': p, 'label': p, 'configured': True, 'has_key': True,
                              'model': p + '-model'} for p in self.configs]}

    def wait(self, studio=None):
        studio = studio or self.studio
        deadline = time.monotonic() + 3
        while studio.snapshot()['job']['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotEqual(studio.snapshot()['job']['status'], 'running')

    def complete(self, config, messages, **kwargs):
        return {'text': '已答复', 'model': config['model'], 'provider': config['provider'], 'warnings': []}

    def test_history_isolated_by_provider_and_no_config_persisted(self):
        captured = []
        def answer(config, messages, **kwargs):
            captured.append((config['provider'], messages))
            return self.complete(config, messages)
        with patch('app.ai_studio.ai_gateway.complete', side_effect=answer):
            self.assertTrue(self.studio.chat('只属于DeepSeek的提问'))
            self.wait()
            self.provider = 'openai'
            self.assertEqual(self.studio.snapshot()['messages'], [])
            self.assertTrue(self.studio.chat('只属于OpenAI的提问'))
            self.wait()
        self.assertNotIn('只属于DeepSeek', json.dumps(captured[1], ensure_ascii=False))
        path = Path(self.temp.name) / 'ai-messages.json'
        self.assertNotIn('fake-test-key', path.read_text(encoding='utf-8'))
        reloaded = AIStudio(self.temp.name)
        self.assertEqual(reloaded.snapshot()['messages'][0]['content'], '只属于OpenAI的提问')
        self.provider = 'deepseek'
        self.assertEqual(reloaded.snapshot()['messages'][0]['content'], '只属于DeepSeek的提问')

    def test_inflight_config_and_context_captured_and_tasks_exclusive(self):
        started, finish = threading.Event(), threading.Event()
        captured = {}
        def answer(config, messages, **kwargs):
            captured.update(config=config, messages=messages)
            started.set()
            finish.wait(2)
            return self.complete(config, messages)
        self.studio.context = {'summary': {'marker': 'original-context'}, 'mode': 'demo', 'cached': True}
        with patch('app.ai_studio.ai_gateway.complete', side_effect=answer):
            self.assertTrue(self.studio.chat('研究问题', True))
            self.assertTrue(started.wait(1))
            self.provider = 'openai'
            self.configs['deepseek']['model'] = 'changed-model'
            self.studio.context['summary']['marker'] = 'changed-context'
            self.assertFalse(self.studio.chat('不应启动'))
            self.assertFalse(self.studio.load_context())
            with self.assertRaises(ValueError):
                self.studio.clear_chat()
            with self.assertRaises(ValueError):
                self.studio.save_profile({'provider': 'deepseek'})
            finish.set()
            self.wait()
        self.assertEqual(captured['config']['model'], 'deepseek-model')
        self.assertIn('original-context', captured['messages'][-1]['content'])
        self.assertNotIn('changed-context', captured['messages'][-1]['content'])
        self.assertEqual(self.studio.snapshot()['messages'], [])
        self.provider = 'deepseek'
        self.assertEqual(len(self.studio.snapshot()['messages']), 2)
        self.assertEqual(self.studio.snapshot()['messages'][0]['content'], '研究问题')

    def test_market_context_opt_in_and_requires_preview(self):
        with self.assertRaisesRegex(ValueError, '预览'):
            self.studio.chat('研究', True)
        self.studio.context = {'summary': {'marker': 'market-only'}, 'mode': 'live'}
        with patch('app.ai_studio.ai_gateway.complete', side_effect=self.complete) as mock:
            self.assertTrue(self.studio.chat('普通问题'))
            self.wait()
        self.assertNotIn('market-only', json.dumps(mock.call_args.args[1]))

    def test_clear_only_active_history(self):
        for provider in ('deepseek', 'openai'):
            self.studio.messages[provider] = [{'role': 'user', 'content': provider}]
        self.studio.clear_chat()
        self.assertEqual(self.studio.messages['deepseek'], [])
        self.assertEqual(len(self.studio.messages['openai']), 1)

    def test_explicit_page_provider_wins_over_changed_global_selection(self):
        self.provider = 'deepseek'
        self.studio.messages['deepseek'] = [{'role': 'user', 'content': 'other-provider-history'}]
        with patch('app.ai_studio.ai_gateway.complete', side_effect=self.complete) as mock:
            self.assertTrue(self.studio.chat('发给页面选择的OpenAI', provider='openai'))
            self.wait()
        config, messages = mock.call_args.args[:2]
        self.assertEqual(config['provider'], 'openai')
        self.assertNotIn('other-provider-history', json.dumps(messages))
        self.assertEqual(len(self.studio.messages['openai']), 2)
        self.assertEqual(len(self.studio.messages['deepseek']), 1)

    def test_explicit_clear_provider_wins_over_changed_global_selection(self):
        self.provider = 'deepseek'
        for provider in ('deepseek', 'openai'):
            self.studio.messages[provider] = [{'role': 'user', 'content': provider}]
        self.studio.clear_chat('openai')
        self.assertEqual(len(self.studio.messages['deepseek']), 1)
        self.assertEqual(self.studio.messages['openai'], [])
        for provider in ('unknown', {}, 1):
            with self.assertRaises(ValueError):
                self.studio.clear_chat(provider)
            with self.assertRaises(ValueError):
                self.studio.chat('hello', provider=provider)

    def test_validation_and_no_key_does_not_call_model(self):
        with patch('app.ai_studio.ai_gateway.complete') as mock:
            for message in ('', 'x' * 8001, None):
                with self.assertRaises(ValueError):
                    self.studio.chat(message)
            with self.assertRaises(ValueError):
                self.studio.chat('hello', 'false')
            self.configs['deepseek']['api_key'] = ''
            with self.assertRaisesRegex(ValueError, 'API Key'):
                self.studio.chat('hello')
            mock.assert_not_called()

    def test_completion_failure_is_visible_and_not_retried(self):
        with patch('app.ai_studio.ai_gateway.complete', side_effect=ValueError('模型请求失败，请重试')) as mock:
            self.assertTrue(self.studio.chat('hello'))
            self.wait()
        self.assertEqual(mock.call_count, 1)
        state = self.studio.snapshot()
        self.assertEqual(state['job']['status'], 'error')
        self.assertIn('模型请求失败', state['job']['message'])
        self.assertEqual(len(state['messages']), 1)

    def test_connection_test_does_not_generate(self):
        with patch('app.ai_studio.ai_gateway.test_connection', return_value={'ok': False, 'provider': 'deepseek', 'message': '认证失败'}), patch('app.ai_studio.ai_gateway.complete') as complete:
            self.assertTrue(self.studio.test_connection())
            self.wait()
        complete.assert_not_called()
        self.assertFalse(self.studio.snapshot()['connection_test']['ok'])

    def test_cached_context_is_dated_and_raw_fields_excluded(self):
        folder = Path(self.temp.name) / 'reports'
        folder.mkdir()
        (folder / '2026-09-18.json').write_text(json.dumps({'date': '2026-09-18', 'status': 'partial',
            'raw': {'api_key': 'must-not-send'}, 'market': {'rising': 1}}), encoding='utf-8')
        with patch('app.ai_studio._read_local', side_effect=OSError):
            context = load_market_context(self.temp.name)
        self.assertEqual(context['source'], 'local_report')
        self.assertTrue(context['cached'])
        self.assertEqual(context['date'], '2026-09-18')
        self.assertIn('不是实时', context['message'])
        self.assertNotIn('must-not-send', json.dumps(context))

    def test_service_demo_label_preserved_and_no_arbitrary_url(self):
        state = {'mode': 'demo', 'now': '2026-09-19', 'auction': {'date': '2026-01-05', 'rows': [{}]},
                 'review': None, 'config': {'api_key': 'must-not-send'}}
        with patch('app.ai_studio._read_local', side_effect=[{'application': 'auction-lab'}, state]) as reader:
            context = load_market_context(self.temp.name)
        self.assertEqual(context['mode'], 'demo')
        self.assertFalse(context['cached'])
        self.assertEqual(context['date'], '2026-01-05')
        self.assertEqual([c.args[0] for c in reader.call_args_list],
                         ['http://127.0.0.1:8765/api/health', 'http://127.0.0.1:8765/api/state'])
        self.assertNotIn('must-not-send', json.dumps(context))

    def test_missing_context_does_not_fake_data(self):
        with patch('app.ai_studio._read_local', side_effect=OSError):
            with self.assertRaisesRegex(ValueError, '暂无'):
                load_market_context(self.temp.name)

    def test_cache_skips_future_and_demo_reports(self):
        folder = Path(self.temp.name) / 'reports'
        folder.mkdir()
        fixtures = [('2099-12-31', 'live'), ('2020-01-02', 'demo'), ('2020-01-01', 'live')]
        for day, mode in fixtures:
            (folder / (day + '.json')).write_text(json.dumps({'date': day, 'mode': mode}), encoding='utf-8')
        with patch('app.ai_studio._read_local', side_effect=OSError):
            context = load_market_context(self.temp.name)
        self.assertEqual(context['date'], '2020-01-01')
        self.assertEqual(context['mode'], 'live')

    def test_local_reads_and_launcher_bypass_proxies(self):
        with patch('app.ai_studio.urllib.request.build_opener') as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"application":"auction-ai-assistant","ok":true}'
            self.assertEqual(_read_local('http://127.0.0.1:8765/api/health')['application'], 'auction-ai-assistant')
            self.assertEqual(factory.call_args.args[0].proxies, {})
            self.assertTrue(launch_ai.alive())
            self.assertEqual(factory.call_args.args[0].proxies, {})
            response.read.return_value = b'{"application":"auction-lab","ok":true}'
            self.assertFalse(launch_ai.alive())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.studio = AIStudio(self.temp.name)
        self.server = AIServer(('127.0.0.1', 0), self.studio)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.read()
        status = response.status
        connection.close()
        return status, json.loads(result)

    def test_health_and_static_whitelist(self):
        status, health = self.request('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['application'], 'auction-ai-assistant')
        for path in ('/data/ai-messages.json', '/app/ai_studio.py', '/../../../credentials.env'):
            self.assertEqual(self.request('GET', path)[0], 404)

    def test_host_origin_and_marker_required(self):
        self.assertEqual(self.request('GET', '/api/health', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.request('POST', '/api/chat', '{}')[0], 403)
        headers = {'X-Local-App': 'auction-ai', 'Origin': 'https://evil.test'}
        self.assertEqual(self.request('POST', '/api/chat', '{}', headers)[0], 403)

    def test_invalid_json_never_echoes_input(self):
        status, result = self.request('POST', '/api/chat', '{"secret-marker":oops}', {'X-Local-App': 'auction-ai'})
        self.assertEqual(status, 400)
        self.assertNotIn('secret-marker', json.dumps(result))
        self.assertEqual(self.request('POST', '/api/chat', '{}', {'X-Local-App': 'auction-ai', 'Content-Length': '70000'})[0], 413)

    def test_chat_started_false_is_preserved(self):
        with patch.object(self.studio, 'chat', return_value=False):
            status, result = self.request('POST', '/api/chat', '{"message":"hi"}', {'X-Local-App': 'auction-ai'})
        self.assertEqual(status, 200)
        self.assertFalse(result['started'])

    def test_http_passes_explicit_provider_for_chat_and_clear(self):
        headers = {'X-Local-App': 'auction-ai'}
        with patch.object(self.studio, 'chat', return_value=True) as chat:
            status, result = self.request('POST', '/api/chat', '{"message":"hi","provider":"openai"}', headers)
            self.assertEqual(status, 200)
            chat.assert_called_once_with('hi', False, 'openai')
        with patch.object(self.studio, 'clear_chat') as clear:
            status, result = self.request('POST', '/api/chat/clear', '{"provider":"openai"}', headers)
            self.assertEqual(status, 200)
            clear.assert_called_once_with('openai')


if __name__ == '__main__':
    unittest.main()
