import copy
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.config import DEFAULT
from app.service import Service, SH


def metadata(code):
    return dict(thscode=code,ticker=code[:6],name='测试股票',asset_type='a-share',
                resolution={'source':'official_ticker_search','input':code[:6],'timestamp':1})


class StockServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch('app.service.load_config',return_value=copy.deepcopy(DEFAULT)),
                        patch('app.service.load_llm',return_value={}),
                        patch('app.service.finance_key',return_value='test-only-key'),
                        patch('app.service.credential_status',return_value={}),
                        patch('app.service.now_sh',return_value=datetime(2026,9,18,8,50,tzinfo=SH))]
        for p in self.patches:p.start()
        self.service = Service(Path(self.tmp.name))
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.service.days = ['2026-09-16','2026-09-17','2026-09-18']
        self.service.calendar_loaded_date = '2026-09-18'
        self.service.prepared_date = '2026-09-18'
        self.service.session_date = '2026-09-18'
        self.service.context = {'000001.SZ':dict(name='平安银行',context_date='2026-09-17')}
        self.service.trend_pool = dict(date='2026-09-17',status='ready',rows=[{'thscode':'600519.SH'}])
        self.service._rebuild_codes()

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def wait_job(self, name):
        until = time.monotonic()+3
        while self.service.jobs.get(name,{}).get('status') == 'running' and time.monotonic() < until:
            time.sleep(.01)
        self.assertNotEqual(self.service.jobs.get(name,{}).get('status'),'running')
        return self.service.jobs[name]

    def test_focus_union_sources_and_priority(self):
        self.service.config['watchlist']=['600519.SH','300750.SZ']
        self.service._rebuild_codes()
        self.assertEqual(self.service.codes,['600519.SH','300750.SZ','000001.SZ'])
        self.assertEqual(self.service.sources['600519.SH'],['manual','strong_trend'])
        self.service.trend_pool['date']='2026-09-16'
        self.service.config['watchlist']=[]
        self.service._rebuild_codes()
        self.assertEqual(self.service.codes,['000001.SZ'])

    def _session_service(self):
        """A second process view on the same data directory (no scheduler work)."""
        service = Service(Path(self.tmp.name))
        service.shutdown.set()
        service.thread.join(2)
        service.shutdown.clear()
        service.days = ['2026-09-16','2026-09-17','2026-09-18']
        service.calendar_loaded_date = '2026-09-18'
        service.prepared_date = '2026-09-18'
        service.session_date = '2026-09-18'
        service.context = {'000001.SZ':dict(name='平安银行',context_date='2026-09-17')}
        return service

    def test_session_trend_pool_survives_same_day_restart(self):
        s = self.service
        self.assertEqual(json.loads((Path(s.data_dir)/'trend-pool-session.json').read_text())['date'],'2026-09-17')
        # 收盘后刷新的下一次池（截止今天）属于下一会话，不能反向替换本会话来源
        s.trend_pool = dict(date='2026-09-18',status='ready',rows=[{'thscode':'000333.SZ'}])
        s._rebuild_codes()
        self.assertIn('600519.SH',s.codes)
        self.assertNotIn('000333.SZ',s.codes)
        snapshot = json.loads((Path(s.data_dir)/'trend-pool-session.json').read_text())
        self.assertEqual(snapshot['date'],'2026-09-17')
        # 模拟同日重启：新进程只看到已刷新的 trend-pool.json，仍须保留本会话趋势来源
        (Path(s.data_dir)/'trend-pool.json').write_text(json.dumps(s.trend_pool),encoding='utf-8')
        restarted = self._session_service()
        try:
            self.assertEqual(restarted.trend_pool.get('date'),'2026-09-18')
            restarted._rebuild_codes()
            self.assertIn('600519.SH',restarted.codes)
            self.assertEqual(restarted.sources['600519.SH'],['strong_trend'])
            self.assertNotIn('000333.SZ',restarted.codes)
        finally:
            restarted.close()
            restarted.store.close()

    def test_session_trend_snapshot_rejects_other_dates_and_bad_content(self):
        path = Path(self.service.data_dir)/'trend-pool-session.json'
        path.write_text(json.dumps(dict(date='2026-09-16',rows=[{'thscode':'600519.SH'}])),encoding='utf-8')
        stale = self._session_service()
        try:
            self.assertEqual(stale.session_trends.get('date'),'2026-09-16')
            stale._rebuild_codes()
            self.assertNotIn('600519.SH',stale.codes)
        finally:
            stale.close()
            stale.store.close()
        path.write_text(json.dumps([{'date':'2026-09-17'}]),encoding='utf-8')
        broken = self._session_service()
        try:
            self.assertEqual(broken.session_trends,{})
            broken._rebuild_codes()
            self.assertEqual(broken.codes,['000001.SZ'])
        finally:
            broken.close()
            broken.store.close()

    def test_hot_add_persists_verified_code_without_reset(self):
        s=self.service
        class Provider:
            def resolve_stock(self, code):
                return metadata('300750.SZ')
        s.provider=Provider()
        s.running=True
        old_engine=s.engine
        s.engine.history['000001.SZ']=[{'received_at':'preserved'}]
        self.assertTrue(s.add_watchlist(['300750']))
        self.assertEqual(self.wait_job('watchlist')['status'],'done')
        self.assertEqual(s.config['watchlist'],['300750.SZ'])
        self.assertEqual(s.codes[0],'300750.SZ')
        self.assertIs(s.engine,old_engine)
        self.assertEqual(s.engine.history['000001.SZ'][0]['received_at'],'preserved')
        self.assertEqual(json.loads((Path(self.tmp.name)/'config.json').read_text())['watchlist'],['300750.SZ'])

    def test_failed_batch_add_is_atomic(self):
        class Provider:
            def resolve_stock(self, code):
                if code=='999999':raise ValueError('官方未找到代码')
                return metadata('600519.SH')
        self.service.provider=Provider()
        self.service.add_watchlist(['600519','999999'])
        self.assertEqual(self.wait_job('watchlist')['status'],'error')
        self.assertEqual(self.service.config['watchlist'],[])

    def test_remove_manual_preserves_other_sources(self):
        s=self.service
        s.config['watchlist']=['000001.SZ']
        s._rebuild_codes()
        s.remove_watchlist('000001')
        self.assertEqual(s.config['watchlist'],[])
        self.assertIn('000001.SZ',s.codes)
        self.assertEqual(s.sources['000001.SZ'],['previous_limit_up'])

    def test_protected_period_defers_history_and_rejects_bulk(self):
        s=self.service
        s.metadata={'600519.SH':metadata('600519.SH')}
        with patch('app.service.now_sh',return_value=datetime(2026,9,18,9,20,tzinfo=SH)), patch('app.service.analyze_stock',side_effect=AssertionError('must not fetch history')):
            s.query_stock('600519')
            self.assertEqual(self.wait_job('stock')['status'],'done')
            self.assertEqual(s.stock_analysis['status'],'deferred')
            self.assertEqual(s.pending_stock[0]['thscode'],'600519.SH')
            with self.assertRaises(ValueError):s.refresh_trends()
            with self.assertRaises(ValueError):s.add_watchlist(['000001','600519'])

    def test_history_analysis_uses_only_closed_session(self):
        s=self.service
        s.metadata={'600519.SH':metadata('600519.SH')}
        s.provider=object()
        expected=dict(thscode='600519.SH',date='2026-09-17',status='ready')
        with patch('app.service.analyze_stock',return_value=expected) as analyze:
            s.query_stock('600519')
            self.assertEqual(self.wait_job('stock')['status'],'done')
            self.assertEqual(analyze.call_args.args[2],'2026-09-17')
            self.assertTrue((Path(self.tmp.name)/'stocks/600519.SH-2026-09-17.json').exists())
        s.query_stock('600519','2026-09-18')
        self.assertEqual(self.wait_job('stock')['status'],'error')

    def test_legacy_config_cannot_bypass_official_resolution(self):
        with self.assertRaisesRegex(ValueError,'官方证券目录'):
            self.service.update_config({'watchlist':['999999.SH']})

    def test_unknown_local_bare_code_is_not_guessed(self):
        with self.assertRaises(ValueError):self.service.history('999999')
        self.assertEqual(self.service.history('000001')['symbol'],'000001.SZ')

    def test_no_live_stock_queries_in_demo(self):
        self.service.mode='demo'
        with self.assertRaises(ValueError):self.service.query_stock('600519')
        with self.assertRaises(ValueError):self.service.add_watchlist(['600519'])
        with self.assertRaises(ValueError):self.service.remove_watchlist('600519')
        self.assertEqual(self.service.snapshot()['stocks']['trend_pool'],{})

    def test_bare_code_cache_does_not_assume_globally_unique(self):
        s=self.service
        cached=metadata('600519.SH')
        cached['resolution']['input']='600519.SH'
        s.metadata={'600519.SH':cached}
        class Provider:
            def resolve_stock(self, code):raise ValueError('官方结果有歧义')
        s.provider=Provider()
        with self.assertRaisesRegex(ValueError,'歧义'):s._resolve_stock('600519')
        self.assertEqual(s._resolve_stock('600519.SH')['thscode'],'600519.SH')

    def test_next_session_trend_cache_does_not_remove_current_session_pool(self):
        self.service._rebuild_codes()
        self.service.trend_pool=dict(date='2026-09-18',rows=[{'thscode':'300750.SZ'}])
        self.service._rebuild_codes()
        self.assertIn('600519.SH',self.service.codes)
        self.assertNotIn('300750.SZ',self.service.codes)

    def test_completed_partial_cache_reused_but_interruption_retried(self):
        pool=dict(date='2026-09-17',status='partial',completed_at='2026-09-17T16:00:00+08:00',
                  evaluated_count=60,candidate_count=60,
                  coverage=dict(pool_days_available=5,pool_days_requested=5,history_failures=0,cancelled_or_protected=False))
        self.assertTrue(self.service._complete_trend_cache(pool,'2026-09-17'))
        self.assertFalse(self.service._complete_trend_cache(pool,'2026-09-18'))
        pool['coverage']['cancelled_or_protected']=True
        self.assertFalse(self.service._complete_trend_cache(pool,'2026-09-17'))


if __name__=='__main__':unittest.main()
