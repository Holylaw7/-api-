(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const numeric = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const num = (value, digits = 1) => numeric(value) ? Number(value).toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
  const percent = (value) => numeric(value) ? `${Number(value) > 0 ? '+' : ''}${num(value, 2)}%` : '—';
  const ratio = (value) => numeric(value) ? `${num(Number(value) * 100, 1)}%` : '—';
  const clamp = (value, min = 0, max = 100) => numeric(value) ? Math.min(max, Math.max(min, Number(value))) : 0;
  const amount = (value) => {
    if (!numeric(value)) return '—';
    const n = Number(value);
    if (Math.abs(n) >= 1e8) return `${num(n / 1e8, 2)}亿`;
    if (Math.abs(n) >= 1e4) return `${num(n / 1e4, 1)}万`;
    return num(n, 0);
  };
  const tone = (value) => !numeric(value) || Number(value) === 0 ? 'neutral' : Number(value) > 0 ? 'positive' : 'negative';
  const list = (value) => Array.isArray(value) ? value : Array.isArray(value?.rows) ? value.rows : Array.isArray(value?.items) ? value.items : [];
  const text = (id, value) => { if ($(id)) $(id).textContent = value ?? '—'; };
  const time = (value, full = false) => {
    if (!value) return '—';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', ...(full ? {month: '2-digit', day: '2-digit'} : {}), hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).format(d);
  };
  const dateAtShanghai = () => new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
  const state = {snapshot: null, selected: null, filter: 'all', search: '', page: 'auction', history: [], historyKey: '', historyLastFetch: 0, historyBusy: false, connected: false, configLoaded: false, reportSignature: '', reviewDateTouched: false, watchlistSettingsDirty: false, queryRequested: '', llmProvider: '', llmDirty: false, llmSignature: '', llmSwitching: false, reports: [], reportsBusy: false, reportsMode: '', reportsGeneration: 0, reportSaveBusy: false, reportLoadBusy: false, comparisonBusy: false, comparisonGeneration: 0, diagnosticsBusy: false, diagnosticsLoaded: false};
  Object.assign(state, {sectorSelection: [], sectorMatches: [], sectorSearchBusy: false, sectorFetchBusy: false, sectorCacheBusy: false, sectorEvidence: null, sectorSyncKey: '', sectorPreviewSignature: '', sectorSearchMessage: '', sectorError: ''});
  Object.assign(state, {finance: null, financeBusy: false, financeLoading: false, financeError: ''});
  Object.assign(state, {observationFollow: true, observationScrolling: false, observationSymbol: '', observationScrollTop: 0, observationPending: 0, observationPausedTotal: null});
  let pollTimer = null;
  let polling = false;
  const factors = {
    gap: {label: '开盘溢价', description: '按板别尺度归一化溢价；不直接判定涨停。', weight: .15},
    amount: {label: '竞价金额', description: '对数处理竞价金额，衡量承接与交易活跃度。', weight: .15},
    turnover: {label: '竞价换手', description: '观察竞价阶段参与程度，需有效换手字段。', weight: .10},
    volume_ratio: {label: '量比强度', description: '观察相对量能水平，缺失数据不视作零。', weight: .10},
    late_momentum: {label: '后段动量', description: '09:20 后逐批观察价格快照，重视后段确认。', weight: .20},
    retention: {label: '量能留存', description: '观察竞价金额留存；无法代替真实撤单率。', weight: .15},
    continuity: {label: '连板延续', description: '结合昨日连板高度，识别连续强势背景。', weight: .15},
  };
  const phases = {cancellable: '可撤单观察阶段', locked: '不可撤单确认阶段', firm: '不可撤单确认阶段', non_trading_day: '今日为非交易日', final: '竞价最终快照', awaiting_final: '等待最终竞价快照', waiting: '等待竞价时段', idle: '等待竞价时段', preparing: '正在准备股票池', closed: '本日竞价已结束', preopen: '等待竞价开始', collecting: '正在采集竞价', finalized: '竞价采集已完成'};
  const statuses = {idle: '待启动', preparing: '准备股票池', ready: '已就绪', running: '监测中', scheduled: '等待开盘', waiting: '等待开盘', stopped: '已停止', completed: '已完成', finished: '已完成', finalized: '已完成', final: '已完成', error: '运行异常', demo: '模拟演示', collecting: '实时采集中', not_configured: '请配置密钥'};
  const poolNames = {focus: '重点池：自选＋昨日涨停＋趋势强股', all: '全市场股票池', watchlist: '自选股池'};
  const sourceNames = {manual: '自选', previous_limit_up: '昨日涨停', strong_trend: '趋势强股'};
  const sourceTags = (sources) => list(sources).filter((source) => sourceNames[source]).map((source) => `<span class="source-tag ${esc(source)}">${esc(sourceNames[source])}</span>`).join('');
  const sameCode = (a, b) => Boolean(a && b) && String(a).toUpperCase().split('.')[0] === String(b).toUpperCase().split('.')[0];
  const isCollecting = (tracking) => Boolean(tracking) && ['cancellable', 'firm', 'locked'].includes(state.snapshot?.auction?.phase);
  function trackingLabel(tracking) {
    if (!tracking) return state.snapshot?.running === false ? '服务已停止' : '未加入当前采集池';
    if (isCollecting(tracking)) return '竞价采集中';
    if (['closed', 'final', 'finalized', 'awaiting_final'].includes(state.snapshot?.auction?.phase)) return '已入池，竞价窗口已结束';
    return '已入池，等待竞价窗口';
  }
  function parseStockCodes(value) {
    const codes = [...new Set(String(value || '').split(/[\s,，;；]+/).map((code) => code.trim().toUpperCase()).filter(Boolean))];
    if (!codes.length) throw new Error('请输入股票代码，例如 600519 或 600519.SH。');
    if (codes.length > 50) throw new Error('每批最多加入 50 只股票，请分批提交。');
    if (codes.some((code) => !/^\d{6}(?:\.(?:SH|SZ|BJ))?$/.test(code))) throw new Error('仅支持 6 位股票代码或代码加 .SH / .SZ / .BJ，不支持名称查询。');
    return codes;
  }

  function toast(message, isError = false) {
    const el = document.createElement('div');
    el.className = `toast${isError ? ' error' : ''}`;
    el.textContent = message;
    $('toast-container').append(el);
    setTimeout(() => el.remove(), isError ? 10000 : 5000);
  }

  function showPage(name) {
    if (!['auction', 'stocks', 'review', 'backtest', 'finance', 'settings'].includes(name)) return;
    state.page = name;
    document.querySelectorAll('.page').forEach((el) => el.classList.toggle('active', el.id === `page-${name}`));
    document.querySelectorAll('[data-tab]').forEach((el) => el.classList.toggle('active', el.dataset.tab === name));
    text('page-name', {auction: '实时竞价', stocks: '个股查询', review: '收盘复盘', backtest: '策略回测', finance: '同花顺接入', settings: '策略与接入'}[name]);
    history.replaceState(null, '', `#${name}`);
    if (name === 'auction') loadHistory();
    if (name === 'review' && state.snapshot && !state.reportsBusy) loadReports();
    if (name === 'backtest' && state.snapshot && !state.backtestBusy) loadBacktest();
    if (name === 'backtest' && state.snapshot && !state.dailyAuditListBusy) loadDailyAudits();
    if (name === 'finance') loadFinanceStatus();
  }

  async function api(path, body) {
    const options = {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-Local-App'] = 'auction-lab';
      options.body = JSON.stringify(body);
    }
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 20000);
    options.signal = controller.signal;
    try {
      const response = await fetch(path, options);
      let result;
      try { result = await response.json(); } catch { throw new Error(`本地服务返回了无法读取的响应（${response.status}）。`); }
      if (!response.ok || result.ok === false) throw new Error(result.message || result.error || `请求失败（${response.status}）`);
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('本地服务响应超时，请检查窗口中是否仍在运行。');
      throw error;
    } finally { clearTimeout(deadline); }
  }

  async function mutate(path, body, successMessage, trigger) {
    if (trigger?.disabled) return false;
    if (trigger) trigger.disabled = true;
    try {
      const result = await api(path, body);
      toast(result.message || successMessage || '已完成');
      await fetchState();
      return true;
    } catch (error) { toast(error.message, true); return false; }
    finally { if (trigger) trigger.disabled = Boolean(trigger.dataset.job && state.snapshot?.jobs?.[trigger.dataset.job]?.status === 'running'); }
  }

  function initializeWeights(weights) {
    const known = Object.keys(factors);
    const custom = Object.keys(weights || {}).filter((key) => !known.includes(key));
    $('weights-editor').innerHTML = [...known, ...custom].map((key) => {
      const f = factors[key] || {label: key, description: '自定义因子，详见策略文档。', weight: 0};
      const value = numeric(weights?.[key]) ? Number(weights[key]) * 100 : f.weight * 100;
      return `<div class="weight-control"><div class="weight-label"><span>${esc(f.label)}</span><span class="subtle">${esc(key)}</span></div><p>${esc(f.description)}</p><div class="weight-input"><input type="range" min="0" max="100" step="1" value="${clamp(value)}" data-weight-range="${esc(key)}" aria-label="${esc(f.label)}权重滑块"><input type="number" min="0" max="100" step="0.1" value="${num(value, 1).replace(/,/g, '')}" data-weight="${esc(key)}" aria-label="${esc(f.label)}权重百分比"><span>%</span></div></div>`;
    }).join('');
    updateWeightTotal();
  }

  function updateWeightTotal() {
    const total = [...document.querySelectorAll('[data-weight]')].reduce((sum, el) => sum + (Number(el.value) || 0), 0);
    text('weight-total', `合计 ${num(total, 1)}%`);
  }

  function renderReviewSchedule(snapshot) {
    const config = snapshot.config || {};
    if (!state.runtimeScheduleDirty) {
      if (document.activeElement !== $('auto-review')) $('auto-review').checked = config.auto_review === true;
      if (document.activeElement !== $('review-time')) $('review-time').value = config.review_time || '15:10';
    }
    const enabled = config.auto_review === true;
    text('daily-audit-automation', enabled ? `自动收盘复盘与竞价核验：已开启 · 上海时间 ${config.review_time || '15:10'}。${snapshot.running ? '需保持监测运行。' : '当前监测未启动，启动后才会自动执行。'}` : '自动收盘复盘与竞价核验：已关闭。可在「策略与接入 → 采集范围」开启，保存后启动监测。');
    $('daily-audit-automation').classList.toggle('warn', !enabled || !snapshot.running);
  }

  function financeConfigured() {
    return state.finance ? state.finance.configured === true : state.snapshot?.configured === true;
  }

  function renderFinance() {
    const finance = state.finance;
    const supported = Boolean(finance && finance.provider === 'hithink');
    const demo = state.snapshot?.mode === 'demo';
    $('finance-demo-notice').classList.toggle('hidden', !demo);
    $('finance-exit-demo').disabled = Boolean(state.financeBusy || !supported);
    const configured = financeConfigured();
    const test = supported ? finance.test || {} : {};
    const running = test.status === 'running' || state.snapshot?.jobs?.finance_test?.status === 'running';
    const success = configured && test.status === 'success' && test.ok === true;
    const failed = configured && test.status === 'error';
    const persisted = supported ? finance.persisted === true : state.snapshot?.credential_persisted === true;
    const inputChanged = Boolean($('api-key').value.trim());
    const pending = !state.snapshot && !finance;
    const status = pending ? '读取中' : !supported ? '需重启新版' : !configured ? '待配置' : running ? '验证中' : success ? '验证成功' : failed ? '验证失败' : persisted ? '已保存未验证' : '已配置未验证';
    const shortStatus = pending ? '读取中' : !supported ? '待升级' : !configured ? '待配置' : running ? '验证中' : success ? '已验证' : failed ? '验证失败' : '未验证';
    const statusClass = success ? 'green' : 'amber';
    text('credential-status', status);
    $('credential-status').className = `pill ${statusClass}`;
    text('finance-settings-status', status);
    $('finance-settings-status').className = `pill ${statusClass}`;
    text('finance-nav-status', shortStatus);
    $('finance-nav-status').className = `finance-nav-status ${success ? 'verified' : failed ? 'failed' : ''}`;
    $('setup-banner').classList.toggle('hidden', configured && persisted && supported);
    text('setup-title', !supported && !pending ? '重启新版服务，启用同花顺接入页' : configured ? '同花顺 Key 仅在当前进程中配置' : '接入你自己的同花顺金融 API');
    text('setup-description', !supported && !pending ? '当前后台尚未提供新版接入接口。请用最新代码重启本机服务；已有行情页面仍可使用。' : configured ? '当前进程可读取密钥，但本页尚未确认连接。打开接入页查看来源、保存至本机或手动测试。' : '申请并保存自己的金融 API Key，测试连接后即可读取行情。无需配置 AI 也能计算竞价与复盘。');
    text('setup-action', configured ? '查看同花顺接入 ↗' : '配置同花顺 API ↗');
    text('sidebar-credential', success ? '本机运行 · 同花顺验证成功' : configured ? '本机运行 · 同花顺 Key 已配置' : '本机运行 · 待接入同花顺');
    const sourceLabels = {none: '未配置', missing: '未配置', process: '当前服务进程环境', process_env: '当前服务进程环境', user_environment: 'Windows 用户环境变量', windows_user_environment: 'Windows 用户环境变量', windows_user_env: 'Windows 用户环境变量', user_file: '本机用户凭据文件', session_saved: '本次会话保存'};
    text('finance-source', supported ? sourceLabels[finance.credential_source] || '本机凭据来源' : '等待新版服务状态');
    text('finance-checked-at', test.checked_at ? time(test.checked_at, true) : '尚未验证');
    text('finance-latency', numeric(test.latency_ms) ? `${num(test.latency_ms, 0)} 毫秒` : '—');
    text('finance-calendar', numeric(test.calendar_count) ? `${num(test.calendar_count, 0)} 个交易日 / ${test.latest_trade_date || '—'}` : '尚无验证数据');
    const title = pending ? '等待接入状态' : !supported ? '请重启至 1.7 或更新版本' : !configured ? '尚未配置同花顺 Key' : running ? '正在验证同花顺连接' : success ? '本次连接验证成功' : failed ? '本次连接验证失败' : 'Key 已配置，尚未验证连接';
    text('finance-test-title', title);
    const message = pending ? '正在读取本机配置，不会自动请求金融接口。' : !supported ? '当前后台缺少专用接入接口，不能在本页保存或验证。请使用最新代码重启服务。' : !configured ? '先申请并保存你自己的金融 API Key，再点击测试连接。' : running ? test.message || '正在请求官方交易日历，完成后自动显示结果。' : test.message || (success ? '官方认证与交易日历读取成功。' : failed ? '请检查密钥、网络或接口权限，再手动重试。' : '保存配置与连接测试是独立步骤。点击「测试同花顺连接」验证已配置的 Key。');
    text('finance-test-message', state.financeError || message);
    $('finance-test-state').className = `finance-test-state ${success ? 'success' : failed || state.financeError ? 'error' : running ? 'running' : ''}`;
    text('credential-help', '密钥保存在本机用户凭据目录，不写入项目或浏览器存储，也不回显。保存成功后清空输入框；保存不会自动启动监测。');
    $('api-key').placeholder = configured ? '已配置；如需更换，在此输入新的同花顺 Key' : '粘贴你申请的同花顺金融 API Key';
    $('api-key').disabled = state.financeBusy || running;
    const saveReason = !supported ? '请先重启新版服务。' : demo ? '请先点击「退出演示，配置同花顺」，再保存自己的 Key。' : !finance.can_save ? finance.save_block_reason || '当前不能修改密钥，请停止监测并等待任务结束。' : '';
    const testReason = !supported ? '请先重启新版服务。' : demo ? '请先退出演示，再测试同花顺连接。' : inputChanged ? '输入框已有修改，请先点击「保存同花顺 Key」，再测试已保存的密钥。' : !finance.can_test ? finance.test_block_reason || '当前无法测试，请先配置密钥并等待任务结束。' : '';
    $('finance-save').disabled = Boolean(state.financeBusy || running || saveReason || !inputChanged);
    $('finance-save').title = saveReason || '只保存到本机，不启动行情采集。';
    $('finance-test').disabled = Boolean(state.financeBusy || running || testReason);
    $('finance-test').title = testReason || '使用当前已配置的 Key，只读取一次官方交易日历。';
    text('finance-test', running ? '正在验证连接…' : '测试同花顺连接');
    $('finance-refresh').disabled = state.financeLoading;
    text('finance-input-status', state.financeError || (state.financeBusy ? '正在提交，请稍候…' : pending ? '正在读取本机接入状态…' : inputChanged ? `${saveReason ? `${saveReason} ` : ''}输入已修改，先保存后测试。` : saveReason || testReason || (configured ? '测试使用已配置的 Key；不会提交空输入或调用 AI。' : '先保存你自己的同花顺 Key，再进行连接测试。')));
    $('finance-input-status').classList.toggle('warn', Boolean(state.financeError || saveReason || inputChanged));
    $('sector-finance-setup').classList.toggle('hidden', configured);
  }

  async function loadFinanceStatus(manual = false) {
    if (state.financeLoading) return;
    state.financeLoading = true;
    renderFinance();
    try {
      const finance = await api('/api/finance/status');
      if (finance.provider !== 'hithink') throw new Error('当前后台未提供新版同花顺接入状态，请重启最新版本服务。');
      state.finance = finance;
      if (state.snapshot) state.snapshot.finance = finance;
      state.financeError = '';
    } catch (error) {
      state.financeError = error.message;
      if (manual) toast(error.message, true);
    } finally { state.financeLoading = false; renderFinance(); renderSectorControls(); }
  }

  async function saveFinance(event) {
    event.preventDefault();
    if ($('finance-save').disabled) return;
    const apiKey = $('api-key').value.trim();
    if (!apiKey) return;
    state.financeBusy = true; state.financeError = ''; renderFinance();
    try {
      const result = await api('/api/finance/config', {api_key: apiKey});
      $('api-key').value = '';
      state.finance = result.finance;
      if (state.snapshot) state.snapshot.finance = result.finance;
      toast(result.message || '同花顺 Key 已保存。可继续测试连接。');
      await fetchState();
    } catch (error) { state.financeError = error.message; toast(error.message, true); }
    finally { state.financeBusy = false; renderFinance(); }
  }

  async function testFinance() {
    if ($('api-key').value.trim()) { toast('输入已修改，请先保存同花顺 Key，再测试连接。', true); return; }
    if ($('finance-test').disabled) return;
    state.financeBusy = true; state.financeError = ''; renderFinance();
    try {
      const result = await api('/api/finance/test', {});
      if (result.finance) {
        state.finance = result.finance;
        if (state.snapshot) state.snapshot.finance = result.finance;
      }
      toast(result.message || '已提交同花顺连接测试。');
      await fetchState();
    } catch (error) { state.financeError = error.message; toast(error.message, true); }
    finally { state.financeBusy = false; renderFinance(); }
  }

  async function exitFinanceDemo() {
    if ($('finance-exit-demo').disabled) return;
    state.financeBusy = true; state.financeError = ''; renderFinance();
    try {
      const result = await api('/api/demo/exit', {});
      toast(result.message || '已退出演示。现在可以配置同花顺 Key，监测尚未启动。');
      await fetchState();
    } catch (error) { state.financeError = error.message; toast(error.message, true); }
    finally { state.financeBusy = false; renderFinance(); }
  }

  function hydrateConfig(snapshot) {
    renderReviewSchedule(snapshot);
    if (state.configLoaded) return;
    const config = snapshot.config || {};
    $('universe').value = config.universe || 'focus';
    $('poll-seconds').value = config.poll_seconds || 3;
    $('watchlist').value = (config.watchlist || []).join(', ');
    initializeWeights(config.weights);
    state.configLoaded = true;
  }

  function render(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    state.snapshot = snapshot;
    state.snapshotReceivedAt = Date.now();
    state.apiCooldownDeadline = Date.now() + (numeric(snapshot.api?.cooldown_seconds) ? Math.max(0, Number(snapshot.api.cooldown_seconds)) * 1000 : 0);
    hydrateConfig(snapshot);
    if (!state.watchlistSettingsDirty && document.activeElement !== $('watchlist')) $('watchlist').value = (snapshot.config?.watchlist || []).join(', ');
    const demo = snapshot.mode === 'demo';
    document.body.dataset.mode = demo ? 'demo' : 'live';
    $('demo-banner').classList.toggle('hidden', !demo);
    state.finance = snapshot.finance || null;
    renderFinance();
    text('mode-badge', demo ? 'DEMO' : 'LIVE');
    document.querySelector('.nav-tag').textContent = demo ? 'DEMO' : 'LIVE';
    $('mode-badge').classList.toggle('demo', demo);
    renderLLMConfig(snapshot.llm || {}, snapshot.jobs || {});
    text('monitor-status', demo ? '模拟演示' : statuses[snapshot.status] || snapshot.status || '等待启动');
    text('monitor-message', snapshot.message || '等待开始采集');
    const auction = snapshot.auction || {};
    const rows = list(auction.rows);
    const validCount = rows.filter((row) => numeric(row.score)).length;
    const universeCount = auction.universe_count;
    $('coverage-value').innerHTML = `${esc(num(validCount, 0))}<small> / ${esc(num(universeCount, 0))}</small>`;
    const coverage = numeric(universeCount) && Number(universeCount) > 0 ? validCount / Number(universeCount) : null;
    const pctCoverage = numeric(coverage) ? (coverage <= 1 ? coverage * 100 : coverage) : 0;
    $('coverage-bar').style.width = `${clamp(pctCoverage)}%`;
    text('coverage-note', numeric(universeCount) ? `已处理 ${num(auction.processed_count, 0)} 只 · 有效覆盖 ${num(pctCoverage, 1)}%` : '等待准备股票池');
    $('cycle-value').innerHTML = `${esc(num(auction.cycle_seconds, 1))}<small> 秒</small>`;
    const sourceCounts = auction.source_counts || {};
    const sourceCountText = Object.keys(sourceNames).filter((key) => numeric(sourceCounts[key])).map((key) => `${sourceNames[key]} ${num(sourceCounts[key], 0)}`).join(' / ');
    text('pool-description', `${poolNames[snapshot.config?.universe] || '重点股票池'}${sourceCountText ? ` · ${sourceCountText}（来源可重叠）` : ''}`);
    const phase = auction.phase || auction.summary?.phase;
    text('phase-label', demo ? '演示模式 · 模拟竞价路径' : auction.finalized ? '竞价最终排名已保留' : phase === 'non_trading_day' && !snapshot.calendar?.checked_on ? '等待交易日历校验' : phases[phase] || phase || '等待竞价时段');
    text('phase-note', auction.finalized ? `最终观测 ${num(auction.final_count, 0)} 只 · ${auction.date || auction.summary?.session_date || ''}` : '交易日 09:15 开始，09:25 进入收尾校验');
    const now = new Date(snapshot.now || Date.now());
    const parts = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).formatToParts(now);
    const part = (key) => Number(parts.find((p) => p.type === key)?.value || 0);
    const seconds = part('hour') * 3600 + part('minute') * 60 + part('second');
    const demoStamp = new Date(auction.summary?.last_received_at || snapshot.now || Date.now());
    const demoParts = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).formatToParts(demoStamp);
    const demoPart = (key) => Number(demoParts.find((p) => p.type === key)?.value || 0);
    const demoSeconds = demoPart('hour') * 3600 + demoPart('minute') * 60 + demoPart('second');
    const progress = phase === 'non_trading_day' ? 0 : clamp(((demo ? demoSeconds : seconds) - 33300) / 6);
    $('timeline-progress').style.width = `${progress}%`;
    document.querySelectorAll('.timeline-point').forEach((el, index) => el.classList.toggle('reached', (demo || phase !== 'non_trading_day') && progress >= index * 50 && (progress > 0 || seconds >= 33300)));
    renderAuction();
    renderStocks(snapshot.stocks || {}, snapshot.jobs || {});
    renderReview(snapshot.review, snapshot.jobs?.review);
    renderResearch(snapshot);
    syncBacktest(snapshot);
    renderReportControls();
    const reviewJobStatus = snapshot.jobs?.review?.status;
    if (state.reviewJobStatus === 'running' && reviewJobStatus === 'done') {
      if (state.reportsBusy) state.reportsRefreshPending = true;
      else loadReports();
    }
    state.reviewJobStatus = reviewJobStatus;
    if (state.reportsMode !== snapshot.mode) loadReports();
    if (!state.diagnosticsLoaded) loadDiagnostics();
    renderLLM(snapshot.llm || {}, snapshot.jobs?.llm);
    syncSectorResearch();
    renderErrors(snapshot);
    if (!state.reviewDateTouched) {
      const dates = list(snapshot.calendar?.dates || snapshot.calendar).filter((day) => day < dateAtShanghai() || (day === dateAtShanghai() && seconds >= 54000));
      if (snapshot.review?.date) $('review-date').value = snapshot.review.date;
      else if (dates.length) $('review-date').value = dates[dates.length - 1];
    }
    tick();
    loadHistory();
  }

  function qualityOf(row) {
    const quality = row.quality || {};
    const flags = Array.isArray(quality) ? quality : list(quality.flags);
    const status = quality.status || (flags.length ? 'partial' : 'ok');
    const labels = {ok: '有效', partial: '部分缺失', not_ready: '未就绪', stale: '数据陈旧', provisional: '待终态核验', invalid: '不可用'};
    const title = [numeric(quality.factor_coverage) ? `因子覆盖 ${ratio(quality.factor_coverage)}` : '', ...flags].filter(Boolean).join('；');
    return {status, label: labels[status] || status, title, flags, className: status === 'ok' ? '' : status === 'stale' || status === 'invalid' ? 'bad' : 'warn'};
  }

  function renderAuction() {
    const auction = state.snapshot?.auction || {};
    const allRows = list(auction.rows);
    const rows = allRows.filter((row) => {
      if (state.filter === 'consecutive' && Number(row.continue_day_cnt ?? row.consecutive_days ?? 0) < 2) return false;
      if (state.filter === 'strong' && (!numeric(row.score) || Number(row.score) < 70)) return false;
      return !state.search || `${row.name || ''} ${row.thscode || ''}`.toLowerCase().includes(state.search);
    });
    text('ranking-count', rows.length);
    text('ranking-limit-note', rows.length > 500 ? '显示前 500 条，可搜索股票查看其他记录' : '点击股票查看因子与观测轨迹');
    const latest = auction.summary?.last_received_at || allRows.reduce((last, row) => row.updated_at && (!last || row.updated_at > last) ? row.updated_at : last, '');
    text('ranking-updated', latest ? `采集 ${time(latest)}` : '尚无观测');
    if (!rows.length) {
      $('auction-body').innerHTML = `<tr><td colspan="7"><div class="empty-state"><span class="empty-icon">◷</span><strong>${allRows.length ? '没有符合筛选条件的股票' : '等待第一批竞价数据'}</strong><p>${allRows.length ? '尝试其他筛选或清空名称 / 代码。' : '保存密钥 → 准备关注池 → 启动监测<br>非交易时段可运行演示，检查完整分析流程。'}</p>${allRows.length ? '' : '<button class="button small" data-action="demo">运行模拟演示</button>'}</div></td></tr>`;
    } else {
      $('auction-body').innerHTML = rows.slice(0, 500).map((row, index) => {
        const q = qualityOf(row);
        const rank = numeric(row.score) ? row.rank ?? index + 1 : '—';
        const days = row.continue_day_cnt ?? row.consecutive_days;
        return `<tr data-symbol="${esc(row.thscode)}" tabindex="0" aria-label="查看 ${esc(row.name || row.thscode)}" class="${state.selected === row.thscode ? 'selected' : ''}"><td><span class="rank ${rank <= 3 ? 'top' : ''}">${esc(rank)}</span></td><td><span class="stock-name">${esc(row.name || row.thscode)}</span><span class="stock-code">${esc(row.thscode)}</span><span class="source-tags">${sourceTags(row.sources)}</span></td><td class="score-cell"><span class="score-number">${esc(num(row.score, 1))}</span><span class="score-bar"><i style="width:${clamp(row.score)}%"></i></span></td><td class="${tone(row.auction_pct)}">${esc(percent(row.auction_pct))}</td><td>${esc(amount(row.auction_amount))}</td><td>${numeric(days) && Number(days) > 0 ? `<span class="ladder-badge">${esc(num(days, 0))} 板</span>` : '—'}</td><td><span class="quality-pill ${q.className}" title="${esc(q.title)}">${esc(q.label)}</span></td></tr>`;
      }).join('');
    }
    if (state.selected && !allRows.some((row) => row.thscode === state.selected)) {
      state.selected = null;
      state.history = [];
    }
    renderDetail();
  }

  function selectStock(symbol) {
    if (symbol !== state.selected) {
      state.history = [];
      state.historyKey = '';
    }
    state.selected = symbol;
    renderAuction();
    loadHistory(true);
  }

  async function loadHistory(force = false) {
    if (!state.selected || state.historyBusy || state.page !== 'auction') return;
    const snapshot = state.snapshot || {};
    const key = `${snapshot.mode}:${snapshot.auction?.date || snapshot.auction?.summary?.session_date || ''}:${state.selected}`;
    if (!force && key === state.historyKey && Date.now() - state.historyLastFetch < 5000) return;
    state.historyBusy = true;
    const selected = state.selected;
    try {
      const result = await api(`/api/history?symbol=${encodeURIComponent(selected)}`);
      if (state.selected === selected && state.snapshot?.mode === snapshot.mode) {
        state.history = list(result);
        state.historyKey = key;
        state.historyLastFetch = Date.now();
        renderDetail();
      }
    } catch (error) {
      state.historyLastFetch = Date.now();
      state.historyKey = key;
      if (force) toast(`观测轨迹读取失败：${error.message}`, true);
    } finally { state.historyBusy = false; }
  }

  function sparkline(historyItems) {
    const points = historyItems.filter((item) => numeric(item.auction_pct)).slice(-50);
    if (points.length < 2) return '<p class="subtle">累计两次有效价格观测后绘制轨迹。</p>';
    const values = points.map((item) => Number(item.auction_pct));
    const low = Math.min(...values), high = Math.max(...values), spread = Math.max(high - low, .05);
    const coords = values.map((value, i) => `${(i / (values.length - 1) * 244 + 3).toFixed(2)},${(59 - (value - low) / spread * 47).toFixed(2)}`);
    const poly = coords.join(' ');
    return `<svg class="sparkline" viewBox="0 0 250 72" role="img" aria-label="竞价涨幅观测轨迹，范围 ${esc(num(low, 2))}% 到 ${esc(num(high, 2))}%"><line x1="0" y1="60" x2="250" y2="60" stroke="#2d3c51" stroke-dasharray="3 4"/><polygon points="3,65 ${poly} 247,65" fill="#d8b57410"/><polyline points="${poly}" fill="none" stroke="#d8b574" stroke-width="1.8" stroke-linejoin="round"/><circle cx="247" cy="${coords[coords.length - 1].split(',')[1]}" r="3" fill="#d8b574"/></svg><div class="chart-labels"><span>${esc(time(points[0].received_at || points[0].updated_at))}</span><span>${esc(num(low, 2))} ~ ${esc(num(high, 2))}%</span><span>${esc(time(points[points.length - 1].received_at || points[points.length - 1].updated_at))}</span></div>`;
  }

  const OBSERVATION_WINDOW = 60;

  function observationWindow(items) {
    const shown = items.slice(-OBSERVATION_WINDOW);
    const rows = shown.map((item, index) => {
      const stage = item.stage === 'final' ? 'final' : 'live';
      const latest = index === shown.length - 1 ? ' latest' : '';
      return `<div class="observation-row${latest}"><span>${esc(time(item.received_at || item.updated_at))}</span><span class="${tone(item.auction_pct)}">${esc(percent(item.auction_pct))}</span><span class="obs-amount">${esc(amount(item.auction_amount))}</span><span class="obs-stage ${stage === 'final' ? 'stage-final' : ''}">${stage === 'final' ? '终态' : '实时'}</span></div>`;
    }).join('');
    const note = items.length > shown.length ? `共 ${items.length} 次 · 显示最近 ${shown.length} 次` : `共 ${shown.length} 次观测`;
    return `<div class="observation-window-head"><span class="subtle">按本地接收顺序 · ${note}</span><button type="button" class="text-button" id="observation-follow" data-observation-follow aria-pressed="true">跟随最新 ●</button></div><div class="observation-window" id="observation-window" role="log" aria-live="off" data-total="${items.length}" tabindex="0" aria-label="个股竞价观测流水，默认跟随最新记录">${rows || '<p class="subtle">尚未取得本机观测记录。</p>'}</div><p class="observation-window-note">新批次到达时自动滑到最新；滑轮滚动不会打断跟随；需要停留查看历史时点「跟随最新」暂停，暂停期间按钮会显示未跟随的新观测条数。时间为本地接收时间，不是交易所行情时间。</p>`;
  }

  function updateObservationFollowButton() {
    const button = $('observation-follow');
    if (!button) return;
    const pending = Math.max(0, Number(state.observationPending || 0));
    button.textContent = state.observationFollow ? '跟随最新 ●'
      : pending > 0 ? `${pending} 条新观测 · 回到最新` : '已暂停 · 点此跟随';
    button.setAttribute('aria-pressed', state.observationFollow ? 'true' : 'false');
    button.classList.toggle('paused', !state.observationFollow);
  }

  function observationTotal() {
    const box = $('observation-window');
    const total = box ? Number(box.dataset.total) : NaN;
    return Number.isFinite(total) ? total : null;
  }

  function setObservationFollow(following, scroll = true) {
    state.observationFollow = following;
    state.observationPausedTotal = observationTotal();
    state.observationPending = 0;
    updateObservationFollowButton();
    const box = $('observation-window');
    if (!box || !following) return;
    state.observationScrollTop = box.scrollHeight;
    if (!scroll) return;
    state.observationScrolling = true;
    try { box.scrollTo({top: box.scrollHeight, behavior: 'smooth'}); } catch (error) { box.scrollTop = box.scrollHeight; }
    window.setTimeout(() => { state.observationScrolling = false; }, 450);
  }

  function mountObservationWindow() {
    const box = $('observation-window');
    if (!box) return;
    const total = Number(box.dataset.total || 0);
    if (state.observationSymbol !== state.selected) {
      state.observationSymbol = state.selected;
      state.observationScrollTop = 0;
      state.observationFollow = true;
      state.observationPausedTotal = total;
      state.observationPending = 0;
    }
    if (state.observationFollow) {
      // 跟随开启时，每次重绘都滑到最新一条；滑轮滚动不会关闭跟随。
      state.observationScrolling = true;
      box.scrollTop = box.scrollHeight;
      try { box.scrollTo({top: box.scrollHeight, behavior: 'smooth'}); } catch (error) { /* 旧内核已直接置底 */ }
      state.observationScrollTop = box.scrollHeight;
      window.setTimeout(() => { state.observationScrolling = false; }, 450);
    } else {
      state.observationPending = Number.isFinite(state.observationPausedTotal)
        ? Math.max(0, total - state.observationPausedTotal) : 0;
      box.scrollTop = Math.max(0, Math.min(state.observationScrollTop, box.scrollHeight - box.clientHeight));
    }
    updateObservationFollowButton();
    box.addEventListener('scroll', () => {
      state.observationScrollTop = box.scrollHeight - box.scrollTop - box.clientHeight <= 2 ? box.scrollHeight : box.scrollTop;
    }, {passive: true});
  }

  function renderDetail() {
    const row = list(state.snapshot?.auction?.rows).find((item) => item.thscode === state.selected);
    if (!row) {
      $('stock-detail').innerHTML = '<div class="empty-state detail-empty"><span class="empty-icon">⌁</span><strong>选择一只股票</strong><p>查看实时因子贡献、数据质量<br>和已采集的竞价变化。</p></div>';
      return;
    }
    const q = qualityOf(row);
    const factorEntries = Object.entries(row.factors || {});
    const factorHTML = factorEntries.map(([key, value]) => {
      const f = value && typeof value === 'object' ? value : {score: value};
      const label = f.label || factors[key]?.label || key;
      const missing = f.available === false || !numeric(f.score);
      const tooltip = `分数 ${num(f.score, 1)} / 权重 ${ratio(f.weight)} / 贡献 ${num(f.contribution, 1)}${f.value === null || f.value === undefined ? '' : ` / 原值 ${f.value}`}`;
      return `<div class="factor-row${missing ? ' missing' : ''}" title="${esc(tooltip)}"><span>${esc(label)}</span><div class="factor-track"><i style="width:${clamp(f.score)}%"></i></div><strong>${missing ? '缺失' : esc(num(f.score, 0))}</strong></div>`;
    }).join('');
    const items = state.history.length ? state.history : list(row.history);
    const normalizedItems = items.map((item) => ({...item, auction_pct: item.auction_pct ?? item.price_change_ratio_pct ?? item.change_ratio, auction_amount: item.auction_amount ?? item.amount}));
    const qualityNotes = [...q.flags];
    if (numeric(row.quality?.window_coverage)) qualityNotes.push(`十分钟窗口观测覆盖 ${ratio(row.quality.window_coverage)}，首次接收 ${time(row.quality.first_observed_at)}。`);
    if (row.quality?.upstream_freshness === 'unknown' && !qualityNotes.some((note) => String(note).includes('上游实时延迟未知'))) qualityNotes.push('上游未提供可靠行情时间，页面时间为本地接收时间。');
    $('stock-detail').innerHTML = `<div class="detail-top"><div><strong class="detail-stock-name">${esc(row.name || row.thscode)}</strong><span class="detail-stock-code">${esc(row.thscode)}</span></div><div class="detail-score">${esc(num(row.score, 1))}<small>竞价综合评分 / 100</small></div></div><div class="detail-facts"><div><span>竞价涨幅</span><strong class="${tone(row.auction_pct)}">${esc(percent(row.auction_pct))}</strong></div><div><span>竞价金额</span><strong>${esc(amount(row.auction_amount))}</strong></div><div><span>有效观测次数</span><strong>${esc(num(row.quality?.observation_count ?? normalizedItems.length, 0))}</strong></div><div><span>因子覆盖</span><strong>${esc(ratio(row.quality?.factor_coverage))}</strong></div></div><div class="detail-section"><h3>因子拆解<span>悬停查看权重贡献</span></h3>${factorHTML || '<p class="subtle">暂无有效因子。</p>'}</div><div class="detail-section"><h3>竞价涨幅轨迹<span>接收顺序</span></h3>${sparkline(normalizedItems)}${observationWindow(normalizedItems)}</div><div class="detail-section"><h3>数据口径<span class="quality-pill ${q.className}">${esc(q.label)}</span></h3><div class="quality-note">${qualityNotes.length ? qualityNotes.map(esc).join('<br>') : '有效字段已参与计算；评分只反映采集范围内的竞价表现。'}<br>最近接收：${esc(time(row.updated_at))}</div></div>`;
    mountObservationWindow();
  }

  function factorBars(entries, labels = {}) {
    return Object.entries(entries || {}).map(([key, value]) => {
      const f = value && typeof value === 'object' ? value : {score: value};
      const missing = f.available === false || !numeric(f.score);
      const label = f.label || labels[key] || factors[key]?.label || key;
      const detail = `${f.formula || ''}${f.formula ? '；' : ''}原值 ${numeric(f.value) ? num(f.value, 3) : '—'}；权重 ${ratio(f.weight)}；贡献 ${num(f.contribution, 2)}`;
      return `<div class="factor-row${missing ? ' missing' : ''}" title="${esc(detail)}"><span>${esc(label)}</span><div class="factor-track"><i style="width:${clamp(f.score)}%"></i></div><strong>${missing ? '缺失' : esc(num(f.score, 0))}</strong></div>`;
    }).join('');
  }

  function dailySparkline(historyItems) {
    const points = list(historyItems).filter((item) => numeric(item.close_price) && Number(item.close_price) > 0).slice(-40);
    if (points.length < 2) return '<p class="subtle">可用日线不足，尚不能绘制价格轨迹。</p>';
    const values = points.map((item) => Number(item.close_price));
    const low = Math.min(...values), high = Math.max(...values), spread = Math.max(high - low, .01);
    const coords = values.map((value, index) => `${(index / (values.length - 1) * 244 + 3).toFixed(2)},${(62 - (value - low) / spread * 52).toFixed(2)}`);
    return `<svg class="sparkline" viewBox="0 0 250 72" role="img" aria-label="前复权收盘价轨迹，${esc(num(low, 2))} 到 ${esc(num(high, 2))}"><line x1="0" y1="65" x2="250" y2="65" stroke="#2d3c51" stroke-dasharray="3 4"/><polygon points="3,66 ${coords.join(' ')} 247,66" fill="#d8b57410"/><polyline points="${coords.join(' ')}" fill="none" stroke="#d8b574" stroke-width="1.8" stroke-linejoin="round"/></svg><div class="chart-labels"><span>${esc(points[0].date || '起始日')}</span><span>${esc(num(low, 2))} ~ ${esc(num(high, 2))}</span><span>${esc(points[points.length - 1].date || '末日')}</span></div>`;
  }

  function renderStocks(stocks, jobs) {
    const job = jobs.stock || {};
    const analysis = stocks.analysis;
    const requested = stocks.query_code || state.queryRequested || analysis?.thscode;
    let status = '输入股票代码开始查询。没有已采集行情时会明确显示为空。';
    if (job.status === 'running') status = `${requested || ''} · ${job.message || '正在查询分析，任务在后台继续；完成后自动更新。'}`;
    else if (job.status === 'error') status = `个股查询失败：${job.message || '请检查代码与行情服务。'}`;
    else if (analysis?.status === 'deferred') status = `${analysis.thscode || requested || ''} · 当前优先保障竞价采集，历史分析已延后；下方可查看已有本机竞价记录。`;
    else if (analysis) status = `${analysis.thscode || ''} · ${analysis.status === 'partial' ? '部分数据需核验' : analysis.status === 'unavailable' ? '部分数据不可用' : '查询结果已更新'}${analysis.generated_at ? ` · ${time(analysis.generated_at, true)}` : ''}`;
    text('stock-job-status', status);
    $('stock-job-status').classList.toggle('warn', job.status === 'error' || ['partial', 'deferred', 'unavailable'].includes(analysis?.status));
    $('stock-job-status').classList.toggle('stock-query-progress', job.status === 'running');
    const hideOld = analysis && job.status === 'running' && requested && !sameCode(requested, analysis.thscode);
    if (!analysis || hideOld) {
      text('stock-analysis-date', job.status === 'running' ? '查询进行中' : '尚未查询');
      $('single-stock-trend').innerHTML = `<div class="empty-state"><span class="empty-icon">⌁</span><strong>${job.status === 'running' ? '正在读取个股数据' : '一只股票，一份独立研究'}</strong><p>${job.status === 'running' ? '较长查询在后台运行，结果会自动显示。' : '输入代码后，查看均线、动量、量能和回撤。<br>日线强弱分与实时竞价评分分别展示。'}</p></div>`;
      renderSingleAuction(null);
    } else {
      renderSingleTrend(analysis);
      renderSingleAuction(analysis);
    }
    const watchlist = Array.isArray(stocks.watchlist) ? stocks.watchlist : (state.snapshot?.config?.watchlist || []).map((code) => ({thscode: code, sources: ['manual'], tracking: false}));
    text('watchlist-count', watchlist.length);
    const watchJob = jobs.watchlist || {};
    text('watchlist-job-status', watchJob.status === 'running' ? watchJob.message || '正在核验并加入股票，请稍候…' : watchJob.status === 'error' ? `加入失败：${watchJob.message}` : '逗号、空格或换行分隔。09:10—09:26 每次仅加入 1 只。');
    $('watchlist-items').innerHTML = watchlist.length ? watchlist.map((item) => {
      const row = typeof item === 'string' ? {thscode: item, sources: ['manual']} : item;
      return `<div class="watchlist-item"><div><button class="watchlist-stock-link" data-query-stock="${esc(row.thscode)}"><span class="stock-name">${esc(row.name || row.thscode)}</span><span class="stock-code">${esc(row.thscode)}</span></button><div class="source-tags">${sourceTags(row.sources?.length ? row.sources : ['manual'])}</div><span class="tracking-label${isCollecting(row.tracking) ? '' : ' pending'}">${esc(trackingLabel(row.tracking))}</span></div><div class="watchlist-item-actions"><button class="watchlist-remove" data-remove-stock="${esc(row.thscode)}" aria-label="移除自选 ${esc(row.name || row.thscode)}" ${watchJob.status === 'running' ? 'disabled' : ''}>移除</button></div></div>`;
    }).join('') : '<div class="table-empty">还没有自选股。输入代码即可加入关注。</div>';
    renderTrendPool(stocks.trend_pool, jobs.trends);
    document.querySelectorAll('[data-job]').forEach((button) => { button.disabled = jobs[button.dataset.job]?.status === 'running'; });
  }

  function renderSingleTrend(analysis) {
    const trend = analysis.trend || {};
    const sourceLabel = typeof analysis.source === 'string' ? analysis.source : typeof analysis.source?.provider === 'string' ? analysis.source.provider : '行情数据源';
    const methodDescription = typeof analysis.score_method === 'string' ? analysis.score_method : typeof analysis.score_method?.description === 'string' ? analysis.score_method.description : '固定研究权重；悬停查看各项原值、权重和贡献。';
    const warningList = [...new Set([...list(analysis.warnings), ...list(trend.warnings)])];
    const deferred = analysis.status === 'deferred';
    text('stock-analysis-date', analysis.date ? `截止 ${analysis.date}` : deferred ? '历史分析已延后' : '日期未提供');
    const positions = [[5, trend.above_ma5], [10, trend.above_ma10], [20, trend.above_ma20]].filter(([, value]) => value !== null && value !== undefined).map(([days, value]) => `${value ? '上' : '下'} MA${days}`);
    const factorHTML = factorBars(analysis.trend_factors || trend.score_factors, {momentum_5d: '五日动量', ma_position: '均线位置', drawdown_control: '回撤控制', volume_confirmation: '量能确认'});
    $('single-stock-trend').innerHTML = `<div class="stock-trend-summary"><div><h3>${esc(analysis.name || analysis.thscode)}</h3><span class="stock-code">${esc(analysis.thscode)}</span><div class="stock-analysis-info">${esc(sourceLabel)}${trend.as_of ? ` · K 线截至 ${esc(trend.as_of)}` : ''}${analysis.generated_at ? ` · 生成 ${esc(time(analysis.generated_at))}` : ''}</div></div><div class="detail-score">${esc(num(analysis.trend_score, 1))}<small>日线强弱分 / 100</small></div></div>${deferred ? '<div class="stock-warning-list">竞价保护时段内，日线请求已延后。此处空值表示尚未完成采集，不表示趋势为零。</div>' : ''}<div class="stock-mini-metrics"><div class="stock-mini-metric"><span>前复权收盘价</span><strong>${esc(num(trend.close, 2))}</strong><small>不等同未复权报价</small></div><div class="stock-mini-metric"><span>近 5 日涨幅</span><strong class="${tone(trend.return_5d_pct)}">${esc(percent(trend.return_5d_pct))}</strong><small>${esc(num(trend.bar_count, 0))} 根有效日线</small></div><div class="stock-mini-metric"><span>相对 5 日量能</span><strong>${esc(num(trend.volume_ratio_5d, 2))}${numeric(trend.volume_ratio_5d) ? ' 倍' : ''}</strong><small>结合价格方向观察</small></div><div class="stock-mini-metric"><span>20 日最大回撤</span><strong>${numeric(trend.max_drawdown_20d_pct) ? `${esc(num(trend.max_drawdown_20d_pct, 2))}%` : '—'}</strong><small>仅历史窗口表现</small></div></div><div class="single-trend-body"><div class="single-trend-chart"><h3>前复权收盘轨迹</h3>${dailySparkline(analysis.history)}<p class="stock-analysis-info">MA5 ${esc(num(trend.ma5, 2))} · MA10 ${esc(num(trend.ma10, 2))} · MA20 ${esc(num(trend.ma20, 2))}<br>${esc(positions.join(' / ') || '均线历史不足')}</p></div><div class="single-trend-factors"><h3>日线强弱因子 <span class="subtle">覆盖 ${esc(ratio(analysis.trend_coverage))}</span></h3>${factorHTML || '<p class="subtle">暂无可计算的趋势因子。</p>'}<p class="field-help">${esc(methodDescription)}</p></div></div>${warningList.length ? `<div class="stock-warning-list">${warningList.map(esc).join('<br>')}</div>` : ''}`;
  }

  function renderSingleAuction(analysis) {
    const auction = analysis?.auction;
    const row = auction?.row;
    const demo = auction?.mode === 'demo';
    text('single-auction-mode', auction ? demo ? 'DEMO · 模拟观测' : 'LIVE · 本机记录' : '尚无记录');
    $('single-auction-mode').className = `pill ${demo ? 'amber' : ''}`;
    text('single-auction-context', auction ? `${auction.date || '日期未标注'} · ${trackingLabel(auction.tracking)} · 排名范围 ${num(auction.scope_count, 0)} 只` : '仅显示实际采集记录；加入关注不会补齐过去十分钟。');
    if (!row) {
      $('single-stock-auction').innerHTML = `<div class="empty-state"><span class="empty-icon">◷</span><strong>${analysis ? '这只股票尚无本机竞价记录' : '尚无个股竞价记录'}</strong><p>${analysis ? '可加入关注，在交易日竞价窗口持续采集。<br>未采集的数据不会用收盘行情或零值替代。' : '查询股票后会显示本机观测、七项因子与排名范围。'}</p></div>`;
      return;
    }
    const q = qualityOf(row);
    const items = list(auction.history);
    const flags = [...q.flags];
    if (!auction.tracking) flags.unshift('当前展示已留存的观测，本机未持续监测这只股票。');
    if (demo) flags.unshift('以下为明确标记的模拟数据，不是实盘行情。');
    $('single-stock-auction').innerHTML = `<div class="single-auction-summary"><div><strong>${esc(num(row.score, 1))}<small> 分</small></strong><div class="stock-analysis-info">本池排名 ${numeric(row.rank) ? `第 ${esc(num(row.rank, 0))}` : '未参与有效排名'} / ${esc(num(auction.scope_count, 0))} 只</div></div><div class="subtle"><span class="quality-pill ${q.className}">${esc(q.label)}</span><br>最近接收 ${esc(time(row.updated_at))}<div class="source-tags">${sourceTags(row.sources)}</div></div></div><div class="stock-mini-metrics"><div class="stock-mini-metric"><span>竞价涨幅</span><strong class="${tone(row.auction_pct)}">${esc(percent(row.auction_pct))}</strong></div><div class="stock-mini-metric"><span>竞价金额</span><strong>${esc(amount(row.auction_amount))}</strong></div><div class="stock-mini-metric"><span>已采集观测</span><strong>${esc(num(row.quality?.observation_count ?? items.length, 0))}<small>窗口覆盖 ${esc(ratio(row.quality?.window_coverage))}</small></strong></div><div class="stock-mini-metric"><span>因子覆盖</span><strong>${esc(ratio(row.quality?.factor_coverage))}</strong></div></div><div class="single-auction-detail"><div><h3>七项竞价因子</h3>${factorBars(row.factors) || '<p class="subtle">暂无有效因子。</p>'}</div><div><h3>竞价涨幅轨迹 <span class="subtle">按本地接收顺序</span></h3>${sparkline(items)}<div class="history-list">${items.slice(-6).reverse().map((item) => `<div class="history-item"><span>${esc(time(item.received_at))}</span><span class="${tone(item.auction_pct)}">${esc(percent(item.auction_pct))}</span><span>${esc(amount(item.auction_amount))}</span></div>`).join('')}</div></div></div>${flags.length ? `<div class="stock-warning-list">${flags.map(esc).join('<br>')}</div>` : ''}`;
  }

  function renderTrendPool(pool, job) {
    const rows = list(pool);
    text('trend-pool-count', rows.length);
    text('trend-pool-description', pool?.date ? `截止 ${pool.date} · 候选 ${num(pool.candidate_count, 0)} · 已分析 ${num(pool.evaluated_count, 0)} · 有效历史 ${num(pool.valid_history_count, 0)} · 入选 ${num(pool.selected_count ?? rows.length, 0)}` : '自选＋昨日涨停＋趋势强股，构成重点关注池。');
    const notes = list(pool?.warnings);
    const status = job?.status === 'running' ? job.message || '正在刷新趋势池，请稍候…' : job?.status === 'error' ? `刷新失败：${job.message}` : pool ? `${pool.status === 'partial' ? '部分候选数据不完整。' : '趋势池已更新。'}${pool.generated_at ? `生成于 ${time(pool.generated_at, true)}。` : ''}${pool.method || ''}` : '尚未建立趋势池。09:10—09:26 暂停手动刷新，以优先保障竞价采集。';
    text('trend-pool-status', `${status} 筛选分只用于入池排序，与日线强弱分、竞价评分分别计算。${notes.length ? ` ${notes.join('；')}` : ''}`);
    $('trend-pool-status').classList.toggle('warn', job?.status === 'error' || pool?.status === 'partial' || notes.length > 0);
    $('trend-pool-body').innerHTML = rows.length ? rows.map((row) => {
      const trend = row.trend || {};
      return `<tr><td><button class="watchlist-stock-link" data-query-stock="${esc(row.thscode)}"><span class="stock-name">${esc(row.name || row.thscode)}</span><span class="stock-code">${esc(row.thscode)}</span></button></td><td><span class="score-number">${esc(num(row.trend_score ?? row.score, 1))}</span></td><td>${esc(num(trend.close, 2))}</td><td class="${tone(trend.return_5d_pct)}">${esc(percent(trend.return_5d_pct))}</td><td>${esc(num(trend.ma5, 2))} / ${esc(num(trend.ma20, 2))}</td><td class="trend-reason">${esc(Array.isArray(row.reason) ? row.reason.join('；') : row.reason || '满足趋势池研究条件')}</td><td><div class="trend-pool-actions"><button class="button" data-query-stock="${esc(row.thscode)}">查询</button><button class="button" data-add-stock="${esc(row.thscode)}" data-job="watchlist">＋ 关注</button></div></td></tr>`;
    }).join('') : '<tr><td colspan="7" class="table-empty">尚无入选股票。未取得历史数据不等于趋势评分为零。</td></tr>';
  }

  async function queryStock(code, trigger) {
    if (state.snapshot?.jobs?.stock?.status === 'running') { toast('已有个股查询正在后台运行，完成后可继续查询。'); return; }
    let codes;
    try { codes = parseStockCodes(code); } catch (error) { toast(error.message, true); return; }
    if (codes.length !== 1) { toast('单股查询每次请输入一个股票代码。', true); return; }
    state.queryRequested = codes[0];
    $('stock-code').value = codes[0];
    showPage('stocks');
    text('stock-job-status', `${codes[0]} · 正在提交查询；较长分析在后台运行，完成后自动更新。`);
    await mutate('/api/stocks/analyze', {code: codes[0], ...($('stock-query-date').value ? {date: $('stock-query-date').value} : {})}, '个股查询已提交', trigger);
  }

  async function addWatchlist(value, trigger) {
    let codes;
    try { codes = parseStockCodes(value); } catch (error) { toast(error.message, true); return; }
    text('watchlist-job-status', `正在提交 ${codes.length} 只股票进行核验…`);
    await mutate('/api/watchlist/add', {codes}, '自选加入任务已提交', trigger);
  }

  function displayedReportKey() {
    const report = state.snapshot?.review;
    return report ? `${state.snapshot.mode}|${state.snapshot.review_id || `${report.date}|${report.generated_at}|${report.status}`}` : `${state.snapshot?.mode}|empty`;
  }

  function matchingReviewAI() {
    const result = state.snapshot?.llm?.result;
    return Boolean(result && result.scope === 'review' && result.mode === state.snapshot?.mode && result.review_id && result.review_id === state.snapshot?.review_id && result.review_date === state.snapshot?.review?.date);
  }

  function renderReportControls() {
    const report = state.snapshot?.review;
    const demo = state.snapshot?.mode === 'demo' || report?.mode === 'demo' || report?.source === 'demo';
    const available = Boolean(report?.date);
    text('report-display-date', report?.date || '尚未生成');
    text('report-display-mode', available ? `${demo ? '模拟报告' : '真实数据复盘'} · 保存与导出均使用此日期` : '保存与导出均使用此报告日期');
    text('comparison-current', report?.date || '—');
    $('report-save').disabled = !available || state.reportSaveBusy;
    $('report-save').textContent = state.reportSaveBusy ? '正在保存…' : '保存 Markdown ↓';
    const aiAvailable = available && matchingReviewAI();
    $('report-include-ai').disabled = !aiAvailable || state.reportSaveBusy;
    if (!aiAvailable) $('report-include-ai').checked = false;
    $('report-include-ai').parentElement.title = aiAvailable ? '仅附上与当前这份报告匹配的 AI 研判，不额外调用模型。' : '请先在下方使用当前报告生成 AI 研判；其他日期或版本的分析不会附入。';
    const jsonLink = $('report-json');
    jsonLink.classList.toggle('disabled-link', !available);
    jsonLink.setAttribute('aria-disabled', String(!available));
    if (available) {
      const query = new URLSearchParams({date: report.date, format: 'json'});
      if (state.snapshot.review_id) query.set('review_id', state.snapshot.review_id);
      jsonLink.href = `/api/report?${query}`;
      jsonLink.download = `${demo ? 'demo-' : ''}market-review-${report.date}.json`;
    } else jsonLink.removeAttribute('href');
    $('report-history-date').disabled = demo || state.reportsBusy || state.reportLoadBusy;
    $('report-history-refresh').disabled = demo || state.reportsBusy;
    $('report-load').disabled = demo || !state.reports.length || !$('report-history-date').value || state.reportLoadBusy || state.snapshot?.jobs?.review?.status === 'running';
    $('report-load').textContent = state.reportLoadBusy ? '正在读取…' : '读取历史';
    $('comparison-baseline').disabled = demo || state.reportsBusy || state.comparisonBusy;
    $('comparison-run').disabled = demo || !available || !$('comparison-baseline').value || state.comparisonBusy || state.reportsBusy;
    $('comparison-run').textContent = state.comparisonBusy ? '计算中…' : '开始对比';
  }

  function renderReportOptions() {
    const current = state.snapshot?.review?.date;
    const reportDates = [...new Set(state.reports.map((item) => item.date).filter((date) => /^\d{4}-\d{2}-\d{2}$/.test(date)))].sort().reverse();
    for (const id of ['report-history-date', 'comparison-baseline']) {
      const select = $(id);
      const previousValue = select.value;
      const dates = id === 'comparison-baseline' ? reportDates.filter((date) => current && date < current) : reportDates;
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = dates.length ? '选择已保存日期' : id === 'comparison-baseline' ? '暂无更早日期报告' : '暂无本机历史报告';
      select.replaceChildren(placeholder, ...dates.map((date) => {
        const item = state.reports.find((entry) => entry.date === date);
        const option = document.createElement('option');
        option.value = date;
        option.textContent = `${date}${item?.status === 'partial' ? ' · 部分数据' : ''}`;
        return option;
      }));
      select.value = dates.includes(previousValue) ? previousValue : id === 'report-history-date' && dates.includes(current) ? current : dates.find((date) => date < current) || dates[0] || '';
    }
    renderReportControls();
  }

  async function loadReports() {
    const mode = state.snapshot?.mode;
    if (!mode || state.reportsBusy) return;
    state.reportsMode = mode;
    const generation = ++state.reportsGeneration;
    if (mode === 'demo') {
      state.reports = [];
      renderReportOptions();
      text('report-history-status', '演示期间不读取真实历史报告；切换实盘后可查看。');
      return;
    }
    state.reportsBusy = true;
    renderReportControls();
    text('report-history-status', '正在读取本机报告目录…');
    try {
      const result = await api('/api/reports');
      if (generation !== state.reportsGeneration || state.snapshot?.mode !== mode) return;
      state.reports = list(result.items).filter((item) => item.mode !== 'demo');
      renderReportOptions();
      text('report-history-status', state.reports.length ? `共 ${state.reports.length} 份真实报告 · 本机读取无需联网` : '还没有本机历史报告；联网生成后自动留存。');
    } catch (error) {
      if (state.snapshot?.mode === mode) { text('report-history-status', `列表读取失败：${error.message}`); toast(error.message, true); }
    } finally {
      state.reportsBusy = false;
      renderReportControls();
      if (state.snapshot?.mode !== mode || state.reportsRefreshPending) {
        state.reportsRefreshPending = false;
        loadReports();
      }
    }
  }

  async function loadSavedReport() {
    const date = $('report-history-date').value;
    if (!date || state.reportLoadBusy || state.snapshot?.mode === 'demo') return;
    state.reportLoadBusy = true;
    renderReportControls();
    text('report-history-status', `正在读取 ${date} 的本机报告…`);
    try {
      const result = await api('/api/reports/load', {date});
      await fetchState();
      text('report-history-status', result.message || `已读取 ${date}，未请求行情服务。`);
      toast(`已读取 ${date} 的本机复盘`);
    } catch (error) { text('report-history-status', `读取失败：${error.message}`); toast(error.message, true); }
    finally { state.reportLoadBusy = false; renderReportControls(); }
  }

  async function saveMarkdown() {
    const report = state.snapshot?.review;
    if (!report?.date || state.reportSaveBusy) return;
    const body = {date: report.date, include_ai: $('report-include-ai').checked && matchingReviewAI(), provider: state.snapshot?.llm?.active_provider};
    if (state.snapshot.review_id) body.review_id = state.snapshot.review_id;
    if ($('report-include-comparison').checked && state.comparisonBaseline) body.baseline = state.comparisonBaseline;
    state.reportSaveBusy = true;
    renderReportControls();
    text('report-save-status', `正在保存 ${report.date} 的 Markdown 文档…`);
    try {
      const result = await api('/api/reports/save', body);
      text('report-save-status', `${report.date} 已保存：${result.path || result.filename || '本机报告目录'}。`);
      if (result.download_url) {
        const url = new URL(result.download_url, location.origin);
        if (url.origin === location.origin && url.pathname === '/api/reports/download') {
          const link = document.createElement('a');
          link.href = url.href;
          link.download = result.filename || `market-review-${report.date}.md`;
          link.textContent = '再次下载 Markdown';
          link.className = 'text-button report-download';
          $('report-save-status').append(' 已尝试启动浏览器下载；', link);
          link.click();
        }
      }
      toast(`${report.date} Markdown 已保存${body.include_ai ? '，含对应 AI 研判' : ''}`);
      loadReports();
    } catch (error) { text('report-save-status', `保存失败：${error.message}`); toast(error.message, true); }
    finally { state.reportSaveBusy = false; renderReportControls(); }
  }

  function resetComparison() {
    ++state.comparisonGeneration;
    state.comparisonBusy = false;
    state.comparisonBaseline = '';
    $('report-include-comparison').checked = false;
    $('report-include-comparison').disabled = true;
    $('comparison-result').replaceChildren();
    const demo = state.snapshot?.mode === 'demo';
    text('comparison-status', demo ? '演示模式不对比真实历史报告。' : '选择另一份已保存报告，比较两期涨停名单、市场指标与共同板块；不会联网补齐缺失数据。');
    $('comparison-status').classList.remove('warn');
  }

  function compareValue(value, unit, delta = false) {
    if (!numeric(value)) return '—';
    const prefix = delta && Number(value) > 0 ? '+' : '';
    if (['元', 'yuan', 'CNY', 'amount'].includes(unit)) return `${prefix}${amount(value)}`;
    if (['%', 'pct', 'percent', '百分点', 'pp'].includes(unit)) return `${prefix}${num(value, 2)}${delta ? ' 个百分点' : '%'}`;
    return `${prefix}${num(value, Number.isInteger(Number(value)) ? 0 : 2)}${['家', '只', '板'].includes(unit) ? unit : ''}`;
  }

  function renderComparison(result) {
    const warnings = [...list(result.warnings), ...list(result.sectors?.warnings)];
    text('comparison-status', `${result.current_date || '当前'} 对比 ${result.previous_date || '基准'} · ${result.adjacent_sessions === true ? '相邻交易日' : result.adjacent_sessions === false ? '非相邻交易日；两期交集不等于连续涨停' : '未确认是否相邻交易日'}${warnings.length ? ` · ${warnings.join('；')}` : ''}`);
    $('comparison-status').classList.toggle('warn', result.status !== 'ready' || warnings.length > 0);
    const marketRows = list(result.market?.rows);
    const changes = result.limit_up || {};
    const stockGroups = [['retained', '共同两期涨停', changes.retained_count], ['new', '当期新出现', changes.new_count], ['exited', '从对比池退出', changes.exited_count]];
    const marketHtml = `<div class="table-scroll"><table class="data-table comparison-market"><thead><tr><th>盘面指标</th><th>当期</th><th>对比期</th><th>变化</th><th>数据质量</th></tr></thead><tbody>${marketRows.length ? marketRows.map((row) => `<tr><td>${esc(row.label || row.id)}</td><td>${esc(compareValue(row.current, row.unit))}</td><td>${esc(compareValue(row.previous, row.unit))}</td><td class="${tone(row.delta)}">${esc(compareValue(row.delta, row.unit, true))}</td><td>${esc({ok: '可比', ready: '可比', provisional: '日期待核验', missing: '缺少数据', unavailable: '不可比'}[row.status] || row.status || '—')}</td></tr>`).join('') : '<tr><td colspan="5" class="table-empty">没有可比较的市场指标。</td></tr>'}</tbody></table></div>`;
    const stocksHtml = `<div class="comparison-stock-groups">${stockGroups.map(([key, label, count]) => {
      const rows = list(changes[key]);
      const countText = numeric(count) ? num(count, 0) : '—';
      return `<section><h3>${label}<span>${esc(countText)}</span></h3><p>${key === 'retained' ? '仅说明两期都在涨停池中' : key === 'new' ? '不等同于首次涨停' : '不等同于断板或下跌'}</p><div class="comparison-stock-list">${rows.length ? rows.slice(0, 30).map((row) => `<button class="comparison-stock" data-query-stock="${esc(row.thscode)}"><span><strong>${esc(row.name || row.thscode)}</strong><small>${esc(row.thscode)}</small></span><span>${numeric(row.current?.score) ? `${esc(num(row.current.score, 1))} 分` : numeric(row.previous?.score) ? `前期 ${esc(num(row.previous.score, 1))}` : '—'}<small>${row.current?.consecutive_lower_bound ? '≥ ' : ''}${numeric(row.current?.consecutive_days) ? `${esc(num(row.current.consecutive_days, 0))} 板` : '当前板数未知'}</small></span></button>`).join('') : `<span class="comparison-empty">${changes.status === 'unavailable' ? '数据不足，无法确认名单' : '该分类没有股票'}</span>`}</div>${rows.length > 30 ? `<p>显示前 30 / ${esc(rows.length)} 只</p>` : ''}</section>`;
    }).join('')}</div>`;
    const sectors = result.sectors || {};
    const coverage = sectors.coverage || {};
    const sectorRows = list(sectors.rows);
    const sectorHtml = `<div class="comparison-sector-title"><h3>共同板块的强弱变化</h3><p>当期样本 ${esc(num(coverage.current_count, 0))} · 对比期 ${esc(num(coverage.previous_count, 0))} · 共同 ${esc(num(coverage.common_count, 0))} · 可比 ${esc(num(coverage.comparable_count, 0))}。名次为各期样本相对排名；成交额变化不代表资金净流入。</p></div><div class="table-scroll"><table class="data-table"><thead><tr><th>板块</th><th>当期 / 前期名次</th><th>名次前进</th><th>当期涨幅</th><th>前期涨幅</th><th>涨幅变化</th><th>成交额变化</th><th>数据质量</th></tr></thead><tbody>${sectorRows.length ? sectorRows.slice(0, 30).map((row) => `<tr><td><span class="stock-name">${esc(row.name || row.thscode)}</span><span class="stock-code">${esc(row.thscode)}</span></td><td>${esc(num(row.current_rank, 0))} / ${esc(num(row.previous_rank, 0))}</td><td class="${tone(row.rank_change)}">${esc(compareValue(row.rank_change, '', true))}</td><td class="${tone(row.current_change_pct)}">${esc(percent(row.current_change_pct))}</td><td class="${tone(row.previous_change_pct)}">${esc(percent(row.previous_change_pct))}</td><td class="${tone(row.change_delta_pp)}">${esc(compareValue(row.change_delta_pp, 'pp', true))}</td><td class="${tone(row.turnover_change_pct)}">${esc(percent(row.turnover_change_pct))}</td><td>${esc({ok: '可比', ready: '可比', provisional: '日期待核验', missing: '缺少数据', unavailable: '不可比'}[row.status] || row.status || '—')}</td></tr>`).join('') : '<tr><td colspan="8" class="table-empty">没有可比的共同板块，缺失数据保持为空。</td></tr>'}</tbody></table></div>`;
    $('comparison-result').innerHTML = marketHtml + stocksHtml + sectorHtml;
  }

  async function compareReports() {
    const date = state.snapshot?.review?.date;
    const baseline = $('comparison-baseline').value;
    if (!date || !baseline || state.comparisonBusy || state.snapshot?.mode === 'demo') return;
    const key = displayedReportKey();
    const generation = ++state.comparisonGeneration;
    state.comparisonBusy = true;
    state.comparisonBaseline = '';
    $('report-include-comparison').checked = false;
    $('report-include-comparison').disabled = true;
    renderReportControls();
    text('comparison-status', `正在对比 ${date} 与 ${baseline} 的本机报告…`);
    $('comparison-result').replaceChildren();
    try {
      const result = await api(`/api/reports/compare?${new URLSearchParams({date, baseline})}`);
      if (generation === state.comparisonGeneration && key === displayedReportKey()) {
        renderComparison(result);
        state.comparisonBaseline = result.status !== 'unavailable' ? baseline : '';
        $('report-include-comparison').disabled = !state.comparisonBaseline;
      }
    } catch (error) {
      if (generation === state.comparisonGeneration) { text('comparison-status', `对比失败：${error.message}`); $('comparison-status').classList.add('warn'); }
    } finally {
      if (generation === state.comparisonGeneration) state.comparisonBusy = false;
      renderReportControls();
    }
  }

  async function loadDiagnostics() {
    if (state.diagnosticsBusy) return;
    state.diagnosticsBusy = true;
    state.diagnosticsLoaded = true;
    $('diagnostics-refresh').disabled = true;
    text('diagnostics-summary', '正在检查本机状态…');
    try {
      const result = await api('/api/diagnostics');
      const names = {ok: '就绪', warn: '待处理', error: '需处理', info: '提示'};
      text('diagnostics-summary', result.summary || '检查已完成');
      text('diagnostics-badge', names[result.status] || '已检查');
      $('diagnostics-badge').className = `pill ${result.status === 'ok' ? 'green' : ['warn', 'error'].includes(result.status) ? 'amber' : ''}`;
      text('diagnostics-time', `${result.checked_at ? `检查于 ${time(result.checked_at, true)} · ` : ''}仅检查本机状态，不验证交易所时延或外部接口认证。`);
      $('diagnostics-checks').innerHTML = list(result.checks).map((check) => `<div class="diagnostic-check"><span class="diagnostic-status ${['ok', 'warn', 'error', 'info'].includes(check.status) ? check.status : 'info'}">${esc(names[check.status] || '提示')}</span><div><strong>${esc(check.label || check.id)}</strong><p>${esc(check.message || '—')}</p></div></div>`).join('');
    } catch (error) { text('diagnostics-summary', `本机检查失败：${error.message}`); text('diagnostics-badge', '检查失败'); }
    finally { state.diagnosticsBusy = false; $('diagnostics-refresh').disabled = false; }
  }

  function backtestBlockedReason() {
    if (!state.snapshot) return '正在连接本机服务。';
    if (state.snapshot.mode !== 'live') return '演示模式不运行或导入真实历史回测。';
    const stamp = new Date(state.snapshot.now || Date.now()).getTime() + Math.max(0, Date.now() - (state.snapshotReceivedAt || Date.now()));
    const hhmm = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(stamp));
    if (hhmm >= '09:10' && hhmm <= '09:26') return '09:10—09:26 优先竞价，09:27 后可运行实验或导入；已有结果仍可浏览。';
    if (state.backtestRunBusy || state.backtestImportBusy || state.snapshot.jobs?.research?.status === 'running') return '正在处理历史研究资料，请等待当前任务完成。';
    return '';
  }

  function renderBacktestControls() {
    const reason = backtestBlockedReason();
    const job = state.snapshot?.jobs?.research || {};
    $('backtest-run').disabled = Boolean(reason);
    $('backtest-run').title = reason || '按当前权重回放本机真实历史；不会自动替换参数。';
    $('backtest-run').textContent = state.backtestRunBusy || job.status === 'running' ? '正在运行回测…' : '运行本地回测';
    $('backtest-import-button').disabled = Boolean(reason);
    $('backtest-import-button').title = reason;
    $('backtest-refresh').disabled = Boolean(state.backtestBusy) || state.snapshot?.mode !== 'live';
    text('backtest-job-status', job.status === 'error' ? `本次实验未完成：${job.message || '请稍后重试。'}${state.backtest?.id ? ' 上一次已保存的结果仍保留。' : ''}` : reason || (state.backtestBusy ? '正在读取本机实验结果…' : '只读取本机历史数据，不调用行情或 AI 接口；参数建议不会自动应用。'));
    $('backtest-job-status').classList.toggle('warn', job.status === 'error' || Boolean(reason && !state.backtestRunBusy && job.status !== 'running'));
    for (const [id, format] of [['backtest-markdown', 'markdown'], ['backtest-json', 'json']]) {
      const link = $(id);
      const available = state.snapshot?.mode === 'live' && Boolean(state.backtest?.id);
      link.classList.toggle('disabled-link', !available);
      link.setAttribute('aria-disabled', String(!available));
      if (available) link.href = `/api/research/export?${new URLSearchParams({id: state.backtest.id, format})}`;
      else link.removeAttribute('href');
    }
    const aiDatasetLink = $('backtest-ai-dataset');
    const aiDatasetAvailable = state.snapshot?.mode === 'live' && Boolean(state.backtest?.id);
    aiDatasetLink.classList.toggle('disabled-link', !aiDatasetAvailable);
    aiDatasetLink.setAttribute('aria-disabled', String(!aiDatasetAvailable));
    if (aiDatasetAvailable) aiDatasetLink.href = `/api/research/ai-dataset?${new URLSearchParams({id: state.backtest.id})}`;
    else aiDatasetLink.removeAttribute('href');
    $('daily-audit-refresh').disabled = state.snapshot?.mode !== 'live' || Boolean(state.dailyAuditListBusy || state.dailyAuditBusy);
    for (const [id, format] of [['daily-audit-markdown', 'markdown'], ['daily-audit-json', 'json']]) {
      const link = $(id);
      const available = state.snapshot?.mode === 'live' && Boolean(state.dailyAudit?.id || state.dailyAudit?.frozen_id) && !state.dailyAuditBusy;
      link.classList.toggle('disabled-link', !available);
      link.setAttribute('aria-disabled', String(!available));
      if (available) link.href = `/api/research/daily/export?${new URLSearchParams({date: state.dailyAudit.date, format})}`;
      else link.removeAttribute('href');
    }
  }

  function syncBacktest(snapshot) {
    if (state.backtestMode !== snapshot.mode) {
      state.backtestMode = snapshot.mode;
      state.backtest = null;
      state.backtestRequestedId = undefined;
      state.dailyAudit = null;
      state.dailyAuditListLoaded = false;
      state.dailyAuditGeneration = (state.dailyAuditGeneration || 0) + 1;
      state.dailyAuditListGeneration = (state.dailyAuditListGeneration || 0) + 1;
      state.dailyAuditBusy = false;
      state.dailyAuditListBusy = false;
      $('daily-audit-date').innerHTML = '<option value="">暂无真实每日存档</option>';
      renderDailyAudit();
      renderBacktestReport();
    }
    const currentId = snapshot.research?.id || '';
    if (state.page === 'backtest' && snapshot.mode === 'live' && !state.backtestBusy && state.backtestRequestedId !== currentId) loadBacktest();
    if (state.page === 'backtest' && snapshot.mode === 'live' && !state.dailyAuditListBusy && !state.dailyAuditListLoaded) loadDailyAudits();
    renderBacktestControls();
  }

  async function loadDailyAudits() {
    if (state.dailyAuditListBusy || state.snapshot?.mode !== 'live') return;
    state.dailyAuditListBusy = true;
    state.dailyAuditListLoaded = true;
    const generation = (state.dailyAuditListGeneration || 0) + 1;
    state.dailyAuditListGeneration = generation;
    renderBacktestControls();
    try {
      const result = await api('/api/research/daily');
      if (state.snapshot?.mode !== 'live' || generation !== state.dailyAuditListGeneration) return;
      const items = list(result.items).filter((item) => /^\d{4}-\d{2}-\d{2}$/.test(item.date || ''));
      const previousDate = $('daily-audit-date').value;
      $('daily-audit-date').innerHTML = items.length ? items.map((item) => `<option value="${esc(item.date)}">${esc(item.date)} · ${esc({frozen: '已冻结 · 待收盘核验', ready: '已核验', partial: '部分数据', unavailable: '尚不可用'}[item.status] || '已存档')}</option>`).join('') : '<option value="">暂无真实每日存档</option>';
      $('daily-audit-date').disabled = !items.length;
      if (items.some((item) => item.date === previousDate)) $('daily-audit-date').value = previousDate;
      if (items.length) await loadDailyAudit();
      else { state.dailyAudit = null; renderDailyAudit(); }
    } catch (error) { text('daily-audit-status', `每日存档读取失败：${error.message}`); }
    finally { state.dailyAuditListBusy = false; renderBacktestControls(); }
  }

  async function loadDailyAudit() {
    const date = $('daily-audit-date').value;
    if (!date || state.snapshot?.mode !== 'live') return;
    const generation = (state.dailyAuditGeneration || 0) + 1;
    state.dailyAuditGeneration = generation;
    state.dailyAuditBusy = true;
    state.dailyAudit = null;
    renderDailyAudit();
    text('daily-audit-status', `正在读取 ${date} 的原评分核验…`);
    renderBacktestControls();
    try {
      const result = await api(`/api/research/daily?${new URLSearchParams({date})}`);
      if (generation !== state.dailyAuditGeneration || state.snapshot?.mode !== 'live') return;
      state.dailyAudit = result;
      renderDailyAudit();
    } catch (error) { if (generation === state.dailyAuditGeneration) text('daily-audit-status', `每日存档读取失败：${error.message}`); }
    finally { if (generation === state.dailyAuditGeneration) state.dailyAuditBusy = false; renderBacktestControls(); }
  }

  function renderDailyAudit() {
    const artifact = state.snapshot?.mode === 'live' ? state.dailyAudit : null;
    const sessions = list(artifact?.sessions);
    const checkpoint = $('daily-audit-checkpoint').value;
    $('daily-audit-checkpoint').innerHTML = sessions.length ? sessions.map((session, index) => `<option value="${index}">${esc(session.checkpoint)}${session.checkpoint === '09:24:50' ? ' · 盘前' : ' · 收尾'}</option>`).join('') : '<option value="">等待存档</option>';
    $('daily-audit-checkpoint').value = sessions.length ? String(sessions[Number(checkpoint)] ? Number(checkpoint) : 0) : '';
    $('daily-audit-checkpoint').disabled = !sessions.length;
    renderDailyAuditRows();
  }

  function renderDailyAuditRows() {
    const artifact = state.snapshot?.mode === 'live' ? state.dailyAudit : null;
    const session = $('daily-audit-checkpoint').value !== '' ? list(artifact?.sessions)[Number($('daily-audit-checkpoint').value)] : null;
    const rows = list(session?.rows);
    const quality = session?.quality || {};
    const provenance = session?.strategy_provenance || {};
    const scoreMethod = session?.method === 'recorded_ranking' ? '原始评分记录' : session ? '按原权重重放' : '等待评分证据';
    const status = {frozen: '已冻结竞价评分，等待收盘完整池核验', ready: '已完成收盘核验', partial: '存在缺失或待核验项', unavailable: '尚无可用存档'}[artifact?.status] || '已保存';
    const provenanceText = provenance.legacy_fallback ? '旧记录缺少当时的权重证据；这是带标记的恢复值，不能视为已证实的当日原评分。' : provenance.weights_at ? `权重记录时间 ${provenance.weights_at}` : '';
    text('daily-audit-status', artifact?.date ? `${artifact.date} · ${status} · ${scoreMethod}${artifact.frozen_at || artifact.auction_frozen_at ? ` · 竞价冻结于 ${time(artifact.frozen_at || artifact.auction_frozen_at, true)}` : ''}${provenanceText ? ` · ${provenanceText}` : ''}${list(session?.warnings).length ? ` · ${list(session.warnings).join('；')}` : ''}` : state.snapshot?.mode === 'demo' ? '演示模式不展示真实每日评分档案。' : '尚无真实交易日存档。');
    $('daily-audit-summary').innerHTML = session ? [researchMetric('当日候选 / 可评分', `${num(quality.candidate_count ?? rows.length, 0)} / ${num(quality.scored_count, 0)}`, '固定昨日涨停候选，保留缺观察记录'), researchMetric('结果已知 / 未知', `${rows.filter((row) => typeof row.label === 'boolean').length} / ${rows.filter((row) => typeof row.label !== 'boolean').length}`, '没有完整收盘池，不把未知改成未涨停'), researchMetric('对照时点', session.checkpoint || '—', `${scoreMethod} · ${session.checkpoint === '09:24:50' ? '盘前时点' : '收尾诊断，不混入盘前预测'}`)].join('') : '';
    $('daily-audit-body').innerHTML = backtestRowsHtml(rows.map((row, index) => ({row, index})), '等待真实竞价与收盘核验记录。');
  }

  async function loadBacktest() {
    if (state.backtestBusy || state.snapshot?.mode !== 'live') return;
    state.backtestBusy = true;
    state.backtestRequestedId = state.snapshot?.research?.id || '';
    const mode = state.snapshot.mode;
    renderBacktestControls();
    try {
      const result = await api('/api/research');
      if (state.snapshot?.mode !== mode) return;
      state.backtest = result;
      state.backtestRequestedId = result.id || state.backtestRequestedId;
      renderBacktestReport();
    } catch (error) {
      text('backtest-record', `结果读取失败：${error.message}`);
      toast(error.message, true);
    } finally { state.backtestBusy = false; renderBacktestControls(); }
  }

  async function runBacktest() {
    const reason = backtestBlockedReason();
    if (reason) { toast(reason, true); return; }
    state.backtestRunBusy = true;
    renderBacktestControls();
    try {
      const result = await api('/api/research/run', {});
      toast(result.message || '已开始回放历史竞价并核对结果。');
      await fetchState();
    } catch (error) { toast(error.message, true); }
    finally { state.backtestRunBusy = false; renderBacktestControls(); }
  }

  async function importBacktestFile(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    const reason = backtestBlockedReason();
    if (reason) { toast(reason, true); return; }
    state.backtestImportBusy = true;
    renderBacktestControls();
    text('backtest-import-status', `正在核验 ${file.name}…`);
    try {
      if (file.size > 5 * 1024 * 1024) throw new Error('历史 JSON 不能超过 5 MB，请按交易日分批导入。');
      let dataset;
      try { dataset = JSON.parse(await file.text()); } catch { throw new Error('文件不是可读取的 JSON，请参照空白格式模板。'); }
      if (!dataset || typeof dataset !== 'object' || Array.isArray(dataset)) throw new Error('历史文件顶层必须为 JSON 对象，请参照模板。');
      if (new Blob([JSON.stringify({dataset})]).size > 5 * 1024 * 1024) throw new Error('提交内容超过 5 MB，请按交易日分批导入。');
      const result = await api('/api/research/import', {dataset});
      text('backtest-import-status', result.message || '历史资料已导入。点击「运行本地回测」，按当前算法重新计算。');
      toast(result.message || '导入完成，可运行本地回测。');
      await fetchState();
    } catch (error) { text('backtest-import-status', `未导入：${error.message}`); toast(error.message, true); }
    finally { state.backtestImportBusy = false; renderBacktestControls(); }
  }

  function backtestMetricTable(baseline, candidate, caption) {
    const rows = [['日均前十涨停占比', 'precision', ratio], ['入选股票合计涨停占比', 'pooled_precision', ratio], ['样本池涨停占比', 'pool_base_rate', ratio], ['捕获的涨停占比', 'recall', ratio], ['相对样本池提升倍数', 'lift', (value) => numeric(value) ? `${num(value, 2)} 倍` : '—'], ['有效日期', 'day_count', (value) => num(value, 0)], ['入选股票日 / 涨停数', 'selected_count', (_, data) => `${num(data?.selected_count, 0)} / ${num(data?.hits, 0)}`]];
    return `<div class="research-section-heading"><h3>${esc(caption)}</h3><span class="subtle">每日取前 min(10, 样本数) 只</span></div><div class="table-scroll"><table class="data-table backtest-metric-table"><thead><tr><th>检验指标</th><th>当前权重</th>${candidate ? '<th>候选权重</th>' : ''}</tr></thead><tbody>${rows.map(([label, key, format]) => `<tr><td>${esc(label)}</td><td>${esc(format(baseline?.[key], baseline))}</td>${candidate ? `<td>${esc(format(candidate[key], candidate))}</td>` : ''}</tr>`).join('')}</tbody></table></div>`;
  }

  function renderBacktestReport() {
    const result = state.snapshot?.mode === 'live' ? state.backtest : null;
    const available = Boolean(result?.id);
    const labels = {not_run: '尚未运行', insufficient_data: '历史样本不足', completed: '实验已完成', exploratory_holdout_reuse: '重复检验 · 探索性结果', cancelled: '实验已中断'};
    text('backtest-status', labels[result?.status] || (available ? '已保存实验' : '尚未运行'));
    text('backtest-record', available ? `${result.generated_at ? `生成于 ${time(result.generated_at, true)} · ` : ''}实验 ${result.id} · 版本 ${result.version || '—'}` : state.snapshot?.mode === 'demo' ? '演示数据不参与真实策略回测。切换实盘后可读取历史实验。' : '尚未运行；已有实时参数保持不变。');
    const availability = result?.availability || {};
    $('backtest-availability').innerHTML = [researchMetric('本机真实竞价日期', num(availability.live_batch_days, 0), '只统计实际留存的 live 批次'), researchMetric('导入历史日期', num(availability.imported_days, 0), '外部资料保留来源与时点核验'), researchMetric('完整涨停池日期', num(availability.pool_days, 0), '仅池名单不能重建七因子'), researchMetric('回放会话日期', num(availability.session_days, 0), '合格调参样本另行筛选')].join('');
    $('backtest-warnings').innerHTML = researchWarnings(result?.warnings);
    const optimization = result?.optimization || {};
    const sample = optimization.sample_summary || {};
    text('backtest-optimization-status', optimization.recommendation?.accepted ? '候选通过本次检验' : labels[optimization.status] || '等待真实样本');
    $('backtest-optimization-status').className = `pill ${optimization.recommendation?.accepted ? 'green' : 'amber'}`;
    text('backtest-recommendation', optimization.recommendation?.reason || '没有足够的真实历史竞价，不能选出更优参数。');
    $('backtest-optimization-metrics').innerHTML = [researchMetric('完整有效日期', `${num(sample.complete_days, 0)} / ${num(sample.required_days ?? 30, 0)}`, '按日期划分训练、滚动验证与保留检验'), researchMetric('完整股票日', `${num(sample.complete_rows, 0)} / ${num(sample.required_rows ?? 300, 0)}`, '七因子齐全且收盘结果已核验'), researchMetric('涨停 / 未涨停结果', `${num(sample.positive_count, 0)} / ${num(sample.negative_count, 0)}`, `此处为全样本；开发集两类各须至少 ${num(sample.required_class_rows ?? 30, 0)} 个，未知排除`)].join('');
    const search = optimization.search || {};
    const dateRange = (dates) => list(dates).length ? `${list(dates)[0]} 至 ${list(dates)[list(dates).length - 1]}（${list(dates).length} 日）` : '尚未划分';
    $('backtest-partitions').innerHTML = search.development_dates ? `<div><span>较早日期 · 参数开发</span><strong>${esc(dateRange(search.development_dates))}</strong></div><div><span>较晚日期 · 保留检验</span><strong>${esc(dateRange(search.holdout_dates))}</strong></div><p>候选参数 ${esc(num(search.candidate_count, 0))} 组 · 滚动验证 ${esc(num(list(search.folds).length, 0))} 轮。保留期只评价选定参数，不参与选择。</p>` : '';
    const holdout = optimization.holdout;
    $('backtest-holdout').innerHTML = holdout ? backtestMetricTable(holdout.baseline, holdout.candidate, '较晚日期的独立检验') + `<p class="research-note backtest-quality">日均前十涨停占比变化 ${esc(numeric(holdout.precision_delta_pp) ? `${Number(holdout.precision_delta_pp) > 0 ? '+' : ''}${num(holdout.precision_delta_pp, 2)} 个百分点` : '未知')} · 胜过当前权重的日期占比 ${esc(ratio(holdout.positive_gain_day_ratio))}。${result?.status === 'exploratory_holdout_reuse' ? '这份保留样本已经使用过，本次结果只作探索，不能视为新的独立验证。' : '一次通过不代表未来收益，也不代表已验证成交可行性。'}</p>` : optimization.baseline?.day_count ? backtestMetricTable(optimization.baseline, null, '当前权重在已有合格样本中的表现') : '';
    const candidate = optimization.candidate_weights;
    $('backtest-weight-comparison').innerHTML = candidate ? `<div class="research-section-heading"><h3>本次候选权重</h3><span class="subtle">保留建议，未修改实时策略</span></div><div class="table-scroll"><table class="data-table backtest-metric-table"><thead><tr><th>竞价因子</th><th>当前权重</th><th>候选权重</th></tr></thead><tbody>${Object.entries(factors).map(([key, value]) => `<tr><td>${esc(value.label)}</td><td>${esc(ratio(result.baseline_weights?.[key]))}</td><td>${esc(ratio(candidate[key]))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="research-note backtest-no-candidate">当前没有可供采用的新权重；继续保留现有配置。</p>';
    const diagnostics = list(optimization.factor_diagnostics);
    const missing = sample.missing_factors || {};
    $('backtest-factor-diagnostics').innerHTML = Object.keys(missing).length || diagnostics.length ? `<details class="backtest-factor-section"><summary>因子贡献实验与缺失统计</summary><p class="research-note">单因子与移除因子实验只在较早的开发日期比较；不反复利用保留期挑选因子。占比单位为百分数。</p><div class="table-scroll"><table class="data-table backtest-factors-table"><thead><tr><th>因子</th><th>缺失股票日</th><th>单因子验证前十涨停占比</th><th>移除后验证占比变化</th></tr></thead><tbody>${Object.entries(factors).map(([key, info]) => {
      const item = diagnostics.find((entry) => entry.factor === key);
      const delta = item?.without_factor?.validation_precision_delta;
      return `<tr><td>${esc(info.label)}</td><td>${esc(num(item?.missing_rows ?? missing[key], 0))}</td><td>${esc(ratio(item?.single_factor?.validation?.precision))}</td><td>${esc(numeric(delta) ? `${Number(delta) > 0 ? '+' : ''}${num(Number(delta) * 100, 2)} 个百分点` : '尚未实验')}</td></tr>`;
    }).join('')}</tbody></table></div></details>` : '';
    $('backtest-optimization-warnings').innerHTML = researchWarnings(optimization.warnings);
    const sessions = list(result?.sessions);
    $('backtest-session').innerHTML = sessions.length ? sessions.map((session, index) => `<option value="${index}" data-session-key="${esc(`${session.date}|${session.checkpoint}`)}">${esc(session.date)} · ${esc(session.checkpoint)}${session.checkpoint === '09:24:50' ? ' · 盘前检验' : ' · 收尾对照'}</option>`).join('') : '<option value="">暂无真实回放</option>';
    const retainedIndex = sessions.findIndex((session) => `${session.date}|${session.checkpoint}` === state.backtestSessionKey);
    $('backtest-session').value = sessions.length ? String(retainedIndex >= 0 ? retainedIndex : sessions.findIndex((session) => session.checkpoint === '09:24:50') >= 0 ? sessions.findIndex((session) => session.checkpoint === '09:24:50') : 0) : '';
    $('backtest-session').disabled = !sessions.length;
    renderBacktestSession();
    const transitions = list(result?.transitions?.rows);
    $('backtest-transitions-body').innerHTML = transitions.length ? transitions.map((row) => `<tr><td>${esc(row.date)}</td><td>${esc(row.previous_date)}</td><td>${esc(num(row.yesterday_count, 0))}</td><td>${esc(num(row.repeat_count, 0))}</td><td><div class="backtest-rate"><span style="width:${clamp(row.repeat_rate_pct)}%"></span><strong>${esc(pctValue(row.repeat_rate_pct))}</strong></div></td></tr>`).join('') : '<tr><td colspan="5" class="table-empty">没有可核验的相邻完整涨停池；缺失不补零。</td></tr>';
    renderBacktestControls();
  }

  function backtestRowsHtml(rows, emptyMessage) {
    return rows.length ? rows.map(({row, index}) => {
      const label = row.label === true ? '<span class="pill green">是</span>' : row.label === false ? '<span class="pill">否</span>' : '<span class="pill amber">未知</span>';
      const factorHtml = Object.entries(factors).map(([key, info]) => `<span><em>${esc(info.label)}</em><strong>${esc(num(row.factors?.[key]?.score, 1))}</strong></span>`).join('');
      const flags = list(row.quality?.flags);
      return `<tr><td>${numeric(row.score) ? esc(num(index + 1, 0)) : '—'}</td><td><strong class="stock-name">${esc(row.name || row.thscode)}</strong><span class="stock-code">${esc(row.thscode)}</span></td><td>${esc(num(row.score, 2))}</td><td>${esc(ratio(row.quality?.factor_coverage))}</td><td>${label}</td><td><details class="backtest-row-factors"><summary>展开七因子${flags.length ? ` · ${esc(flags.length)} 项提示` : ''}</summary><div>${factorHtml}</div>${flags.length ? `<p>${esc(flags.join('；'))}</p>` : ''}</details></td></tr>`;
    }).join('') : `<tr><td colspan="6" class="table-empty">${esc(emptyMessage)}</td></tr>`;
  }

  function renderBacktestSession() {
    const session = state.snapshot?.mode === 'live' && $('backtest-session').value !== '' ? list(state.backtest?.sessions)[Number($('backtest-session').value)] : null;
    state.backtestSessionKey = session ? `${session.date}|${session.checkpoint}` : '';
    const quality = session?.quality || {};
    $('backtest-session-metrics').innerHTML = session ? [researchMetric('盘前候选 / 已观察', `${num(quality.candidate_count, 0)} / ${num(quality.observed_count, 0)}`, '先确定候选，后核对收盘结果'), researchMetric('可评分 / 未观察', `${num(quality.scored_count, 0)} / ${num(quality.unobserved_count, 0)}`, '未观察或无效分数不变成零分'), researchMetric('优化样本资格', quality.eligible_for_optimization === true ? '可进入筛选' : '仅作诊断', '还需满足七因子、完整日期与样本门槛')].join('') : '';
    text('backtest-session-quality', session ? `盘前背景 ${quality.context_verified === true ? '已核验' : '未完整核验'} · 同日结果池 ${quality.outcome_verified === true ? '已核验' : '未完整核验'}${list(session.warnings).length ? ` · ${list(session.warnings).join('；')}` : ''}` : '尚无可回放的真实会话。');
    const search = $('backtest-stock-search').value.trim().toLowerCase();
    const allRows = list(session?.rows);
    const rows = allRows.map((row, index) => ({row, index})).filter(({row}) => !search || `${row.name || ''} ${row.thscode || ''}`.toLowerCase().includes(search));
    $('backtest-stocks-body').innerHTML = backtestRowsHtml(rows, session ? search ? '没有匹配的名称或股票代码。' : '本时点没有可展示的候选记录。' : '尚无可回放的真实竞价记录。涨停池名单不能代替历史竞价序列。');
    text('backtest-stock-count', session ? `${session.date} ${session.checkpoint} · 显示 ${rows.length} / ${allRows.length} 只候选` : '股票代码保留交易所后缀与前导零');
  }

  const researchStatus = (status) => ({ready: '已取得', partial: '部分数据', unavailable: '尚不可用', cancelled: '已中断'}[status] || '未补充');
  const pctValue = (value) => numeric(value) ? `${num(value, 1)}%` : '—';
  const researchMetric = (label, value, note) => `<div class="research-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const researchWarnings = (warnings) => {
    const rows = [...new Set(list(warnings).filter(Boolean))];
    return rows.length ? `<details><summary>数据质量与缺失说明 · ${rows.length} 项</summary><ul>${rows.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></details>` : '';
  };
  const datedStockButton = (code, date, name) => /^[0-9]{6}\.(SH|SZ|BJ)$/.test(code || '')
    ? `<button class="research-code" data-query-stock="${esc(code)}" data-query-date="${esc(date)}" title="查询 ${esc(date)} 截止日的 ${esc(code)}">${name ? `<strong>${esc(name)}</strong>` : ''}<span>${esc(code)}</span></button>`
    : `<span class="subtle">${esc(name || '代码缺失')}</span>`;

  function officialBlockedReason() {
    const snapshot = state.snapshot || {};
    if (snapshot.mode === 'demo' || snapshot.review?.mode === 'demo') return '演示模式不请求真实官方观察。';
    if (!snapshot.review?.date || !snapshot.review_id) return '请先生成或读取一份已收盘的真实报告。';
    const stamp = new Date(snapshot.now || Date.now()).getTime() + Math.max(0, Date.now() - (state.snapshotReceivedAt || Date.now()));
    const hhmm = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(stamp));
    if (hhmm >= '09:10' && hhmm <= '09:26') return '09:10—09:26 为竞价保护时段，09:27 后可补充官方观察。';
    if (snapshot.jobs?.review?.status === 'running') return '复盘正在生成，完成后可补充。';
    if (state.evidenceBusy || snapshot.jobs?.evidence?.status === 'running') return '正在补充官方观察，请等待当前任务完成。';
    if (snapshot.api?.rate_limited && (state.apiCooldownDeadline || 0) > Date.now()) return '行情接口正在限流冷却，结束后可补充。';
    return '';
  }

  function renderOfficialControls() {
    const report = state.snapshot?.review;
    const job = state.snapshot?.jobs?.evidence || {};
    const reason = officialBlockedReason();
    $('official-enrich').disabled = Boolean(reason);
    $('official-enrich').title = reason || '按当前报告日期读取最多四个官方分项，不调用 AI。';
    $('official-enrich').textContent = state.evidenceBusy || job.status === 'running' ? '正在补充…' : report?.official_context ? '重新补充官方观察' : '联网补充官方观察';
    const status = job.status === 'error' ? `最近一次补充任务失败：${job.message || '请检查接口状态后重试'}`
      : job.status === 'running' ? job.message || reason
      : reason || (job.status === 'done' ? `${job.message || '补充任务已结束'}；请核对下方各分项的日期与缺失说明。` : '手动补充最多 4 个官方分项；完成后可用上方「保存 Markdown」留存。');
    text('official-job-status', state.evidenceSubmitError || status);
    $('official-job-status').classList.toggle('warn', Boolean(state.evidenceSubmitError) || job.status === 'error');
    const unavailable = Boolean(state.snapshot?.jobs?.evidence?.status === 'running');
    if (unavailable) {
      $('report-load').disabled = true;
      document.querySelectorAll('[data-action="review"]').forEach((button) => { button.disabled = true; });
    }
  }

  async function enrichReport() {
    const reason = officialBlockedReason();
    if (reason) { toast(reason); return; }
    const body = {date: state.snapshot.review.date, review_id: state.snapshot.review_id};
    state.evidenceBusy = true;
    state.evidenceSubmitError = '';
    renderOfficialControls();
    try {
      const result = await api('/api/reports/enrich', body);
      toast(result.message || `${body.date} 官方观察已提交`);
      await fetchState();
    } catch (error) { state.evidenceSubmitError = `补充提交失败：${error.message}`; toast(error.message, true); }
    finally { state.evidenceBusy = false; renderOfficialControls(); }
  }

  function renderResearch(snapshot) {
    const report = snapshot.review;
    const sentiment = snapshot.review_sentiment || report?.sentiment;
    const official = report?.official_context;
    const signature = JSON.stringify([displayedReportKey(), sentiment?.version, sentiment?.status, official?.generated_at]);
    if (state.researchSignature === signature) return;
    state.researchSignature = signature;
    state.evidenceSubmitError = '';
    const date = report?.date || '—';
    text('sentiment-date', report ? `${date}${snapshot.mode === 'demo' ? ' · DEMO' : ''}` : '等待报告');
    text('sentiment-summary', sentiment ? `${researchStatus(sentiment.status)} · ${sentiment.definition || '仅描述所选日期的已留存涨停证据。'}` : '读取已有报告即可计算，不新增行情请求，不生成预测。');
    const matrix = sentiment?.matrix || {}, retention = sentiment?.retention || {}, reasons = sentiment?.reasons || {};
    $('sentiment-retention').innerHTML = [
      researchMetric('封单留存中位数', pctValue(retention.median_pct), `有效 ${num(retention.valid_count, 0)} / 全池 ${num(retention.total_count, 0)} · 覆盖 ${pctValue(retention.coverage_pct)}`),
      researchMetric('留存低于 50% 的占比', pctValue(retention.below_50_pct), `${num(retention.below_50_count, 0)} 只 / ${num(retention.valid_count, 0)} 只有效样本`),
      researchMetric('留存超过 100% 的异常', num(retention.above_100_count, 0), `异常剔除；其他无效 ${num(retention.invalid_count, 0)} · 缺失 ${num(retention.missing_count, 0)}`)
    ].join('');
    text('sentiment-coverage', `完整 ${num(matrix.coverage?.complete_days, 0)} / ${num(matrix.coverage?.expected_days, 0)} 日 · ${matrix.coverage?.calendar_verified ? '交易日历已核验' : '交易日连续性未确认'}`);
    const days = list(matrix.rows).slice(-10);
    const maxCell = Math.max(1, ...days.flatMap((row) => Object.values(row.buckets || {}).filter(numeric).map(Number)));
    $('sentiment-matrix-body').innerHTML = days.length ? days.map((row) => `<tr><td>${esc(row.date)}</td><td>${esc(num(row.limit_up_count, 0))}</td>${['1', '2', '3', '4', '5+', 'unknown'].map((key) => {
      const value = row.buckets?.[key], lower = row.lower_bound_by_bucket?.[key];
      return `<td class="heat-cell" style="--cell-strength:${numeric(value) ? .035 + Number(value) / maxCell * .22 : 0}"><strong>${esc(num(value, 0))}</strong>${numeric(lower) && Number(lower) > 0 ? `<small>下限 ${esc(num(lower, 0))}</small>` : ''}</td>`;
    }).join('')}<td>${esc(num(row.lower_bound_count, 0))}</td><td><span class="${row.coverage?.complete ? 'neutral' : 'research-warn'}">${row.coverage?.complete ? '完整' : '不完整'}</span><small class="sector-source">观察 ${esc(num(row.coverage?.observed_count, 0))} 只</small></td></tr>`).join('') : '<tr><td colspan="10" class="table-empty">尚无可核验的逐日完整涨停池，缺失不记作零。</td></tr>';
    text('sentiment-reason-coverage', `有原因 ${num(reasons.known_count, 0)} / 全池 ${num(reasons.total_count, 0)} · 共 ${num(reasons.group_count, 0)} 组 · 覆盖 ${pctValue(reasons.coverage_pct)}`);
    $('sentiment-reasons-body').innerHTML = list(reasons.rows).length ? list(reasons.rows).slice(0, 30).map((row) => `<tr><td class="research-reason">${esc(row.reason)}</td><td>${esc(num(row.count, 0))}</td><td>${esc(pctValue(row.share_pct))}</td><td><div class="research-codes">${list(row.codes).slice(0, 10).map((code) => datedStockButton(code, date)).join('')}</div>${numeric(row.other_code_count) && row.other_code_count > 0 ? `<small class="sector-source">另 ${esc(num(row.other_code_count, 0))} 只未展开</small>` : ''}</td></tr>`).join('') : '<tr><td colspan="4" class="table-empty">暂无可用官方涨停原因；不由名称或其他标签推测。</td></tr>';
    $('sentiment-warnings').innerHTML = researchWarnings([...list(sentiment?.warnings), ...days.flatMap((row) => list(row.warnings).map((warning) => `${row.date}：${warning}`)), ...(retention.definition ? [retention.definition] : [])]);
    renderOfficialContext(official, report);
  }

  function renderOfficialContext(context, report) {
    const date = report?.date;
    text('official-context-date', date ? `观察日期 ${date}${date < dateAtShanghai() ? ' · 历史报告' : ''}` : '尚未生成报告');
    const valid = context && context.date === date && context.mode === state.snapshot?.mode;
    text('official-context-status', valid ? researchStatus(context.status) : context ? '日期或模式不匹配' : '未补充');
    if (!valid) {
      $('official-context-body').innerHTML = `<div class="table-empty">${context ? '补充数据与当前报告日期或模式不一致，未显示。请重新补充。' : '点击上方按钮，为当前报告补充可核验的同日观察。'}</div>`;
      return;
    }
    const benchmark = context.benchmark;
    const benchValid = benchmark?.date === date;
    const benchmarkHtml = `<section class="official-benchmark"><div class="research-section-heading"><h3>短线风向标 · 竞价样本</h3><span class="subtle">${esc(date)} · ${benchValid ? researchStatus(benchmark.status) : '未取得'}</span></div>${benchValid ? `<div class="research-metrics">${researchMetric('竞价涨幅均值', percent(benchmark.mean_auction_pct), '有效样本计算，百分数原值')}${researchMetric('竞价涨幅中位数', percent(benchmark.median_auction_pct), `有效 ${num(benchmark.valid_auction_count, 0)} / 样本 ${num(benchmark.sample_count, 0)}`)}${researchMetric('正涨幅样本占比', pctValue(benchmark.positive_ratio_pct), '零涨幅计入分母，不计上涨')}</div><p class="research-note">官方样本并非全市场；本页显示 ${esc(num(benchmark.shown_count, 0))} / ${esc(num(benchmark.sample_count, 0))} 只。标签仅作来源说明，不参与评分。</p><div class="official-sample-list">${list(benchmark.rows).slice(0, 30).map((row) => `<div>${datedStockButton(row.thscode, date, row.name)}<span class="${tone(row.auction_pct)}">${esc(percent(row.auction_pct))}</span><small>${esc(list(row.tags).join(' · ') || '无标签')}</small></div>`).join('') || '<p class="research-note">官方返回空样本，均值、中位数及上涨占比均保持未知。</p>'}</div>` : '<p class="research-note research-warn">风向标分项未取得或日期不匹配，不以零代替。</p>'}</section>`;
    const boardsHtml = ['all', 'org', 'hot_money'].map((key) => renderOfficialBoard(context.dragon_tiger?.[key], key, date)).join('');
    const errors = list(context.errors).map((error) => `${({benchmark:'风向标',all:'全部龙虎榜',org:'机构龙虎榜',hot_money:'游资龙虎榜'})[error.section] || error.section}：${error.message || '分项失败'}${numeric(error.code) ? `（code=${error.code}）` : ''}`);
    $('official-context-body').innerHTML = `<p class="research-note">补充于 ${esc(time(context.generated_at, true))} · 已请求 ${esc(num(context.requests?.attempted, 0))} / ${esc(num(context.requests?.max, 0))} 分项${context.cancelled ? ' · 任务已中断，未请求部分保留为空' : ''}</p>${errors.length ? `<div class="official-errors" role="status">${errors.map((error) => `<p>${esc(error)}</p>`).join('')}</div>` : ''}${benchmarkHtml}<div class="official-boards">${boardsHtml}</div><div class="research-warnings">${researchWarnings(context.warnings)}</div>`;
  }

  function renderOfficialBoard(board, key, date) {
    const label = {all: '全部龙虎榜', org: '机构龙虎榜', hot_money: '游资龙虎榜'}[key];
    const netField = {all: 'net_value', org: 'org_net_value', hot_money: 'hot_money_item_net_value'}[key];
    const netLabel = {all: '榜单净买入', org: '机构净买入', hot_money: '该游资该股净买入'}[key];
    const valid = board?.trade_date === date && board?.board_type === key;
    const heading = `<summary><strong>${label}</strong><span>${valid ? `${esc(num(board.received_rows, 0))} 条记录 · ${researchStatus(board.status)}` : '分项未取得'}</span></summary>`;
    if (!valid) return `<details class="official-board" open>${heading}<p class="research-note research-warn">${esc(date)} 的该分项尚不可用；不能据此认定无股票上榜。</p></details>`;
    const groups = [['one_day', '1 日榜'], ['three_day', '3 日榜'], ['other', '其他 / 未知区间']].map(([groupKey, groupLabel]) => {
      const rows = list(board.groups?.[groupKey]);
      const header = `<div class="official-group-heading"><h4>${groupLabel}</h4><span>显示 ${rows.length} / ${esc(num(board.group_counts?.[groupKey], 0))} 条</span></div>`;
      if (!rows.length) return `<section>${header}<p class="research-note">本次分项返回该区间 0 条记录。</p></section>`;
      return `<section>${header}<div class="table-scroll"><table class="data-table official-board-table"><thead><tr><th>股票 · 查询同日</th>${key === 'hot_money' ? '<th>游资名称</th>' : ''}<th>涨幅</th><th>${netLabel}（万元）</th><th>涨跌停原因 / 数据说明</th></tr></thead><tbody>${rows.slice(0, 30).map((row) => `<tr><td>${datedStockButton(row.thscode, date, row.name)}</td>${key === 'hot_money' ? `<td class="research-reason">${esc(row.hot_money_name || '未知')}</td>` : ''}<td class="${tone(row.change_pct)}">${esc(percent(row.change_pct))}</td><td class="${tone(row[netField])}">${numeric(row[netField]) ? esc(num(Number(row[netField]) / 10000, 2)) : '—'}</td><td class="research-reason">${esc(row.limit_reason || '原因未提供')}${row.duplicate_record ? '<small class="research-warn">同股同区间多条记录 · 不累计</small>' : ''}${list(row.quality).length ? `<small>${esc(list(row.quality).join('；'))}</small>` : ''}${groupKey === 'other' ? `<small>原始区间 ${esc(num(row.range_days, 0))} 日</small>` : ''}</td></tr>`).join('')}</tbody></table></div></section>`;
    }).join('');
    return `<details class="official-board">${heading}<p class="research-note">${esc(date)} · 上游记录 ${esc(num(board.reported_count, 0))} 条 / 股票 ${esc(num(board.reported_stock_count, 0))} 只 · 本页接收 ${esc(num(board.received_rows, 0))} 条 / 可识别股票 ${esc(num(board.observed_stock_count, 0))} 只 · 各区间最多显示 30 条${board.truncated ? '，部分记录未展开' : ''}。${key === 'hot_money' ? '本页按游资与股票展开，与上游总计范围不同，不据此计算完整覆盖率；未逐行相加游资聚合净额。' : ''}</p>${board.coverage?.definition ? `<p class="research-note">${esc(board.coverage.definition)}</p>` : ''}${groups}<div class="research-warnings">${researchWarnings(board.warnings)}</div></details>`;
  }

  function renderReview(report, job) {
    const signature = displayedReportKey();
    const changed = state.reportSignature !== signature;
    if (changed) {
      state.reportSignature = signature;
      resetComparison();
      renderReportOptions();
      if (!state.reportSaveBusy) text('report-save-status', 'Markdown 保存到本机报告目录，并提供浏览器下载；不会重新请求行情。');
      if (state.reportsMode === state.snapshot?.mode) loadReports();
    }
    if (!report) {
      text('review-status', job?.status === 'running' ? job.message || '正在生成复盘，请稍候…' : job?.status === 'error' ? `复盘失败：${job.message}` : '尚未生成复盘。请选择已收盘的交易日期。');
      $('review-status').classList.toggle('warn', job?.status === 'error');
      if (!changed) return;
      ['review-limit-count', 'review-ladder-value', 'review-seal-rate', 'review-breadth'].forEach((id) => text(id, '—'));
      text('review-limit-note', '实际涨停池');
      text('review-breadth-note', '市场宽度');
      $('review-breadth-note').classList.remove('date-unverified');
      $('review-breadth-note').removeAttribute('title');
      $('review-stocks-body').innerHTML = '<tr><td colspan="7" class="table-empty">生成复盘后显示涨停股分析。</td></tr>';
      $('sectors-body').innerHTML = '<tr><td colspan="7" class="table-empty">暂无板块数据。</td></tr>';
      $('price-trend-body').innerHTML = '<tr><td colspan="9" class="table-empty">生成复盘后显示样本股日线趋势。</td></tr>';
      $('review-trend').innerHTML = '<p class="subtle">暂无梯队数据。</p>';
      $('review-status').classList.toggle('warn', job?.status === 'error');
      return;
    }
    const market = report.market || {};
    const limit = report.limit_up || {};
    const stocks = list(limit);
    const warnings = list(report.warnings);
    const partial = report.status === 'partial' || warnings.length > 0;
    const demo = report.mode === 'demo' || report.source === 'demo' || state.snapshot?.mode === 'demo';
    text('review-status', `${demo ? '【模拟复盘】 ' : ''}${report.date || '未标注日期'} · ${job?.status === 'running' ? job.message || '正在更新' : job?.status === 'error' ? `上次更新失败：${job.message}` : report.status === 'partial' ? '部分数据需核验' : '复盘已生成'}${report.generated_at ? ` · 生成于 ${time(report.generated_at, true)}` : ''}${warnings.length ? ` · ${warnings.join('；')}` : ''}`);
    $('review-status').classList.toggle('warn', partial || demo);
    if (!changed) return;
    text('review-limit-count', num(market.limit_up_count ?? limit.count, 0));
    text('review-limit-note', `炸板 ${num(market.limit_break_count, 0)} · 跌停 ${num(market.limit_down_count, 0)}`);
    $('review-ladder-value').innerHTML = `${esc(num(limit.consecutive_count, 0))}<small> / ${esc(num(limit.max_consecutive, 0))} 板</small>`;
    text('review-seal-rate', numeric(market.seal_rate_pct) ? `${num(market.seal_rate_pct, 1)}%` : '—');
    $('review-breadth').innerHTML = `<span class="positive">${esc(num(market.advancing, 0))}</span><small> / </small><span class="negative">${esc(num(market.declining, 0))}</span>`;
    const inferredDate = market.date_basis === 'inferred_latest_closed_session';
    const breadthNote = `${inferredDate ? '休市快照推断，交易日未直接核验 · ' : ''}成交额 ${amount(market.total_turnover)}${market.snapshot_date ? ` · 快照 ${market.snapshot_date}` : ''}${inferredDate && market.effective_trade_date ? ` · 推断交易日 ${market.effective_trade_date}` : ''}`;
    text('review-breadth-note', breadthNote);
    $('review-breadth-note').title = breadthNote;
    $('review-breadth-note').classList.toggle('date-unverified', inferredDate);
    $('review-stocks-body').innerHTML = stocks.length ? stocks.slice(0, 250).map((row, index) => {
      const quality = Array.isArray(row.quality) ? row.quality.join('；') : '';
      const lowerBound = row.consecutive_lower_bound ? '≥ ' : '';
      return `<tr title="${esc(quality)}"><td><span class="rank ${index < 3 ? 'top' : ''}">${esc(row.rank ?? index + 1)}</span></td><td><span class="stock-name">${esc(row.name || row.stock_name || row.thscode)}</span><span class="stock-code">${esc(row.thscode || row.code)}</span></td><td><span class="score-number">${esc(num(row.score, 1))}</span><span class="sector-source">覆盖 ${esc(num(row.factor_coverage_pct, 0))}%</span></td><td><span class="ladder-badge">${lowerBound}${esc(num(row.consecutive_days ?? row.continue_day_cnt, 0))} 板</span></td><td>${esc(row.limit_up_time || '—')}</td><td>${esc(amount(row.seal_money))}</td><td>${esc(num(row.trend?.last_5_appearances, 0))} / ${esc(num(row.trend?.last_5_observed_days, 0))} 日</td></tr>`;
    }).join('') : '<tr><td colspan="7" class="table-empty">暂无可显示的涨停池；请查看上方数据完整性提示。</td></tr>';
    renderTrend(report);
    renderPriceTrends(report);
    const sectorData = report.sectors || {};
    const sectors = list(sectorData);
    const sectorDateNote = sectorData.date_basis === 'inferred_latest_closed_session' ? ` 休市快照推断，交易日未直接核验；快照 ${list(sectorData.snapshot_dates).join('、') || '未标注'}，暂定归属 ${sectorData.effective_trade_date || report.date || '未知'}。` : '';
    text('sector-method', `${sectorData.method || '以成交活跃度、涨幅和涨停参与度估计板块强弱；不等同于主力净流入。'}${sectorDateNote}`);
    $('sectors-body').innerHTML = sectors.length ? sectors.slice(0, 150).map((sector) => {
      const netAvailable = numeric(sector.net_flow) && !['unavailable', 'missing'].includes(sector.net_flow_status);
      const category = {industry: '行业', cn_concept: '概念', concept: '概念', ths: '概念', sw: '行业'}[sector.category] || sector.category || '—';
      return `<tr><td><span class="stock-name">${esc(sector.name || sector.thscode)}</span><span class="stock-code">${esc(sector.thscode)}</span></td><td>${esc(category)}</td><td class="${tone(sector.price_change_ratio_pct)}">${esc(percent(sector.price_change_ratio_pct))}</td><td>${esc(amount(sector.turnover))}</td><td>${netAvailable ? `<span class="${tone(sector.net_flow)}">${esc(amount(sector.net_flow))}</span>` : '<span class="subtle">未提供</span>'}</td><td>${esc(num(sector.limit_up_count, 0))}<span class="sector-source">连板 ${esc(num(sector.consecutive_count, 0))}</span></td><td><span class="score-number">${esc(num(sector.score, 1))}</span><span class="sector-source">${netAvailable ? '含真实净流入字段' : '量价活跃度代理指标'}</span></td></tr>`;
    }).join('') : '<tr><td colspan="7" class="table-empty">暂无可用的板块数据；数据获取失败不会计为零。</td></tr>';
  }

  function renderPriceTrends(report) {
    const rows = list(report.trend?.price_leaders);
    const coverage = report.trend?.price_coverage || {};
    text('price-trend-coverage', `涨停强度前 ${num(coverage.limit ?? 20, 0)} 只 · 有效 ${num(coverage.available, 0)} / 请求 ${num(coverage.requested, 0)} · 前复权日线（价格不等同当日未复权报价）`);
    $('price-trend-body').innerHTML = rows.length ? rows.map((row) => {
      const positions = [[5, row.above_ma5], [10, row.above_ma10], [20, row.above_ma20]].filter(([, value]) => value !== null && value !== undefined).map(([days, value]) => `${value ? '上' : '下'} MA${days}`);
      const warnings = list(row.warnings).join('；');
      return `<tr title="${esc(warnings)}"><td><span class="stock-name">${esc(row.name || row.thscode)}</span><span class="stock-code">${esc(row.thscode)}</span></td><td>${esc(num(row.close, 2))}</td><td>${esc(num(row.ma5, 2))}</td><td>${esc(num(row.ma10, 2))}</td><td>${esc(num(row.ma20, 2))}</td><td class="${tone(row.return_5d_pct)}">${esc(percent(row.return_5d_pct))}</td><td>${esc(num(row.volume_ratio_5d, 2))} 倍</td><td>${numeric(row.max_drawdown_20d_pct) ? `${esc(num(row.max_drawdown_20d_pct, 2))}%` : '—'}</td><td><span class="subtle">${esc(positions.join(' / ') || '历史不足')}</span></td></tr>`;
    }).join('') : '<tr><td colspan="9" class="table-empty">暂无可用日线趋势。历史不足或获取失败的指标保持空值。</td></tr>';
  }

  function renderTrend(report) {
    const limit = report.limit_up || {};
    const promotion = limit.promotion || {};
    const ladder = list(report.ladder).filter((r) => numeric(r.height)).sort((a, b) => Number(a.height) - Number(b.height));
    const maximum = Math.max(1, ...ladder.map((r) => Number(r.count) || 0));
    const chart = ladder.length ? `<div class="ladder-chart" role="img" aria-label="连板梯队家数">${ladder.slice(0, 10).map((r) => `<div class="ladder-column"><span>${esc(num(r.count, 0))}</span><i style="height:${clamp(Number(r.count) / maximum * 60, 2, 60)}px"></i><small>${esc(num(r.height, 0))}板</small></div>`).join('')}</div>` : '';
    const trends = list(report.trend);
    const recent = trends.slice(-5);
    $('review-trend').innerHTML = `<div class="trend-fact"><span>昨日涨停今日晋级</span><strong>${esc(num(promotion.promoted_count, 0))} / ${esc(num(promotion.previous_count, 0))}</strong></div><div class="trend-fact"><span>涨停延续率</span><strong>${numeric(promotion.rate_pct) ? `${esc(num(promotion.rate_pct, 1))}%` : '—'}</strong></div>${chart}${recent.length ? '<h3 class="small-title">最近交易日走势</h3>' : ''}${recent.map((r) => `<div class="history-item"><span>${esc(r.date)}</span><span>涨停 ${esc(num(r.limit_up_count, 0))}</span><span>${esc(num(r.max_consecutive, 0))} 板</span></div>`).join('')}<p class="trend-note">${esc(report.ladder?.limitation || '延续率为昨日涨停股在今日继续涨停的比例。连板高度依据可用历史和官方字段，缺失项保持未知。')}</p>`;
  }

  const sectorCategory = (category) => ({industry: '行业', cn_concept: '概念', concept: '概念'}[category] || category || '板块');
  const sectorStatus = (status) => ({ready: '数据已读取', partial: '部分数据可用', unavailable: '暂无可用数据', error: '读取失败', not_run: '尚未读取'}[status] || '待核验');
  const selectedSectorCodes = () => state.sectorSelection.map((row) => row.thscode);
  const sameSectorCodes = (a, b) => JSON.stringify([...new Set(list(a))].sort()) === JSON.stringify([...new Set(list(b))].sort());
  const stockCodeButton = (code, name, date) => datedStockButton(code, date, name);

  function sectorBlockedReason(requireReport = true) {
    const snapshot = state.snapshot || {};
    if (!financeConfigured()) return '请先在「同花顺接入」保存你自己的金融 API Key。';
    if (snapshot.mode !== 'live' || snapshot.review?.mode === 'demo') return '请切换到实盘模式后查询同花顺板块。';
    if (requireReport && (!snapshot.review?.date || !snapshot.review_id)) return '请先生成或读取一份已收盘的真实报告。';
    const stamp = new Date(snapshot.now || Date.now()).getTime() + Math.max(0, Date.now() - (state.snapshotReceivedAt || Date.now()));
    const hhmm = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(stamp));
    if (hhmm >= '09:10' && hhmm <= '09:26') return '09:10—09:26 优先竞价；09:27 后可联网查询，已保存数据仍可查看。';
    if (snapshot.api?.rate_limited && (state.apiCooldownDeadline || 0) > Date.now()) return '行情接口正在限流冷却，请在冷却结束后读取。';
    if (snapshot.jobs?.review?.status === 'running' || snapshot.jobs?.evidence?.status === 'running') return '报告正在更新，完成后再读取对应版本的板块数据。';
    if (state.sectorFetchBusy || snapshot.jobs?.sectors?.status === 'running') return '正在读取指定板块，请等待任务完成。';
    if (snapshot.jobs?.llm?.status === 'running') return 'AI 正在处理已提交的内容，请等待本次完成。';
    return '';
  }

  function renderSectorChoices() {
    const selected = new Set(selectedSectorCodes());
    $('sector-search-results').innerHTML = state.sectorMatches.map((row) => `<button type="button" class="sector-choice${selected.has(row.thscode) ? ' selected' : ''}" data-sector-select="${esc(row.thscode)}" aria-pressed="${selected.has(row.thscode)}"><strong>${esc(row.name || row.thscode)}</strong><span>${esc(row.thscode)} · ${esc(sectorCategory(row.category))}</span><small>${selected.has(row.thscode) ? '已选择 ✓' : '选择 ＋'}</small></button>`).join('');
    $('sector-selected').innerHTML = state.sectorSelection.length ? state.sectorSelection.map((row) => `<button type="button" class="sector-selected-chip" data-sector-remove="${esc(row.thscode)}" aria-label="移除 ${esc(row.name || row.thscode)}"><span>${esc(row.name || row.thscode)} <small>${esc(row.thscode)}</small></span><b aria-hidden="true">×</b></button>`).join('') : '<span class="subtle">尚未选择</span>';
  }

  function renderSectorControls() {
    const snapshot = state.snapshot || {};
    const selected = state.sectorSelection.length > 0;
    const reason = sectorBlockedReason();
    const searchReason = sectorBlockedReason(false);
    const job = snapshot.jobs?.sectors || {};
    const llmBusy = snapshot.jobs?.llm?.status === 'running';
    $('sector-finance-setup').classList.toggle('hidden', financeConfigured());
    text('sector-research-date', snapshot.review?.date ? `数据目标日 ${snapshot.review.date}` : '等待收盘报告');
    $('sector-search-button').disabled = Boolean(state.sectorSearchBusy || searchReason);
    $('sector-search-button').title = searchReason || '查询同花顺官方目录，不调用模型。';
    text('sector-search-button', state.sectorSearchBusy ? '正在搜索…' : '搜索板块');
    text('sector-search-status', state.sectorSearchBusy ? '正在读取官方板块目录…' : state.sectorSearchMessage || searchReason || '输入名称后查看官方匹配项，再选择需要读取的板块。没有匹配项时不会猜测其他板块。');
    $('sector-fetch').disabled = Boolean(reason || !selected || state.sectorSearchBusy);
    $('sector-fetch').title = reason || (!selected ? '先搜索并选择板块。' : '只请求行情数据，不调用 AI。');
    text('sector-fetch', state.sectorFetchBusy || job.status === 'running' ? '正在读取板块…' : '只读取板块数据');
    $('sector-cache-refresh').disabled = Boolean(state.sectorCacheBusy || snapshot.mode !== 'live' || !snapshot.review_id);
    const status = state.sectorError || (job.status === 'error' ? `最近一次板块查询未完成：${job.message || '请检查接口后重试。'} 已保存的旧数据仍保留。` : job.status === 'running' ? job.message || '正在读取板块及同行数据，完成后自动展示。' : reason || (state.sectorEvidence?.evidence_id ? '下方为已保存的板块证据；重新取数会生成新的证据版本，已有 AI 回答不会自动更新。' : '按当前报告日期读取指定板块与同行；可只读数据，也可点击右上方按钮一次取数并分析。'));
    text('sector-job-status', status);
    $('sector-job-status').classList.toggle('warn', Boolean(state.sectorError) || job.status === 'error');
    $('llm-generate').disabled = Boolean(llmBusy || state.llmSubmitBusy || (selected && (reason || state.sectorSearchBusy)));
    $('llm-generate').title = selected ? reason || '先读取所选板块，再发送给当前 AI；本次会调用模型。' : '发送已有结构化摘要给所选 AI。';
    text('llm-generate', llmBusy || state.llmSubmitBusy ? '正在分析…' : selected ? '联网取数并 AI 分析 ↗' : '生成文字分析 ↗');
  }

  async function searchSectors(event) {
    event.preventDefault();
    const reason = sectorBlockedReason(false);
    if (reason || state.sectorSearchBusy) { if (reason) toast(reason); return; }
    const query = $('sector-query').value.trim();
    if (!query) { toast('请输入板块名称或完整板块代码。'); $('sector-query').focus(); return; }
    state.sectorSearchBusy = true;
    state.sectorMatches = [];
    state.sectorSearchMessage = '';
    renderSectorChoices(); renderSectorControls();
    try {
      const result = await api('/api/sectors/search', {query});
      if (state.snapshot?.mode !== 'live') return;
      const seen = new Set();
      state.sectorMatches = list(result.matches).filter((row) => row && typeof row.thscode === 'string' && !seen.has(row.thscode) && seen.add(row.thscode));
      const exact = list(result.exact);
      state.sectorSearchMessage = result.message || (state.sectorMatches.length ? `找到 ${state.sectorMatches.length} 个匹配项${exact.length ? `，其中 ${exact.length} 个为精确匹配` : '，没有精确同名项'}。请核对名称后选择，最多 3 个。` : `官方目录没有找到「${query}」。可使用官网目录中的名称或完整代码；不会自动用其他板块替代。`);
    } catch (error) { state.sectorSearchMessage = `搜索未完成：${error.message}`; toast(error.message, true); }
    finally { state.sectorSearchBusy = false; renderSectorChoices(); renderSectorControls(); }
  }

  function toggleSector(code, remove = false) {
    const index = state.sectorSelection.findIndex((row) => row.thscode === code);
    if (index >= 0) state.sectorSelection.splice(index, 1);
    else if (!remove) {
      const row = state.sectorMatches.find((item) => item.thscode === code);
      if (!row) return;
      if (state.sectorSelection.length >= 3) { toast('每次最多选择 3 个板块，请先移除一个。'); return; }
      state.sectorSelection.push({thscode: row.thscode, name: row.name, category: row.category});
    }
    renderSectorChoices(); renderSectorControls(); renderSectorEvidence();
    renderLLM(state.snapshot?.llm || {}, state.snapshot?.jobs?.llm);
  }

  async function fetchSectors() {
    const reason = sectorBlockedReason();
    if (reason || !state.sectorSelection.length) { toast(reason || '请先选择板块。'); return; }
    const body = {codes: selectedSectorCodes(), date: state.snapshot.review.date, review_id: state.snapshot.review_id};
    state.sectorFetchBusy = true; state.sectorError = ''; renderSectorControls();
    try {
      const result = await api('/api/sectors/research', body);
      toast(result.message || '板块数据读取已提交；不调用 AI。');
      await fetchState();
    } catch (error) { state.sectorError = `本次板块读取未提交：${error.message}`; toast(error.message, true); }
    finally { state.sectorFetchBusy = false; renderSectorControls(); }
  }

  async function loadSectorEvidence(force = false) {
    const snapshot = state.snapshot || {};
    if (snapshot.mode !== 'live' || !snapshot.review?.date || !snapshot.review_id) return;
    const reportKey = displayedReportKey();
    const requestKey = `${reportKey}|${snapshot.sector_research?.evidence_id || ''}|${snapshot.jobs?.sectors?.status || ''}|${snapshot.jobs?.llm?.status || ''}`;
    if (!force && requestKey === state.sectorSyncKey) return;
    state.sectorSyncKey = requestKey;
    const generation = state.sectorCacheGeneration = (state.sectorCacheGeneration || 0) + 1;
    state.sectorCacheBusy = true;
    renderSectorControls();
    try {
      const query = new URLSearchParams({date: snapshot.review.date, review_id: snapshot.review_id});
      const evidence = await api(`/api/sectors/research?${query}`);
      if (generation !== state.sectorCacheGeneration || reportKey !== displayedReportKey()) return;
      if (evidence.status === 'not_run') state.sectorEvidence = null;
      else if (evidence.date === snapshot.review.date && evidence.review_id === snapshot.review_id) state.sectorEvidence = evidence;
      else throw new Error('板块证据日期或报告版本不匹配，未展示。');
      state.sectorError = '';
      renderSectorEvidence();
      renderLLM(state.snapshot?.llm || {}, state.snapshot?.jobs?.llm);
    } catch (error) { if (generation === state.sectorCacheGeneration && reportKey === displayedReportKey()) state.sectorError = `读取已保存板块数据失败：${error.message}`; }
    finally { if (generation === state.sectorCacheGeneration) { state.sectorCacheBusy = false; renderSectorControls(); } }
  }

  function syncSectorResearch() {
    const reportKey = displayedReportKey();
    if (state.sectorReportKey !== reportKey) {
      state.sectorReportKey = reportKey;
      state.sectorEvidence = null;
      state.sectorError = '';
      state.sectorSyncKey = '';
      state.sectorCacheGeneration = (state.sectorCacheGeneration || 0) + 1;
      state.sectorCacheBusy = false;
      renderSectorEvidence();
    }
    if (state.snapshot?.mode === 'demo') {
      state.sectorMatches = []; state.sectorSelection = [];
      state.sectorSearchMessage = ''; renderSectorChoices();
    }
    renderSectorControls();
    loadSectorEvidence();
  }

  function renderSectorEvidence() {
    const evidence = state.sectorEvidence;
    const exportLink = $('sector-export');
    const downloadable = Boolean(evidence?.evidence_id && /^[a-f0-9]{24}$/.test(evidence.evidence_id));
    if (downloadable) exportLink.href = `/api/sectors/export?${new URLSearchParams({evidence_id: evidence.evidence_id})}`;
    else exportLink.removeAttribute('href');
    exportLink.classList.toggle('disabled-link', !downloadable);
    exportLink.setAttribute('aria-disabled', String(!downloadable));
    const signature = JSON.stringify([evidence, selectedSectorCodes()]);
    if (signature === state.sectorPreviewSignature) return;
    state.sectorPreviewSignature = signature;
    if (!evidence) { $('sector-evidence-preview').innerHTML = ''; return; }
    const boards = list(evidence.boards);
    const selectedMismatch = state.sectorSelection.length && !sameSectorCodes(selectedSectorCodes(), boards.map((board) => board.thscode));
    const warnings = list(evidence.warnings);
    $('sector-evidence-preview').innerHTML = `<div class="sector-evidence-header"><strong>已保存证据 · ${esc(evidence.date)}</strong><span>${esc(sectorStatus(evidence.status))} · ${esc(time(evidence.generated_at, true))}</span></div>${selectedMismatch ? '<p class="sector-research-note warn">下方数据属于上次读取的板块，与当前选择不同。点击读取后才会更新。</p>' : ''}${boards.map((board) => {
      const coverage = board.coverage || {};
      const statistics = board.statistics || {};
      const trend = board.price_trend || {};
      const members = list(board.members).slice(0, 20);
      const limitUps = list(board.limit_up_members);
      const boardWarnings = list(board.warnings);
      const noLimitUps = coverage.limit_pool_complete === true && coverage.membership_complete === true
        ? '当前完整成分名单与该日完整涨停池未发现匹配项。'
        : '涨停池或成分证据不完整，不能认定没有涨停股。';
      const memberTable = members.length ? `<div class="table-scroll"><table class="data-table sector-members-table"><thead><tr><th>同行股票</th><th>目标日涨幅</th><th>成交额</th><th>当日涨停</th><th>连续板数</th><th>数据情况</th></tr></thead><tbody>${members.map((row) => `<tr><td>${stockCodeButton(row.thscode, row.name, evidence.date)}</td><td class="${tone(row.price_change_ratio_pct)}">${esc(percent(row.price_change_ratio_pct))}</td><td>${esc(amount(row.turnover))}</td><td>${row.limit_up === true ? '是' : row.limit_up === false ? '否' : '未知'}</td><td>${numeric(row.consecutive_days) ? `${row.consecutive_lower_bound ? '≥' : ''}${esc(num(row.consecutive_days, 0))}` : '—'}</td><td><span class="subtle">${esc(row.date_verified === true ? '日期已核验' : '日期待核验')}${row.data_status && row.data_status !== 'ready' ? ` · ${esc(row.data_status === 'partial' ? '部分缺失' : row.data_status === 'unavailable' ? '无可用行情' : row.data_status)}` : ''}</span></td></tr>`).join('')}</tbody></table></div>` : '<p class="sector-research-note">暂无可展示同行行情，缺失不当作零涨幅或零成交额。</p>';
      return `<article class="sector-evidence-card"><div class="sector-board-heading"><div><strong>${esc(board.name || board.thscode)}</strong><span>${esc(board.thscode)} · ${esc(sectorCategory(board.category))}</span></div><span class="pill ${board.status === 'ready' ? 'green' : 'amber'}">${esc(sectorStatus(board.status))}</span></div><div class="sector-board-metrics"><div><span>当前成分总数</span><strong>${esc(num(board.member_count, 0))}</strong></div><div><span>同行行情覆盖</span><strong>${esc(num(coverage.quoted_count, 0))} / ${esc(num(coverage.requested_count, 0))}</strong><small>全体覆盖 ${numeric(coverage.quote_coverage_pct) ? `${esc(num(coverage.quote_coverage_pct, 1))}%` : '未知'}</small></div><div><span>目标日涨停 / 连板</span><strong>${esc(num(statistics.limit_up_count, 0))} / ${esc(num(statistics.consecutive_count, 0))}</strong></div><div><span>板块指数近 5 日</span><strong class="${tone(trend.return_5d_pct)}">${esc(percent(trend.return_5d_pct))}</strong></div></div><p class="sector-research-note">同行有效涨幅 ${esc(num(statistics.valid_change_count, 0))} 只：上涨 ${esc(num(statistics.advancing, 0))} / 下跌 ${esc(num(statistics.declining, 0))} / 平盘 ${esc(num(statistics.unchanged, 0))}；样本均值 ${esc(percent(statistics.mean_change_pct))}，中位数 ${esc(percent(statistics.median_change_pct))}。已知成交额合计 ${esc(amount(statistics.turnover_sum))}（${esc(num(statistics.turnover_known_count, 0))} 只）；主力净流入：未提供。</p><p class="sector-research-note warn">成分依据：当前名单，读取于 ${esc(time(board.members_as_of, true))}；不证明 ${esc(evidence.date)} 当时的历史归属。行情为有限样本${board.quote_scope?.selection === 'code_order' ? '，按代码顺序读取' : ''}。</p>${limitUps.length ? `<div class="sector-limit-up-list"><span>当前成分中，目标日涨停的股票</span><div class="research-codes">${limitUps.slice(0, 20).map((row) => stockCodeButton(row.thscode, row.name, evidence.date)).join('')}</div>${limitUps.length > 20 || coverage.limit_up_truncated ? '<small>名单在此截取展示，不能以未展示推断未涨停。</small>' : ''}</div>` : `<p class="sector-research-note">${esc(noLimitUps)}</p>`}<details class="sector-members"><summary>查看同行明细 · 展示 ${members.length} 只${list(board.members).length > 20 || coverage.truncated ? '（已截取）' : ''}</summary>${memberTable}</details>${boardWarnings.length ? `<div class="research-warnings"><details><summary>数据边界与缺失说明（${boardWarnings.length}）</summary><ul>${boardWarnings.map((warning) => `<li>${esc(warning)}</li>`).join('')}</ul></details></div>` : ''}</article>`;
    }).join('')}${warnings.length ? `<div class="research-warnings"><ul>${warnings.map((warning) => `<li>${esc(warning)}</li>`).join('')}</ul></div>` : ''}<p class="sector-research-note">来源：同花顺金融 API。以上为指定板块证据，尚不表示已生成新的 AI 回答；模型使用的证据见回答前的说明。</p>`;
  }

  function renderLLMSource(result, job) {
    let message = '尚未生成 AI 回答。输入问题、选择板块不会自动调用模型。';
    let warning = false;
    if (job?.status === 'running' || state.llmSubmitBusy) message = '正在处理已提交的提问和板块选择；完成后显示本次使用的数据来源。';
    else if (result && typeof result === 'object') {
      const resultCodes = list(result.sector_codes);
      message = result.sector_evidence_id ? `本回答已使用同花顺指定板块证据：${resultCodes.join('、') || '代码未标注'} · 目标日 ${result.review_date || '未标注'}${result.sector_generated_at ? ` · 取数 ${time(result.sector_generated_at, true)}` : ''}。` : '本回答使用原有结构化摘要，没有读取本次指定板块的完整证据。';
      if (!sameSectorCodes(resultCodes, selectedSectorCodes())) { message += ' 当前所选板块与这份回答不同，需重新点击分析。'; warning = true; }
      if (result.question !== undefined && String(result.question) !== $('llm-question').value.trim()) { message += ' 输入框提问已变化，已有回答不会随之更新。'; warning = true; }
      if (state.sectorEvidence?.evidence_id && result.sector_evidence_id && state.sectorEvidence.evidence_id !== result.sector_evidence_id) { message += ' 下方板块预览已经更新；这份回答仍基于先前证据。'; warning = true; }
      if (result.scope === 'review' && !matchingReviewAI()) { message += ' 回答属于其他报告日期或版本。'; warning = true; }
    }
    text('llm-source-status', message);
    $('llm-source-status').classList.toggle('warn', warning);
  }

  async function submitLLM() {
    if (state.llmSubmitBusy || state.snapshot?.jobs?.llm?.status === 'running') return;
    if (!state.snapshot?.llm?.configured) { showPage('settings'); toast('请先保存 AI 接口与模型。', true); return; }
    if (!state.snapshot?.review && !list(state.snapshot?.auction?.rows).length && !state.snapshot?.stocks?.analysis) { toast('请先采集竞价数据、查询个股或生成收盘复盘报告。', true); return; }
    const codes = selectedSectorCodes();
    const reason = codes.length ? sectorBlockedReason() : '';
    if (reason) { toast(reason); return; }
    const body = {question: $('llm-question').value.trim(), provider: state.llmProvider, ...(state.snapshot?.review?.date ? {review_date: state.snapshot.review.date, review_id: state.snapshot.review_id} : {}), ...(codes.length ? {sector_codes: codes, fetch_sectors: true} : {})};
    state.llmSubmitBusy = true; state.sectorError = ''; renderSectorControls();
    renderLLMSource(state.snapshot?.llm?.result, state.snapshot?.jobs?.llm);
    try {
      const result = await api('/api/llm', body);
      toast(result.message || (codes.length ? '已提交：先读取指定板块，再调用所选 AI。' : '已提交 AI 分析。'));
      await fetchState();
    } catch (error) { toast(error.message, true); }
    finally { state.llmSubmitBusy = false; renderSectorControls(); renderLLM(state.snapshot?.llm || {}, state.snapshot?.jobs?.llm); }
  }

  function renderLLMConfig(llm, jobs) {
    const profiles = list(llm.profiles);
    const active = llm.active_provider || 'custom';
    const profile = profiles.find((item) => item.id === active) || {id: active, label: llm.label || 'AI', model: llm.model, base_url: llm.base_url, configured: llm.configured};
    const changed = state.llmProvider !== active;
    const signature = JSON.stringify([active, profile.model, profile.base_url]);
    if (changed) { state.llmDirty = false; $('llm-key').value = ''; }
    if (profiles.length) {
      const optionsSignature = JSON.stringify(profiles.map((item) => [item.id, item.label]));
      for (const id of ['llm-provider', 'llm-review-provider']) {
        if ($(id).dataset.signature !== optionsSignature) {
          $(id).replaceChildren(...profiles.map((item) => { const option = document.createElement('option'); option.value = item.id; option.textContent = item.label; return option; }));
          $(id).dataset.signature = optionsSignature;
        }
      }
    }
    if (!state.llmSwitching) { $('llm-provider').value = active; $('llm-review-provider').value = active; }
    if (changed || (!state.llmDirty && signature !== state.llmSignature)) {
      $('llm-model').value = profile.model || '';
      $('llm-base').value = profile.base_url || '';
      $('llm-model-options').replaceChildren(...list(profile.models).map((model) => { const option = document.createElement('option'); option.value = model; return option; }));
      state.llmSignature = signature;
    }
    $('llm-base').readOnly = active !== 'custom';
    if (changed) document.querySelector('.llm-advanced').open = active === 'custom';
    $('llm-key').placeholder = profile.has_key ? '当前服务已保存 Key；留空保留，输入可替换' : `输入 ${profile.label} 的 API Key`;
    text('llm-config-status', profile.configured ? `${profile.label} · 已配置` : `${profile.label} · 待配置`);
    $('llm-config-status').className = `pill ${profile.configured ? 'green' : 'amber'}`;
    text('llm-ready', `${profile.label} · ${profile.configured ? profile.model || '已配置' : '请先保存 Key'}`);
    text('llm-provider-help', active === 'custom' ? '此配置只用于所填服务。变更服务地址时请重新提供该服务的 Key，保存不调用模型。' : `${profile.label} 的官方地址与默认模型已预设，填 Key 并保存即可。切换服务不会转移另一家的密钥。`);
    const test = llm.connection_test;
    text('llm-test-status', jobs.llm_test?.status === 'running' ? '正在检查认证与模型列表…' : jobs.llm_test?.status === 'error' ? `连接测试失败：${jobs.llm_test.message}` : test && test.provider === active ? test.message || (test.ok ? '连接正常，尚未生成回答。' : '连接未通过，请检查配置。') : '测试仅检查认证和模型列表，不生成回答。修改后请先保存再测试。');
    state.llmProvider = active;
  }

  async function switchLLM(provider, source) {
    if (state.llmSwitching || provider === state.llmProvider) return;
    state.llmSwitching = true;
    $('llm-provider').disabled = true; $('llm-review-provider').disabled = true;
    try {
      const ok = await mutate('/api/llm-select', {provider}, '已切换 AI 服务');
      if (ok) { $('llm-key').value = ''; state.llmDirty = false; }
    } finally {
      state.llmSwitching = false;
      $('llm-provider').disabled = false; $('llm-review-provider').disabled = false;
      renderLLMConfig(state.snapshot?.llm || {}, state.snapshot?.jobs || {});
    }
  }

  function renderLLM(llm, job) {
    const result = llm.result;
    renderLLMSource(result, job);
    let output;
    if (job?.status === 'running') output = job.message || 'AI 正在分析复盘报告，请稍候…';
    else if (llm.error || job?.status === 'error') output = `AI 分析失败：${llm.error || job.message}`;
    else if (typeof result === 'string' && result) output = result;
    else if (result && typeof result === 'object') output = result.content || result.text || result.analysis || JSON.stringify(result, null, 2);
    else output = '生成复盘并配置模型后，可请求文字分析。输出保留原始数据边界与缺失项。';
    if (result && typeof result === 'object' && job?.status !== 'running' && job?.status !== 'error') {
      const reportScope = result.scope === 'review' ? ` · 报告 ${result.review_date || '日期未标注'}` : '';
      const staleReview = result.scope === 'review' && !matchingReviewAI() ? '这份研判来自其他日期或版本的报告，不能附入当前报告。可重新生成当前报告的文字分析。\n\n' : '';
      output = `${result.mode === 'demo' ? '【模拟数据分析】' : '【实盘数据研究】'} ${result.label || llm.label || ''}${result.model ? ` · ${result.model}` : ''}${reportScope}${result.generated_at ? ` 生成于 ${time(result.generated_at, true)}` : ''}\n\n${staleReview}${output}`;
    }
    text('llm-output', output);
  }

  function renderErrors(snapshot) {
    const errors = [...list(snapshot.errors), ...list(snapshot.auction?.summary?.warnings)];
    $('errors-panel').classList.toggle('hidden', !errors.length);
    $('errors-list').innerHTML = errors.slice(-12).map((error) => `<li>${esc(typeof error === 'string' ? error : [error.time || error.at, error.message || error.error || JSON.stringify(error)].filter(Boolean).join(' · '))}</li>`).join('');
  }

  function tick() {
    const limited = state.snapshot?.mode !== 'demo' && state.snapshot?.api?.rate_limited === true;
    const remaining = Math.max(0, Math.ceil(((state.apiCooldownDeadline || 0) - Date.now()) / 1000));
    $('api-cooldown').classList.toggle('hidden', !limited);
    text('api-cooldown-text', remaining > 0 ? `约 ${num(remaining, 0)} 秒后可恢复请求；同一 Key 的后台请求同步暂停，已有观测继续保留。` : '冷却时间已到，等待服务确认；已有观测继续保留。');
    renderOfficialControls();
    renderBacktestControls();
    renderSectorControls();
    text('clock', time(new Date().toISOString()));
    const auction = state.snapshot?.auction || {};
    const latest = auction.summary?.last_received_at || list(auction.rows).reduce((last, row) => row.updated_at && (!last || row.updated_at > last) ? row.updated_at : last, '');
    const age = latest ? Math.max(0, (Date.now() - new Date(latest).getTime()) / 1000) : null;
    const demo = state.snapshot?.mode === 'demo';
    $('freshness-value').innerHTML = demo ? '<span style="font-size:21px">模拟时间</span>' : `${esc(num(age, 0))}<small> 秒</small>`;
    text('freshness-note', latest ? `${demo ? '模拟观测' : '最新本地接收'} ${time(latest)}${demo ? '' : ' · 非交易所时延'}` : '基于最近一次本地采集时间');
  }

  function setConnection(label, mode) {
    text('connection', label);
    $('connection').className = `connection ${mode || ''}`;
  }

  async function fetchState() {
    if (polling) return;
    polling = true;
    try {
      render(await api('/api/state'));
      if (!state.connected) setConnection('轮询连接', 'polling');
      $('notice').classList.add('hidden');
    } catch (error) {
      setConnection('服务未连接', '');
      text('notice', `无法连接本地服务：${error.message} 请确认启动窗口保持运行。系统将自动重试。`);
      $('notice').classList.remove('hidden');
    } finally { polling = false; }
  }

  function startPolling() { if (!pollTimer) pollTimer = setInterval(fetchState, 5000); }
  function connectEvents() {
    if (!window.EventSource) { startPolling(); return; }
    const events = new EventSource('/api/events');
    events.onopen = () => {
      state.connected = true;
      setConnection('实时推送已连接', 'online');
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      $('notice').classList.add('hidden');
    };
    events.addEventListener('state', (event) => {
      try { render(JSON.parse(event.data)); }
      catch { text('notice', '实时消息无法读取，正在重新获取状态。'); $('notice').classList.remove('hidden'); fetchState(); }
    });
    events.onerror = () => { state.connected = false; setConnection('正在恢复连接', 'polling'); startPolling(); };
    window.addEventListener('pagehide', () => { events.close(); if (pollTimer) clearInterval(pollTimer); });
  }

  document.addEventListener('click', async (event) => {
    const nav = event.target.closest('[data-tab],[data-open]');
    if (nav) { showPage(nav.dataset.tab || nav.dataset.open); return; }
    const sectorSelect = event.target.closest('[data-sector-select]');
    if (sectorSelect) { toggleSector(sectorSelect.dataset.sectorSelect); return; }
    const sectorRemove = event.target.closest('[data-sector-remove]');
    if (sectorRemove) { toggleSector(sectorRemove.dataset.sectorRemove, true); return; }
    const stockQuery = event.target.closest('[data-query-stock]');
    if (stockQuery) {
      if (Object.prototype.hasOwnProperty.call(stockQuery.dataset, 'queryDate')) $('stock-query-date').value = stockQuery.dataset.queryDate;
      await queryStock(stockQuery.dataset.queryStock, stockQuery); return;
    }
    const stockAdd = event.target.closest('[data-add-stock]');
    if (stockAdd) { await addWatchlist(stockAdd.dataset.addStock, stockAdd); return; }
    const stockRemove = event.target.closest('[data-remove-stock]');
    if (stockRemove) { await mutate('/api/watchlist/remove', {code: stockRemove.dataset.removeStock}, '已取消自选来源', stockRemove); return; }
    const stockAction = event.target.closest('[data-stock-action]');
    if (stockAction?.dataset.stockAction === 'add-current') { await addWatchlist($('stock-code').value || state.snapshot?.stocks?.analysis?.thscode, stockAction); return; }
    if (stockAction?.dataset.stockAction === 'refresh-trends') { await mutate('/api/trends/refresh', {}, '趋势池刷新已提交', stockAction); return; }
    const follow = event.target.closest('[data-observation-follow]');
    if (follow) { setObservationFollow(!state.observationFollow); return; }
    const filter = event.target.closest('[data-filter]');
    if (filter) {
      state.filter = filter.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach((el) => el.classList.toggle('selected', el === filter));
      renderAuction(); return;
    }
    const row = event.target.closest('[data-symbol]');
    if (row) { selectStock(row.dataset.symbol); return; }
    const action = event.target.closest('[data-action]');
    if (!action) return;
    const name = action.dataset.action;
    if (name === 'llm') { await submitLLM(); return; }
    if (['start', 'prepare', 'review'].includes(name) && !financeConfigured()) { showPage('finance'); toast('请先保存你自己的同花顺金融 API Key，再读取行情。'); return; }
    const body = name === 'review' ? {date: $('review-date').value || undefined} : {};
    await mutate(`/api/${name}`, body, {prepare: '已开始准备关注池', start: '已启动监测', stop: '已停止监测', review: '已开始生成复盘', demo: '已切换到明确标记的模拟演示', llm: '已提交 AI 分析'}[name], action);
    if (name === 'demo') showPage('auction');
  });

  document.addEventListener('keydown', (event) => {
    if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('[data-symbol]')) { event.preventDefault(); selectStock(event.target.dataset.symbol); }
  });
  $('symbol-search').addEventListener('input', (event) => { state.search = event.target.value.trim().toLowerCase(); renderAuction(); });
  $('sector-search-form').addEventListener('submit', searchSectors);
  $('sector-fetch').addEventListener('click', fetchSectors);
  $('sector-cache-refresh').addEventListener('click', () => loadSectorEvidence(true));
  $('llm-question').maxLength = 2000;
  $('llm-question').addEventListener('input', () => renderLLMSource(state.snapshot?.llm?.result, state.snapshot?.jobs?.llm));
  $('watchlist').addEventListener('input', () => { state.watchlistSettingsDirty = true; });
  for (const id of ['auto-review', 'review-time']) $(id).addEventListener('input', () => {
    state.runtimeScheduleDirty = true;
    state.runtimeScheduleRevision = (state.runtimeScheduleRevision || 0) + 1;
  });
  $('review-date').addEventListener('input', () => { state.reviewDateTouched = true; });
  $('report-save').addEventListener('click', saveMarkdown);
  $('report-load').addEventListener('click', loadSavedReport);
  $('report-history-refresh').addEventListener('click', loadReports);
  $('report-history-date').addEventListener('change', renderReportControls);
  $('comparison-baseline').addEventListener('change', () => { resetComparison(); renderReportControls(); });
  $('comparison-run').addEventListener('click', compareReports);
  $('diagnostics-refresh').addEventListener('click', loadDiagnostics);
  $('official-enrich').addEventListener('click', enrichReport);
  $('backtest-refresh').addEventListener('click', loadBacktest);
  $('backtest-run').addEventListener('click', runBacktest);
  $('backtest-session').addEventListener('change', renderBacktestSession);
  $('backtest-stock-search').addEventListener('input', renderBacktestSession);
  $('backtest-import-button').addEventListener('click', () => { if (!backtestBlockedReason()) $('backtest-import-file').click(); });
  $('backtest-import-file').addEventListener('change', importBacktestFile);
  $('daily-audit-refresh').addEventListener('click', loadDailyAudits);
  $('daily-audit-date').addEventListener('change', loadDailyAudit);
  $('daily-audit-checkpoint').addEventListener('change', renderDailyAuditRows);
  window.addEventListener('hashchange', () => showPage(location.hash.slice(1) || 'auction'));
  $('toggle-errors').addEventListener('click', () => { const closed = $('errors-list').classList.toggle('hidden'); text('toggle-errors', closed ? '展开' : '收起'); });
  $('weights-editor').addEventListener('input', (event) => {
    const target = event.target;
    const key = target.dataset.weightRange || target.dataset.weight;
    if (!key) return;
    const selector = target.dataset.weightRange ? '[data-weight]' : '[data-weight-range]';
    const partner = [...$('weights-editor').querySelectorAll(selector)].find((el) => (el.dataset.weight || el.dataset.weightRange) === key);
    if (partner) partner.value = target.value;
    updateWeightTotal();
  });

  $('finance-form').addEventListener('submit', saveFinance);
  $('finance-test').addEventListener('click', testFinance);
  $('finance-exit-demo').addEventListener('click', exitFinanceDemo);
  $('finance-refresh').addEventListener('click', () => loadFinanceStatus(true));
  $('api-key').addEventListener('input', () => { state.financeError = ''; renderFinance(); });
  $('stock-query-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    await queryStock($('stock-code').value, event.submitter);
  });
  $('watchlist-add-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    await addWatchlist($('watchlist-batch').value, event.submitter);
  });
  async function restartService() {
    const button = $('service-restart');
    if (!button || button.disabled) return;
    const confirmed = window.confirm('重启本机服务以加载新的代码与配置？\n\n· 会先停止当前采集（原始批次已落盘，重启后可用同日恢复）\n· 09:10—09:26 或后台任务运行时会拒绝\n· 约 3—5 秒不可用，随后页面自动重连并刷新');
    if (!confirmed) return;
    button.disabled = true;
    text('service-restart-status', '正在请求重启…');
    try {
      const result = await api('/api/service/restart', {});
      text('service-restart-status', result.message || '服务正在重启…');
    } catch (error) {
      text('service-restart-status', `重启未执行：${error.message}`);
      button.disabled = false;
      return;
    }
    const deadline = Date.now() + 30000;
    const poll = async () => {
      try {
        const response = await fetch('/api/health', {cache: 'no-store'});
        if (response.ok) {
          const health = await response.json();
          if (health && health.ok) { text('service-restart-status', '服务已重启，正在刷新页面…'); window.location.reload(); return; }
        }
      } catch (error) { /* 重启期间连接失败属正常，继续等待 */ }
      if (Date.now() > deadline) {
        text('service-restart-status', '等待超时：请双击 启动系统.cmd 重新启动，再刷新页面。');
        button.disabled = false;
        return;
      }
      window.setTimeout(poll, 1000);
    };
    window.setTimeout(poll, 1200);
  }

  $('service-restart').addEventListener('click', restartService);
  $('runtime-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const watchlist = [...new Set($('watchlist').value.split(/[\s,，;；]+/).map((value) => value.trim().toUpperCase()).filter(Boolean))];
    if ($('universe').value === 'watchlist' && !watchlist.length) { toast('自选股池至少需要一个股票代码。', true); return; }
    const savedWatchlist = state.snapshot?.config?.watchlist || [];
    const added = watchlist.filter((code) => !savedWatchlist.includes(code));
    if (added.length) { $('watchlist-batch').value = added.join(', '); showPage('stocks'); toast('新增代码需要核验，已带入个股查询页的批量自选输入框；采集设置尚未保存。'); return; }
    const reviewTime = $('review-time').value;
    if (!/^15:[1-5]\d$/.test(reviewTime)) { toast('自动收盘时间须为上海时间 15:10—15:59。', true); return; }
    const scheduleRevision = state.runtimeScheduleRevision || 0;
    const ok = await mutate('/api/config', {universe: $('universe').value, poll_seconds: Number($('poll-seconds').value), watchlist, auto_review: $('auto-review').checked, review_time: reviewTime}, '采集设置已保存', event.submitter);
    if (ok) {
      state.watchlistSettingsDirty = false;
      if ((state.runtimeScheduleRevision || 0) === scheduleRevision) state.runtimeScheduleDirty = false;
      renderReviewSchedule(state.snapshot || {});
    }
  });
  $('weights-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const weights = Object.fromEntries([...document.querySelectorAll('[data-weight]')].map((el) => [el.dataset.weight, Number(el.value) / 100]));
    if (Object.values(weights).some((value) => !Number.isFinite(value) || value < 0) || !Object.values(weights).some((value) => value > 0)) { toast('权重必须为非负数字，且至少一个因子权重大于零。', true); return; }
    const total = Object.values(weights).reduce((a, b) => a + b, 0);
    const normalized = Object.fromEntries(Object.entries(weights).map(([key, value]) => [key, value / total]));
    const ok = await mutate('/api/config', {weights: normalized}, '因子权重已保存', event.submitter);
    if (ok) initializeWeights(state.snapshot?.config?.weights || normalized);
  });
  $('llm-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = {provider: state.llmProvider, base_url: $('llm-base').value.trim(), model: $('llm-model').value.trim()};
    if ($('llm-key').value.trim()) body.api_key = $('llm-key').value.trim();
    const ok = await mutate('/api/llm-config', body, '当前 AI 配置已保存', event.submitter);
    if (ok) { $('llm-key').value = ''; state.llmDirty = false; state.llmSignature = ''; renderLLMConfig(state.snapshot?.llm || {}, state.snapshot?.jobs || {}); }
  });
  for (const id of ['llm-key', 'llm-model', 'llm-base']) $(id).addEventListener('input', () => { state.llmDirty = true; });
  for (const id of ['llm-provider', 'llm-review-provider']) $(id).addEventListener('change', (event) => switchLLM(event.target.value, event.target));
  $('llm-test').addEventListener('click', async (event) => {
    if (state.llmDirty) { toast('请先保存当前 Key 或模型修改，再测试连接。', true); return; }
    await mutate('/api/llm-test', {provider: state.llmProvider}, '已提交连接检查', event.currentTarget);
  });

  $('review-date').value = dateAtShanghai();
  initializeWeights();
  showPage(location.hash.slice(1) || 'auction');
  tick();
  setInterval(tick, 1000);
  fetchState();
  startPolling();
  connectEvents();
})();
