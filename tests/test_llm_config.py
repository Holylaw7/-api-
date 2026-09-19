import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import DEFAULT, validate_config, save_llm, load_llm
from app.llm import analyze, build_summary


class ConfigTests(unittest.TestCase):
    def test_nan_and_overflow_weights_rejected(self):
        for n in (float('nan'),float('inf'),1e308):
            with self.assertRaises(ValueError):
                validate_config(dict(DEFAULT,weights={k:n for k in DEFAULT['weights']}))

    def test_new_host_does_not_inherit_old_secret(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.config.credential_dir',return_value=Path(tmp)):
            save_llm({'base_url':'https://one.example/v1','model':'test','api_key':'fake-old-key'})
            save_llm({'base_url':'https://two.example/v1','model':'test','api_key':''})
            self.assertEqual(load_llm()['api_key'],'')

    def test_remote_http_rejected_local_http_allowed(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.config.credential_dir',return_value=Path(tmp)):
            with self.assertRaises(ValueError):save_llm({'base_url':'http://remote.example/v1','model':'test'})
            save_llm({'base_url':'http://127.0.0.1:1234/v1','model':'test'})
            self.assertEqual(load_llm()['model'],'test')

    def test_llm_payload_excludes_raw_data_and_finance_credentials(self):
        state={'mode':'live','now':'2026-09-18T15:30:00+08:00','config':{'api_key':'never-send'},
               'auction':{'phase':'closed','summary':{},'rows':[]},
               'review':{'date':'2026-09-18','raw':{'private':'never-send'},'sectors':{'rows':[{'name':'s'}]*100}}}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,*args):return json.dumps({'choices':[{'message':{'content':'研判结果'},'finish_reason':'stop'}]}).encode()
        class Opener:
            def open(self,req,timeout):
                parsed=json.loads(req.data)
                self_test.assertNotIn('never-send',str(parsed))
                self_test.assertEqual(req.get_header('Authorization'),'Bearer model-only')
                content=parsed['messages'][1]['content'].split('\n数据：',1)[1]
                self_test.assertEqual(len(json.loads(content)['sectors_top30']['rows']),30)
                return Response()
        self_test=self
        with patch('app.ai_gateway.build_opener',return_value=Opener()):
            result=analyze({'base_url':'https://example.test/v1','model':'test','api_key':'model-only'},state)
        self.assertEqual(result,'研判结果')

    def test_summary_nested_allowlist_dates_modes_and_missing_values(self):
        row={'thscode':'000001.SZ','score':float('nan'),
             'factors':{'gap':{'value':2,'score':60,'api_key':'nested-secret'},
                        'new_private_field':{'value':'private-marker'}},
             'quality':{'factor_coverage':.5,'flags':['数据未齐'],'raw':'nested-secret'}}
        state={'mode':'demo','now':'2026-09-18T09:25:00+08:00',
               'auction':{'date':'2026-09-18','phase':'closed','rows':[row]*50},
               'stocks':{'analysis':{'date':'2026-09-17','auction':{'date':'2026-09-18','row':row,'history':['full-history-private']}}},
               'review':{'date':'2026-09-17','market':{'date_verified':False,'raw':'nested-secret'},
                         'trend':{'rows':[{'date':'2026-09-17','api_key':'nested-secret'}]}}}
        summary=build_summary(state)
        serial=json.dumps(summary,allow_nan=False)
        self.assertNotIn('nested-secret',serial)
        self.assertNotIn('private-marker',serial)
        self.assertNotIn('full-history-private',serial)
        self.assertEqual(summary['mode'],'demo')
        self.assertEqual(len(summary['auction_top30']),30)
        self.assertIsNone(summary['auction_top30'][0]['score'])
        self.assertEqual(summary['auction_top30'][0]['factors']['gap']['score'],60)
        self.assertEqual(summary['selected_stock']['date'],'2026-09-17')
        self.assertEqual(summary['selected_stock']['auction']['date'],'2026-09-18')
        self.assertIs(summary['review']['market']['date_verified'],False)

    def test_summary_accepts_cached_review_without_live_sections(self):
        result=build_summary({'mode':'cached','review':{'date':'2026-09-18','status':'partial'}})
        self.assertEqual(result['review']['date'],'2026-09-18')
        self.assertEqual(result['auction_top30'],[])
        self.assertIsNone(result['observed_at'])


if __name__=='__main__':unittest.main()
