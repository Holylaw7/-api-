import copy
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.config import DEFAULT
from app.report_library import ReportLibrary
from app.reporting import report_identity
from app.server import LocalServer
from app.service import Service, SH
from app.storage import Store


def report(date='2026-09-18', mode='live'):
    return {'date':date, 'mode':mode, 'status':'partial', 'generated_at':date+'T15:30:00+08:00',
            'warnings':['日期未核验'], 'market':{'limit_up_count':1, 'limit_break_count':0},
            'limit_up':{'count':1, 'status':'ready', 'rows':[
                {'thscode':'000001.SZ', 'name':'测试公司', 'score':75, 'consecutive_days':2,
                 'consecutive_lower_bound':False}]}, 'raw':{'pools_by_date':{}, 'private':'do-not-export'}}


class ReportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch('app.service.load_config',return_value=copy.deepcopy(DEFAULT)),
                        patch('app.service.finance_key',return_value=''),
                        patch('app.service.credential_status',return_value={}),
                        patch('app.ai_gateway.credential_dir',return_value=self.root/'credentials'),
                        patch.dict('os.environ',{'DEEPSEEK_API_KEY':'','OPENAI_API_KEY':'','CUSTOM_AI_API_KEY':''}),
                        patch('app.service.now_sh',return_value=datetime(2026,9,20,12,tzinfo=SH))]
        for item in self.patches:
            item.start()
        self.service = Service(self.root)
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.first, self.second = report('2026-09-17'), report()
        self.service.store.report(self.first['date'],'live',self.first)
        self.service.store.report(self.second['date'],'live',self.second)
        self.service.review = self.second

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_history_lookup_never_calls_remote_provider_or_resets_auction(self):
        self.service.codes = ['000001.SZ']
        self.service.processed = {'000001.SZ'}
        with patch.object(self.service,'_provider',side_effect=AssertionError('no network')):
            self.assertEqual(len(self.service.report_catalog()['items']),2)
            self.service.load_report('2026-09-17')
        self.assertEqual(self.service.review['date'],'2026-09-17')
        self.assertEqual(self.service.codes,['000001.SZ'])
        self.assertEqual(self.service.processed,{'000001.SZ'})
        self.assertEqual(self.service.snapshot()['review_id'],report_identity(self.first))

    def test_markdown_save_is_utf8_and_downloads_exact_saved_bytes(self):
        saved = self.service.markdown_report('2026-09-18',review_id=report_identity(self.second),save=True)
        path = Path(saved['path'])
        self.assertTrue(path.is_relative_to(self.root/'reports'))
        self.assertEqual(path.name,'2026-09-18.md')
        content = path.read_bytes()
        self.assertIn('收盘复盘'.encode(),content)
        self.assertNotIn(b'do-not-export',content)
        self.assertEqual(saved['sha256'],hashlib.sha256(content).hexdigest())
        self.assertEqual(self.service.report_library.read_markdown(saved['filename'],'live',saved['sha256']),content)
        path.write_text('newer version',encoding='utf-8')
        with self.assertRaises(ValueError):
            self.service.report_library.read_markdown(saved['filename'],'live',saved['sha256'])

    def test_wrong_date_path_traversal_and_stale_report_id_are_rejected(self):
        for date in ('../credentials','2026-02-30','2026-09-21',None):
            with self.subTest(date=date), self.assertRaises(ValueError):
                self.service.load_report(date)
        with self.assertRaises(ValueError):
            self.service.markdown_report('2026-09-18',review_id='old-report',save=True)
        for filename in ('../config.json','2026-09-18.json','2026-09-18-with-ai-evil.md'):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.service.report_library.read_markdown(filename,'live')
        self.assertFalse((self.root/'reports').exists())

    def test_demo_markdown_is_separate_and_real_history_is_not_loaded(self):
        self.service.mode = 'demo'
        self.service.review = report(mode='demo')
        saved = self.service.markdown_report(save=True)
        self.assertTrue(Path(saved['path']).is_relative_to(self.root/'reports'/'demo'))
        self.assertIn('DEMO',Path(saved['path']).read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            self.service.load_report('2026-09-17')
        self.assertEqual(self.service.report_catalog()['items'],[])

    def test_ai_appendix_persists_only_matching_report_scope_and_version(self):
        identity = report_identity(self.second)
        result = dict(text='基于这份报告的研究解释',provider='deepseek',label='DeepSeek',model='test',
                      mode='live',scope='review',review_date='2026-09-18',review_id=identity,generated_at='now')
        self.service.report_library.save_ai(dict(result,api_key='never-persist'))
        disk=(self.root/'ai-reviews'/'live'/'2026-09-18-deepseek.json').read_text(encoding='utf-8')
        self.assertNotIn('never-persist',disk)
        self.service.load_report('2026-09-18')
        self.assertEqual(self.service.llm_results['deepseek']['text'],result['text'])
        saved=self.service.markdown_report('2026-09-18',True,'deepseek',identity,save=True)
        self.assertEqual(saved['filename'],'2026-09-18-with-ai-deepseek.md')
        self.service.review['generated_at']='2026-09-18T16:00:00+08:00'
        with self.assertRaises(ValueError):
            self.service.markdown_report('2026-09-18',True,'deepseek',save=True)

    def test_report_ai_uses_only_dated_report_and_does_not_attach_to_new_view(self):
        config=dict(provider='deepseek',model='test',base_url='https://api.deepseek.com',api_key='fixture')
        queued={}
        with patch('app.service.load_llm',return_value=config), patch.object(self.service,'_job',side_effect=lambda name, worker:queued.setdefault(name,worker)):
            self.service.run_llm('解释证据','deepseek','2026-09-18',report_identity(self.second))
        self.service.load_report('2026-09-17')
        with patch('app.llm.analyze',return_value='研究结果') as analyze:
            queued['llm']()
        state=analyze.call_args.args[1]
        self.assertEqual(state['review']['date'],'2026-09-18')
        self.assertEqual(state['auction']['rows'],[])
        self.assertIsNone(state['stocks']['analysis'])
        self.assertFalse(self.service.llm_results)
        self.service.load_report('2026-09-18')
        self.assertEqual(self.service.llm_results['deepseek']['text'],'研究结果')

    def test_comparison_can_be_saved_without_additional_market_reads(self):
        with patch.object(self.service,'_provider',side_effect=AssertionError('no network')):
            result=self.service.compare_report('2026-09-18','2026-09-17')
            self.assertEqual(result['limit_up']['retained_count'],1)
            saved=self.service.markdown_report('2026-09-18',baseline='2026-09-17',save=True)
        self.assertIn('2026-09-17',Path(saved['path']).read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            self.service.compare_report('2026-09-17','2026-09-18')

    def test_automatic_review_keeps_selected_archive_and_updates_latest(self):
        self.service.load_report('2026-09-17')
        queued={}
        class Provider:
            def calendar(self):
                return ['2026-09-17','2026-09-18']
        with patch.object(self.service,'_provider',return_value=Provider()), \
                patch.object(self.service,'_job',side_effect=lambda name, worker:queued.setdefault(name,worker)), \
                patch('app.service.build_review',return_value=report()):
            self.service.run_review('2026-09-18',automatic=True)
            queued['review']()
        self.assertEqual(self.service.review['date'],'2026-09-17')
        self.assertEqual(self.service.latest_review['date'],'2026-09-18')
        self.assertTrue(self.service.trend_refresh_needed)

    def test_file_only_archive_is_read_but_mismatched_mode_rejected(self):
        directory=self.root/'reports'; directory.mkdir()
        data=report('2026-09-16')
        path=directory/'2026-09-16.json'
        path.write_text(json.dumps(data),encoding='utf-8')
        self.assertEqual(len(self.service.report_catalog()['items']),3)
        self.assertEqual(self.service.saved_report('2026-09-16')['date'],'2026-09-16')
        data['mode']='demo'; path.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError):
            self.service.saved_report('2026-09-16')

    def test_http_save_download_and_unknown_format(self):
        server=LocalServer(('127.0.0.1',0),self.service)
        worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            request=urllib.request.Request(base+'/api/reports/save',data=json.dumps({'date':'2026-09-18','review_id':report_identity(self.second)}).encode(),
                headers={'Content-Type':'application/json','X-Local-App':'auction-lab'})
            with opener.open(request,timeout=3) as response:
                saved=json.load(response)
            with opener.open(base+saved['download_url'],timeout=3) as response:
                content=response.read()
                self.assertEqual(response.headers.get_content_type(),'text/markdown')
                self.assertIn('attachment;',response.headers['Content-Disposition'])
            self.assertEqual(content,Path(saved['path']).read_bytes())
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(base+'/api/report?format=html',timeout=3)
            self.assertEqual(error.exception.code,400)
            with opener.open(base+'/api/diagnostics',timeout=3) as response:
                self.assertIn('checks',json.load(response))
        finally:
            server.shutdown();server.server_close();worker.join(2)

    def test_sse_idle_keepalive_does_not_rebuild_unchanged_report(self):
        server=LocalServer(('127.0.0.1',0),self.service)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with patch.object(self.service,'snapshot',return_value={'mode':'live'}) as snapshot, patch.object(self.service,'_auction_priority',return_value=False):
                with opener.open(f'http://127.0.0.1:{server.server_port}/api/events',timeout=6) as response:
                    lines=[]
                    while len(lines)<12:
                        line=response.readline().decode()
                        lines.append(line)
                        if line.startswith(': keepalive'):
                            break
                    self.assertTrue(any(line.startswith('event: state') for line in lines))
                    self.assertTrue(any(line.startswith(': keepalive') for line in lines))
                    self.assertEqual(snapshot.call_count,1)
        finally:
            self.service.shutdown.set();self.service.touch()
            server.shutdown();server.server_close();worker.join(2)


if __name__=='__main__':
    unittest.main()
