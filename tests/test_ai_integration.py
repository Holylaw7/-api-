"""AI remains optional and in-flight market analysis keeps its selected profile."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import ai_gateway
from app.config import DEFAULT
from app.service import Service


class AIMarketIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.credentials = self.root / 'credentials'
        self.credentials.mkdir()
        self.patches = [
            patch('app.ai_gateway.credential_dir', return_value=self.credentials),
            patch.dict('os.environ', {'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': '', 'CUSTOM_AI_API_KEY': ''}),
            patch('app.service.load_config', return_value=copy.deepcopy(DEFAULT)),
            patch('app.service.finance_key', return_value=''),
            patch('app.service.credential_status', return_value={}),
        ]
        for item in self.patches:
            item.start()
        self.service = Service(self.root)
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.assertFalse(self.service.thread.is_alive())
        self.service.review = {'date': '2026-09-18', 'status': 'partial'}

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_broken_optional_ai_profile_does_not_disable_market_snapshot(self):
        (self.credentials / 'ai-profiles.json').write_text('{broken-json', encoding='utf-8')
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['review']['date'], '2026-09-18')
        self.assertIn('rows', snapshot['auction'])
        self.assertFalse(snapshot['llm']['configured'])
        self.assertTrue(snapshot['llm'].get('error'))
        self.assertEqual((self.credentials / 'ai-profiles.json').read_text(encoding='utf-8'), '{broken-json')

    def test_page_provider_is_used_even_if_other_window_switched_global_selection(self):
        ai_gateway.save_profile({'provider':'openai', 'api_key':'openai-fixture'})
        ai_gateway.select_provider('deepseek')
        scheduled = {}
        with patch.object(self.service, '_job', side_effect=lambda name, worker: scheduled.setdefault(name, worker)):
            self.service.run_llm('页面选择OpenAI', 'openai')
        with patch('app.llm.analyze', return_value='回答') as analyze:
            scheduled['llm']()
        self.assertEqual(analyze.call_args.args[0]['provider'], 'openai')
        self.assertEqual(analyze.call_args.args[0]['api_key'], 'openai-fixture')
        self.assertIsNone(self.service.snapshot()['llm']['result'])

    def test_queued_market_analysis_captures_provider_key_model_and_isolates_result(self):
        ai_gateway.save_profile({'provider': 'deepseek', 'api_key': 'fixture-first-key'})
        ai_gateway.select_provider('deepseek')
        scheduled = {}

        def enqueue(name, worker):
            scheduled[name] = worker
            return True

        with patch.object(self.service, '_job', side_effect=enqueue):
            self.assertTrue(self.service.run_llm('检查连板强弱'))
        ai_gateway.save_profile({'provider': 'deepseek', 'api_key': 'fixture-replacement-key',
                                 'model': 'deepseek-v4-pro'})
        ai_gateway.select_provider('openai')
        with patch('app.llm.analyze', return_value='这次由原配置完成') as analyze:
            scheduled['llm']()
        config, state, question = analyze.call_args.args
        self.assertEqual(config['provider'], 'deepseek')
        self.assertEqual(config['api_key'], 'fixture-first-key')
        self.assertEqual(config['model'], 'deepseek-flash')
        self.assertEqual(state['review']['date'], '2026-09-18')
        self.assertEqual(question, '检查连板强弱')
        self.assertIsNone(self.service.snapshot()['llm']['result'])
        ai_gateway.select_provider('deepseek')
        result = self.service.snapshot()['llm']['result']
        self.assertEqual(result['provider'], 'deepseek')
        self.assertEqual(result['model'], 'deepseek-flash')
        self.assertEqual(result['text'], '这次由原配置完成')


if __name__ == '__main__':
    unittest.main()
