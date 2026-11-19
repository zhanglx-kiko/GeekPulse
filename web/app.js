'use strict';

// Query parameters are never an authentication source.
if (location.search) history.replaceState(null, '', location.pathname + location.hash);

(() => {
  const $ = (id) => document.getElementById(id);
  const formField = (form, name) => form.elements.namedItem(name);
  const html = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const number = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
  const integer = (value) => number(value) === null ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  const percent = (value) => number(value) === null ? '—' : Number(value).toFixed(1) + '%';
  const seconds = (value) => number(value) === null ? '—' : (Number(value) / 1000).toFixed(Number(value) < 10000 ? 2 : 1) + ' s';
  const time = (value, short = false) => {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', short ? { hour: '2-digit', minute: '2-digit' } : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };
  const statusNames = { queued: '排队中', running: '运行中', passed: '不降智', failed: '降智', error: '请求错误', cancelled: '已取消', completed: '未评分' };
  const statusClass = (status) => Object.hasOwn(statusNames, status) ? status : 'error';
  const statusPill = (status) => `<span class="status-pill ${statusClass(status)}">${html(statusNames[status] || status)}</span>`;
  const terminal = (status) => ['passed', 'failed', 'error', 'cancelled', 'completed'].includes(status);
  const loading = () => '<div class="loading-state"><span class="spinner"></span>正在加载…</div>';
  const empty = (message) => `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">⌁</span><p>${html(message)}</p></div>`;
  const state = { bootstrap: null, dashboard: null, hours: 1, groupFilter: 'all', tab: 'overview', scope: 'mine', probes: [], probeNameFilter: '', autoProbeName: null, currentRun: null, pollTimer: null, editingProbe: null, probeDrafts: new Map(), modalOpeners: new Map(), historyReturn: null, probeSaved: false, probeEditSequence: 0, probeModelSequence: 0, probeModels: null, probeSaving: null, runCache: new Map(), runSummaries: new Map(), deletedRuns: new Set(), deletedProbes: new Set(), recordsVersion: 0, confirmation: null, authExpired: false, dashboardSequence: 0, probesSequence: 0, mineSequence: 0, artworksSequence: 0, detailSequence: 0, submitting: false, pendingRequest: null, recovering: false, polling: false, probeRequests: new Map() };
  function isRunDeleted(run) { return state.deletedRuns.has(String(run.id)) || (run.probe_id != null && state.deletedProbes.has(String(run.probe_id))); }
  let refreshPromise = null;
  let toastTimer = null;
  let sessionEpoch = 0;
  const pendingControllers = new Set();

  class ApiError extends Error {
    constructor(message, status = 0, code = '') { super(message); this.status = status; this.code = code; }
  }

  function readSession() {
    let user = null;
    try { user = JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { /* Invalid user storage means no known user. */ }
    return { access: localStorage.getItem('auth_token'), refresh: localStorage.getItem('refresh_token'), expires: Number(localStorage.getItem('token_expires_at')), userId: user?.id == null ? null : String(user.id) };
  }

  function sameUser(a, b) { return a.userId === b.userId; }
  function assertSession(epoch, original) {
    const current = readSession();
    if (epoch !== sessionEpoch || state.authExpired) throw new ApiError('登录状态已变化。', 401);
    if (!current.access || !sameUser(current, original)) {
      showAuth('登录状态已变化，请重新打开实验室。');
      throw new ApiError('登录状态已变化。', 401);
    }
  }
  function peerToken(snapshot, failedToken) {
    const current = readSession();
    return sameUser(current, snapshot) && current.access && current.refresh && current.expires > Date.now() && (current.refresh !== snapshot.refresh || current.access !== failedToken) ? current.access : null;
  }

  async function refreshAccess(failedToken) {
    if (refreshPromise) return refreshPromise;
    const initial = readSession();
    const epoch = sessionEpoch;
    if (!initial.refresh || !initial.access) throw new ApiError('登录已过期，请重新登录。', 401);
    const execute = async (unlocked) => {
      assertSession(epoch, initial);
      const peer = peerToken(initial, failedToken);
      if (peer) return peer;
      const snapshot = readSession();
      if (!sameUser(snapshot, initial) || !snapshot.refresh || !snapshot.access) throw new ApiError('登录状态已变化，请重新进入。', 401);
      const controller = new AbortController();
      pendingControllers.add(controller);
      const timeout = setTimeout(() => controller.abort(), 30000);
      const deadline = Date.now() + 31000;
      let responseStatus = 0;
      try {
        const response = await fetch('/api/v1/auth/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: snapshot.refresh }), credentials: 'same-origin', cache: 'no-store', signal: controller.signal, referrerPolicy: 'no-referrer' });
        responseStatus = response.status;
        const payload = await response.json();
        assertSession(epoch, initial);
        const tokens = payload.data;
        if (!response.ok || payload.code !== 0 || !tokens?.access_token || !tokens?.refresh_token || !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0) throw new ApiError('登录已过期，请重新登录。', response.status);
        const current = readSession();
        if (!sameUser(current, snapshot) || current.refresh !== snapshot.refresh || !current.access) {
          const newer = peerToken(snapshot, failedToken);
          if (newer) return newer;
          throw new ApiError('登录状态已变化，请重新进入。', 401);
        }
        localStorage.setItem('auth_token', tokens.access_token);
        localStorage.setItem('token_expires_at', String(Date.now() + tokens.expires_in * 1000));
        localStorage.setItem('refresh_token', tokens.refresh_token);
        return tokens.access_token;
      } catch (error) {
        const until = unlocked && responseStatus >= 400 && responseStatus < 500 ? deadline : Date.now() + 1000;
        while (Date.now() < until) {
          assertSession(epoch, initial);
          const current = readSession();
          if (!sameUser(current, snapshot) || !current.refresh || !current.access) break;
          const newer = peerToken(snapshot, failedToken);
          if (newer) return newer;
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
        throw error;
      } finally { clearTimeout(timeout); pendingControllers.delete(controller); }
    };
    const pending = navigator.locks ? navigator.locks.request('sub2api-auth-token-refresh', () => execute(false)) : execute(true);
    refreshPromise = pending;
    try { return await pending; } finally { if (refreshPromise === pending) refreshPromise = null; }
  }

  function showAuth(message = '登录已过期，请重新登录。') {
    sessionEpoch++;
    state.authExpired = true;
    pendingControllers.forEach((controller) => controller.abort());
    pendingControllers.clear();
    stopPolling(true);
    $('workspace').hidden = true;
    $('initialLoading').hidden = true;
    $('authGate').hidden = false;
    $('authMessage').textContent = message;
    $('userName').textContent = '';
    state.runCache.clear(); state.runSummaries.clear(); state.confirmation = null; state.recordsVersion++;
    clearTimeout(state.updateTimer); state.updateView = null;
    state.probeDrafts.clear(); state.historyReturn = null;
    if (typeof syncPreviews === 'function') syncPreviews();
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
    document.querySelectorAll('input[name="api_key"]').forEach((input) => { input.value = ''; });
  }

  async function api(path, options = {}) {
    const original = readSession();
    const epoch = sessionEpoch;
    if (!original.access || state.authExpired || (state.bootstrap && original.userId !== String(state.bootstrap.user.id))) {
      showAuth('请登录后重新打开实验室。');
      throw new ApiError('请先登录。', 401);
    }
    const method = options.method || 'GET';
    const body = options.body == null ? undefined : JSON.stringify(options.body);
    const controller = new AbortController();
    pendingControllers.add(controller);
    const send = async (token) => {
      assertSession(epoch, original);
      const timeout = setTimeout(() => controller.abort(), 35000);
      try {
        return await fetch((options.original ? '/api/v1' : '/model-lab/api') + path, { method, body, headers: { Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json' } : {}) }, credentials: 'same-origin', cache: 'no-store', referrerPolicy: 'no-referrer', signal: controller.signal });
      } finally { clearTimeout(timeout); }
    };
    try {
      let response;
      try {
        response = await send(original.access);
        if (response.status === 401) {
          try { response = await send(await refreshAccess(original.access)); }
          catch { showAuth(); throw new ApiError('登录已过期，请重新登录。', 401); }
        }
      } catch (error) {
        assertSession(epoch, original);
        if (error instanceof ApiError) throw error;
        throw new ApiError(method === 'GET' ? '连接中断，请刷新重试。' : '未收到确认，请先查看记录或配置，再决定是否重试。');
      }
      assertSession(epoch, original);
      if (response.status === 401) { showAuth(); throw new ApiError('登录已过期，请重新登录。', 401); }
      let payload;
      try { payload = await response.json(); }
      catch { assertSession(epoch, original); throw new ApiError('服务返回异常，请稍后重试。', response.status); }
      assertSession(epoch, original);
      if (!response.ok || (options.original && payload.code !== 0)) throw new ApiError(payload.error?.message || (options.original ? payload.message : '') || '请求失败，请稍后重试。', response.status, payload.error?.code);
      return options.original ? payload.data : payload;
    } finally { pendingControllers.delete(controller); }
  }

  function notice(message) { $('globalNotice').textContent = message; $('globalNotice').hidden = !message; }
  function toast(message) { clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false; toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4000); }
  function showError(target, error, retryAction) {
    if (state.authExpired) return;
    target.innerHTML = `<div class="error-state"><p>${html(error.message)}</p>${retryAction ? `<button class="button secondary small" data-action="${html(retryAction)}">重试</button>` : ''}</div>`;
  }
  function formError(id, message) { $(id).textContent = message; $(id).hidden = !message; }
  function busy(button, value) { if (button) { button.disabled = value; button.setAttribute('aria-busy', String(value)); } }
  function testLocked() { return state.submitting || state.recovering || Boolean(state.pendingRequest && !state.pendingRequest.retryAllowed) || Boolean(state.currentRun && !terminal(state.currentRun.status)); }
  function syncTestButtons() {
    busy($('submitTest'), testLocked());
    $('submitTest').textContent = state.pendingRequest?.retryAllowed ? '重新提交' : '开始测试';
    document.querySelectorAll('[data-action="probe-run"]').forEach((button) => {
      const pending = state.probeRequests.get(String(button.dataset.id));
      busy(button, Boolean(pending) || !state.probes.find((probe) => String(probe.id) === button.dataset.id)?.has_key);
    });
    document.querySelectorAll('[data-probe-state]').forEach((node) => {
      const pending = state.probeRequests.get(String(node.dataset.probeState));
      node.innerHTML = pending ? `${pending.run ? `<button class="button ghost small" data-action="detail" data-id="${html(pending.run.id)}">${html(statusNames[pending.run.status] || '查看结果')}</button>` : '<span>等待提交确认</span>'}${pending.error ? `<span class="form-error">${html(pending.error)}</span><button class="button secondary small" data-action="recover-probe" data-id="${html(pending.probeId)}" ${pending.recovering ? 'disabled' : ''}>查询原任务</button>${pending.retryAllowed ? `<button class="button secondary small" data-action="retry-probe" data-id="${html(pending.probeId)}">重新提交</button>` : ''}` : ''}` : '';
    });
  }
  function receiptKey(kind) { return `${state.bootstrap.receipt_namespace}-${kind}`; }
  function savePendingRequest(request) {
    state.pendingRequest = request;
    // Only the request receipt and owner survive reload. Never store the key or prompt.
    try {
      if (request) sessionStorage.setItem(receiptKey('pending'), JSON.stringify({ requestId: request.requestId, probeId: request.probeId || null, userId: String(state.bootstrap.user.id) }));
      else sessionStorage.removeItem(receiptKey('pending'));
    } catch { /* In-memory protection remains when browser storage is unavailable. */ }
    syncTestButtons();
  }
  function pendingNotice(message) {
    $('currentResult').innerHTML = `<div class="error-state"><p>${html(message)}</p><button class="button secondary small" data-action="recover-request">查询原任务</button>${state.pendingRequest?.retryAllowed && state.pendingRequest.probeId ? '<button class="button secondary small" data-action="retry-probe">重新提交</button>' : ''}</div>`;
  }
  async function recoverRequest() {
    if (!state.pendingRequest || state.recovering || state.submitting) return;
    state.recovering = true; syncTestButtons();
    const pending = state.pendingRequest;
    try {
      const run = await api('/requests/' + encodeURIComponent(pending.requestId));
      if (state.pendingRequest !== pending) return;
      pending.retryAllowed = false; startRun(run);
    } catch (error) {
      if (state.authExpired || state.pendingRequest !== pending) return;
      pending.retryAllowed = error.code === 'request_not_found';
      pendingNotice(pending.retryAllowed ? '尚未查到请求。可继续查询，或重新提交；沿用原编号，不会重复创建任务。' : error.message);
    } finally { state.recovering = false; syncTestButtons(); }
  }
  async function restorePendingRequest() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(receiptKey('pending')) || 'null');
      if (saved?.userId === String(state.bootstrap.user.id) && /^[0-9a-f-]{36}$/i.test(saved.requestId)) {
        // Migrate an in-flight receipt from the previous single-task UI without another POST.
        if (saved.probeId) {
          state.probeRequests.set(String(saved.probeId), { requestId: saved.requestId, probeId: String(saved.probeId), retryAllowed: false });
          sessionStorage.removeItem(receiptKey('pending'));
        } else {
          savePendingRequest({ requestId: saved.requestId, probeId: null, retryAllowed: false });
          await selectTab('test'); await recoverRequest();
        }
      }
      const probes = JSON.parse(sessionStorage.getItem(receiptKey('probes')) || 'null');
      if (probes?.userId === String(state.bootstrap.user.id) && Array.isArray(probes.requests)) {
        for (const savedProbe of probes.requests) if (savedProbe.probeId && /^[0-9a-f-]{36}$/i.test(savedProbe.requestId)) state.probeRequests.set(String(savedProbe.probeId), { requestId: savedProbe.requestId, probeId: String(savedProbe.probeId), retryAllowed: false });
      }
      saveProbeRequests();
      await Promise.all([...state.probeRequests.keys()].map(recoverProbeRequest));
    } catch { /* Invalid local receipt is never an authentication source. */ }
  }
  function saveProbeRequests() {
    try {
      if (state.probeRequests.size) sessionStorage.setItem(receiptKey('probes'), JSON.stringify({ userId: String(state.bootstrap.user.id), requests: [...state.probeRequests.values()].map(({ requestId, probeId }) => ({ requestId, probeId })) }));
      else sessionStorage.removeItem(receiptKey('probes'));
    } catch { /* The in-memory locks remain active. */ }
    syncTestButtons();
  }
  async function recoverProbeRequest(id) {
    const pending = state.probeRequests.get(String(id));
    if (!pending || pending.recovering || pending.submitting || state.authExpired) return;
    pending.recovering = true; syncTestButtons();
    try {
      const run = await api('/requests/' + encodeURIComponent(pending.requestId));
      if (state.probeRequests.get(String(id)) !== pending) return;
      const saved = state.probes.find(item => String(item.id) === String(id));
      if (saved) saved.identity_locked = true;
      pending.error = ''; pending.retryAllowed = false; pending.run = run; pollProbeRun(String(id));
    } catch (error) {
      if (!state.authExpired && state.probeRequests.get(String(id)) === pending) { pending.retryAllowed = error.code === 'request_not_found'; pending.error = pending.retryAllowed ? '尚未查到请求，可查询或沿用原编号重新提交。' : error.message; }
    } finally { pending.recovering = false; syncTestButtons(); }
  }
  async function pollProbeRun(id) {
    const pending = state.probeRequests.get(String(id));
    if (!pending?.run || pending.polling || state.authExpired) return;
    pending.polling = true;
    try {
      const run = await api('/runs/' + encodeURIComponent(pending.run.id));
      if (state.probeRequests.get(String(id)) !== pending || state.authExpired) return;
      pending.run = run; pending.error = '';
      if (terminal(run.status)) {
        state.runCache.set(String(run.id), run); clearTimeout(pending.timer); state.probeRequests.delete(String(id)); saveProbeRequests();
        toast(`${run.model || '探测'} · ${run.kind === 'drawing' && run.status === 'completed' ? '已完成' : statusNames[run.status] || run.status}`); loadDashboard();
      } else pending.timer = setTimeout(() => pollProbeRun(String(id)), 2000);
    } catch (error) {
      if (!state.authExpired && state.probeRequests.get(String(id)) === pending) {
        pending.error = error.message; clearTimeout(pending.timer);
        if (error.code === 'run_not_found') { state.probeRequests.delete(String(id)); saveProbeRequests(); }
      }
    } finally { pending.polling = false; syncTestButtons(); }
  }

  async function selectTab(tab) {
    if (tab === 'admin' && state.bootstrap?.user.role !== 'admin') return;
    state.tab = tab;
    document.querySelectorAll('[data-tab]').forEach((button) => { const selected = button.dataset.tab === tab; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; });
    document.querySelectorAll('.panel').forEach((panel) => { panel.hidden = panel.id !== 'panel-' + tab; });
    for (const [node, preview] of previews) if (node.closest('.panel')?.hidden) { preview.visible = false; syncPreview(node, preview); }
    if (tab === 'test') await loadMine();
    if (tab === 'artworks') await loadArtworks();
    if (tab === 'admin') await loadProbes();
  }

  function metric(label, value, className = 'result-metric') { return `<div class="${className}"><span class="metric-label">${html(label)}</span><span class="value">${html(value)}</span></div>`; }
  function latestModelMetrics(model) {
    const latest = [...(model.history || [])].reverse().find((run) => run.kind !== 'drawing' && ['passed', 'failed', 'completed'].includes(run.status));
    if (!latest) return '';
    return `${number(latest.duration_ms) !== null ? `<span class="model-duration">最近耗时 <strong>${seconds(latest.duration_ms)}</strong></span>` : ''}${number(latest.total_tokens) !== null ? `<span class="model-tokens">最近 Token <strong>${integer(latest.total_tokens)}</strong></span>` : ''}`;
  }
  function historyStrip(model) {
    const runs = (model.history || []).filter((run) => ['passed', 'failed', 'completed'].includes(run.status) && !isRunDeleted(run)).slice(-60);
    if (!runs.length) return '<div class="history-none">暂无测试</div>';
    return `<button class="history-strip" data-action="history" data-id="${html(model.probe_id)}" aria-label="查看 ${html(model.model)} 最近 ${runs.length} 次运行">${runs.map((run) => `<span class="history-mark ${statusClass(run.status)}" title="${html(time(run.created_at) + ' · ' + (statusNames[run.status] || run.status))}"></span>`).join('')}</button>`;
  }
  function artworkCard(run, compact = false) {
    if (isRunDeleted(run)) return '';
    state.runSummaries.set(String(run.id), run);
    const card = `<button class="artwork-open" data-action="detail" data-id="${html(run.id)}" aria-label="查看 ${html(run.model)} ${html(time(run.created_at))} 绘图"><div class="artwork-preview" ${run.has_artifact ? `data-preview="${html(run.id)}"` : ''}>${run.has_artifact ? '<span class="preview-note">加载预览…</span>' : `<span class="artwork-error">${html(run.error_message ? '生成错误 · 查看详情' : ['queued', 'running'].includes(run.status) ? '正在生成' : '暂无作品 · 查看详情')}</span>`}<span class="preview-cover" aria-hidden="true"></span></div><div class="artwork-caption">${compact ? '' : `<span class="run-model">${html(run.model)}</span>`}<span class="run-time">${html(time(run.created_at))}</span></div></button>`;
    return compact ? card : `<article class="artwork-card">${card}${run.can_delete ? `<div class="artwork-actions"><button class="button ghost small record-delete" data-action="run-delete" data-id="${html(run.id)}" aria-label="删除 ${html(run.model)} ${html(time(run.created_at))} 的作品">删除</button></div>` : ''}</article>`;
  }
  function multiplierBadge(value) {
    const multiplier = number(value);
    return multiplier === null || multiplier < 0 ? '' : `<span class="multiplier-badge">参考倍率 ×${html(multiplier)}</span>`;
  }
  function renderDashboard() {
    const data = state.dashboard;
    const available = data.groups || [];
    if (state.groupFilter !== 'all' && !available.some((group) => String(group.id) === state.groupFilter)) state.groupFilter = 'all';
    $('groupFilter').innerHTML = '<option value="all">全部监控分组</option>' + available.map((group) => `<option value="${html(String(group.id))}">${html(group.name)}</option>`).join('');
    $('groupFilter').value = state.groupFilter;
    const selected = available.find((group) => String(group.id) === state.groupFilter);
    const summary = (selected ? selected.summary : data.summary) || {};
    $('updatedAt').textContent = '更新于 ' + time(data.updated_at);
    $('summaryScope').textContent = (selected?.name || '全部监控分组') + ' · 近 24 小时答题';
    const items = [['综合不降智率', percent(summary.pass_rate), `${integer(summary.scored)} 次有效判定`], ['平均耗时', seconds(summary.avg_duration_ms), '近 24 小时'], ['近期检测 Token', integer(summary.total_tokens), '近 24 小时']];
    $('summaryGrid').innerHTML = items.map(([label, value, note]) => `<div class="summary-card"><span class="metric-label">${html(label)}</span><strong class="summary-value">${html(value)}</strong>${note ? `<span class="summary-note">${html(note)}</span>` : ''}</div>`).join('');
    const groups = available.filter((group) => (state.groupFilter === 'all' || String(group.id) === state.groupFilter) && (group.models?.length || group.drawing_models?.length));
    $('groupGrid').innerHTML = groups.length ? groups.map((group) => {
      const stats = group.stats || {};
      const models = (group.models || []).filter((model) => !state.deletedProbes.has(String(model.probe_id)));
      const drawings = (group.drawing_models || []).filter((model) => !state.deletedProbes.has(String(model.probe_id))).map((model) => ({ ...model, history: (model.history || []).filter((run) => ['passed', 'failed', 'completed'].includes(run.status) && run.has_artifact && !isRunDeleted(run)) }));
      return `<article class="card group-card"><div class="group-head"><div><div class="group-name"><h2>${html(group.name)}</h2>${multiplierBadge(group.display_multiplier)}</div><div class="group-meta">${integer(models.length)} 个答题探测${drawings.length ? ` · ${integer(drawings.length)} 个图片探测` : ''}</div></div></div>${models.length ? `<div class="group-metrics">${metric('渠道成功率', percent(stats.success_rate), 'group-metric')}${metric('平均首 T', seconds(stats.avg_ttft_ms), 'group-metric')}<span class="metric-label">近 ${state.hours} 小时</span></div><div class="group-body"><h3 class="group-section-title">模型检测 <span class="muted">最近 60 轮</span></h3>${models.map((model) => `<div class="model-row"><div class="model-title"><span class="model-name">${html(model.model)}</span><span class="model-effort">${html(model.effort || 'default')}</span><div class="model-summary"><span class="model-rate">不降智率 <strong>${percent(model.stats?.pass_rate)}</strong></span>${latestModelMetrics(model)}</div></div><div class="model-meta"><span>${html(model.name)}${model.enabled ? '' : ' · 已暂停'}</span><span>${integer(model.stats?.scored)} 次有效判定</span>${number(model.display_multiplier) !== number(group.display_multiplier) ? multiplierBadge(model.display_multiplier) : ''}</div>${historyStrip(model)}</div>`).join('')}</div>` : ''}${drawings.length ? `<div class="drawing-section"><h3 class="group-section-title">图片检测 <span class="muted">各探测最近 60 轮</span></h3>${drawings.map((model) => `<div class="drawing-probe"><div class="drawing-heading"><div><strong>${html(model.model)}</strong><span class="model-effort">${html(model.effort || 'default')}</span><span class="muted">${html(model.name)}${model.enabled ? '' : ' · 已暂停'}</span></div>${model.history?.length ? `<div class="carousel-controls"><button class="icon-button" data-action="carousel-prev" data-id="${html(model.probe_id)}" aria-label="查看较新图片">‹</button><button class="icon-button" data-action="carousel-next" data-id="${html(model.probe_id)}" aria-label="查看较早图片">›</button></div>` : ''}</div>${model.history?.length ? `<div class="artwork-carousel" data-carousel="${html(model.probe_id)}" tabindex="0" role="region" aria-label="${html(model.model)} 最近 60 轮图片，可左右滚动">${model.history.map((run) => artworkCard(run, true)).join('')}</div>` : empty('暂无图片')}</div>`).join('')}</div>` : ''}</article>`;
    }).join('') : empty('暂无探测配置');
    observePreviews($('groupGrid'));
  }
  async function loadDashboard() {
    const sequence = ++state.dashboardSequence;
    busy($('refreshDashboard'), true);
    if (!state.dashboard) $('groupGrid').innerHTML = loading();
    try { const data = await api('/dashboard?hours=' + state.hours); if (sequence !== state.dashboardSequence) return; state.dashboard = data; renderDashboard(); notice(''); }
    catch (error) { if (sequence === state.dashboardSequence) { if (state.dashboard) notice(error.message); else showError($('groupGrid'), error, 'dashboard'); } }
    finally { if (sequence === state.dashboardSequence) busy($('refreshDashboard'), false); }
  }

  async function loadTraffic() {
    busy($('refreshTraffic'), true);
    $('trafficContent').innerHTML = loading();
    try {
      const settings = await api('/settings/public', { original: true });
      if (!settings.channel_monitor_enabled || settings.channel_monitor_mode !== 'v2') {
        $('trafficSection').hidden = true;
        $('trafficContent').replaceChildren();
        return;
      }
      $('trafficSection').hidden = false;
      $('trafficStatus').textContent = '近 24 小时';
      const snapshot = await api('/channel-monitor-v2/snapshot?range=24h', { original: true });
      const metrics = snapshot.metrics || {};
      const coverage = snapshot.coverage || {};
      // The original service returns ratios in 0..1. Counts may be intentionally redacted.
      const ratio = (value) => number(value) === null ? '—' : percent(Number(value) * 100);
      $('trafficUpdatedAt').textContent = '近 24 小时 · ' + (coverage.data_through ? '数据截至 ' + time(coverage.data_through) : '数据时间未提供');
      $('trafficContent').innerHTML = `<div class="traffic-metrics">${metric('请求成功率', ratio(metrics.success_rate), 'group-metric')}${metric('输入缓存占比', ratio(metrics.cache_rate), 'group-metric')}${metric('TTFT P50', seconds(metrics.ttft?.p50_ms), 'group-metric')}${metric('TTFT P95', seconds(metrics.ttft?.p95_ms), 'group-metric')}</div>${coverage.coverage_complete === false || coverage.bootstrap?.active ? '<p class="traffic-coverage">统计区间尚未覆盖完整</p>' : ''}`;
    } catch (error) {
      if (!state.authExpired) { $('trafficSection').hidden = false; $('trafficStatus').textContent = '暂不可用'; }
      showError($('trafficContent'), error, 'traffic');
    }
    finally { busy($('refreshTraffic'), false); }
  }

  function runRows(runs) {
    runs = runs.filter((run) => !isRunDeleted(run));
    for (const run of runs) state.runSummaries.set(String(run.id), run);
    if (!runs.length) return empty('暂无测试记录');
    return `<div class="recent-run-list">${runs.map((run) => `<div class="recent-run-item"><button class="recent-run" data-action="detail" data-id="${html(run.id)}"><span class="recent-run-header"><span class="run-model">${html(run.model)}</span><span class="model-effort" aria-label="推理等级">${html(run.effort || 'default')}</span>${statusPill(run.status)}</span><span class="recent-run-meta"><span class="run-time">${html(time(run.created_at))}</span>${terminal(run.status) && number(run.duration_ms) !== null ? `<span>耗时 ${seconds(run.duration_ms)}</span>` : ''}</span><span class="recent-run-tokens">${number(run.input_tokens) !== null ? `<span>输入 Token <strong>${integer(run.input_tokens)}</strong></span>` : '<span>输入 Token 未返回</span>'}${number(run.output_tokens) !== null ? `<span>输出 Token <strong>${integer(run.output_tokens)}</strong></span>` : '<span>输出 Token 未返回</span>'}</span></button>${run.can_delete ? `<button class="button ghost small record-delete" data-action="run-delete" data-id="${html(run.id)}" aria-label="删除 ${html(run.model)} ${html(time(run.created_at))} 的测试记录">删除</button>` : ''}</div>`).join('')}</div>`;
  }
  async function loadMine() {
    const sequence = ++state.mineSequence;
    busy($('refreshMine'), true);
    $('myRuns').innerHTML = loading();
    try { const data = await api('/runs?scope=mine&kind=reasoning'); if (sequence === state.mineSequence) $('myRuns').innerHTML = runRows(data.runs || []); }
    catch (error) { if (sequence === state.mineSequence) showError($('myRuns'), error, 'mine'); }
    finally { if (sequence === state.mineSequence) busy($('refreshMine'), false); }
  }
  async function loadArtworks() {
    const sequence = ++state.artworksSequence;
    busy($('refreshArtworks'), true);
    $('artworkGrid').innerHTML = loading();
    try { const data = await api('/runs?scope=' + state.scope + '&kind=drawing'); if (sequence !== state.artworksSequence) return; const runs = (data.runs || []).filter((run) => !isRunDeleted(run)); $('artworkGrid').innerHTML = runs.length ? runs.map((run) => artworkCard(run)).join('') : empty('暂无绘图记录'); observePreviews($('artworkGrid')); }
    catch (error) { if (sequence === state.artworksSequence) showError($('artworkGrid'), error, 'artworks'); }
    finally { if (sequence === state.artworksSequence) busy($('refreshArtworks'), false); }
  }

  function artifactCanvas(artifact) {
    // Inspect dimensions as text only. Generated markup never enters the parent DOM.
    const svg = artifact.match(/<svg\b[^>]*>/i)?.[0] || '';
    const viewBox = svg.match(/\bviewBox\s*=\s*["']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)/i);
    const svgWidth = number(viewBox?.[1]) || number(svg.match(/\bwidth\s*=\s*["']([\d.]+)(?:px)?["']/i)?.[1]);
    const svgHeight = number(viewBox?.[2]) || number(svg.match(/\bheight\s*=\s*["']([\d.]+)(?:px)?["']/i)?.[1]);
    const hasLayout = /<(?:main|header|footer|div|section)\b/i.test(artifact);
    const styles = [...artifact.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style\s*>/gi)].map((match) => match[1]).join(' ');
    const widths = [...styles.matchAll(/(?:^|[;{\s])(?:min-|max-)?width\s*:\s*(\d+(?:\.\d+)?)px\b/gi)].map((match) => Number(match[1]));
    const heights = [...styles.matchAll(/(?:^|[;{\s])(?:min-)?height\s*:\s*(\d+(?:\.\d+)?)px\b/gi)].map((match) => Number(match[1]));
    const width = Math.min(4096, Math.max(320, svgWidth || 960, ...widths) + (hasLayout ? 48 : 0));
    const ratioHeight = svgWidth && svgHeight ? (width - (hasLayout ? 48 : 0)) * svgHeight / svgWidth : width * 2 / 3;
    const height = Math.min(4096, Math.max(240, ratioHeight + (hasLayout ? 200 : 0), ...heights));
    return { width: Math.ceil(width), height: Math.ceil(height) };
  }
  function fitPreview(node, preview) {
    if (!preview.frame || preview.large) return;
    const canvas = preview.canvas || artifactCanvas(preview.artifact);
    const width = node.clientWidth || 330, height = node.clientHeight || 180;
    const scale = Math.min(width / canvas.width, height / canvas.height);
    Object.assign(preview.frame.style, { position: 'absolute', width: canvas.width + 'px', height: canvas.height + 'px', maxWidth: 'none', left: (width - canvas.width * scale) / 2 + 'px', top: (height - canvas.height * scale) / 2 + 'px', transformOrigin: 'top left', transform: `scale(${scale})` });
  }
  function artifactFrame(artifact, title, large = false) {
    const frame = document.createElement('iframe');
    frame.className = large ? 'artifact-large' : '';
    frame.title = title;
    frame.setAttribute('sandbox', '');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.setAttribute('loading', 'lazy');
    frame.tabIndex = large ? 0 : -1;
    const dark = document.documentElement?.classList?.contains('dark') || false;
    const palette = typeof getComputedStyle === 'function' && document.documentElement ? getComputedStyle(document.documentElement) : null;
    const track = palette?.getPropertyValue('--surface-2').trim() || (dark ? '#0f172a' : '#f9fafb');
    const thumb = palette?.getPropertyValue('--scrollbar').trim() || (dark ? '#475569' : '#d1d5db');
    const text = palette?.getPropertyValue('--text').trim() || (dark ? '#f1f5f9' : '#111827');
    const scrollbars = `:root{color-scheme:${dark ? 'dark' : 'light'}}*{scrollbar-width:thin;scrollbar-color:${thumb} ${track}}::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:${track}}::-webkit-scrollbar-thumb{background:${thumb};border-radius:8px}`;
    // These styles stay inside the opaque sandbox and cannot affect the parent page.
    // Thumbnails fit their viewport; full previews retain scrolling to reach all content.
    const viewport = large
      ? `html:root{overflow:auto!important;scrollbar-width:thin!important;scrollbar-color:${thumb} ${track}!important}`
      : 'html:root{width:100%!important;height:100%!important;margin:0!important;overflow:hidden!important}html:root body{min-height:100%!important;overflow:hidden!important}html:root body>svg{display:block;width:100%!important;height:auto!important;max-height:100vh!important}html:root,html:root *{scrollbar-width:none!important}html:root::-webkit-scrollbar,html:root *::-webkit-scrollbar{display:none!important;width:0!important;height:0!important}';
    // SMIL has no CSS pause property. Reduced motion removes only animation nodes
    // from the already sanitized string; generated content never enters our DOM.
    const content = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      ? artifact.replace(/<(animateTransform|animate)\b[^>]*>[\s\S]*?<\/\1\s*>|<(?:animateTransform|animate)\b[^>]*\/\s*>/gi, '') : artifact;
    frame.srcdoc = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src data:; base-uri \'none\'; form-action \'none\'"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{margin:0;min-height:100%;background:' + track + ';color:' + text + '}svg{max-width:100%}*{box-sizing:border-box}</style>' + content + '<style>' + scrollbars + viewport + '@media(prefers-reduced-motion:reduce){html:root *,html:root *::before,html:root *::after{animation:none!important;transition:none!important}}</style>';
    return frame;
  }
  async function runDetail(id) {
    const cached = state.runCache.get(String(id));
    if (state.deletedRuns.has(String(id)) || (cached && isRunDeleted(cached))) throw Object.assign(new Error('记录已删除。'), { code: 'run_not_found' });
    if (cached) return cached;
    const version = state.recordsVersion;
    const run = await api('/runs/' + encodeURIComponent(id));
    if (version !== state.recordsVersion || isRunDeleted(run)) throw Object.assign(new Error('记录已更新，请重新打开。'), { code: 'run_not_found' });
    if (terminal(run.status)) { state.runCache.set(String(id), run); if (state.runCache.size > 120) state.runCache.delete(state.runCache.keys().next().value); }
    return run;
  }
  const previews = new Map();
  const previewObserver = 'IntersectionObserver' in window ? new IntersectionObserver((entries) => {
    entries.forEach((entry) => { const preview = previews.get(entry.target); if (preview) { preview.visible = entry.isIntersecting; syncPreview(entry.target, preview); } });
  }, { rootMargin: '0px' }) : null;
  function prunePreviews() {
    for (const [node, preview] of previews) if (!node.isConnected) { preview.frame?.remove(); previewObserver?.unobserve(node); previewResizeObserver?.unobserve(node); previews.delete(node); }
  }
  function syncPreview(node, preview) {
    if (!node.isConnected || document.hidden || !preview.visible || state.authExpired) { preview.frame?.remove(); preview.frame = null; return; }
    if (preview.frame) return;
    if (!preview.artifact) { if (node.dataset.preview && !preview.loading && !preview.loaded) loadPreview(node, preview); return; }
    preview.frame = artifactFrame(preview.artifact, preview.title, preview.large);
    node.prepend(preview.frame);
    fitPreview(node, preview);
  }
  function trackPreview(node, details = {}) {
    prunePreviews();
    if (details.large) node.classList.add('artifact-host');
    const preview = { visible: !previewObserver, frame: null, ...details };
    previews.set(node, preview);
    if (!details.large) { preview.canvas = details.artifact ? artifactCanvas(details.artifact) : null; previewResizeObserver?.observe(node); }
    if (previewObserver) previewObserver.observe(node); else syncPreview(node, preview);
  }
  function observePreviews(root) { root.querySelectorAll('[data-preview]').forEach((node) => trackPreview(node)); }
  const previewResizeObserver = 'ResizeObserver' in window ? new ResizeObserver((entries) => { for (const entry of entries) { const preview = previews.get(entry.target); if (preview) fitPreview(entry.target, preview); } }) : null;
  function syncPreviews() { prunePreviews(); for (const [node, preview] of previews) syncPreview(node, preview); }
  function rebuildPreviewFrames() { for (const [node, preview] of previews) if (preview.frame) { preview.frame.remove(); preview.frame = null; syncPreview(node, preview); } }
  async function loadPreview(node, preview) {
    preview.loading = true;
    try { const run = await runDetail(node.dataset.preview); if (!node.isConnected) return; node.querySelector('.preview-note')?.remove(); preview.artifact = run.artifact_html; preview.title = run.model + ' 绘图预览'; preview.loaded = true; if (run.artifact_html) syncPreview(node, preview); else node.insertAdjacentHTML('afterbegin', '<span class="preview-note">暂无可预览内容</span>'); }
    catch { if (node.isConnected) { const note = node.querySelector('.preview-note'); if (note) note.textContent = '预览暂不可用'; } }
    finally { preview.loading = false; preview.loaded = true; }
  }

  function resultMetrics(run) {
    const main = [
      ['首字延迟', run.ttft_ms, seconds],
      ['文本速率', run.tokens_per_second, (value) => Number(value).toFixed(1) + ' t/s'],
      ['总 Token', run.total_tokens, integer]
    ].filter(([, value]) => number(value) !== null);
    const additional = [
      ['输入 Token', run.input_tokens, integer],
      ['输出 Token', run.output_tokens, integer], ['缓存 Token', run.cached_tokens, integer],
      ['推理 Token', run.reasoning_tokens, integer],
      ['预估费用', run.cost_usd, (value) => '$' + Number(value).toFixed(6)]
    ].filter(([, value]) => number(value) !== null);
    const hasUsage = ['input_tokens', 'output_tokens', 'cached_tokens', 'reasoning_tokens', 'total_tokens'].some((key) => number(run[key]) !== null);
    const usageMissing = terminal(run.status) && !hasUsage ? '<p class="usage-missing">上游未返回用量</p>' : '';
    return `${main.length ? `<div class="result-metrics">${main.map(([label, value, format]) => metric(label, format(value))).join('')}</div>` : ''}${usageMissing}${additional.length ? `<details class="disclosure usage-details"><summary>用量明细</summary><dl class="usage-list">${additional.map(([label, value, format]) => `<div><dt>${html(label)}</dt><dd>${html(format(value))}</dd></div>`).join('')}</dl></details>` : ''}`;
  }
  function runParameters(run) {
    const effort = run.effort || 'default';
    const protocol = run.protocol === 'responses' ? 'Responses' : run.protocol === 'chat_completions' ? 'Chat Completions' : '';
    return `<details class="disclosure"><summary>请求参数</summary><dl class="usage-list">${protocol ? `<div><dt>接口协议</dt><dd>${protocol}</dd></div>` : ''}${effort ? `<div><dt>推理强度</dt><dd>${html(effort)}</dd></div>` : ''}</dl>${run.prompt ? `<h3 class="parameter-label">提示词</h3><pre class="output">${html(run.prompt)}</pre>` : ''}${run.kind === 'reasoning' && run.expected_answer ? `<p>预期答案：${html(run.expected_answer)}</p>` : ''}</details>`;
  }
  function renderRun(target, run, detail = false) {
    const duration = terminal(run.status) && number(run.duration_ms) !== null ? `<span class="result-duration">耗时 ${html(seconds(run.duration_ms))}</span>` : '';
    const pill = run.kind === 'drawing' && run.status === 'completed' ? '<span class="status-pill completed">已完成</span>' : statusPill(run.status);
    target.innerHTML = `<div class="result-banner"><span class="result-model">${html(run.model || '测试任务')}</span><div class="result-status">${pill}${duration}</div></div>${detail ? `<div class="detail-meta"><span>${html(time(run.created_at))}</span>${run.group_name ? `<span>${html(run.group_name)}</span>` : ''}</div>` : ''}${!terminal(run.status) ? `<div class="run-progress"><div class="progress-title"><span class="spinner"></span><span>${run.status === 'queued' ? '等待执行' : '正在等待模型返回'}</span></div></div>` : ''}${run.kind === 'drawing' ? '' : resultMetrics(run)}${run.error_message ? `<p class="form-error">${html(run.error_message)}</p>` : ''}${run.artifact_html ? '<div data-artifact></div>' : ''}${run.output_text ? `<div class="output-head"><h3>模型输出</h3><button class="button ghost small" data-action="copy-output" data-id="${html(run.id)}">复制</button></div><pre class="output">${html(run.output_text)}</pre>` : terminal(run.status) ? '<p class="empty-output">无输出内容</p>' : ''}${detail ? runParameters(run) : ''}`;
    prunePreviews();
    if (run.artifact_html) trackPreview(target.querySelector('[data-artifact]'), { artifact: run.artifact_html, title: run.model + ' 绘图结果', large: true });
  }
  function syncModalScroll() {
    const locked = Boolean(document.querySelector('dialog[open]') || (state.historyReturn && !state.authExpired));
    document.body.classList.toggle('modal-open', locked);
    document.documentElement.classList.toggle('modal-open', locked);
  }
  function openModal(dialog, opener = document.activeElement) {
    if (!dialog.open) { state.modalOpeners.set(dialog.id, opener); dialog.showModal(); }
    syncModalScroll();
  }
  function closeModal(dialog) {
    if (dialog?.id === 'confirmDialog' && state.confirmation?.pending) return;
    if (dialog?.open) dialog.close();
  }
  function openConfirmation(options, opener = document.activeElement) {
    if (state.confirmation || state.authExpired) return;
    state.confirmation = { ...options, pending: false };
    $('confirmTitle').textContent = options.title;
    $('confirmDescription').textContent = options.description;
    $('confirmName').textContent = options.name;
    $('confirmMeta').textContent = options.meta || '';
    $('confirmMeta').hidden = !options.meta;
    $('confirmSubmit').textContent = options.label;
    $('confirmError').textContent = ''; $('confirmError').hidden = true;
    busy($('confirmSubmit'), false); busy($('confirmCancel'), false);
    openModal($('confirmDialog'), opener);
    $('confirmCancel').focus({ preventScroll: true });
  }
  async function submitConfirmation() {
    const confirmation = state.confirmation;
    if (!confirmation || confirmation.pending || state.authExpired) return;
    confirmation.pending = true;
    busy($('confirmSubmit'), true); busy($('confirmCancel'), true);
    $('confirmSubmit').textContent = '正在删除…';
    $('confirmError').textContent = ''; $('confirmError').hidden = true;
    try {
      await confirmation.execute();
      if (state.confirmation !== confirmation || state.authExpired) return;
      confirmation.pending = false; closeModal($('confirmDialog'));
    } catch (error) {
      if (state.confirmation !== confirmation || state.authExpired) return;
      $('confirmError').textContent = error.message || '删除未完成，请重试。';
      $('confirmError').hidden = false;
    } finally {
      if (state.confirmation === confirmation) {
        confirmation.pending = false;
        busy($('confirmSubmit'), false); busy($('confirmCancel'), false);
        $('confirmSubmit').textContent = confirmation.label;
      }
    }
  }
  function probeDraftKey() { return state.editingProbe ? String(state.editingProbe.id) : 'new'; }
  function rememberProbeDraft() {
    const form = $('probeForm');
    if (state.authExpired || state.probeSaved) return;
    const values = {};
    for (const field of form.elements) if (field.name && field.name !== 'api_key') values[field.name] = field.type === 'checkbox' ? field.checked : field.value;
    state.probeDrafts.set(probeDraftKey(), { values, locked: Boolean(state.editingProbe?.identity_locked), autoName: state.autoProbeName, advanced: Boolean(form.querySelector('details.advanced')?.open), editSequence: state.probeEditSequence });
  }
  function handleModalClose(dialog) {
    if (dialog.open) return;
    const opener = state.modalOpeners.get(dialog.id);
    state.modalOpeners.delete(dialog.id);
    const fallbackFocus = dialog.id === 'confirmDialog' ? state.confirmation?.fallbackFocus : null;
    if (dialog.id === 'confirmDialog') state.confirmation = null;
    if (dialog.id === 'probeDialog') { rememberProbeDraft(); state.probeModelSequence++; state.probeModels = null; }
    if (dialog.id === 'detailDialog') {
      state.detailSequence++; $('detailContent').replaceChildren(); prunePreviews();
      const back = state.historyReturn; state.historyReturn = null; $('detailBack').hidden = true;
      if (back && !state.authExpired) {
        openModal($('historyDialog'), back.pageOpener);
        $('historyContent').scrollTop = back.scrollTop;
        if (back.focus?.isConnected) back.focus.focus({ preventScroll: true });
        syncModalScroll(); return;
      }
    }
    const suspended = dialog.id === 'historyDialog' && Boolean(state.historyReturn);
    syncModalScroll();
    if (!suspended && !state.authExpired && opener?.isConnected) opener.focus({ preventScroll: true });
    else if (!suspended && !state.authExpired && fallbackFocus?.isConnected) fallbackFocus.focus({ preventScroll: true });
  }
  function bindModal(dialog) {
    let outsidePointer = null;
    const outside = (event) => { const rect = dialog.getBoundingClientRect(); return event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom); };
    dialog.addEventListener('pointerdown', (event) => { outsidePointer = event.button === 0 && event.isPrimary !== false && outside(event) ? { id: event.pointerId, x: event.clientX, y: event.clientY } : null; });
    dialog.addEventListener('pointerup', (event) => {
      const start = outsidePointer; outsidePointer = null;
      if (start && start.id === event.pointerId && outside(event) && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 8) closeModal(dialog);
    });
    dialog.addEventListener('pointercancel', () => { outsidePointer = null; });
    dialog.addEventListener('cancel', (event) => { if (dialog.id === 'confirmDialog' && state.confirmation?.pending) event.preventDefault(); });
    dialog.addEventListener('close', () => handleModalClose(dialog));
    // Native dialog cancel handles Escape and keeps keyboard focus within the top modal.
  }
  async function openDetail(id, opener = document.activeElement) {
    const historyDialog = $('historyDialog');
    if (historyDialog.open) {
      state.historyReturn = { scrollTop: $('historyContent').scrollTop, focus: opener, pageOpener: state.modalOpeners.get(historyDialog.id) };
      closeModal(historyDialog);
    }
    const sequence = ++state.detailSequence;
    $('detailBack').hidden = !state.historyReturn;
    $('detailTitle').textContent = '测试详情';
    $('detailContent').innerHTML = loading();
    openModal($('detailDialog'), opener);
    try { const run = await runDetail(id); if (sequence !== state.detailSequence || !$('detailDialog').open) return; $('detailTitle').textContent = run.kind === 'drawing' ? '绘图详情' : '答题详情'; renderRun($('detailContent'), run, true); }
    catch (error) { if (sequence === state.detailSequence && $('detailDialog').open) showError($('detailContent'), error); }
  }
  function openHistory(id) {
    const model = state.dashboard?.groups.flatMap((group) => group.models || []).find((item) => String(item.probe_id) === String(id));
    if (!model) return;
    $('historyTitle').textContent = model.model + ' · 最近运行';
    $('historyContent').innerHTML = runRows([...(model.history || [])].filter((run) => ['passed', 'failed', 'completed'].includes(run.status)).reverse());
    openModal($('historyDialog'));
  }
  function updatePresets(form, selected, applyDefaults = false) {
    const kind = formField(form, 'kind').value;
    const presets = (state.bootstrap?.presets || []).filter((preset) => preset.kind === kind && preset.id !== 'ping');
    formField(form, 'preset_id').innerHTML = presets.map((preset) => `<option value="${html(preset.id)}">${html(preset.name)}</option>`).join('');
    const preferred = presets.some((preset) => preset.id === selected) ? selected : kind === 'drawing' ? 'svg' : 'candy';
    if (presets.some((preset) => preset.id === preferred)) formField(form, 'preset_id').value = preferred;
    updatePresetHints(form);
    if (applyDefaults) applyPresetDefaults(form);
  }
  function canonicalReasoningModel(model) {
    return model.split('/').pop().trim().toLowerCase().replaceAll('_', '-').replace(/\s+/g, '-').replace(/-+/g, '-');
  }
  function blockedReasoningEfforts(form) {
    const model = formField(form, 'model').value;
    if (form === $('probeForm')) return state.probeModels?.effortBlocks?.get(model) || [];
    const name = canonicalReasoningModel(model);
    const blocks = state.bootstrap?.reasoning_effort_blocks;
    return blocks && Object.hasOwn(blocks, name) ? blocks[name] : [];
  }
  function syncReasoningEffort(form, suggest = false) {
    const blocked = blockedReasoningEfforts(form);
    const field = formField(form, 'effort');
    const isProbe = form === $('probeForm');
    const suffix = canonicalReasoningModel(formField(form, 'model').value).split('-').pop();
    const invalidModel = !isProbe && blocked.includes(suffix);
    if (suggest && !invalidModel && blocked.includes(field.value)) field.value = blocked.includes('none') ? 'low' : 'none';
    for (const option of field.options) option.disabled = blocked.includes(option.value);
    const invalidEffort = blocked.includes(field.value);
    const hint = $(isProbe ? 'probeEffortHint' : 'testEffortHint');
    let message = '';
    if (invalidModel) message = '模型名带有不支持的推理强度后缀，请改用不带该后缀的模型。';
    else if (invalidEffort) message = '当前强度不可用，请在高级参数中调整。';
    else if (blocked.includes('none')) message = '最低显式推理强度为低（low）。';
    hint.textContent = message;
    hint.hidden = !message;
    return !invalidModel && !invalidEffort;
  }
  function applyPresetDefaults(form) {
    const budget = formField(form, 'kind').value === 'drawing' ? 1024 : 64;
    formField(form, 'max_output_tokens').value = Math.min(budget, state.bootstrap?.limits?.max_output_tokens || 32768);
    formField(form, 'effort').value = blockedReasoningEfforts(form).includes('none') ? 'low' : 'none';
    syncReasoningEffort(form);
  }
  function changePreset(form) {
    formField(form, 'prompt').value = '';
    formField(form, 'expected_answer').value = '';
    updatePresetHints(form);
    applyPresetDefaults(form);
  }
  function updatePresetHints(form) {
    const preset = state.bootstrap?.presets.find((item) => item.id === formField(form, 'preset_id').value);
    const fixed = preset?.id === 'candy';
    const prompt = formField(form, 'prompt');
    const expected = formField(form, 'expected_answer');
    prompt.readOnly = fixed;
    expected.readOnly = fixed;
    form.querySelector('.prompt-field').hidden = fixed;
    form.querySelector('.expected-field').hidden = fixed || formField(form, 'kind').value !== 'reasoning';
    prompt.required = preset?.id === 'custom' || preset?.id === 'custom_drawing';
    prompt.placeholder = preset?.prompt || '输入测试提示词';
    expected.placeholder = preset?.expected_answer || '可选：填写预期答案';
    if (fixed) { prompt.value = preset.prompt || ''; expected.value = preset.expected_answer || ''; }
    else if (preset?.id === 'svg' && !prompt.value) { prompt.value = preset.prompt || ''; }
  }
  function readTestFields(form) {
    const data = Object.fromEntries(new FormData(form));
    const payload = { base_url: String(data.base_url || '').trim(), api_key: String(data.api_key || '').trim(), model: String(data.model || '').trim(), protocol: data.protocol, effort: formField(form, 'effort').value, kind: data.kind, preset_id: data.preset_id, max_output_tokens: Number(data.max_output_tokens), timeout_seconds: Number(data.timeout_seconds), publish: formField(form, 'publish').checked };
    if (data.preset_id !== 'candy') {
      payload.prompt = String(data.prompt || '').trim();
      if (data.kind === 'reasoning') payload.expected_answer = String(data.expected_answer || '').trim();
    }
    return payload;
  }
  function readProbeFields(form) {
    const data = Object.fromEntries(new FormData(form));
    const payload = { name: String(data.name || '').trim(), group_id: Number(data.group_id), model: String(data.model || '').trim(), protocol: data.protocol, effort: formField(form, 'effort').value, kind: data.kind, preset_id: data.preset_id, max_output_tokens: Number(data.max_output_tokens), timeout_seconds: Number(data.timeout_seconds), interval_minutes: Number(data.interval_minutes), publish: formField(form, 'publish').checked, enabled: formField(form, 'enabled').checked };
    if (data.preset_id !== 'candy') {
      payload.prompt = String(data.prompt || '').trim();
      if (data.kind === 'reasoning') payload.expected_answer = String(data.expected_answer || '').trim();
    }
    for (const key of ['input_price', 'cached_price', 'output_price', 'display_multiplier']) payload[key] = data[key] === '' || data[key] == null ? null : Number(data[key]);
    return payload;
  }
  function validateBaseURL(value) {
    try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash; } catch { return false; }
  }
  async function submitTest(event) {
    event.preventDefault();
    if (testLocked()) return;
    const payload = readTestFields($('testForm'));
    if (!validateBaseURL(payload.base_url)) { formError('testError', '请填写不含凭据、查询串的 HTTPS 地址。'); return; }
    if (!payload.api_key || !payload.model) { formError('testError', '请填写 API Key 和模型。'); return; }
    const form = $('testForm');
    if (!syncReasoningEffort(form)) {
      if (blockedReasoningEfforts(form).includes(payload.effort)) form.querySelector('details.advanced').open = true;
      formError('testError', $('testEffortHint').textContent);
      return;
    }
    payload.request_id = state.pendingRequest?.requestId || crypto.randomUUID();
    formField($('testForm'), 'api_key').value = '';
    state.submitting = true;
    savePendingRequest({ requestId: payload.request_id, probeId: null, retryAllowed: false });
    formError('testError', '');
    try { const run = await api('/runs', { method: 'POST', body: payload }); startRun({ ...run, model: payload.model, kind: payload.kind, protocol: payload.protocol, effort: payload.effort, created_at: new Date().toISOString() }); }
    catch (error) {
      if (state.authExpired) return;
      if (error.status >= 400 && error.status < 500) { state.pendingRequest.retryAllowed = true; formError('testError', error.message); }
      else pendingNotice('未收到提交确认，请查询原任务。不会自动再次发起测试。');
    }
    finally { payload.api_key = ''; state.submitting = false; syncTestButtons(); }
  }
  function stopPolling(all = false) { clearTimeout(state.pollTimer); state.pollTimer = null; if (all) for (const pending of state.probeRequests.values()) clearTimeout(pending.timer); }
  function startRun(run) {
    stopPolling();
    state.currentRun = run;
    syncTestButtons();
    $('cancelRun').hidden = terminal(run.status);
    busy($('cancelRun'), false);
    renderRun($('currentResult'), run);
    pollRun();
  }
  async function pollRun() {
    if (!state.currentRun || state.authExpired || state.polling) return;
    state.polling = true;
    const id = state.currentRun.id;
    try {
      const run = await api('/runs/' + encodeURIComponent(id));
      if (String(state.currentRun?.id) !== String(id) || state.authExpired) return;
      state.currentRun = run;
      renderRun($('currentResult'), run);
      if (terminal(run.status)) {
        state.runCache.set(String(run.id), run);
        stopPolling();
        savePendingRequest(null);
        $('cancelRun').hidden = true;
        loadMine();
        return;
      }
      state.pollTimer = setTimeout(pollRun, 2000);
    } catch (error) {
      if (state.authExpired) return;
      $('currentResult').insertAdjacentHTML('beforeend', `<div class="error-state"><p>${html(error.message)}</p><button class="button secondary small" data-action="resume-poll">继续查询</button></div>`);
      clearTimeout(state.pollTimer);
      if (error.code === 'run_not_found') { state.currentRun = null; savePendingRequest(null); $('cancelRun').hidden = true; }
    } finally { state.polling = false; }
  }
  async function cancelRun() {
    if ($('cancelRun').disabled || !state.currentRun || terminal(state.currentRun.status)) return;
    if (!confirm('取消当前任务？已产生的上游费用无法撤销。')) return;
    busy($('cancelRun'), true);
    try { await api('/runs/' + encodeURIComponent(state.currentRun.id) + '/cancel', { method: 'POST', body: {} }); toast('已提交取消'); }
    catch (error) { toast(error.message); }
    finally { busy($('cancelRun'), false); }
  }

  async function loadProbes() {
    if (state.bootstrap?.user.role !== 'admin') return;
    const sequence = ++state.probesSequence;
    busy($('refreshProbes'), true);
    $('probeList').innerHTML = loading();
    try {
      const data = await api('/admin/probes');
      if (sequence !== state.probesSequence) return;
      state.probes = (data.probes || []).filter((probe) => !state.deletedProbes.has(String(probe.id)));
      renderProbeList();
    } catch (error) { if (sequence === state.probesSequence) showError($('probeList'), error, 'probes'); }
    finally { if (sequence === state.probesSequence) busy($('refreshProbes'), false); syncTestButtons(); }
  }
  function renderUpdates(view) {
    const op = view.operation, active = ['verifying', 'installing'].includes(op?.phase);
    const messages = { verifying: '正在校验版本和程序文件', installing: '正在切换版本并检查服务', installed: '升级成功', rolled_back: '升级检查失败，已恢复原版本' };
    const reasons = { release_changed: '可用版本已变化，请重新检查。', manual_upgrade: '此版本需要运维手动升级。', local_changes: '已安装文件与发布版本不一致，请联系运维。', manual_recovery: '升级需要运维恢复，请检查升级器状态。', interrupted: '上次升级已中断，请重新检查。' };
    const newer = view.available && view.latest;
    const update = $('installUpdate'), badge = $('releaseBadge');
    badge.hidden = !newer;
    badge.textContent = newer ? '有新版本 ' + view.latest.tag : '';
    update.hidden = !newer || !view.managed;
    update.disabled = active || state.updateSubmitting || Date.now() < (state.updateAckUntil || 0);
    update.textContent = newer ? '升级到 ' + view.latest.tag : '升级';
    const status = view.source === 'unavailable' ? '暂时无法从 GitHub 检查版本' : view.source === 'checking' ? '正在检查 GitHub' : newer ? '可用版本 ' + view.latest.tag : view.latest ? '当前没有更高的稳定版' : '仓库尚无稳定发布';
    const operation = op?.phase === 'failed' ? reasons[op.reason] || '升级未完成，请检查升级器后重试。' : messages[op?.phase];
    $('updateStatus').textContent = ['当前 v' + view.current, status, newer && !view.managed ? '升级器未就绪，请检查 geekpulse-updater.service。' : '', operation && op.tag ? op.tag + ' ' + operation : ''].filter(Boolean).join(' · ');
    $('reloadUpdate').hidden = view.current === state.bootstrap.version;
    return active;
  }
  async function refreshUpdates() {
    if (state.updateReading || state.authExpired || state.bootstrap?.user.role !== 'admin') return;
    clearTimeout(state.updateTimer); state.updateReading = true;
    let active = false;
    try {
      const view = await api('/admin/updates');
      if (state.authExpired) return;
      state.updateView = view;
      active = renderUpdates(view);
    } catch (error) {
      state.updateView = null;
      $('installUpdate').disabled = true;
      $('updateStatus').textContent = error.message;
    } finally {
      state.updateReading = false;
      if (!state.authExpired && !state.pageHidden) state.updateTimer = setTimeout(refreshUpdates, active || Date.now() < (state.updateWatchUntil || 0) ? 2000 : 60000);
    }
  }
  async function checkUpdates() {
    if ($('checkUpdates').disabled) return;
    busy($('checkUpdates'), true);
    try { await api('/admin/updates/check', { method: 'POST', body: {} }); state.updateWatchUntil = Date.now() + 45000; await refreshUpdates(); }
    catch (error) { $('updateStatus').textContent = error.message; }
    finally { busy($('checkUpdates'), false); }
  }
  async function installUpdate() {
    const target = state.updateView?.latest;
    if (!target || $('installUpdate').disabled || state.updateSubmitting || !state.updateView.managed || !state.updateView.available) return;
    if (!confirm('升级到 ' + target.tag + '？监控不用暂停。GeekPulse 将短暂重启，正在运行的测试可能中断，不会自动重试。')) return;
    state.updateSubmitting = true;
    busy($('installUpdate'), true);
    state.updateWatchUntil = Date.now() + 90000;
    try {
      await api('/admin/updates/install', { method: 'POST', body: { tag: target.tag, revision: target.revision } });
      state.updateAckUntil = Date.now() + 4000;
    } catch (error) { toast(error.message); }
    finally { state.updateSubmitting = false; await refreshUpdates(); }
  }
  function renderProbeList() {
    const names = [...new Set(state.probes.map(probe => probe.name))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
    if (!names.includes(state.probeNameFilter)) state.probeNameFilter = '';
    const filter = $('probeNameFilter');
    filter.innerHTML = '<option value="">全部名称</option>' + names.map(name => '<option value="' + html(name) + '">' + html(name) + '</option>').join('');
    filter.value = state.probeNameFilter;
    const probes = state.probes.filter(probe => !filter.value || probe.name === filter.value);
    $('probeList').innerHTML = probes.length ? `<div class="probe-list">${probes.map((probe) => `<div class="probe-row"><div class="probe-info"><span class="probe-name">${html(probe.name)}</span><span class="probe-sub">${html(probe.group_name)}</span><span class="probe-sub">每 ${integer(probe.interval_minutes)} 分钟</span></div><div class="probe-info"><span class="probe-model">${html(probe.model)}</span><span class="probe-sub">${probe.kind === 'drawing' ? '图片检测' : '答题检测'} · ${html(probe.effort || 'default')}</span>${probe.enabled && probe.next_run_at ? `<span class="probe-sub">下次 ${html(time(probe.next_run_at))}</span>` : ''}<div class="probe-run-state" data-probe-state="${html(probe.id)}"></div></div><span class="status-pill ${probe.enabled ? 'enabled' : 'disabled'}">${probe.enabled ? '已启用' : '已暂停'}</span><div class="probe-actions"><button class="button secondary small" data-action="probe-run" data-id="${html(probe.id)}" ${probe.has_key ? '' : 'disabled'}>运行</button><button class="button secondary small" data-action="probe-edit" data-id="${html(probe.id)}">编辑</button><button class="button ghost small" data-action="probe-toggle" data-id="${html(probe.id)}">${probe.enabled ? '暂停' : '启用'}</button><button class="button ghost small" data-action="probe-delete" data-id="${html(probe.id)}">删除</button></div></div>`).join('')}</div>` : empty('暂无探测配置');
    syncTestButtons();
  }
  function editProbe(id) {
    const probe = id == null ? null : state.probes.find((item) => String(item.id) === String(id));
    if (id != null && !probe) return;
    state.editingProbe = probe; state.probeSaved = false; state.probeEditSequence++; state.probeSaving = null; state.probeModels = null; state.autoProbeName = null;
    busy($('saveProbe'), false);
    const form = $('probeForm'); form.reset();
    form.querySelector('details.advanced').open = false;
    formField(form, 'group_id').innerHTML = (state.bootstrap.groups || []).map((group) => `<option value="${html(group.id)}">${html(group.name)}</option>`).join('');
    const values = probe || { protocol: 'responses', effort: 'none', kind: 'reasoning', preset_id: 'candy', interval_minutes: 60, timeout_seconds: state.bootstrap.limits?.max_timeout_seconds || 900, max_output_tokens: 64, enabled: false, publish: false };
    for (const [key, value] of Object.entries(values)) { const field = formField(form, key); if (!field || key === 'preset_id' || key === 'model') continue; if (field.type === 'checkbox') field.checked = Boolean(value); else field.value = key === 'effort' && value === 'default' ? '' : value ?? ''; }
    updatePresets(form, values.preset_id, !probe);
    const draft = state.probeDrafts.get(probeDraftKey());
    if (draft && Boolean(draft.locked) === Boolean(probe?.identity_locked)) {
      for (const [key, value] of Object.entries(draft.values)) { const field = formField(form, key); if (!field || key === 'preset_id' || key === 'model') continue; if (field.type === 'checkbox') field.checked = Boolean(value); else field.value = value; }
      updatePresets(form, draft.values.preset_id, false);
      form.querySelector('details.advanced').open = draft.advanced;
    }
    if (!probe) {
      state.autoProbeName = draft?.autoName ?? null;
      defaultProbeName();
    }
    const locked = Boolean(probe?.identity_locked);
    for (const key of ['group_id', 'kind', 'preset_id', 'prompt', 'expected_answer']) formField(form, key).disabled = locked;
    $('probeTitle').textContent = probe ? '编辑探测' : '新建探测';
    formError('probeError', '');
    openModal($('probeDialog'));
    if (locked) {
      const model = formField(form, 'model');
      model.innerHTML = '<option value="' + html(probe.model) + '">' + html(probe.model) + '</option>';
      model.value = probe.model; model.disabled = true;
      $('probeModelHint').textContent = '分组、模型、测试类型、预设、题目和预期答案已锁定。要换模型或题目，请新建探测；频率仍可调整。';
      $('retryProbeModels').hidden = true;
      syncProbeModelSelection();
    } else loadProbeModels(draft?.values.model || probe?.model || '');
  }
  function defaultProbeName() {
    if (state.editingProbe) return;
    const form = $('probeForm'), name = formField(form, 'name');
    if (name.value && name.value !== state.autoProbeName) return;
    const group = state.bootstrap.groups.find(item => String(item.id) === formField(form, 'group_id').value);
    name.value = state.autoProbeName = group?.name || '';
  }
  function syncProbeModelSelection(suggestEffort = false) {
    const form = $('probeForm'), catalog = state.probeModels;
    syncReasoningEffort(form, suggestEffort);
    if (state.editingProbe?.identity_locked) {
      $('saveProbe').disabled = state.probeSaving?.editSequence === state.probeEditSequence;
      return;
    }
    const valid = catalog?.ready && catalog.groupId === formField(form, 'group_id').value && catalog.models.includes(formField(form, 'model').value);
    $('saveProbe').disabled = !valid || state.probeSaving?.editSequence === state.probeEditSequence;
    if (valid) { catalog.preferred = formField(form, 'model').value; $('probeModelHint').textContent = ''; $('retryProbeModels').hidden = true; }
  }
  async function loadProbeModels(preferred = formField($('probeForm'), 'model').value) {
    const form = $('probeForm'), select = formField(form, 'model');
    const groupId = formField(form, 'group_id').value;
    const sequence = ++state.probeModelSequence, editSequence = state.probeEditSequence;
    state.probeModels = { groupId, models: [], ready: false, preferred };
    select.innerHTML = preferred ? `<option value="${html(preferred)}">${html(preferred)}</option>` : '<option value="">正在加载模型…</option>';
    select.value = preferred; select.disabled = true;
    $('probeModelHint').textContent = groupId ? '正在加载此分组可用的模型…' : '当前没有可选分组。';
    $('retryProbeModels').hidden = true; syncProbeModelSelection();
    if (!groupId) return;
    const current = () => sequence === state.probeModelSequence && editSequence === state.probeEditSequence && $('probeDialog').open && formField(form, 'group_id').value === groupId && !state.authExpired;
    try {
      const result = await api('/admin/groups/' + encodeURIComponent(groupId) + '/models');
      if (!current()) return;
      if (!Array.isArray(result.models) || result.models.some((model) => typeof model !== 'string' || !model.trim())) throw new Error('分组模型目录格式无效。');
      const models = [...new Set(result.models)];
      const blocks = result.reasoning_effort_blocks;
      if (!blocks || typeof blocks !== 'object' || Array.isArray(blocks)) throw new Error('分组模型强度信息无效，请刷新页面。');
      const effortBlocks = new Map(Object.entries(blocks));
      for (const [model, blocked] of effortBlocks) {
        if (!models.includes(model) || !Array.isArray(blocked) || blocked.some((effort) => effort !== 'none' && effort !== 'minimal')) {
          throw new Error('分组模型强度信息无效，请刷新页面。');
        }
      }
      state.probeModels = { groupId, models, effortBlocks, ready: true, preferred };
      const missing = Boolean(preferred && !models.includes(preferred));
      select.innerHTML = (missing ? `<option value="${html(preferred)}" disabled>${html(preferred)}（当前不可用）</option>` : '<option value="">请选择模型</option>') + models.map((model) => `<option value="${html(model)}">${html(model)}</option>`).join('');
      select.value = preferred || ''; select.disabled = models.length === 0;
      const emptyMessage = typeof result.message === 'string' && result.message.trim() ? result.message : '此分组暂未返回可用模型。';
      $('probeModelHint').textContent = models.length ? missing ? '原模型当前不在此分组目录中，请重新选择。' : '' : emptyMessage;
      $('retryProbeModels').hidden = models.length > 0 && !missing;
    } catch (error) {
      if (!current()) return;
      $('probeModelHint').textContent = error.message || '模型目录加载失败。';
      $('retryProbeModels').hidden = false;
    } finally { if (current()) syncProbeModelSelection(); }
  }
  async function saveProbe(event) {
    event.preventDefault();
    if ($('saveProbe').disabled) return;
    const form = $('probeForm');
    const editingProbe = state.editingProbe, editSequence = state.probeEditSequence, draftKey = probeDraftKey();
    const payload = readProbeFields(form);
    if (editingProbe?.identity_locked) {
      for (const key of ['group_id', 'model', 'kind', 'preset_id', 'prompt', 'expected_answer']) delete payload[key];
      for (const key of Object.keys(payload)) {
        const saved = key === 'effort' && editingProbe[key] === 'default' ? '' : editingProbe[key];
        if (payload[key] === saved) delete payload[key];
      }
    } else if (!state.probeModels?.ready || state.probeModels.groupId !== String(payload.group_id) || !state.probeModels.models.includes(payload.model)) { formError('probeError', '请从所选分组的模型目录中选择模型。'); return; }
    const unchangedPaused = !payload.enabled && editingProbe && editingProbe.group_id === payload.group_id
      && editingProbe.model === payload.model && (editingProbe.effort === 'default' ? '' : editingProbe.effort) === payload.effort;
    if (!syncReasoningEffort(form) && !unchangedPaused) {
      form.querySelector('details.advanced').open = true;
      formError('probeError', '所选推理强度不适用于当前模型，请调整后再试。');
      return;
    }
    const saving = { editSequence }; state.probeSaving = saving;
    busy($('saveProbe'), true); formError('probeError', '');
    try {
      await api(editingProbe ? '/admin/probes/' + encodeURIComponent(editingProbe.id) : '/admin/probes', { method: editingProbe ? 'PUT' : 'POST', body: payload });
      if (state.probeDrafts.get(draftKey)?.editSequence <= editSequence) state.probeDrafts.delete(draftKey);
      if (editSequence === state.probeEditSequence) { state.probeSaved = true; closeModal($('probeDialog')); }
      toast('配置已保存'); await loadProbes(); loadDashboard();
    }
    catch (error) { if (editSequence === state.probeEditSequence && !state.authExpired) formError('probeError', error.message); }
    finally { if (state.probeSaving === saving) state.probeSaving = null; if (editSequence === state.probeEditSequence) { busy($('saveProbe'), false); syncProbeModelSelection(); } }
  }
  async function probeAction(action, id, button, explicitRetry = false) {
    const probe = state.probes.find((item) => String(item.id) === String(id)); if (!probe || button?.disabled) return;
    const existing = state.probeRequests.get(String(id));
    if (action === 'probe-run' && existing && !(explicitRetry && existing.retryAllowed && !existing.submitting && !existing.recovering)) return;
    if (action === 'probe-edit') { editProbe(id); return; }
    if (action === 'probe-delete') { requestProbeDeletion(probe, button); return; }
    busy(button, true);
    try {
      const path = '/admin/probes/' + encodeURIComponent(id);
      if (action === 'probe-run') {
        const pending = { requestId: existing?.requestId || crypto.randomUUID(), probeId: String(id), retryAllowed: false, submitting: true };
        state.probeRequests.set(String(id), pending); saveProbeRequests();
        const requestId = pending.requestId;
        const result = await api(path + '/run', { method: 'POST', body: { request_id: requestId } });
        if (state.probeRequests.get(String(id)) !== pending || state.authExpired) return;
        probe.identity_locked = true;
        pending.run = { ...result, model: probe.model, kind: probe.kind, protocol: probe.protocol, effort: probe.effort, created_at: new Date().toISOString() };
        toast(`${probe.model} · 检测已开始`); pollProbeRun(String(id));
      } else { await api(path, { method: 'PUT', body: { enabled: !probe.enabled } }); await loadProbes(); loadDashboard(); toast('探测状态已更新'); }
    } catch (error) {
      if (state.authExpired) return;
      if (action === 'probe-run') {
        const pending = state.probeRequests.get(String(id));
        if (pending) { pending.retryAllowed = error.status >= 400 && error.status < 500; pending.error = pending.retryAllowed ? error.message : '未收到提交确认，请查询原任务。'; }
      } else toast(error.message);
    } finally { if (action === 'probe-run') { const pending = state.probeRequests.get(String(id)); if (pending) pending.submitting = false; } busy(button, false); syncTestButtons(); }
  }

  function invalidateRecordViews() {
    state.recordsVersion++; state.dashboardSequence++; state.mineSequence++; state.artworksSequence++; state.detailSequence++;
    state.runCache.clear();
    for (const [id, run] of state.runSummaries) if (isRunDeleted(run)) state.runSummaries.delete(id);
    state.dashboard = null;
    state.historyReturn = null;
    closeModal($('detailDialog')); closeModal($('historyDialog'));
    if (state.currentRun && isRunDeleted(state.currentRun)) { state.currentRun = null; $('currentResult').innerHTML = empty('暂无测试结果'); }
    loadDashboard(); loadMine(); loadArtworks();
    prunePreviews();
  }
  function requestRunDeletion(id, button) {
    const run = state.runSummaries.get(String(id)) || state.runCache.get(String(id));
    if (!run?.can_delete || isRunDeleted(run)) return;
    const drawing = run.kind === 'drawing';
    openConfirmation({
      title: drawing ? '删除这件作品？' : '删除这条测试记录？', label: drawing ? '删除作品' : '删除记录', fallbackFocus: drawing ? $('refreshArtworks') : $('refreshMine'),
      name: run.model || '测试记录', meta: time(run.created_at),
      description: drawing ? '作品及其公开展示会一并删除，删除后无法恢复。' : '本次答题记录及其公开结果会一并删除，删除后无法恢复。',
      execute: async () => {
        await api('/runs/' + encodeURIComponent(id), { method: 'DELETE' });
        if (state.authExpired) return;
        state.deletedRuns.add(String(id)); invalidateRecordViews();
        toast(drawing ? '作品已删除' : '测试记录已删除');
      }
    }, button);
  }
  function requestProbeDeletion(probe, button) {
    openConfirmation({
      title: '删除这个探测？', label: '删除探测', name: probe.name, fallbackFocus: $('newProbe'),
      meta: [probe.model, probe.group_name].filter(Boolean).join(' · '),
      description: '将停止此探测，移除其不降智率样本、检测记录和公开作品。删除后无法恢复。',
      execute: async () => {
        await api('/admin/probes/' + encodeURIComponent(probe.id), { method: 'DELETE' });
        if (state.authExpired) return;
        state.deletedProbes.add(String(probe.id));
        const pending = state.probeRequests.get(String(probe.id));
        if (pending) { clearTimeout(pending.timer); state.probeRequests.delete(String(probe.id)); saveProbeRequests(); }
        state.probeDrafts.delete(String(probe.id));
        state.probes = state.probes.filter((item) => String(item.id) !== String(probe.id));
        invalidateRecordViews(); loadProbes(); toast('探测及关联记录已删除');
      }
    }, button);
  }

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if (button.hasAttribute('data-close')) { closeModal(button.closest('dialog')); return; }
    if (button.dataset.tab) { selectTab(button.dataset.tab); return; }
    if (button.dataset.hours) { state.hours = Number(button.dataset.hours); document.querySelectorAll('[data-hours]').forEach((item) => { const selected = item === button; item.classList.toggle('selected', selected); item.setAttribute('aria-pressed', String(selected)); }); loadDashboard(); return; }
    if (button.dataset.scope) { state.scope = button.dataset.scope; document.querySelectorAll('[data-scope]').forEach((item) => { const selected = item === button; item.classList.toggle('selected', selected); item.setAttribute('aria-pressed', String(selected)); }); loadArtworks(); return; }
    const action = button.dataset.action; const id = button.dataset.id;
    if (action === 'detail') openDetail(id, button);
    else if (action === 'run-delete') requestRunDeletion(id, button);
    else if (action === 'detail-back') closeModal($('detailDialog'));
    else if (action === 'history') openHistory(id);
    else if (action === 'carousel-prev' || action === 'carousel-next') { const carousel = [...document.querySelectorAll('[data-carousel]')].find((node) => node.dataset.carousel === String(id)); carousel?.scrollBy({ left: (action === 'carousel-prev' ? -1 : 1) * carousel.clientWidth * .8, behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' }); }
    else if (action === 'dashboard') loadDashboard();
    else if (action === 'traffic') loadTraffic();
    else if (action === 'mine') loadMine();
    else if (action === 'artworks') loadArtworks();
    else if (action === 'probes') loadProbes();
    else if (action === 'recover-request') { busy(button, true); await recoverRequest(); busy(button, false); }
    else if (action === 'recover-probe') await recoverProbeRequest(id);
    else if (action === 'retry-probe') await probeAction('probe-run', id, button, true);
    else if (action === 'resume-poll') { button.closest('.error-state').remove(); pollRun(); }
    else if (action === 'copy-output') { try { const run = state.runCache.get(String(id)) || (String(state.currentRun?.id) === String(id) ? state.currentRun : await runDetail(id)); await navigator.clipboard.writeText(run.output_text || ''); toast('已复制输出'); } catch { toast('复制失败，请手动选择输出内容。'); } }
    else if (action?.startsWith('probe-')) probeAction(action, id, button);
  });
  document.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = [...document.querySelectorAll('[data-tab]')].filter((tab) => !tab.hidden); const current = tabs.indexOf(document.activeElement); if (current < 0) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[index].focus(); selectTab(tabs[index].dataset.tab);
  });
  $('refreshDashboard').addEventListener('click', loadDashboard);
  $('groupFilter').addEventListener('change', (event) => { state.groupFilter = event.target.value; renderDashboard(); });
  $('refreshTraffic').addEventListener('click', loadTraffic);
  $('refreshMine').addEventListener('click', loadMine);
  $('refreshArtworks').addEventListener('click', loadArtworks);
  $('releaseBadge').addEventListener('click', () => { selectTab('admin'); $('checkUpdates').focus(); });
  $('checkUpdates').addEventListener('click', checkUpdates);
  $('installUpdate').addEventListener('click', installUpdate);
  $('reloadUpdate').addEventListener('click', () => location.reload());
  $('refreshProbes').addEventListener('click', loadProbes);
  $('probeNameFilter').addEventListener('change', event => { state.probeNameFilter = event.target.value; renderProbeList(); });
  $('newProbe').addEventListener('click', () => editProbe(null));
  $('testForm').addEventListener('submit', submitTest);
  $('probeForm').addEventListener('submit', saveProbe);
  $('cancelRun').addEventListener('click', cancelRun);
  $('confirmSubmit').addEventListener('click', submitConfirmation);
  formField($('probeForm'), 'group_id').addEventListener('change', () => { defaultProbeName(); loadProbeModels(''); });
  formField($('probeForm'), 'model').addEventListener('change', () => syncProbeModelSelection(true));
  formField($('testForm'), 'model').addEventListener('change', () => syncReasoningEffort($('testForm'), true));
  $('retryProbeModels').addEventListener('click', () => loadProbeModels(state.probeModels?.preferred || ''));
  for (const form of [$('testForm'), $('probeForm')]) {
    form.addEventListener('invalid', (event) => { const advanced = event.target.closest('details'); if (advanced) advanced.open = true; }, true);
    formField(form, 'kind').addEventListener('change', () => { formField(form, 'prompt').value = ''; formField(form, 'expected_answer').value = ''; updatePresets(form, undefined, true); });
    formField(form, 'preset_id').addEventListener('change', () => changePreset(form));
    formField(form, 'effort').addEventListener('change', () => syncReasoningEffort(form));
  }
  for (const dialog of document.querySelectorAll('dialog')) bindModal(dialog);
  document.addEventListener('visibilitychange', syncPreviews);
  window.matchMedia?.('(prefers-reduced-motion: reduce)').addEventListener('change', rebuildPreviewFrames);
  window.addEventListener('geekpulse-themechange', rebuildPreviewFrames);
  window.addEventListener('pageshow', event => { state.pageHidden = false; if (event.persisted) refreshUpdates(); });
  window.addEventListener('pagehide', () => { state.pageHidden = true; clearTimeout(state.updateTimer); stopPolling(true); previewObserver?.disconnect(); previewResizeObserver?.disconnect(); for (const preview of previews.values()) preview.frame?.remove(); previews.clear(); });
  window.addEventListener('storage', (event) => {
    if (!['auth_token', 'auth_user', 'refresh_token'].includes(event.key)) return;
    const session = readSession();
    if (!session.access || !session.refresh || (state.bootstrap && session.userId !== String(state.bootstrap.user.id))) showAuth('登录状态已变化，请重新打开实验室。');
  });

  async function initialize() {
    const epoch = sessionEpoch;
    const original = readSession();
    try {
      if (!readSession().access) { showAuth('请使用站点账号登录。'); return; }
      const bootstrap = await api('/bootstrap');
      assertSession(epoch, original);
      state.bootstrap = bootstrap;
      const session = readSession();
      if (session.userId !== String(state.bootstrap.user.id)) { showAuth('登录状态已变化，请重新进入。'); return; }
      $('userName').textContent = state.bootstrap.user.name || '';
      $('versionLabel').textContent = state.bootstrap.version ? 'v' + String(state.bootstrap.version).replace(/^v/, '') : '';
      $('tab-admin').hidden = state.bootstrap.user.role !== 'admin';
      const endpoint = formField($('testForm'), 'base_url');
      if (!endpoint.value) endpoint.value = bootstrap.public_origin;
      for (const form of [$('testForm'), $('probeForm')]) {
        const limits = state.bootstrap.limits || {};
        formField(form, 'max_output_tokens').max = limits.max_output_tokens || 32768;
        formField(form, 'timeout_seconds').max = limits.max_timeout_seconds || 900;
        formField(form, 'max_output_tokens').value = 64;
        formField(form, 'effort').value = 'none';
        formField(form, 'timeout_seconds').value = limits.max_timeout_seconds || 900;
        updatePresets(form, 'candy', true);
      }
      assertSession(epoch, original);
      await restorePendingRequest();
      assertSession(epoch, original);
      $('workspace').hidden = false; $('initialLoading').hidden = true;
      if (state.bootstrap.user.role === 'admin') refreshUpdates();
      await Promise.all([loadDashboard(), loadTraffic()]);
    } catch (error) { if (!state.authExpired) { $('initialLoading').hidden = true; notice(error.message + ' 请刷新页面重试。'); } }
  }
  initialize();
})();
