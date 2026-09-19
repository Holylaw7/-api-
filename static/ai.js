(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = value ?? ''; };
  const list = (value) => Array.isArray(value) ? value : [];
  const state = {snapshot: null, active: '', formDirty: false, profileSignature: '', messagesSignature: '', contextSignature: '', pending: false, polling: false, online: false};
  const emptyConversation = $('conversation').cloneNode(true);
  const notes = {deepseek: 'DeepSeek 官方 API', openai: 'OpenAI 官方 API', custom: '兼容 Chat Completions'};
  const icons = {deepseek: 'D', openai: '✳', custom: '＋'};
  const currentProfile = () => list(state.snapshot?.ai?.profiles).find((p) => p.id === state.snapshot?.ai?.active_provider);
  const running = () => state.snapshot?.job?.status === 'running';
  function toast(message, error = false) {
    const item = document.createElement('div');
    item.className = `toast${error ? ' error' : ''}`;
    item.textContent = message;
    $('toasts').append(item);
    setTimeout(() => item.remove(), error ? 9000 : 5000);
  }
  async function api(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const options = {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: {Accept: 'application/json'}};
      if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.headers['X-Local-App'] = 'auction-ai'; options.body = JSON.stringify(body); }
      const response = await fetch(path, options);
      let result;
      try { result = await response.json(); } catch { throw new Error('本机服务的响应无法读取，请重新启动 AI 助手。'); }
      if (!response.ok || result.ok === false) throw new Error(result.message || result.error || `请求失败（${response.status}）`);
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('本机服务响应超时，请稍后重试。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function updateControls() {
    const busy = state.pending || running();
    document.querySelectorAll('[data-provider]').forEach((button) => { button.disabled = busy || !state.online; });
    for (const id of ['save-profile', 'test-profile', 'clear-chat', 'load-context']) $(id).disabled = busy || !state.online;
    $('send-message').disabled = busy || !state.online || !currentProfile()?.configured;
    $('include-market').disabled = busy || !state.snapshot?.context?.summary;
    text('send-message', running() ? '正在处理…' : '发送问题 ↑');
    text('send-context-note', $('include-market').checked ? `将附带预览中的盘面摘要，发送给 ${currentProfile()?.label || '当前 AI'}` : '本次不附加盘面摘要；会发送当前服务的对话历史');
  }
  function hydrateProfile(profile, force = false) {
    const signature = JSON.stringify([profile.id, profile.model, profile.base_url]);
    const changed = state.active !== profile.id;
    if (changed) { $('api-key').value = ''; $('include-market').checked = false; state.formDirty = false; }
    if (force || changed || (!state.formDirty && signature !== state.profileSignature)) {
      $('model').value = profile.model || '';
      $('base-url').value = profile.base_url || '';
      state.profileSignature = signature;
    }
    $('base-url').readOnly = profile.id !== 'custom';
    $('model-options').replaceChildren(...list(profile.models).map((model) => { const option = document.createElement('option'); option.value = model; return option; }));
    $('api-key').placeholder = profile.has_key ? '已保存；留空保留，输入新 Key 可替换' : `粘贴 ${profile.label} 的 API Key`;
    $('advanced').open = profile.id === 'custom' ? true : changed ? false : $('advanced').open;
    text('base-help', profile.id === 'custom' ? '填写兼容服务的 Base URL，通常以 /v1 结尾。变更地址后请重新填写该服务的 Key。' : '官方服务地址已预设，只需填写对应的 Key。');
    text('key-help', profile.has_key ? '当前服务已配置 Key。输入框始终留空，保存时留空会保留已有 Key。' : 'Key 保存在本机用户凭据目录，页面不会回显。');
    text('model-summary', profile.model || '请填写模型名称');
    text('key-status', profile.configured ? '已配置' : '待配置');
    $('key-status').className = `badge${profile.configured ? ' ready' : ''}`;
    text('active-description', `当前使用 ${profile.label}。切换只改变此后发出的请求。`);
    text('chat-title', profile.label);
    text('chat-model', `${profile.model || '未选择模型'} · ${profile.configured ? '已配置 Key，可以开始对话' : '先在左侧保存对应 API Key'}`);
    state.active = profile.id;
  }
  function renderProviders(profiles, active) {
    const signature = JSON.stringify(profiles.map((p) => [p.id, p.label, p.configured]));
    if ($('provider-options').dataset.signature !== signature) {
      $('provider-options').replaceChildren(...profiles.map((profile) => {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'provider-choice'; button.dataset.provider = profile.id;
        const icon = document.createElement('span'); icon.className = 'provider-icon'; icon.textContent = icons[profile.id] || 'AI';
        const labels = document.createElement('span');
        const name = document.createElement('span'); name.className = 'provider-name'; name.textContent = profile.label;
        const note = document.createElement('span'); note.className = 'provider-note'; note.textContent = `${notes[profile.id] || '模型服务'} · ${profile.configured ? '已配置' : '待配置'}`;
        const tick = document.createElement('span'); tick.className = 'provider-tick'; tick.setAttribute('aria-hidden', 'true');
        labels.append(name, note); button.append(icon, labels, tick); return button;
      }));
      $('provider-options').dataset.signature = signature;
    }
    document.querySelectorAll('[data-provider]').forEach((button) => { const selected = button.dataset.provider === active; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); button.querySelector('.provider-tick').textContent = selected ? '✓' : ''; });
  }
  function renderMessages(snapshot) {
    const messages = list(snapshot.messages);
    const signature = JSON.stringify([snapshot.ai?.active_provider, messages]);
    if (signature === state.messagesSignature) return;
    const nearBottom = $('conversation').scrollHeight - $('conversation').scrollTop - $('conversation').clientHeight < 100;
    if (!messages.length) $('conversation').replaceChildren(...[...emptyConversation.childNodes].map((child) => child.cloneNode(true)));
    else $('conversation').replaceChildren(...messages.map((message) => {
      const article = document.createElement('article'); article.className = `message ${message.role === 'user' ? 'user' : 'assistant'}`;
      const meta = document.createElement('div'); meta.className = 'message-meta';
      const name = document.createElement('strong'); name.textContent = message.role === 'user' ? '你' : `${currentProfile()?.label || 'AI'}${message.model ? ` · ${message.model}` : ''}`;
      const stamp = document.createElement('span');
      if (message.created_at) { const date = new Date(message.created_at); stamp.textContent = Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'}); }
      meta.append(name, stamp);
      const content = document.createElement('div'); content.className = 'message-content'; content.textContent = message.content || '';
      article.append(meta, content);
      const warnings = list(message.warnings);
      if (warnings.length) { const warning = document.createElement('div'); warning.className = 'message-warning'; warning.textContent = warnings.join('\n'); article.append(warning); }
      return article;
    }));
    if (nearBottom || !state.messagesSignature || state.active !== snapshot.ai?.active_provider) $('conversation').scrollTop = $('conversation').scrollHeight;
    state.messagesSignature = signature;
  }
  function render(snapshot) {
    const previousProvider = state.active;
    state.snapshot = snapshot;
    const ai = snapshot.ai || {};
    renderProviders(list(ai.profiles), ai.active_provider);
    const profile = currentProfile();
    if (profile) hydrateProfile(profile);
    if (previousProvider !== ai.active_provider) state.messagesSignature = '';
    renderMessages(snapshot);
    const job = snapshot.job || {};
    text('job-status', job.status === 'running' ? job.message || 'AI 正在处理，请稍候…' : job.status === 'error' ? job.message || '请求失败，请检查连接后重试。' : profile?.configured ? '准备就绪 · Enter 换行，Ctrl / ⌘ + Enter 发送' : '保存当前服务商的 API Key 后即可提问');
    $('job-status').className = `job-status ${['running', 'error'].includes(job.status) ? job.status : ''}`;
    const test = snapshot.connection_test;
    if (test && test.provider === ai.active_provider) { text('test-status', test.message || (test.ok ? '认证成功。模型列表可读取，不代表已生成回答。' : '连接失败，请检查 Key 与配置。')); $('test-status').className = `test-status ${test.ok ? 'success' : 'error'}`; }
    else { text('test-status', '测试连接只检查认证与模型列表，不生成回答。'); $('test-status').className = 'test-status'; }
    const context = snapshot.context;
    const contextSignature = JSON.stringify(context);
    if (contextSignature !== state.contextSignature) {
      $('include-market').checked = false;
      $('context-details').classList.toggle('hidden', !context?.summary);
      text('context-preview', context?.summary ? JSON.stringify(context.summary, null, 2) : '');
      text('context-status', context?.summary ? `${{local_service: '来自竞价研究台', local_report: '本机历史报告'}[context.source] || '本机摘要'}${context.cached ? '（历史缓存，非实时）' : ''} · 数据日期 ${context.date || context.summary?.review?.date || '以预览为准'} · ${context.mode === 'demo' || context.summary?.mode === 'demo' ? '模拟数据' : '请核对数据时点'}。加载后不会自动刷新；勾选才发送。${context.message || ''}` : '未加载。独立聊天不需要启动竞价研究台。');
      state.contextSignature = contextSignature;
    }
    updateControls();
  }
  async function fetchState() {
    if (state.polling) return;
    state.polling = true;
    try { const snapshot = await api('/api/state'); state.online = true; render(snapshot); text('connection', '本机已连接'); $('connection').className = 'connection online'; $('notice').classList.add('hidden'); }
    catch (error) { state.online = false; text('connection', '连接中断'); $('connection').className = 'connection offline'; text('notice', `${error.message} 请双击「启动AI助手.cmd」，页面会自动重新连接。`); $('notice').classList.remove('hidden'); updateControls(); }
    finally { state.polling = false; }
  }
  async function mutate(path, body, onSuccess) {
    if (state.pending || running()) return false;
    state.pending = true; updateControls();
    try { const result = await api(path, body); if (result.started === false) { toast(result.message || '已有任务正在处理，请稍候。'); } else if (onSuccess) onSuccess(result); await fetchState(); return result.started !== false; }
    catch (error) { toast(error.message, true); return false; }
    finally { state.pending = false; updateControls(); }
  }
  $('provider-options').addEventListener('click', async (event) => {
    const choice = event.target.closest('[data-provider]');
    if (!choice || choice.disabled || choice.dataset.provider === state.active) return;
    await mutate('/api/profiles/select', {provider: choice.dataset.provider}, () => { $('api-key').value = ''; state.formDirty = false; toast('已切换 AI，后续问题将使用所选服务。'); });
  });
  for (const id of ['api-key', 'model', 'base-url']) $(id).addEventListener('input', () => { state.formDirty = true; });
  $('profile-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = {provider: state.active, model: $('model').value.trim(), base_url: $('base-url').value.trim()};
    if ($('api-key').value.trim()) body.api_key = $('api-key').value.trim();
    await mutate('/api/profiles/save', body, () => { $('api-key').value = ''; state.formDirty = false; state.profileSignature = ''; toast('当前服务配置已保存。'); });
  });
  $('test-profile').addEventListener('click', async () => {
    if (state.formDirty) { toast('请先保存 Key 或模型修改，再测试已保存的配置。', true); return; }
    await mutate('/api/profiles/test', {provider: state.active}, () => toast('已开始检查认证与模型列表。'));
  });
  $('load-context').addEventListener('click', async () => { await mutate('/api/context/load', {}, () => toast('已请求读取本机盘面摘要，请核对预览。')); });
  $('include-market').addEventListener('change', updateControls);
  $('conversation').addEventListener('click', (event) => { const example = event.target.closest('[data-prompt]'); if (example) { $('message').value = example.dataset.prompt; $('message').focus(); } });
  $('chat-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = $('message').value.trim();
    if (!message) return;
    if (!currentProfile()?.configured) { toast('请先在左侧保存当前服务商的 API Key。', true); $('api-key').focus(); return; }
    const includeMarket = $('include-market').checked && Boolean(state.snapshot?.context?.summary);
    await mutate('/api/chat', {message, include_market: includeMarket, provider: state.active}, () => { $('message').value = ''; $('include-market').checked = false; });
  });
  $('message').addEventListener('keydown', (event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); if (!$('send-message').disabled) $('chat-form').requestSubmit(); } });
  $('clear-chat').addEventListener('click', async () => { await mutate('/api/chat/clear', {provider: state.active}, () => toast('已清空当前服务的对话。')); });
  fetchState();
  const poll = setInterval(fetchState, 2000);
  window.addEventListener('pagehide', () => { clearInterval(poll); $('api-key').value = ''; });
})();
