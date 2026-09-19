import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app import ai_gateway as gateway


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.payload[:size]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        directory = patch('app.ai_gateway.credential_dir', return_value=self.folder)
        directory.start()
        self.addCleanup(directory.stop)
        environment = patch.dict(os.environ, {'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': '', 'CUSTOM_AI_API_KEY': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def test_defaults_and_saved_keys_are_private_and_separate(self):
        state = gateway.profiles_state()
        self.assertEqual(state['active_provider'], 'deepseek')
        self.assertEqual(gateway.active_config()['model'], 'deepseek-flash')
        gateway.save_profile({'provider': 'deepseek', 'api_key': 'fake-deepseek-private'})
        gateway.save_profile({'provider': 'openai', 'api_key': 'fake-openai-private'})
        self.assertEqual(gateway.profiles_state()['active_provider'], 'deepseek')
        state = gateway.select_provider('openai')
        self.assertEqual(gateway.active_config()['api_key'], 'fake-openai-private')
        self.assertNotIn('private', json.dumps(state))
        self.assertNotIn('api_key', json.dumps(state))
        gateway.save_profile({'provider': 'deepseek', 'api_key': '', 'model': 'deepseek-v4-pro'})
        self.assertEqual(gateway.active_config('deepseek')['api_key'], 'fake-deepseek-private')

    def test_builtin_urls_cannot_forward_key(self):
        for provider in ('deepseek', 'openai'):
            with self.assertRaises(gateway.AIGatewayError):
                gateway.save_profile({'provider': provider, 'base_url': 'https://other.example/v1', 'api_key': 'fake-secret'})
        self.assertNotIn('fake-secret', (self.folder / 'ai-profiles.json').read_text(encoding='utf-8'))

    def test_custom_url_change_drops_saved_and_environment_key(self):
        gateway.save_profile({'provider': 'custom', 'base_url': 'https://first.example/v1', 'model': 'model-a', 'api_key': 'fake-old-private'})
        with patch.dict(os.environ, {'CUSTOM_AI_API_KEY': 'fake-env-private'}):
            gateway.save_profile({'provider': 'custom', 'base_url': 'https://second.example/v1', 'model': 'model-b'})
            self.assertEqual(gateway.active_config('custom')['api_key'], '')
            self.assertEqual(gateway.active_config('custom')['key_source'], 'missing')

    def test_environment_is_used_only_without_saved_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-env-private'}):
            self.assertEqual(gateway.active_config('openai')['key_source'], 'environment')
            gateway.save_profile({'provider': 'openai', 'api_key': 'fake-saved-private'})
            self.assertEqual(gateway.active_config('openai')['api_key'], 'fake-saved-private')
            self.assertEqual(gateway.active_config('openai')['key_source'], 'saved')

    def test_legacy_profile_becomes_custom_not_guessed_provider(self):
        legacy = {'base_url': 'https://legacy.example/v1', 'model': 'older-model', 'api_key': 'fake-legacy-private'}
        (self.folder / 'auction-lab-llm.json').write_text(json.dumps(legacy), encoding='utf-8')
        state = gateway.profiles_state()
        self.assertEqual(state['active_provider'], 'custom')
        self.assertEqual(gateway.active_config()['api_key'], 'fake-legacy-private')
        self.assertEqual(gateway.active_config('deepseek')['api_key'], '')
        self.assertEqual(json.loads((self.folder / 'auction-lab-llm.json').read_text(encoding='utf-8')), legacy)

    def test_invalid_configuration_kept_and_error_does_not_echo(self):
        path = self.folder / 'ai-profiles.json'
        path.write_text('{"secret-private":broken}', encoding='utf-8')
        with self.assertRaises(gateway.AIGatewayError) as failure:
            gateway.profiles_state()
        self.assertNotIn('secret-private', str(failure.exception))
        self.assertEqual(path.read_text(encoding='utf-8'), '{"secret-private":broken}')

    def test_remote_http_and_embedded_credentials_rejected(self):
        for url in ('http://remote.example/v1', 'https://user:pass@example.test',
                    'https://example.test/v1?key=fake-private', 'https://example.test/v1/responses'):
            with self.assertRaises(gateway.AIGatewayError):
                gateway.save_profile({'provider': 'custom', 'base_url': url, 'model': 'model'})
        gateway.save_profile({'provider': 'custom', 'base_url': 'http://127.0.0.1:9999/v1', 'model': 'local-model'})
        self.assertTrue(next(p for p in gateway.profiles_state()['profiles'] if p['id'] == 'custom')['configured'])

    def test_parallel_saves_keep_both_provider_profiles(self):
        errors = []
        def save(provider):
            try:
                for index in range(5):
                    gateway.save_profile({'provider': provider, 'api_key': f'fake-{provider}-{index}'})
            except Exception as exc:
                errors.append(type(exc).__name__)
        threads = [threading.Thread(target=save, args=(p,)) for p in ('deepseek', 'openai')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(gateway.active_config('deepseek')['api_key'], 'fake-deepseek-4')
        self.assertEqual(gateway.active_config('openai')['api_key'], 'fake-openai-4')


class ProtocolTests(unittest.TestCase):
    def config(self, provider):
        definition = gateway.PROVIDERS[provider]
        return {'provider': provider, 'base_url': definition['base_url'] or 'https://custom.example/v1',
                'model': definition['model'] or 'custom-model', 'api_key': 'fake-test-private',
                'protocol': definition['protocol']}

    def chat_result(self, reason='stop', content='正文'):
        return {'choices': [{'finish_reason': reason, 'message': {'content': content, 'reasoning_content': '不可作为正文的推理'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'api_key': 'fake-hidden'}}

    def test_deepseek_chat_uses_official_endpoint_and_disables_thinking(self):
        with patch('app.ai_gateway.build_opener') as factory:
            factory.return_value.open.return_value = Response(self.chat_result())
            result = gateway.complete(self.config('deepseek'), [{'role': 'user', 'content': '你好'}])
            request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fake-test-private')
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'deepseek-flash')
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(result['text'], '正文')
        self.assertNotIn('fake-hidden', json.dumps(result))

    def test_openai_responses_collects_text_across_messages_not_reasoning(self):
        payload = {'status': 'completed', 'output': [
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': '不要输出'}]},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '第一段'}]},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '第二段'}]}],
            'usage': {'input_tokens': 12, 'output_tokens': 3}}
        with patch('app.ai_gateway.build_opener') as factory:
            factory.return_value.open.return_value = Response(payload)
            result = gateway.complete(self.config('openai'), [{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '问题'}])
            request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
        body = json.loads(request.data)
        self.assertFalse(body['store'])
        self.assertNotIn('temperature', body)
        self.assertEqual(result['text'], '第一段\n第二段')
        self.assertEqual(result['usage']['input_tokens'], 12)

    def test_truncated_text_returns_warning_without_followup(self):
        with patch('app.ai_gateway.build_opener') as factory:
            factory.return_value.open.return_value = Response(self.chat_result('length'))
            result = gateway.complete(self.config('deepseek'), [{'role': 'user', 'content': '你好'}])
        self.assertEqual(factory.return_value.open.call_count, 1)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['finish_reason'], 'length')

    def test_incomplete_openai_response_is_marked(self):
        payload = {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
                   'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '部分正文'}]}]}
        with patch('app.ai_gateway.build_opener') as factory:
            factory.return_value.open.return_value = Response(payload)
            result = gateway.complete(self.config('openai'), [{'role': 'user', 'content': '你好'}])
        self.assertEqual(result['finish_reason'], 'length')
        self.assertTrue(result['warnings'])

    def test_reasoning_only_and_pending_status_never_become_answer(self):
        fixtures = [('deepseek', self.chat_result('stop', None)),
                    ('openai', {'status': 'in_progress', 'output': []}),
                    ('openai', {'status': 'completed', 'output': [{'type': 'reasoning', 'text': 'hidden'}]})]
        for provider, payload in fixtures:
            with self.subTest(provider=provider, payload=payload), patch('app.ai_gateway.build_opener') as factory:
                factory.return_value.open.return_value = Response(payload)
                with self.assertRaises(gateway.AIGatewayError):
                    gateway.complete(self.config(provider), [{'role': 'user', 'content': '你好'}])

    def test_errors_are_safe_and_paid_request_not_retried_or_switched(self):
        for error in (HTTPError('https://example.test', 401, 'fake-private-leak', {}, None),
                      URLError('fake-private-leak')):
            with self.subTest(error=type(error).__name__), patch('app.ai_gateway.build_opener') as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(gateway.AIGatewayError) as failure:
                    gateway.complete(self.config('deepseek'), [{'role': 'user', 'content': '你好'}])
                self.assertNotIn('fake-private-leak', str(failure.exception))
                self.assertEqual(factory.return_value.open.call_count, 1)

    def test_redirect_rejected_before_other_host(self):
        with self.assertRaises(gateway.AIGatewayError):
            gateway.NoRedirect().redirect_request(None, None, 302, 'move', {}, 'https://other.example')

    def test_invalid_json_and_oversized_response_rejected(self):
        for payload in (b'not-json-private-marker', b'{"value":NaN}'):
            with patch('app.ai_gateway.build_opener') as factory:
                factory.return_value.open.return_value = Response(payload)
                with self.assertRaises(gateway.AIGatewayError) as failure:
                    gateway.complete(self.config('deepseek'), [{'role': 'user', 'content': '你好'}])
                self.assertNotIn('private-marker', str(failure.exception))
        with patch('app.ai_gateway.MAX_RESPONSE_BYTES', 20), patch('app.ai_gateway.build_opener') as factory:
            factory.return_value.open.return_value = Response(b'x' * 30)
            with self.assertRaises(gateway.AIGatewayError):
                gateway.complete(self.config('deepseek'), [{'role': 'user', 'content': '你好'}])

    def test_model_connection_only_uses_get_and_reports_missing_model(self):
        with patch('app.ai_gateway.active_config', return_value=self.config('openai')), patch('app.ai_gateway.build_opener') as factory:
            factory.return_value.open.return_value = Response({'data': [{'id': 'different-model'}, {'id': 'bad model\n'}]})
            result = gateway.test_connection('openai')
            request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/models')
        self.assertTrue(result['ok'])
        self.assertFalse(result['model_available'])
        self.assertEqual(result['models'], ['different-model'])

    def test_builtin_missing_key_and_invalid_messages_fail_before_network(self):
        config = self.config('deepseek')
        config['api_key'] = ''
        with patch('app.ai_gateway.build_opener') as factory:
            with self.assertRaises(gateway.AIGatewayError):
                gateway.complete(config, [{'role': 'user', 'content': '你好'}])
            with self.assertRaises(gateway.AIGatewayError):
                gateway.complete(self.config('deepseek'), [{'role': 'tool', 'content': 'not-supported'}])
            with self.assertRaises(gateway.AIGatewayError):
                gateway.complete(self.config('deepseek'), [{'role': 'user', 'content': 'x' * 200001}])
            factory.assert_not_called()

    def test_parallel_process_updates_preserve_both_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            code=("import sys,time; from pathlib import Path; from app import ai_gateway as g; "
                  "g.credential_dir=lambda:Path(sys.argv[1]); "
                  "[(g.save_profile({'provider':sys.argv[2],'api_key':sys.argv[2]+'-process-fixture','model':sys.argv[2]+'-test-'+str(i)}),time.sleep(.002)) for i in range(12)]")
            processes=[]
            try:
                for provider in ('deepseek','openai'):
                    processes.append(subprocess.Popen([sys.executable,'-c',code,str(folder),provider],
                        cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        env={**os.environ,'DEEPSEEK_API_KEY':'','OPENAI_API_KEY':'','CUSTOM_AI_API_KEY':''},
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
                for process in processes:
                    process.communicate(timeout=15)
                    self.assertEqual(process.returncode,0,'parallel fixture worker failed')
                with patch.object(gateway,'credential_dir',return_value=folder):
                    self.assertEqual(gateway.active_config('deepseek')['model'],'deepseek-test-11')
                    self.assertEqual(gateway.active_config('openai')['model'],'openai-test-11')
                    self.assertEqual(gateway.profiles_state()['active_provider'],'deepseek')
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill();process.wait()


if __name__ == '__main__':
    unittest.main()
