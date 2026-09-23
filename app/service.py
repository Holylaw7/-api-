import copy
import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import (DATA, atomic_json, finance_key, credential_status, load_config,
                     validate_config, save_finance_key, validate_finance_key)
from .ai_gateway import active_config as load_llm, profiles_state, test_connection, AIGatewayError
from .engine import AuctionEngine, strict_continuity
from .provider import HiThinkProvider, APIError, BASE_URL
from .review import build_review
from .storage import Store
from .stocks import analyze_stock, validate_code
from .selection import build_trend_pool
from .report_library import ReportLibrary, valid_date
from .reporting import report_identity
from .insights import compare_reports, readiness
from .sentiment import build_sentiment
from .official_context import build_official_context
from .research import ResearchLibrary, CHECKPOINTS
from .research_data import pool_rows
from .daily_validation import DailyValidation
from .sector_library import SectorLibrary
from .sector_research import load_catalog, search_catalog, validate_sector_codes, build_sector_research

SH = timezone(timedelta(hours=8), name='Asia/Shanghai')


def now_sh():
    return datetime.now(SH)


def phase_at(now, trading):
    if not trading:
        return 'non_trading_day'
    hm = now.strftime('%H:%M:%S')
    if hm < '09:15:00':
        return 'waiting'
    if hm < '09:20:00':
        return 'cancellable'
    if hm < '09:25:00':
        return 'firm'
    return 'closed'


