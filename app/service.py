import copy
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import DATA, atomic_json, finance_key, credential_status, load_config, validate_config
from .ai_gateway import active_config as load_llm, profiles_state, test_connection, AIGatewayError
from .engine import AuctionEngine, strict_continuity
from .provider import HiThinkProvider
from .review import build_review
from .storage import Store
from .stocks import analyze_stock, validate_code
from .selection import build_trend_pool
from .report_library import ReportLibrary, valid_date
from .reporting import report_identity
from .insights import compare_reports, readiness

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
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.version = 0
        self.shutdown = threading.Event()
        self.running = False
        self.mode = 'live'
        self.status = 'idle'
        self.message = '请配置数据 Key；随后自动等待交易日竞价及收盘复盘'
        self.provider = None
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
                         now=current.isoformat(), configured=bool(finance_key()), running=self.running,
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
                         review=self._public_report(self.review), review_id=self._review_identity(), jobs=self.jobs, config=self.config,
                         llm=dict(**ai_state, configured=active['configured'], base_url=active['base_url'],
                                  model=active['model'], label=active['label'],
                                  result=self.llm_results.get(active['id']),
                                  connection_test=self.llm_connection_test), errors=self.errors)
            state.update(credential_status())
            return copy.deepcopy(state)

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

    def _prepare(self):
        provider = self._provider()
        today = now_sh().date().isoformat()
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
        with self.lock:
            new_session = self.session_date != today or self.mode != 'live' or self.prepared_date is None
            self.context = context
            self.all_codes = all_codes
            self.prepared_date = today
            self._rebuild_codes()
            codes = self.codes[:]
            if new_session:
                self.engine.reset()
                self.provisional_rows.clear()
                self.processed.clear()
                self.final_codes.clear()
                self.finalized = False
                self.session_date = today
                for received, stage, data in self.store.batches(today, 'live'):
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
            self.message = f'已准备 {len(codes)} 只股票；上一交易日 {previous}。每批返回立即更新排名'
            if not codes:
                self.message = '当前股票池为空，请按股票代码添加关注；不使用示例证券替代'
        self.touch()

    def collect_cycle(self, stage, clock=now_sh):
        """Independent batches are ingested before fetching the next, never gathered first."""
        started = time.monotonic()
        size = self.config['batch_size']
        with self.lock:
            codes = [c for c in self.codes if stage != 'final' or c not in self.final_codes]
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
                if getattr(exc, 'code', None) in (2001,2003,4001,429):
                    # Do not hammer an invalid credential or a rate-limited service.
                    if getattr(exc,'code',None) in (2001,2003):
                        self.stop()
                    else:
                        self.api_backoff_seconds = min(60, max(3, self.api_backoff_seconds*2))
                        self.api_backoff_until = time.monotonic()+self.api_backoff_seconds
                    break
                continue
            data = dict(data, _requested_codes=batch)
            self.api_backoff_seconds = 0
            received = clock()
            with self.lock:
                if generation != self.demo_generation or self.mode != 'live':
                    break
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
        return self._job('review', work)

    def _save_markdown(self, report):
        return self.report_library.save(report)

    def run_llm(self, question='', provider=None, review_date=None, review_id=None):
        state = self.snapshot()
        generation = self.demo_generation
        config = copy.deepcopy(load_llm(provider) if provider is not None else load_llm())
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
                     'auction':{'date':None,'phase':'not_included','summary':{},'rows':[]},
                     'stocks':{'analysis':None}}
            scope = 'review'
        if not state['auction']['rows'] and not state['review'] and not state['stocks']['analysis']:
            raise ValueError('还没有可分析的数据，请先采集或生成复盘')
        def work():
            from .llm import analyze
            result = analyze(config, state, question)
            record = {'text':result, 'generated_at':now_sh().isoformat(),'mode':state['mode'],
                      'provider':provider, 'label':config.get('label'), 'model':config.get('model'),
                      'scope':scope, 'review_date':review_date, 'review_id':captured_review_id}
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
                if boundary <= current <= boundary+timedelta(seconds=self.config['final_grace_seconds']) and not self.finalized and time.monotonic() >= max(next_poll,self.api_backoff_until):
                    self.collect_cycle('final')
                    next_poll = time.monotonic()+self.config['poll_seconds']
                if current > boundary+timedelta(seconds=self.config['final_grace_seconds']) and not self.finalized:
                    with self.lock:
                        self.message = f'竞价窗口已结束。已收集 {len(self.processed)}/{len(self.codes)} 只，终态 {len(self.final_codes)}/{len(self.codes)}；缺失数据未补造'
                if self.config['auto_review'] and current.strftime('%H:%M') >= self.config['review_time'] and self.last_review_attempt != today:
                    self.last_review_attempt = today
                    if not self.latest_review or self.latest_review.get('date') != today or self.latest_review.get('status') != 'ready':
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
