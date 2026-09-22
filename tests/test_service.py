import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.service import Service, SH, phase_at
from app.server import LocalServer
from app.provider import APIError
from app.storage import Store


def quote(code):
    return dict(thscode=code,name='测试证券',auction_price=10.2,auction_pct=2,
                auction_amount=10_000_000,auction_volume=100_000,
                auction_turnover_pct=.3,auction_volume_ratio=2)


def instant(h=9,m=20,s=0):
    return datetime(2026,9,18,h,m,s,tzinfo=SH)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches=[patch('app.service.load_llm',return_value={}),patch('app.service.finance_key',return_value='test-only-key')]
        for item in self.patches:item.start()
        self.service=Service(Path(self.tmp.name))
        # No real scheduler/network in deterministic integration tests.
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.service.running=True
        self.service.session_date='2026-09-18'
        self.service.prepared_date='2026-09-18'
        self.service.codes=['000001.SZ','600519.SH']
        self.service.config['batch_size']=1

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def test_every_batch_computed_before_next_fetch(self):
        service=self.service
        calls=[]
        class Provider:
            def auction(self,codes,stage):
                if calls:
                    self_test.assertEqual(service.engine.summary()['symbol_count'],1)
                    self_test.assertEqual(len(service.store.batches('2026-09-18')),1)
                calls.append(codes)
                return dict(timestamp=int(instant().timestamp()*1000),auction_phase=stage,data_status='ready',item=[quote(codes[0])])
        self_test=self
        service.provider=Provider()
        service.collect_cycle('live',clock=lambda:instant())
        self.assertEqual(len(calls),2)
        self.assertEqual(service.engine.summary()['symbol_count'],2)
        self.assertEqual(service.processed,set(service.codes))

    def test_failed_batch_does_not_starve_other_stocks(self):
        class Provider:
            def auction(self,codes,stage):
                if codes[0]=='000001.SZ':raise APIError('测试超时')
                return dict(timestamp=int(instant().timestamp()*1000),auction_phase=stage,data_status='ready',item=[quote(codes[0])])
        self.service.provider=Provider()
        self.service.collect_cycle('live',clock=lambda:instant())
        self.assertEqual(self.service.processed,{'600519.SH'})
        self.assertEqual(len(self.service.errors),1)

    def test_no_live_requests_after_0925(self):
        class Provider:
            def auction(self,*args):raise AssertionError('must not request')
        self.service.provider=Provider()
        self.service.collect_cycle('live',clock=lambda:instant(m=25))
        self.assertEqual(self.service.engine.summary()['symbol_count'],0)

    def test_final_requires_all_batches_ready(self):
        class Provider:
            def auction(self,codes,stage):
                return dict(timestamp=int(instant(m=25).timestamp()*1000),auction_phase=stage,
                            data_status='not_ready' if codes[0]=='600519.SH' else 'ready',
                            item=[] if codes[0]=='600519.SH' else [quote(codes[0])])
        self.service.provider=Provider()
        self.service.collect_cycle('final',clock=lambda:instant(m=25))
        self.assertFalse(self.service.finalized)
        self.assertEqual(self.service.final_codes,{'000001.SZ'})

    def test_real_closed_final_status_is_counted_after_engine_acceptance(self):
        class Provider:
            def auction(self,codes,stage):
                return dict(timestamp=int(instant(m=25).timestamp()*1000),auction_phase='closed',data_status='final',item=[quote(codes[0])])
        self.service.provider=Provider()
        self.service.collect_cycle('final',clock=lambda:instant(m=25))
        self.assertTrue(self.service.finalized)

    def test_morning_ranking_markdown_publishes_right_after_0925(self):
        service = self.service

        class Provider:
            def auction(self, codes, stage):
                stamp = instant(m=24, s=50) if stage == 'live' else instant(m=25, s=3)
                return dict(timestamp=int(stamp.timestamp()*1000),
                            auction_phase='no_cancel' if stage == 'live' else 'matched',
                            data_status='live' if stage == 'live' else 'final',
                            item=[quote(codes[0])])
        service.provider = Provider()
        service.collect_cycle('live', clock=lambda: instant(m=24, s=50))
        service.collect_cycle('final', clock=lambda: instant(m=25, s=3))
        path = service.data_dir / 'research' / 'daily' / '2026-09-18-morning-ranking.md'
        service._publish_morning_ranking(instant(m=25, s=30))
        self.assertFalse(path.exists(), '09:25 内不应提前生成早盘排名')
        service._publish_morning_ranking(instant(m=26, s=2))
        text = path.read_text(encoding='utf-8')
        self.assertIn('# 早盘竞价排名', text)
        self.assertIn('## 09:26:00 实时排名', text)
        self.assertIn('| 名次 | 股票代码 | 名称 | 昨日涨停候选 | 竞价评分 | 待核验分 |', text)
        self.assertIn('000001.SZ', text)
        self.assertIn('不是不可变证据', text)
        self.assertIsNotNone(service.store.decision('2026-09-18', '09:26:00'))
        archive = service.data_dir / 'research' / 'daily' / '2026-09-18-auction.json'
        self.assertFalse(archive.exists(), '早盘排名不得创建冻结档案')
        first = path.read_bytes()
        service._publish_morning_ranking(instant(m=26, s=10))
        self.assertEqual(first, path.read_bytes(), '同一分钟内 20 秒内不重复刷新')
        service._publish_morning_ranking(instant(m=26, s=30))
        self.assertNotEqual(first, path.read_bytes())
        self.assertFalse((service.data_dir / 'research' / 'daily' / '2026-09-18-auction.md').exists())

    def test_same_day_manifest_restores_saved_batches_without_network(self):
        service = self.service
        day = '2026-09-18'
        context = {'000001.SZ': {'thscode':'000001.SZ', 'name':'测试证券',
                                 'context_date':'2026-09-17', 'continue_day_cnt':1,
                                 'continue_day_text':'首板'}}
        service.store.freeze_manifest({
            'date':day, 'mode':'live', 'prepared_at':day+'T09:05:00+08:00',
            'previous_date':'2026-09-17', 'calendar':['2026-09-17',day],
            'context':context, 'codes':['000001.SZ'], 'context_complete':True,
            'point_in_time':True, 'source':'local_preparation',
            'weights':dict(service.config['weights'])})
        live_at = instant(m=20)
        final_at = instant(m=25,s=3)
        service.store.batch(day,'live',live_at.isoformat(),'live',
            dict(timestamp=int(live_at.timestamp()*1000),auction_phase='no_cancel',data_status='live',
                 item=[quote('000001.SZ')],_requested_codes=['000001.SZ'],
                 _strategy_weights=dict(service.config['weights'])))
        service.store.batch(day,'live',final_at.isoformat(),'final',
            dict(timestamp=int(final_at.timestamp()*1000),auction_phase='matched',data_status='final',
                 item=[quote('000001.SZ')],_requested_codes=['000001.SZ'],
                 _strategy_weights=dict(service.config['weights'])))
        service.prepared_date = None
        service.session_date = None
        service.engine.reset()
        with patch('app.service.now_sh',return_value=instant(m=30)):
            self.assertTrue(service._restore_local_session(day))
        summary = service.engine.summary()
        self.assertEqual(summary['scored_count'],1)
        self.assertEqual(summary['not_ready_batches'],0)
        self.assertEqual(summary['phase'],'final')
        self.assertTrue(service.finalized)
        self.assertIn('本机原始批次恢复分析',service.message)

    def test_wrong_date_final_cannot_mark_complete(self):
        class Provider:
            def auction(self,codes,stage):
                return dict(timestamp=int(instant(m=25).timestamp()*1000)-86400000,auction_phase='final',data_status='ready',item=[quote(codes[0])])
        self.service.provider=Provider()
        self.service.collect_cycle('final',clock=lambda:instant(m=25))
        self.assertFalse(self.service.finalized)
        self.assertEqual(self.service.final_codes,set())

    def test_postclose_retains_only_near_cutoff_provisional_score(self):
        received=instant(m=24,s=55)
        data=dict(timestamp=int(received.timestamp()*1000),auction_phase='live',data_status='ready',item=[quote('000001.SZ')])
        rows=self.service.engine.ingest(data,received)
        self.service.provisional_rows={r['thscode']:r for r in rows}
        with patch('app.service.now_sh',return_value=instant(m=26)):
            state=self.service.snapshot()
        self.assertIsNotNone(state['auction']['rows'][0]['score'])
        self.assertTrue(state['auction']['rows'][0]['is_provisional'])
        self.assertFalse(state['auction']['finalized'])

    def test_report_state_omits_raw_export_keeps_it(self):
        self.service.review={'date':'2026-09-18','raw':{'item':[1,2]},'ladder':{'rows':[], 'raw':[3]}}
        self.assertNotIn('raw',self.service.snapshot()['review'])
        self.assertNotIn('raw',self.service.snapshot()['review']['ladder'])
        self.assertIn('raw',self.service.export_report())

    def test_phase_boundaries(self):
        self.assertEqual(phase_at(instant(m=14),True),'waiting')
        self.assertEqual(phase_at(instant(m=15),True),'cancellable')
        self.assertEqual(phase_at(instant(m=20),True),'firm')
        self.assertEqual(phase_at(instant(m=25),True),'closed')
        self.assertEqual(phase_at(instant(m=15),False),'non_trading_day')

    def test_mode_storage_separated(self):
        self.service.store.batch('2026-09-18','live',instant().isoformat(),'live',{'item':[quote('000001.SZ')]})
        self.service.store.batch('2026-09-18','demo',instant().isoformat(),'live',{'item':[quote('600519.SH')]})
        result=self.service.store.batches('2026-09-18','live')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0][2]['item'][0]['thscode'],'000001.SZ')

    def test_state_never_contains_credentials(self):
        state=json.dumps(self.service.snapshot())
        self.assertNotIn('test-only-key',state)
        self.assertNotIn('api_key',state)


class HttpBoundaryTest(unittest.TestCase):
    def setUp(self):
        class Stub:
            running=False
            def snapshot(self):return {'ok':True}
            def stop(self):self.running=False
        self.server=LocalServer(('127.0.0.1',0),Stub())
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_cross_origin_mutation_rejected(self):
        req=urllib.request.Request(self.base+'/api/stop',data=b'{}',headers={'Origin':'https://malicious.example','X-Local-App':'auction-lab'})
        with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code,403)

    def test_custom_header_required(self):
        req=urllib.request.Request(self.base+'/api/stop',data=b'{}')
        with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code,403)

    def test_path_traversal_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(self.base+'/../app/config.py')
        self.assertEqual(cm.exception.code,404)

    def test_local_mutation_works(self):
        req=urllib.request.Request(self.base+'/api/stop',data=b'{}',headers={'X-Local-App':'auction-lab'})
        with urllib.request.urlopen(req) as res:self.assertTrue(json.load(res)['ok'])


if __name__=='__main__':unittest.main()