class Service:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir or DATA
        self.config = load_config()
        self.engine = AuctionEngine(self.config['weights'])
        self.store = Store(self.data_dir / 'market.sqlite3')
        self.report_library = ReportLibrary(self.store, self.data_dir)
        self.research_library = ResearchLibrary(self.store, self.data_dir)
        self.daily_validation = DailyValidation(self.store, self.data_dir)
        self.sector_library = SectorLibrary(self.data_dir)
        self.sector_summary = {}
        self.sector_catalog = []
        self.sector_catalog_at = 0
        self.sector_catalog_lock = threading.Lock()
        self.sector_fetch_lock = threading.Lock()
        try:
            research = self.research_library.latest()
        except ValueError:
            research = {'status':'not_run'}
        self.research_summary = {k:research.get(k) for k in ('id','status','generated_at')}
        self.last_daily_attempt = None
        self.morning_ranking_at = None
        self.final_window_notice = None
        self.recorded_decisions = set()
        self.engine_source_sha256 = hashlib.sha256((Path(__file__).parent/'engine.py').read_bytes()).hexdigest()
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.version = 0
        self.shutdown = threading.Event()
        self.running = False
        self.mode = 'live'
        self.status = 'idle'
        self.message = '请配置数据 Key；随后自动等待交易日竞价及收盘复盘'
        self.provider = None
        self._sentiment_source = None
        self._sentiment_value = None
        self.days = []
        self.prepared_date = None
        self.calendar_loaded_date = None
        self.context = {}
        self.codes = []
        self.sources = {}
        self.all_codes = []
        self.metadata = self._read_json('stock-metadata.json', {})
        self.trend_pool = self._read_json('trend-pool.json', {})
        self.session_trends = {}
        self.stock_analysis = None
        self.query_code = None
        self.pending_stock = None
        self.last_trend_attempt = None
        self.trend_refresh_needed = False
        self.session_date = None
        self.processed = set()
        self.final_codes = set()
        self.finalized = False
        self.provisional_rows = {}
        self.cycle_seconds = 0
        self.last_batch_ms = None
        self.errors = []
        self.jobs = {}
        self.review = self.store.latest_report()
        self.latest_review = self.review
        self.viewing_archive = False
        self._identity_source = None
        self._identity_value = None
        self.llm_result = None
        self.llm_results = self.report_library.load_ai(self.review) if self.review else {}
        self.llm_connection_test = None
        self.finance_connection_test = self._empty_finance_test()
        self._finance_test_key = None
        self._finance_revision = 0
        self.last_review_attempt = None
        self.retry_prepare_at = 0
        self.api_backoff_until = 0
        self.api_backoff_seconds = 0
        self.demo_generation = 0
        self.thread = threading.Thread(target=self._loop, daemon=True, name='auction-scheduler')
        self.thread.start()

    def _read_json(self, name, default):
        try:
            return json.loads((self.data_dir / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return copy.deepcopy(default)

    @staticmethod
    def _auction_priority(current=None):
        return '09:10' <= (current or now_sh()).strftime('%H:%M') <= '09:26'

    @staticmethod
    def _complete_trend_cache(pool, date):
        if pool.get('date') != date:
            return False
        if pool.get('status') == 'ready':
            return True
        coverage = pool.get('coverage',{})
        # Data-quality caveats (e.g. short listing history or provisional source
        # dates) do not justify downloading the same completed window on restart.
        return bool(pool.get('completed_at') and not coverage.get('cancelled_or_protected')
                    and not coverage.get('history_failures')
                    and coverage.get('pool_days_available',0) == coverage.get('pool_days_requested')
                    and coverage.get('pool_days_requested',0) >= 5
                    and pool.get('evaluated_count') == pool.get('candidate_count'))

    def touch(self):
        with self.condition:
            self.version += 1
            self.condition.notify_all()

    def error(self, message):
        # All provider errors are safe structured summaries, never URLs/headers/keys.
        with self.lock:
            self.errors.append({'time': now_sh().isoformat(), 'message': str(message)[:500]})
            self.errors = self.errors[-20:]
        self.touch()

    def snapshot(self):
        # Credential-file I/O is outside the lock used by auction processing.
        try:
            ai_state = profiles_state()
            active = next(p for p in ai_state['profiles'] if p['id'] == ai_state['active_provider'])
        except AIGatewayError as exc:
            # An optional AI configuration problem cannot interrupt market SSE.
            ai_state = {'active_provider':None, 'profiles':[], 'error':str(exc)}
            active = {'id':None, 'configured':False, 'base_url':'', 'model':'', 'label':'AI 配置需检查'}
        finance = self.finance_status()
        with self.lock:
            current = now_sh()
            rows = self.engine.rankings(current) if self.mode == 'live' else self.engine.rankings()
            if self.mode == 'live' and self.session_date == current.date().isoformat() and current.strftime('%H:%M:%S') >= '09:25:00':
                cutoff = current.replace(hour=9,minute=25,second=0,microsecond=0)
                for row in rows:
                    saved = self.provisional_rows.get(row['thscode'])
                    if row.get('phase') != 'final' and saved and saved.get('score') is not None:
                        observed = datetime.fromisoformat(saved['updated_at'])
                        if 0 <= (cutoff-observed).total_seconds() <= 30:
                            row['score'] = saved['score']
                            row['is_provisional'] = True
                            row['quality']['status'] = 'provisional'
                            row['quality']['flags'].append('保存的临近09:25排名，尚未取得终态确认；不是当前实时行情')
                rows.sort(key=lambda r:(r.get('score') is None,-(r.get('score') or 0),r['thscode']))
                rank = 0
                for row in rows:
                    if row.get('score') is not None:
                        rank += 1
                        row['rank'] = rank
            observed_rows = rows
            if self.mode == 'live':
                rows = [row for row in rows if row['thscode'] in self.codes]
                rank = 0
                for row in rows:
                    if row.get('score') is not None:
                        rank += 1
                        row['rank'] = rank
            public_rows = [{k: v for k, v in row.items() if k not in ('history', 'raw')} for row in rows]
            for row in public_rows:
                row['sources'] = self.sources.get(row['thscode'], []) if self.mode == 'live' else []
            analysis = copy.deepcopy(self.stock_analysis) if self.mode == 'live' else None
            if analysis:
                code = analysis['thscode']
                observed = next((r for r in observed_rows if r['thscode'] == code), None)
                if observed and code not in self.codes:
                    observed = dict(observed,rank=None)
                analysis['auction'] = dict(row=observed,
                    history=copy.deepcopy(self.engine.history.get(code, [])[-1000:]), date=self.session_date,
                    mode=self.mode, tracking=self.running and code in self.codes, scope_count=len(self.codes))
            summary = self.engine.summary()
            summary['scored_count'] = sum(r.get('score') is not None for r in rows)
            summary['coverage'] = summary['scored_count']/len(rows) if rows else 0
            summary['provisional_count'] = sum(bool(r.get('is_provisional')) for r in rows)
            phase = phase_at(current, current.date().isoformat() in self.days) if self.mode == 'live' else 'demo'
            state = dict(mode=self.mode, status=self.status, message=self.message,
                         now=current.isoformat(), configured=finance['configured'], running=self.running,
                         calendar={'dates': self.days[-15:], 'today_is_trading': current.date().isoformat() in self.days,
                                   'checked_on': self.calendar_loaded_date},
                         auction=dict(phase=phase, date=self.session_date, rows=public_rows,
                                      summary=summary, universe_count=len(self.codes),
                                      processed_count=len(self.processed), cycle_seconds=round(self.cycle_seconds, 3),
                                      last_batch_ms=self.last_batch_ms, finalized=self.finalized,
                                      final_count=len(self.final_codes),
                                      source_counts={s:sum(s in v for v in self.sources.values()) if self.mode == 'live' else 0 for s in ('manual','previous_limit_up','strong_trend')},
                                      coverage=round(len(self.processed)/len(self.codes), 4) if self.codes else 0),
                         stocks=dict(analysis=analysis, query_code=self.query_code,
                                     watchlist=[dict(thscode=c, name=self.metadata.get(c,{}).get('name', self.context.get(c,{}).get('name','')),
                                                     sources=self.sources.get(c,['manual']), tracking=self.mode == 'live' and self.running and c in self.codes)
                                                for c in self.config['watchlist']],
                                     trend_pool=self._public_report(self.trend_pool) if self.mode == 'live' else {}),
                         review=self._public_report(self.review), review_id=self._review_identity(),
                         review_sentiment=self._review_sentiment(), api=self._api_status(), finance=finance, jobs=self.jobs, config=self.config,
                         research=self.research_summary,
                         sector_research=(self.sector_summary if self.mode == 'live' and
                             self.sector_summary.get('review_id') == self._review_identity() else {}),
                         llm=dict(**ai_state, configured=active['configured'], base_url=active['base_url'],
                                  model=active['model'], label=active['label'],
                                  result=self.llm_results.get(active['id']),
                                  connection_test=self.llm_connection_test), errors=self.errors)
            state.update(credential_persisted=finance['persisted'], credential_source=finance['credential_source'])
            return copy.deepcopy(state)

    @staticmethod
    def _empty_finance_test():
        return dict(status='not_tested', ok=None, checked_at=None, message='尚未测试官方接口连接',
                    latency_ms=None, calendar_count=None, latest_trade_date=None)

    def _finance_save_block(self):
        if self.mode != 'live':
            return '演示模式不能更改真实数据接入，请先返回实盘模式'
        if self._auction_priority():
            return '09:10–09:26 优先竞价，请在09:27后更改数据接入'
        if self.running:
            return '请先停止监测，再保存数据 Key'
        if any(job.get('status') == 'running' for job in self.jobs.values()) or self.sector_catalog_lock.locked():
            return '请等待当前任务完成，再保存数据 Key'
        return ''

    def _finance_test_block(self, configured):
        if self.mode != 'live':
            return '演示模式不调用真实接口，请先返回实盘模式'
        if self._auction_priority():
            return '09:10–09:26 优先竞价，请在09:27后测试连接'
        if not configured:
            return '请先保存同花顺金融数据 API Key'
        if self.jobs.get('finance_test', {}).get('status') == 'running':
            return '同花顺连接测试正在进行，请等待完成'
        return ''

    def finance_status(self):
        """Local diagnostics only: no upstream request and no secret fragments."""
        key = finance_key()
        credential = credential_status()
        with self.lock:
            if self._finance_test_key is not None and self._finance_test_key != key:
                self.finance_connection_test = self._empty_finance_test()
                self._finance_test_key = None
            save_block = self._finance_save_block()
            test_block = self._finance_test_block(bool(key))
            return dict(provider='hithink', label='同花顺金融数据 API', base_url=BASE_URL,
                        configured=bool(key), credential_source=credential.get('credential_source', 'missing'),
                        persisted=bool(credential.get('credential_persisted', False)),
                        test=copy.deepcopy(self.finance_connection_test),
                        can_save=not bool(save_block), save_block_reason=save_block,
                        can_test=not bool(test_block), test_block_reason=test_block)

    def save_finance_config(self, key):
        """Save explicitly supplied credentials without starting collection."""
        validate_finance_key(key)
        with self.lock:
            reason = self._finance_save_block()
            if reason:
                raise ValueError(reason)
            save_finance_key(key)
            self._finance_revision += 1
            self.demo_generation += 1
            self.provider = None
            self.finance_connection_test = self._empty_finance_test()
            self._finance_test_key = None
            self.sector_catalog = []
            self.sector_catalog_at = 0
            self.api_backoff_until = 0
            self.api_backoff_seconds = 0
            self.message = '同花顺数据 Key 已保存，可先测试连接，再启动监测'
        self.touch()
        return self.finance_status()

    def run_finance_test(self):
        """One bounded official calendar request; never prepare or start jobs."""
        key = finance_key()
        with self.lock:
            if self.jobs.get('finance_test', {}).get('status') == 'running':
                return False
            reason = self._finance_test_block(bool(key))
            if reason:
                raise ValueError(reason)
            revision, generation = self._finance_revision, self.demo_generation
            interval = self.config['request_interval']
            timeout = min(8, self.config['request_timeout'])
            self._finance_test_key = key
            self.finance_connection_test = dict(self._empty_finance_test(), status='running', message='正在读取官方交易日历验证连接')

            def still_current():
                return revision == self._finance_revision and key == finance_key()

            def cancelled():
                return (self.shutdown.is_set() or self.mode != 'live' or generation != self.demo_generation
                        or self._auction_priority() or not still_current())

            def work():
                began = time.monotonic()
                result = dict(self._empty_finance_test(), status='error', ok=False)
                try:
                    if cancelled():
                        raise ValueError('连接测试已取消，请在实盘模式和竞价保护时段外重试')
                    client = HiThinkProvider(key, min_interval=interval, timeout=timeout, max_retries=0)
                    days = client.calendar()
                    if (not isinstance(days, list) or not days or len(days) > 10000
                            or any(not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) for day in days)):
                        raise ValueError('官方接口未返回有效的非空交易日历，连接尚未通过验证')
                    for day in days:
                        datetime.strptime(day, '%Y-%m-%d')
                    if cancelled():
                        raise ValueError('模式、凭据或采集时段已改变，本次连接测试已取消')
                    result.update(status='success', ok=True, message='官方交易日历连接成功；尚未开始监测或验证其他接口权限',
                                  calendar_count=len(set(days)), latest_trade_date=max(days))
                except APIError as exc:
                    messages = {2001:'同花顺 Key 认证失败', 2003:'同花顺 Key 无效或无接口权限',
                                401:'同花顺 Key 认证失败', 403:'同花顺接口拒绝访问',
                                429:'同花顺接口限流，请稍后重试', 4001:'同花顺接口限流，请稍后重试'}
                    result['message'] = messages.get(exc.code, '官方连接验证失败，请检查网络、Key 权限或接口返回格式')
                    if isinstance(exc.code, int) and not isinstance(exc.code, bool):
                        result['message'] += f'（code={exc.code}）'
                except ValueError:
                    result['message'] = ('模式、凭据或采集时段已改变，本次连接测试已取消' if cancelled()
                                         else '官方接口未返回有效的非空交易日历，连接尚未通过验证')
                except Exception:
                    result['message'] = '连接测试未完成，请检查本机网络与数据接入配置'
                result.update(checked_at=now_sh().isoformat(), latency_ms=round((time.monotonic()-began)*1000))
                with self.lock:
                    if still_current():
                        self.finance_connection_test = result
                    else:
                        self.finance_connection_test = self._empty_finance_test()
                        self._finance_test_key = None
                self.touch()
                if not result['ok']:
                    raise ValueError(result['message'])

            return self._job('finance_test', work)

    @staticmethod
    def _public_report(value):
        if isinstance(value, dict):
            return {k:Service._public_report(v) for k,v in value.items() if k != 'raw'}
        if isinstance(value,list):
            return [Service._public_report(v) for v in value]
        return value

    def export_report(self):
        with self.lock:
            return copy.deepcopy(self.review or {})

    def _review_identity(self):
        # Reports are replaced, not mutated, after publication. Hash once per report.
        if self._identity_source is not self.review:
            self._identity_source = self.review
            self._identity_value = report_identity(self.review) if self.review else None
        return self._identity_value

    def _review_sentiment(self):
        # Old reports gain a local view without rewriting evidence or AI identity.
        if self._sentiment_source is not self.review:
            self._sentiment_source = self.review
            self._sentiment_value = (self.review.get('sentiment') or build_sentiment(self.review)) if self.review else None
        return self._sentiment_value

    def _api_status(self):
        status = {'rate_limited':False, 'cooldown_seconds':0}
        if self.mode == 'live' and self.provider is not None and hasattr(self.provider, 'rate_limit_status'):
            status = self.provider.rate_limit_status()
        remaining = max(0, self.api_backoff_until - time.monotonic()) if self.mode == 'live' else 0
        seconds = max(remaining, status.get('cooldown_seconds', 0))
        return {'rate_limited':seconds > 0, 'cooldown_seconds':round(seconds, 1)}

    def enrich_report(self, date, review_id):
        """Explicit bounded official observations; never part of auction polling."""
        valid_date(date)
        current = now_sh()
        with self.lock:
            if self.mode != 'live':
                raise ValueError('演示中不能联网补充真实报告')
            if self._auction_priority():
                raise ValueError('09:10–09:26 优先竞价，请稍后补充官方观察')
            if self.jobs.get('review', {}).get('status') == 'running':
                raise ValueError('请等待复盘生成完成后补充官方观察')
            if self.jobs.get('evidence', {}).get('status') == 'running':
                return False
            if date > current.date().isoformat() or (date == current.date().isoformat() and current.hour < 15):
                raise ValueError('只能补充已收盘交易日的报告')
            report = self.saved_report(date)
            captured_id = report_identity(report)
            if not isinstance(review_id, str) or review_id != captured_id:
                raise ValueError('报告已经更新，请刷新后再补充官方观察')
            calendar = (report.get('raw') or {}).get('calendar')
            if not isinstance(calendar, list) or date not in calendar:
                raise ValueError('该报告缺少交易日历证据，请先重新生成复盘')
            generation = self.demo_generation

        def cancelled():
            return (self.shutdown.is_set() or generation != self.demo_generation
                    or self.mode != 'live' or self._auction_priority())

        def work():
            self._progress('evidence', '读取短线竞价基准和三类龙虎榜；不改动竞价评分')
            context = build_official_context(self._provider(), date, should_stop=cancelled)
            if cancelled() or context.get('cancelled'):
                raise ValueError('官方观察已中断，原报告保留；可在竞价保护时段外重试')
            if context.get('status') == 'unavailable':
                raise ValueError('官方观察暂无可用数据，原报告保留；请稍后重试或检查接口权限')
            if (report.get('official_context') or {}).get('status') == 'ready' and context.get('status') != 'ready':
                raise ValueError('本次官方观察不完整，已保留上一份完整结果')
            updated = copy.deepcopy(report)
            updated['official_context'] = context
            updated['sentiment'] = build_sentiment(updated)
            updated['enriched_at'] = now_sh().isoformat()
            # Validate rendering before committing evidence; an export file can
            # still be locked on Windows, so its later IO failure is separate.
            self.report_library.markdown(updated)
            with self.lock:
                if cancelled():
                    raise ValueError('模式或采集时段已改变，原报告保留')
                stored = self.report_library.get(date, 'live')
                if report_identity(stored) != captured_id:
                    raise ValueError('报告在补充期间已更新，未覆盖新版本，请重试')
                atomic_json(self.data_dir / 'reports' / (date+'.json'), updated)
                self.store.report(date, 'live', updated)
                if self.latest_review and self.latest_review.get('date') == date:
                    self.latest_review = updated
                if self.review and report_identity(self.review) == captured_id:
                    self.review = updated
                    self.llm_results.clear()
                    self.llm_result = None
                try:
                    self._save_markdown(updated)
                except OSError:
                    raise ValueError('官方观察已保存且报告版本已更新，但 Markdown 文件写入失败；请关闭占用文件后点击保存 Markdown 重试') from None
            self.touch()

        return self._job('evidence', work)

    def report_catalog(self):
        with self.lock:
            mode = self.mode
        today = now_sh().date().isoformat()
        return {'mode':mode, 'items':[r for r in self.report_library.list(mode) if r['date'] <= today], 'limit':365}

    def saved_report(self, date=None):
        if date is not None:
            valid_date(date)
            if date > now_sh().date().isoformat():
                raise ValueError('不能读取未来日期的收盘报告')
        with self.lock:
            mode = self.mode
            current = self.review
            if current and (date is None or current.get('date') == date):
                report = copy.deepcopy(current)
                if report.get('mode', mode) != mode:
                    raise ValueError('当前报告与真实/演示模式不符')
                report.setdefault('mode', mode)
                return report
        if date is None:
            raise ValueError('尚无可保存的收盘报告，请先生成或读取历史报告')
        valid_date(date)
        if date > now_sh().date().isoformat():
            raise ValueError('不能读取未来日期的收盘报告')
        return self.report_library.get(date, mode)

    def load_report(self, date):
        valid_date(date)
        with self.lock:
            if self.mode != 'live':
                raise ValueError('演示中不能载入真实历史报告，请先返回实盘模式')
            if self.jobs.get('review', {}).get('status') == 'running':
                raise ValueError('请等待当前复盘生成完成后读取历史报告')
            generation = self.demo_generation
        if date > now_sh().date().isoformat():
            raise ValueError('不能读取未来日期的收盘报告')
        report = self.report_library.get(date, 'live')
        ai_results = self.report_library.load_ai(report)
        with self.lock:
            if generation != self.demo_generation or self.mode != 'live':
                raise ValueError('运行模式已改变，请重新选择报告')
            self.review = report
            self.viewing_archive = True
            self.llm_results = ai_results
            self.llm_result = None
            if self.jobs.get('llm', {}).get('status') != 'running':
                self.jobs.pop('llm', None)
        self.touch()

    def compare_report(self, date, baseline):
        valid_date(date)
        valid_date(baseline)
        if baseline >= date:
            raise ValueError('对比日期必须早于当前报告日期')
        current = self.saved_report(date)
        previous = self.report_library.get(baseline, current.get('mode', 'live'))
        return compare_reports(current, previous)

    def markdown_report(self, date=None, include_ai=False, provider=None, review_id=None, baseline=None, save=False):
        if not isinstance(include_ai, bool):
            raise ValueError('是否附带 AI 分析必须为 true 或 false')
        report = self.saved_report(date)
        identity = report_identity(report)
        if review_id is not None and review_id != identity:
            raise ValueError('报告已经更新，请刷新页面后再保存，避免混用不同版本')
        ai = None
        if include_ai:
            provider = provider or profiles_state()['active_provider']
            with self.lock:
                ai = copy.deepcopy(self.llm_results.get(provider))
            if not ai or ai.get('review_id') != identity:
                ai = self.report_library.load_ai(report).get(provider)
            if not ai:
                raise ValueError('当前报告尚无所选 AI 的专属分析，请先生成文字分析或取消勾选')
        comparison = None
        if baseline:
            valid_date(baseline)
            if baseline >= report['date']:
                raise ValueError('对比日期必须早于当前报告日期')
            comparison = compare_reports(report, self.report_library.get(baseline, report.get('mode', 'live')))
        if save:
            return self.report_library.save(report, ai, comparison)
        return self.report_library.markdown(report, ai, comparison)

    def diagnostics(self):
        state = self.snapshot()
        with self.lock:
            internal = {'prepared_date':self.prepared_date, 'calendar_loaded_date':self.calendar_loaded_date}
        return readiness(state, internal)

    def _provider(self):
        key = finance_key()
        if not key:
            raise ValueError('尚未配置数据 Key。请在界面本机配置页保存')
        if self.provider is None:
            self.provider = HiThinkProvider(key, min_interval=self.config['request_interval'], timeout=self.config['request_timeout'])
        return self.provider

    def _job(self, name, func):
        with self.lock:
            peer = {'review':'evidence', 'evidence':'review'}.get(name)
            if peer and self.jobs.get(peer, {}).get('status') == 'running':
                raise ValueError('复盘生成与官方观察补充正在使用同一报告，请等待当前任务完成')
            if self.jobs.get(name, {}).get('status') == 'running':
                return False
            self.jobs[name] = dict(status='running', message='处理中')
        self.touch()
        def run():
            try:
                func()
                with self.lock:
                    self.jobs[name] = dict(status='done', message='已完成')
            except Exception as exc:
                # Unexpected exceptions report class only to avoid leaking external contents.
                safe = str(exc) if isinstance(exc, ValueError) or exc.__class__.__name__ == 'APIError' else type(exc).__name__ + '：任务失败，请检查数据及配置'
                self.error(safe)
                with self.lock:
                    self.jobs[name] = dict(status='error', message=safe)
            self.touch()
        threading.Thread(target=run, daemon=True, name='job-'+name).start()
        return True

    def _progress(self, job, text):
        with self.lock:
            self.jobs[job] = dict(status='running', message=text)
        self.touch()

    def start(self):
        self._provider()
        with self.lock:
            if self.mode == 'demo':
                self.demo_generation += 1
                self.engine.reset()
                self.provisional_rows.clear()
                self.prepared_date = None
                self.processed.clear()
                self.final_codes.clear()
                self.finalized = False
                self.review = self.store.latest_report()
                self.latest_review = self.review
                self.viewing_archive = False
                self.llm_result = None
                self.llm_results.clear()
            self.mode = 'live'
            self.running = True
            self.status = 'running'
            self.message = '自动运行：09:15–09:25 逐批分析；收盘后自动复盘'
        self.touch()

    def stop(self):
        with self.lock:
            self.running = False
            self.demo_generation += 1
            self.status = 'stopped'
            self.message = '采集已停止，已保存的数据和报告保留'
        self.touch()

    def update_config(self, changes):
        # Watchlist mutations have their own verified, nonblocking API. This legacy
        # route may retain or remove existing entries, but cannot bypass resolution.
        if 'watchlist' in changes:
            wanted = changes['watchlist']
            if isinstance(wanted, str):
                wanted = re.split(r'[,，\s]+', wanted.strip())
            wanted = [validate_code(c) for c in wanted if c]
            if any(c not in self.config['watchlist'] for c in wanted):
                raise ValueError('新增股票请到「个股查询」按代码加入关注，系统将核对官方证券目录')
            changes = dict(changes, watchlist=wanted)
        with self.lock:
            if self.running or any(v.get('status') == 'running' for v in self.jobs.values()):
                raise ValueError('请停止采集并等待当前任务完成后修改参数，避免同一轮评分口径改变')
            cfg = validate_config(dict(self.config, **changes))
            atomic_json(self.data_dir / 'config.json', cfg)
            self.config = cfg
            self.engine = AuctionEngine(cfg['weights'])
            self.provisional_rows.clear()
            self.prepared_date = None
            self.provider = None
            self.codes = []
            self.sources = {}
            self.all_codes = []
            self.processed.clear()
            self.final_codes.clear()
            self.finalized = False
        self.touch()

    def _rebuild_codes(self):
        """Called under lock. Membership changes do not erase any observations."""
        sources = {}
        def include(code, source):
            sources.setdefault(code, []).append(source)
        for code in self.config['watchlist']:
            include(code, 'manual')
        if self.config['universe'] != 'watchlist':
            for code in self.context:
                include(code, 'previous_limit_up')
            previous = next(iter(self.context.values()), {}).get('context_date')
            if previous is None and self.prepared_date:
                previous = next((d for d in reversed(self.days) if d < self.prepared_date), None)
            if self.trend_pool.get('date') == previous:
                self.session_trends = copy.deepcopy(self.trend_pool)
            active_pool = self.trend_pool
            if self.trend_pool.get('date','') > (previous or '') and self.session_trends.get('date') == previous:
                active_pool = self.session_trends
            if active_pool.get('date') == previous:
                for row in active_pool.get('rows', []):
                    include(row['thscode'], 'strong_trend')
        if self.config['universe'] == 'all':
            for code in self.all_codes:
                sources.setdefault(code, []).append('all_market')
        self.sources = sources
        self.codes = list(sources)
        self.processed.intersection_update(self.codes)
        self.final_codes.intersection_update(self.codes)
        self.finalized = bool(self.codes) and set(self.codes).issubset(self.final_codes)

    def _resolve_stock(self, code):
        code = validate_code(code)
        with self.lock:
            matches = [v for k,v in self.metadata.items() if k == code or ('.' not in code and k.split('.')[0] == code)]
            matches = [m for m in matches if m.get('resolution',{}).get('source') == 'official_ticker_search']
            if '.' not in code:
                matches = [m for m in matches if m.get('resolution',{}).get('input') == code]
        if len(matches) == 1:
            return copy.deepcopy(matches[0])
        result = self._provider().resolve_stock(code)
        with self.lock:
            self.metadata[result['thscode']] = result
            atomic_json(self.data_dir / 'stock-metadata.json', self.metadata)
        return result

    def add_watchlist(self, codes):
        if self.mode != 'live':
            raise ValueError('请退出演示并启动实盘服务后管理真实自选')
        if isinstance(codes, str):
            codes = [s for s in re.split(r'[,，\s]+',codes.strip()) if s]
        if not isinstance(codes, list) or not 1 <= len(codes) <= 50:
            raise ValueError('每次请输入 1–50 个股票代码')
        codes = list(dict.fromkeys(validate_code(c) for c in codes))
        if self._auction_priority() and len(codes) > 1:
            raise ValueError('09:10–09:26 为保障竞价，请每次添加一只；批量添加请在此时段外进行')
        def work():
            resolved = []
            for i, code in enumerate(codes):
                self._progress('watchlist', f'核对官方证券代码 {i+1}/{len(codes)}')
                stock = self._resolve_stock(code)
                if stock.get('end_date') and stock['end_date'] <= now_sh().date().isoformat():
                    raise ValueError('已结束交易的证券不能加入实时关注')
                resolved.append(stock['thscode'])
            with self.lock:
                cfg = validate_config(dict(self.config, watchlist=list(dict.fromkeys(self.config['watchlist']+resolved))))
                atomic_json(self.data_dir / 'config.json',cfg)
                self.config = cfg
                self._rebuild_codes()
            self.touch()
        return self._job('watchlist',work)

    def remove_watchlist(self, code):
        if self.mode != 'live':
            raise ValueError('请退出演示并启动实盘服务后管理真实自选')
        code = validate_code(code)
        with self.lock:
            if self.jobs.get('watchlist',{}).get('status') == 'running':
                raise ValueError('请等待本次添加关注完成后再移除')
            matches = [c for c in self.config['watchlist'] if c == code or ('.' not in code and c.split('.')[0] == code)]
            if len(matches) != 1:
                raise ValueError('代码未在自选中或存在歧义，请使用完整股票代码')
            cfg = dict(self.config,watchlist=[c for c in self.config['watchlist'] if c != matches[0]])
            atomic_json(self.data_dir / 'config.json',cfg)
            self.config = cfg
            self._rebuild_codes()
        self.touch()

    def _closed_session(self, date=None):
        current = now_sh()
        if not self.days or self.calendar_loaded_date != current.date().isoformat():
            days = sorted(set(self._provider().calendar()))
            with self.lock:
                self.days = days
                self.calendar_loaded_date = current.date().isoformat()
        eligible = [d for d in self.days if d < current.date().isoformat() or (d == current.date().isoformat() and current.hour >= 15)]
        chosen = date or (eligible[-1] if eligible else None)
        if chosen not in eligible:
            raise ValueError('请选择官方日历中的已收盘交易日')
        return chosen

    def query_stock(self, code, date=None):
        code = validate_code(code)
        if date and (not isinstance(date,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date)):
            raise ValueError('日期格式应为 YYYY-MM-DD')
        if self.mode != 'live':
            raise ValueError('请退出演示并启动实盘服务后查询真实股票')
        def work():
            metadata = self._resolve_stock(code)
            with self.lock:
                self.query_code = metadata['thscode']
                self.pending_stock = None
            if self._auction_priority():
                result = dict(thscode=metadata['thscode'],name=metadata.get('name'),metadata=metadata,date=date,
                              status='deferred',trend={},trend_score=None,trend_factors={},
                              warnings=['竞价优先时段展示本机已采集观察；日线分析将在 09:27 后自动执行。新加入的股票不会补造此前十分钟数据。'])
                with self.lock:
                    self.stock_analysis = result
                    self.pending_stock = (metadata,date)
                self.touch()
                return
            self._analyze_stock(metadata,date)
        return self._job('stock',work)

    def _analyze_stock(self, metadata, date):
        chosen = self._closed_session(date)
        self._progress('stock','正在读取前复权日线并计算个股趋势')
        with self.lock:
            context = copy.deepcopy(self.context.get(metadata['thscode']))
        result = analyze_stock(self._provider(),metadata,chosen,self.days,context=context)
        with self.lock:
            self.stock_analysis = result
        atomic_json(self.data_dir / 'stocks' / (metadata['thscode']+'-'+chosen+'.json'),result)
        self.touch()

    def refresh_trends(self, date=None):
        if self.mode != 'live':
            raise ValueError('请先退出演示模式')
        if self._auction_priority():
            raise ValueError('09:10–09:26 优先保障竞价，趋势池请提前刷新或等待 09:27 后')
        def work():
            chosen = self._closed_session(date)
            with self.lock:
                source = self.latest_review if self.latest_review and self.latest_review.get('date') == chosen else self.review
                report = copy.deepcopy(source) if source and source.get('date') == chosen else None
                self.trend_refresh_needed = False
            pool = build_trend_pool(self._provider(),chosen,self.days,report=report,
                progress=lambda s:self._progress('trends',s),
                should_stop=lambda:self.shutdown.is_set() or self._auction_priority())
            with self.lock:
                if pool.get('coverage',{}).get('cancelled_or_protected') and self.last_trend_attempt == chosen:
                    self.last_trend_attempt = None
                # An interrupted retry must not discard a previously completed pool.
                if pool.get('coverage',{}).get('cancelled_or_protected') and self._complete_trend_cache(self.trend_pool,chosen):
                    return
                atomic_json(self.data_dir / 'trend-pool.json',pool)
                self.trend_pool = pool
                self._rebuild_codes()
            self.touch()
        return self._job('trends',work)

    def prepare(self):
        if self.mode == 'demo':
            raise ValueError('请先启动实盘服务再准备关注池；演示与实盘数据不能混用')
        return self._job('prepare', self._prepare)

    def run_research(self):
        if self.mode != 'live':
            raise ValueError('请切回实盘模式；演示记录不能用于历史优化')
        if self._auction_priority():
            raise ValueError('09:10–09:26优先采集竞价，请稍后进行历史回放')
        with self.lock:
            weights = dict(self.config['weights'])
            generation = self.demo_generation
        def work():
            result = self.research_library.run(weights,now_sh(),
                should_stop=lambda:self.shutdown.is_set() or self._auction_priority() or self.mode != 'live' or self.demo_generation != generation,
                progress=lambda text:self._progress('research',text))
            with self.lock:
                self.research_summary = {k:result.get(k) for k in ('id','status','generated_at')}
        return self._job('research',work)

    def import_research(self, dataset):
        if self.mode != 'live' or self._auction_priority():
            raise ValueError('历史导入仅限实盘模式且须避开09:10–09:26竞价保护时段')
        with self.lock:
            if self.jobs.get('research',{}).get('status') == 'running':
                raise ValueError('请等待当前回测完成后导入历史数据')
        return self.research_library.import_dataset(dataset,now_sh())

    def _freeze_daily(self, date):
        with self.lock:
            self._capture_due_decisions(now_sh())
        def work():
            self.daily_validation.freeze(date,now_sh(),should_stop=lambda:self.shutdown.is_set() or self._auction_priority())
        return self._job('daily_validation',work)

    def _publish_morning_ranking(self, current):
        """Write the human-readable live ranking about a minute after 09:25.

        The 09:26 minute publishes and refreshes the view from the engine's own
        per-batch ranking, so the morning ranking is readable right after the
        09:25 close instead of waiting for the 09:27 archive.  A refresh happens
        at most every 20 seconds and only when the content actually changes.
        Nothing here touches the frozen archive, the closing pool or the network;
        if the whole minute is missed the 09:27 Markdown still carries the same
        two checkpoints.
        """
        if self.mode != 'live' or (current.hour, current.minute) != (9, 26):
            return
        with self.lock:
            last = self.morning_ranking_at
            if last is not None and current - last < timedelta(seconds=20):
                return
        day = current.date().isoformat()
        try:
            self._capture_due_decisions(current)
        except (ValueError, TypeError, OSError) as exc:
            self.error('每日时点快照保存失败：' + str(exc)[:200])
        with self.lock:
            if self.session_date != day or self.engine.session_date != day or not self.codes:
                return
            try:
                rows = self.engine.rankings(current)
            except ValueError:
                return
            summary = self.engine.summary()
            session = {
                'engine_source_sha256': self.engine_source_sha256,
                'weights': dict(self.engine.weights),
                'strategy_provenance': {'weights_source': 'live_engine', 'weights_at': current.isoformat(),
                                        'weight_changes': None, 'weight_change_scope': 'live_session',
                                        'decision_differs_from_last_batch_weights': None,
                                        'legacy_fallback': False},
                'rows': rows,
                'warnings': list(summary['warnings']) + [
                    f"本机已接收 {summary['batches']} 个原始批次，其中上游未就绪 {summary['not_ready_batches']} 个；"
                    f"终态覆盖 {len(self.final_codes)}/{len(self.codes)} 只，缺失数据未补造。"],
            }
        try:
            self.daily_validation.publish_morning_ranking(day, current, session)
        except (ValueError, OSError) as exc:
            self.error('早盘排名 Markdown 未写入：' + str(exc)[:200])
            return
        with self.lock:
            self.morning_ranking_at = current
            self.message = (f'早盘竞价排名已按实时批次写入 '
                            f'data/research/daily/{day}-morning-ranking.md；09:27 另存不可变档案')
        self.touch()

    def _capture_due_decisions(self, captured_at):
        """Called with the collection lock before ingesting a later observation.

        At most two small ranking snapshots per day. Never time-travel an engine
        which has already consumed data after the decision cutoff.
        """
        day = captured_at.date().isoformat()
        if self.mode != 'live' or self.session_date != day or self.engine.session_date != day:
            return
        for checkpoint in CHECKPOINTS:
            key = (day,checkpoint)
            cutoff = datetime.fromisoformat(day+'T'+checkpoint+'+08:00')
            if key in self.recorded_decisions or captured_at <= cutoff:
                continue
            last = self.engine._last_received
            if last is None or last > cutoff:
                continue
            rows = self.engine.rankings(cutoff)
            self.store.freeze_decision(day,checkpoint,dict(date=day,checkpoint=checkpoint,mode='live',
                captured_at=captured_at.isoformat(),weights=dict(self.engine.weights),rows=rows,
                engine_source_sha256=self.engine_source_sha256))
            self.recorded_decisions.add(key)

    def _restore_saved_batches_locked(self, today, codes, batches):
        """Restore one already prepared live session; caller holds ``self.lock``."""
        self.engine.reset()
        self.provisional_rows.clear()
        self.processed.clear()
        self.final_codes.clear()
        self.finalized = False
        self.session_date = today
        for received, stage, data in batches:
            restored = self.engine.ingest(data, datetime.fromisoformat(received), self.context)
            if stage == 'live':
                self.provisional_rows.update({r['thscode']:r for r in restored})
            accepted = [r for r in restored if r.get('updated_at') == received and r.get('data_status') == 'ready'
                        and isinstance(r.get('auction_price'),(int,float)) and r['auction_price'] > 0]
            seen = {r['thscode'] for r in accepted}
            self.processed.update(seen & set(codes))
            if stage == 'final':
                self.final_codes.update({r['thscode'] for r in accepted if r.get('phase') == 'final'} & set(codes))
        self.finalized = bool(codes) and set(codes).issubset(self.final_codes)

    def _restore_local_session(self, today):
        """Recover today's immutable preparation and raw batches after an API outage.

        This is deliberately limited to a same-day manifest that already proved the
        trading calendar and candidate context.  It never prepares a new day from a
        cache and never invents observations missing from SQLite.
        """
        current = now_sh()
        manifest = self.store.manifest(today)
        batches = self.store.batches(today, 'live')
        if not isinstance(manifest, dict) or not batches:
            return False
        try:
            prepared = datetime.fromisoformat(manifest.get('prepared_at'))
            days = sorted(set(manifest.get('calendar') or []))
            previous = manifest.get('previous_date')
            context = manifest.get('context')
            codes = manifest.get('codes')
            if (manifest.get('date') != today or manifest.get('mode') != 'live'
                    or manifest.get('source') != 'local_preparation'
                    or manifest.get('context_complete') is not True
                    or prepared.tzinfo is None or prepared.date().isoformat() != today or prepared > current
                    or today not in days or previous != next((d for d in reversed(days) if d < today), None)
                    or not isinstance(context, dict) or not isinstance(codes, list) or not codes
                    or len(codes) != len(set(codes))
                    or any(not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code) for code in codes)
                    or any(not isinstance(row, dict) or row.get('context_date') != previous for row in context.values())):
                return False
            for received, stage, payload in batches:
                stamp = datetime.fromisoformat(received)
                if (stamp.tzinfo is None or stamp.date().isoformat() != today or stamp > current
                        or stage not in ('live','final') or not isinstance(payload, dict)):
                    return False
        except (TypeError, ValueError):
            return False
        with self.lock:
            if self.mode != 'live':
                return False
            self.days = days
            self.calendar_loaded_date = today
            self.context = copy.deepcopy(context)
            self.prepared_date = today
            self.all_codes = codes[:] if self.config['universe'] == 'all' else []
            self._rebuild_codes()
            restored_sources = self.sources
            self.codes = codes[:]
            self.sources = {}
            for code in codes:
                known = restored_sources.get(code, [])
                if known:
                    self.sources[code] = known
                elif self.config['universe'] == 'all':
                    self.sources[code] = ['all_market']
                else:
                    self.sources[code] = []
            self._restore_saved_batches_locked(today, self.codes, batches)
            self.message = (f'官方准备请求暂时失败；已从今日盘前清单和 {len(batches)} 个本机原始批次恢复分析，'
                            '未补造或改写行情')
        self.touch()
        return True

    def _prepare(self):
        today = now_sh().date().isoformat()
        try:
            provider = self._provider()
            self._progress('prepare', '读取交易日历与昨日涨停池')
            days = sorted(set(provider.calendar()))
            if not days:
                raise ValueError('交易日历为空，不能确定交易日；已暂停自动采集')
            previous = next((d for d in reversed(days) if d < today), None)
            if not previous:
                raise ValueError('未取得上一交易日，无法构建无未来数据的重点池')
            with self.lock:
                self.days = days
                self.calendar_loaded_date = today
            pool = provider.pool('limit-up', previous)
            pool.sort(key=lambda r:-(strict_continuity(r) or 0))
            context = {r['thscode']: dict(r, context_date=previous) for r in pool if r.get('thscode')}
            universe = self.config['universe']
            all_codes = []
            if universe == 'all':
                self._progress('prepare', '读取全市场证券目录，重点池优先采集')
                tickers = provider.tickers()
                all_codes = [r['thscode'] for r in tickers if r.get('thscode') and not r.get('end_date')]
        except APIError as exc:
            if self._restore_local_session(today):
                self.error('官方准备请求失败；已安全恢复今日本机竞价分析：' + str(exc))
                return
            raise
        with self.lock:
            new_session = self.session_date != today or self.mode != 'live' or self.prepared_date is None
            self.context = context
            self.all_codes = all_codes
            self.prepared_date = today
            self._rebuild_codes()
            codes = self.codes[:]
            if today in days:
                prepared = now_sh()
                clean_context = pool_rows(list(context.values()))
                self.store.freeze_manifest(dict(date=today,mode='live',prepared_at=prepared.isoformat(),
                    previous_date=previous,calendar=days,context={r['thscode']:r for r in clean_context},
                    codes=codes,context_complete=True,point_in_time=prepared.strftime('%H:%M:%S') <= '09:15:00',
                    source='local_preparation',weights=dict(self.config['weights'])))
            if new_session:
                self._restore_saved_batches_locked(today, codes, self.store.batches(today, 'live'))
            self.message = f'已准备 {len(codes)} 只股票；上一交易日 {previous}。每批返回立即更新排名'
            if not codes:
                self.message = '当前股票池为空，请按股票代码添加关注；不使用示例证券替代'
        self.touch()

    def collect_cycle(self, stage, clock=now_sh):
        """Independent batches are ingested before fetching the next, never gathered first."""
        started = time.monotonic()
        size = self.config['batch_size']
        with self.lock:
            if stage == 'final':
                pending = [c for c in self.codes if c not in self.final_codes]
                # 官方终值可能晚于第一次 matched/final 才落定，补采窗口内继续按轮复核并采用最新值。
                codes = pending or list(self.codes)
            else:
                codes = list(self.codes)
            generation = self.demo_generation
        for offset in range(0, len(codes), size):
            if not self.running or self.shutdown.is_set() or self.mode != 'live':
                break
            current = clock()
            boundary = current.replace(hour=9, minute=25, second=0, microsecond=0)
            if stage == 'live' and current >= boundary:
                break
            if stage == 'final' and current > boundary + timedelta(seconds=self.config['final_grace_seconds']):
                break
            batch = codes[offset:offset+size]
            before = time.monotonic()
            try:
                data = self._provider().auction(batch, stage)
            except Exception as exc:
                safe = str(exc) if isinstance(exc,ValueError) or exc.__class__.__name__ == 'APIError' else type(exc).__name__+'：批次请求失败'
                self.error(safe)
                requested_wait = getattr(exc, 'retry_after_seconds', None)
                requested_wait = requested_wait if isinstance(requested_wait, (int, float)) and not isinstance(requested_wait, bool) and math.isfinite(requested_wait) and requested_wait >= 0 else 0
                if getattr(exc, 'code', None) in (2001,2003,4001,429) or requested_wait > 0:
                    # Do not hammer an invalid credential or a rate-limited service.
                    if getattr(exc,'code',None) in (2001,2003):
                        self.stop()
                    else:
                        self.api_backoff_seconds = max(min(60, max(3, self.api_backoff_seconds*2)), requested_wait)
                        self.api_backoff_until = time.monotonic()+self.api_backoff_seconds
                        with self.lock:
                            self.message = f'接口暂缓请求，等待约 {self.api_backoff_seconds:.0f} 秒后再采集；已有排名与缺失标记保留'
                    break
                continue
            data = dict(data, _requested_codes=batch)
            self.api_backoff_seconds = 0
            received = clock()
            with self.lock:
                if generation != self.demo_generation or self.mode != 'live':
                    break
                self._capture_due_decisions(received)
                data['_strategy_weights'] = dict(self.engine.weights)
                self.store.batch(self.session_date, 'live', received.isoformat(), stage, data)
                ranked = self.engine.ingest(data, received, self.context)
                if stage == 'live':
                    self.provisional_rows.update({r['thscode']:r for r in ranked})
                accepted = [r for r in ranked if r.get('updated_at') == received.isoformat() and r.get('data_status') == 'ready'
                            and isinstance(r.get('auction_price'),(int,float)) and r['auction_price'] > 0 and r['thscode'] in batch]
                self.processed.update(r['thscode'] for r in accepted if r['thscode'] in self.codes)
                if stage == 'final':
                    self.final_codes.update(r['thscode'] for r in accepted if r.get('phase') == 'final' and r['thscode'] in self.codes)
                self.last_batch_ms = round((time.monotonic()-before)*1000)
                self.finalized = bool(self.codes) and set(self.codes).issubset(self.final_codes)
                self.message = ('实时分析中：每批响应后立即计算' if stage == 'live' else '正在核验 09:25 终态；已有排名可立即查看')
            self.touch()
        with self.lock:
            self.cycle_seconds = time.monotonic()-started
        self.touch()

    def run_review(self, date=None, automatic=False):
        if date is not None:
            valid_date(date)
        if self.mode == 'demo':
            raise ValueError('当前为演示模式。请启动实盘服务后再生成真实复盘')
        if self.jobs.get('evidence', {}).get('status') == 'running':
            raise ValueError('请等待官方观察补充完成后重新生成复盘')
        current = now_sh()
        if date is not None and date > current.date().isoformat():
            raise ValueError('请选择已收盘的交易日')
        if '09:10' <= current.strftime('%H:%M') <= '09:26':
            raise ValueError('09:10–09:26 优先保障竞价采集，请在此时段外生成复盘')
        def work():
            provider = self._provider()
            days = sorted(set(provider.calendar()))
            today = current.date().isoformat()
            eligible = [d for d in days if d < today or (d == today and current.hour >= 15)]
            chosen = date or (eligible[-1] if eligible else None)
            if chosen not in eligible:
                raise ValueError('请选择已收盘的交易日，不能用盘中快照冒充收盘数据')
            previous = next((d for d in reversed(days) if d < chosen), None)
            report = build_review(provider, chosen, previous, progress=lambda s:self._progress('review',s))
            report['mode'] = 'live'
            report['config_weights'] = dict(self.config['weights'])
            warnings = report.get('warnings')
            warnings = warnings if isinstance(warnings, list) else []
            report['warnings'] = warnings
            # The closing review now reads the same four official sub-items the
            # manual enrichment offers: one benchmark plus three dragon-tiger
            # boards, always with this explicit date and cancellable before
            # every request.  A missing sub-item only adds a warning; it never
            # upgrades or blocks the market part of the report.
            generation = self.demo_generation
            def cancelled():
                return (self.shutdown.is_set() or generation != self.demo_generation
                        or self.mode != 'live' or self._auction_priority())
            self._progress('review','读取短线竞价风向标与三类龙虎榜（显式日期，可取消）')
            try:
                official = build_official_context(provider, chosen, should_stop=cancelled)
            except Exception:
                official = None
                warnings.append('官方补充观察读取异常，未写入报告；可在页面「联网补充官方观察」重试')
            if official is not None:
                if official.get('cancelled'):
                    warnings.append('官方补充观察因停止或竞价保护时段中断，未写入报告；可在保护时段外重新补充')
                elif official.get('status') == 'unavailable':
                    warnings.append('官方补充观察本次不可用（风向标与三类龙虎榜均未取得）；缺失不视为零，可在保护时段外重新补充')
                else:
                    report['official_context'] = official
                    if official.get('status') != 'ready':
                        warnings.append('官方补充观察部分缺失，分层状态与覆盖见 official_context；不改变原盘面的缺口')
            report['sentiment'] = build_sentiment(report)
            self.store.report(chosen, 'live', report)
            atomic_json(self.data_dir / 'reports' / (chosen+'.json'), report)
            self._save_markdown(report)
            ai_results = self.report_library.load_ai(report)
            with self.lock:
                if not self.latest_review or chosen >= self.latest_review.get('date', ''):
                    self.latest_review = report
                if not automatic or not self.viewing_archive:
                    self.review = report
                    self.viewing_archive = False
                    self.llm_results = ai_results
                    self.llm_result = None
                if eligible and chosen == eligible[-1]:
                    self.trend_refresh_needed = True
                    self.last_trend_attempt = None
            # Original observations/ranks are immutable; close-time labels create
            # a separate local artifact and never feed the live auction engine.
            matched_at = now_sh()
            if matched_at >= datetime.fromisoformat(chosen+'T15:10:00+08:00'):
                try:
                    daily = self.daily_validation.label(report,matched_at,
                        should_stop=lambda:self.shutdown.is_set() or self._auction_priority())
                    if daily.get('status') != 'unavailable' and not self._auction_priority():
                        self.run_research()
                except Exception as exc:
                    safe = str(exc) if isinstance(exc,ValueError) else '每日竞价核验未完成，已生成的收盘复盘仍保留'
                    with self.lock:
                        self.jobs['daily_validation'] = dict(status='error',message=safe)
                    self.error(safe)
        return self._job('review', work)

    def _save_markdown(self, report):
        return self.report_library.save(report)

    def _sector_guard(self, generation=None):
        if self.mode != 'live':
            raise ValueError('演示中不能读取真实板块数据，请先返回实盘模式')
        if self.shutdown.is_set() or (generation is not None and generation != self.demo_generation):
            raise ValueError('板块取数已取消，请重新操作')
        if self._auction_priority():
            raise ValueError('09:10–09:26 优先竞价，板块联网取数请在09:27后进行')

    def _sector_catalog(self, generation):
        self._sector_guard(generation)
        with self.sector_catalog_lock:
            self._sector_guard(generation)
            if self.sector_catalog and time.monotonic() - self.sector_catalog_at < 900:
                return copy.deepcopy(self.sector_catalog)
            def stopped():
                return self.shutdown.is_set() or self.mode != 'live' or generation != self.demo_generation or self._auction_priority()
            rows = load_catalog(self._provider(), should_stop=stopped)
            self._sector_guard(generation)
            self.sector_catalog = rows
            self.sector_catalog_at = time.monotonic()
            return copy.deepcopy(rows)

    def search_sectors(self, query):
        # Validate before network access. Catalog is cached; never used by polling.
        if not isinstance(query, str) or not query.strip() or len(query) > 80:
            raise ValueError('请输入 1—80 字的板块名称或完整指数代码')
        return search_catalog(self._sector_catalog(self.demo_generation), query)

    def _sector_report(self, date, review_id):
        report = self.saved_report(valid_date(date))
        if report.get('mode', 'live') != 'live' or self.mode != 'live':
            raise ValueError('板块取数需要真实收盘报告')
        if not review_id or report_identity(report) != review_id:
            raise ValueError('报告已经更新，请刷新页面后再读取板块数据')
        return report

    def sector_research_result(self, date, review_id):
        self._sector_report(date, review_id)
        result = self.sector_library.latest(date, review_id)
        with self.lock:
            if self._review_identity() == review_id:
                self.sector_summary = {k:result.get(k) for k in
                    ('evidence_id','date','review_id','status','generated_at')}
        return self._public_report(result)

    def _fetch_sectors(self, codes, report, review_id, generation, job):
        self._sector_guard(generation)
        if not self.sector_fetch_lock.acquire(blocking=False):
            raise ValueError('已有板块取数正在运行，请等待完成')
        try:
            catalog = self._sector_catalog(generation)
            def stopped():
                return self.shutdown.is_set() or self.mode != 'live' or generation != self.demo_generation or self._auction_priority()
            evidence = build_sector_research(self._provider(), codes, report['date'], report=report,
                catalog=catalog, now=now_sh(), should_stop=stopped,
                progress=lambda message:self._progress(job, message))
            self._sector_guard(generation)
            # Retain source evidence under the captured version, never mutate it.
            evidence = self.sector_library.save(dict(evidence, review_id=review_id))
            with self.lock:
                if self._review_identity() == review_id:
                    self.sector_summary = {k:evidence.get(k) for k in
                        ('evidence_id','date','review_id','status','generated_at')}
            self.touch()
            return self._public_report(evidence)
        finally:
            self.sector_fetch_lock.release()

    def run_sector_research(self, codes, date, review_id):
        self._sector_guard()
        codes = validate_sector_codes(codes)
        report = self._sector_report(date, review_id)
        generation = self.demo_generation
        return self._job('sectors', lambda:self._fetch_sectors(codes, report, review_id, generation, 'sectors'))

    def run_llm(self, question='', provider=None, review_date=None, review_id=None,
                sector_codes=None, fetch_sectors=False):
        if not isinstance(fetch_sectors, bool):
            raise ValueError('是否联网取数必须为 true 或 false')
        if sector_codes is not None and not fetch_sectors:
            raise ValueError('指定板块后请使用联网取数并 AI 分析')
        if fetch_sectors:
            self._sector_guard()
            sector_codes = validate_sector_codes(sector_codes)
            self._sector_report(review_date, review_id)
        state = self.snapshot()
        generation = self.demo_generation
        config = copy.deepcopy(load_llm(provider) if provider is not None else load_llm())
        if fetch_sectors and not config.get('api_key'):
            raise ValueError('请先配置所选 AI 服务商的 Key，或使用“只读取板块数据”')
        provider = config.get('provider', 'custom')
        scope = 'market'
        captured_review_id = None
        if review_date is not None:
            report = self.saved_report(review_date)
            captured_review_id = report_identity(report)
            if review_id is not None and review_id != captured_review_id:
                raise ValueError('报告已经更新，请刷新页面后再生成 AI 分析')
            # A daily report appendix must not mix today's auction with an older report.
            state = {'mode':report.get('mode','live'), 'now':state['now'], 'review':self._public_report(report),
                     'review_sentiment':report.get('sentiment') or build_sentiment(report),
                     'auction':{'date':None,'phase':'not_included','summary':{},'rows':[]},
                     'stocks':{'analysis':None}}
            scope = 'review'
        if not state['auction']['rows'] and not state['review'] and not state['stocks']['analysis']:
            raise ValueError('还没有可分析的数据，请先采集或生成复盘')
        def work():
            from .llm import analyze
            evidence = None
            if fetch_sectors:
                evidence = self._fetch_sectors(sector_codes, report, captured_review_id, generation, 'llm')
                self._sector_guard(generation)
                self._sector_report(review_date, captured_review_id)
                if evidence.get('status') == 'unavailable' or not evidence.get('boards'):
                    raise ValueError('指定板块未取得有效证据，未调用 AI；请查看取数提示后重试')
                state['sector_research'] = evidence
                self._progress('llm', '板块证据已保存，正在调用所选 AI 分析')
            result = analyze(config, state, question)
            record = {'text':result, 'generated_at':now_sh().isoformat(),'mode':state['mode'],
                      'provider':provider, 'label':config.get('label'), 'model':config.get('model'),
                      'scope':scope, 'review_date':review_date, 'review_id':captured_review_id,
                      'question':str(question)[:2000]}
            if evidence:
                record.update(sector_codes=sector_codes, sector_evidence_id=evidence['evidence_id'],
                    sector_generated_at=evidence.get('generated_at'))
            with self.lock:
                if generation != self.demo_generation or self.mode != state['mode']:
                    return
            if scope == 'review':
                self.report_library.save_ai(record)
            with self.lock:
                if generation != self.demo_generation or self.mode != state['mode']:
                    return
                if scope == 'review' and self._review_identity() != captured_review_id:
                    return  # Kept in its own archive, not shown under a different report.
                self.llm_result = record
                self.llm_results[provider] = record
        return self._job('llm', work)

    def run_llm_test(self, provider=None):
        provider = provider or profiles_state()['active_provider']
        def work():
            result = test_connection(provider)
            with self.lock:
                self.llm_connection_test = result
        return self._job('llm_test', work)

    def history(self, symbol):
        symbol = validate_code(symbol)
        if '.' not in symbol:
            with self.lock:
                matches = [c for c in set(self.codes) | set(self.engine.history) if c.split('.')[0] == symbol]
            if len(matches) != 1:
                raise ValueError('本机记录无法唯一匹配，请提供完整股票代码')
            symbol = matches[0]
        result = []
        if self.session_date:
            for received, stage, data in self.store.batches(self.session_date, self.mode):
                for row in data.get('item', []):
                    if row.get('thscode') == symbol:
                        result.append(dict(row, received_at=received, stage=stage))
        return dict(symbol=symbol,date=self.session_date,mode=self.mode,items=result[-1000:])

    def exit_demo(self):
        """Return to a stopped real-data view without credentials or network."""
        with self.lock:
            if self.mode != 'demo':
                return
            self.demo_generation += 1
            self.running = False
            self.mode = 'live'
            self.status = 'stopped'
            self.message = '已退出演示；可配置同花顺数据接入，尚未启动监测'
            self.engine.reset()
            self.provisional_rows.clear()
            self.codes = []
            self.all_codes = []
            self.sources = {}
            self.context = {}
            self.days = []
            self.session_date = None
            self.prepared_date = None
            self.calendar_loaded_date = None
            self.session_trends = {}
            self.processed.clear()
            self.final_codes.clear()
            self.finalized = False
            self.cycle_seconds = 0
            self.last_batch_ms = None
            self.stock_analysis = None
            self.pending_stock = None
            self.query_code = None
            self.sector_summary = {}
            self.review = self.store.latest_report('live')
            self.latest_review = self.review
            self.viewing_archive = False
            self.llm_result = None
            self.llm_results = self.report_library.load_ai(self.review) if self.review else {}
        self.touch()

    def demo(self):
        with self.lock:
            if any(v.get('status') == 'running' for v in self.jobs.values()):
                raise ValueError('请等待当前任务完成后进入演示')
            self.running = False
            self.demo_generation += 1
            generation = self.demo_generation
            self.mode = 'demo'
            self.status = 'demo'
            self.message = '合成演示数据：加速回放十分钟流程，不能用于投资分析'
            self.engine.reset()
            self.provisional_rows.clear()
            self.review = None
            self.llm_result = None
            self.llm_results.clear()
            self.pending_stock = None
            self.stock_analysis = None
            self.processed.clear()
            self.final_codes.clear()
            self.finalized = False
            self.codes = [f'{i:06d}.SZ' for i in range(1,9)]
            self.session_date = '2026-01-05'
        self.touch()
        def work():
            import math
            start = datetime(2026,1,5,9,15,tzinfo=SH)
            context = {c:dict(continue_day_cnt=i%4+1, continue_day_text=str(i%4+1)+'连板', context_date='2026-01-02') for i,c in enumerate(self.codes)}
            for step in range(61):
                if generation != self.demo_generation or self.shutdown.is_set():
                    return
                received = start+timedelta(seconds=step*10)
                rows = []
                for i, code in enumerate(self.codes):
                    pct = i*.5 + step*.03*(1 if i%3 else -1) + math.sin(step/5+i)*.2
                    amount = (i+1)*1_000_000*(1+step*.03)*(1 if i%3 else 1-step*.004)
                    rows.append(dict(thscode=code,name='演示股票'+str(i+1),auction_price=10*(1+pct/100),
                                     auction_pct=pct,auction_amount=amount,auction_volume=amount/10,
                                     auction_turnover_pct=.2+i*.03,auction_volume_ratio=1+i*.3,
                                     float_market_cap=5e9,pre_close_price=10))
                stage = 'final' if step == 60 else 'live'
                data = dict(timestamp=int(received.timestamp()*1000),data_status='ready',auction_phase=stage,item=rows)
                with self.lock:
                    if generation != self.demo_generation or self.mode != 'demo':
                        return
                    self.store.batch(self.session_date,'demo',received.isoformat(),stage,data)
                    self.engine.ingest(data,received,context)
                    self.processed.update(self.codes)
                    self.finalized = step == 60
                    if self.finalized:
                        self.final_codes.update(self.codes)
                    self.cycle_seconds = .3
                self.touch()
                if self.shutdown.wait(.3):
                    return
        threading.Thread(target=work,daemon=True,name='explicit-demo').start()

    def _loop(self):
        next_poll = 0
        while not self.shutdown.wait(.2):
            if self.mode == 'live' and self.pending_stock and not self._auction_priority() and self.jobs.get('stock',{}).get('status') != 'running':
                with self.lock:
                    pending = self.pending_stock
                    self.pending_stock = None
                self._job('stock',lambda p=pending:self._analyze_stock(*p))
            if not self.running or self.mode != 'live':
                continue
            current = now_sh()
            today = current.date().isoformat()
            try:
                if self.prepared_date != today:
                    if time.monotonic() >= self.retry_prepare_at and self.jobs.get('prepare',{}).get('status') != 'running':
                        self.retry_prepare_at = time.monotonic()+60
                        self.prepare()
                    continue
                if not self._auction_priority() and self.config['universe'] != 'watchlist' and self.jobs.get('review',{}).get('status') != 'running':
                    eligible = [d for d in self.days if d < today or (d == today and current.hour >= 15)]
                    if self.config['auto_review'] and (not self.latest_review or self.latest_review.get('date') != today):
                        eligible = [d for d in eligible if d < today]
                    trend_date = eligible[-1] if eligible else None
                    if trend_date and self.last_trend_attempt != trend_date and self.jobs.get('trends',{}).get('status') != 'running':
                        self.last_trend_attempt = trend_date
                        if self.trend_refresh_needed or not self._complete_trend_cache(self.trend_pool,trend_date):
                            self.refresh_trends(trend_date)
                if today not in self.days:
                    with self.lock:
                        self.message = '今日非交易日，自动等待下个交易日；可复盘最近已收盘交易日'
                    continue
                phase = phase_at(current, True)
                if phase in ('cancellable','firm') and time.monotonic() >= max(next_poll,self.api_backoff_until):
                    self.collect_cycle('live')
                    next_poll = time.monotonic()+max(0,self.config['poll_seconds']-self.cycle_seconds)
                boundary = current.replace(hour=9,minute=25,second=0,microsecond=0)
                if boundary <= current <= boundary+timedelta(seconds=self.config['final_grace_seconds']) and time.monotonic() >= max(next_poll,self.api_backoff_until):
                    self.collect_cycle('final')
                    next_poll = time.monotonic()+self.config['poll_seconds']
                if current > boundary+timedelta(seconds=self.config['final_grace_seconds']) and self.final_window_notice != today:
                    self.final_window_notice = today
                    with self.lock:
                        self.message = (f'竞价窗口已结束（含终值复核至 09:26:50）。已收集 {len(self.processed)}/{len(self.codes)} 只，'
                                        f'终态 {len(self.final_codes)}/{len(self.codes)}；缺失数据未补造')
                self._publish_morning_ranking(current)
                if current.strftime('%H:%M') >= '09:27' and self.last_daily_attempt != today:
                    self.last_daily_attempt = today
                    self._freeze_daily(today)
                if (self.config['auto_review'] and current.strftime('%H:%M') >= self.config['review_time']
                        and self.last_review_attempt != today and self.jobs.get('evidence', {}).get('status') != 'running'):
                    self.last_review_attempt = today
                    if (not self.latest_review or self.latest_review.get('date') != today
                            or self.latest_review.get('status') != 'ready'
                            or self.latest_review.get('generated_at','') < today+'T15:10:00'):
                        self.run_review(today, automatic=True)
            except Exception as exc:
                safe = str(exc) if isinstance(exc,ValueError) or exc.__class__.__name__ == 'APIError' else type(exc).__name__+'：采集失败'
                self.error(safe)
                next_poll = time.monotonic()+max(self.config['poll_seconds'],3)
                self.shutdown.wait(1)

    def close(self):
        self.running = False
        self.shutdown.set()
        self.touch()

    def restart_prepare(self):
        """Quiesce this process so the page button can replace it with a fresh one.

        The caller (local HTTP handler) answers first and only then closes the
        socket, spawns ``run.py`` again and exits, so the new process reloads
        both the Python code and ``data/config.json``.  Nothing here touches
        evidence: batches are already persisted and the same-day restore logic
        can rebuild the session after the restart.
        """
        current = now_sh()
        if '09:10' <= current.strftime('%H:%M') <= '09:26':
            raise ValueError('09:10–09:26 优先保障竞价采集，请在此时段外重启服务')
        running = sorted(name for name, job in self.jobs.items()
                         if isinstance(job, dict) and job.get('status') == 'running')
        if running:
            raise ValueError('后台任务正在运行（' + '、'.join(running) + '），请等任务完成后再重启')
        was_running = bool(self.running)
        self.stop()
        return {'restarting': True, 'mode': self.mode, 'config_file': 'data/config.json',
                'was_running': was_running, 'auto_start': was_running,
                'message': ('正在重启本机服务以加载新的代码与配置；约 3—5 秒后自动恢复，页面会自动重连并刷新。'
                            '若 10 秒后仍未恢复，请双击 启动系统.cmd。')}
