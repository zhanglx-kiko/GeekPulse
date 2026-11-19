'use strict';

// Run with: node --test tests/test_frontend_session.cjs
// Only synthetic sessions are used. This test never launches a browser or opens a network connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const appPath = path.join(__dirname, '..', 'web', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');
const wrapperStart = source.indexOf('(() => {');
const authStart = source.indexOf('\n', wrapperStart) + 1;
const authEnd = source.indexOf('  function notice(');
const initializeStart = source.indexOf('  async function initialize()');
const initializeEnd = source.lastIndexOf('\n  initialize();');
assert.ok(wrapperStart >= 0 && authStart > wrapperStart && authEnd > authStart,
  'The app authentication section moved; update extraction boundaries, not a copy of its implementation.');
assert.ok(initializeStart > authEnd && initializeEnd > initializeStart,
  'The actual initialize function must be included in this regression test.');

// Execute the production auth helpers and initializer unchanged. Only DOM, transport, and storage
// are replaced. This keeps a later production edit visible to the tests instead of testing a clone.
const executableSource = source.slice(authStart, authEnd) + '\n' +
  source.slice(initializeStart, initializeEnd) + '\n' +
  'globalThis.frontend = { api, refreshAccess, showAuth, initialize, state, pendingControllers };';

function fixture(t) {
  const values = new Map([
    ['auth_token', 'synthetic-access-original'],
    ['refresh_token', 'synthetic-refresh-original'],
    ['auth_user', JSON.stringify({ id: 101 })],
    ['token_expires_at', String(Date.now() + 60000)]
  ]);
  const elements = new Map();
  const timers = new Set();
  const requests = [];
  const notices = [];
  const renders = { dashboard: 0, traffic: 0 };
  const element = (id) => {
    if (!elements.has(id)) {
      const fields = new Map();
      elements.set(id, {
        hidden: true, textContent: '',
        elements: { namedItem(name) { if (!fields.has(name)) fields.set(name, {}); return fields.get(name); } }
      });
    }
    return elements.get(id);
  };
  const env = {
    AbortController,
    navigator: {},
    document: { getElementById: element, querySelectorAll: () => [] },
    localStorage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, String(value))
    },
    setTimeout(callback, delay) {
      const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay);
      timers.add(timer);
      return timer;
    },
    clearTimeout(timer) { timers.delete(timer); clearTimeout(timer); },
    stopPolling() {},
    updatePresets() {},
    notice(message) { notices.push(message); },
    async loadDashboard() { renders.dashboard++; },
    async loadTraffic() { renders.traffic++; },
    async restorePendingRequest() {},
    async fetch(url, options) {
      let resolveBody;
      const body = new Promise((resolve) => { resolveBody = resolve; });
      const request = { url, signal: options.signal, resolve: resolveBody, readingBody: false };
      requests.push(request);
      // Deliberately ignore abort while parsing JSON: epoch checks must reject even a late body
      // delivered by a transport that cannot be stopped after response headers were received.
      return { ok: true, status: 200, json() { request.readingBody = true; return body; } };
    }
  };
  vm.createContext(env);
  vm.runInContext(executableSource, env, { filename: appPath });
  t.after(() => {
    env.frontend.showAuth('Synthetic test cleanup');
    for (const request of requests) request.resolve({});
    for (const timer of timers) clearTimeout(timer);
    timers.clear();
  });
  return {
    env, values, element, notices, renders,
    async bodyPending(index = 0) {
      for (let attempt = 0; attempt < 20; attempt++) {
        if (requests[index]?.readingBody) return requests[index];
        await new Promise(setImmediate);
      }
      assert.fail('The actual app did not reach delayed response.json().');
    }
  };
}

const isSessionError = (error) => error.status === 401;
const bootstrap = () => ({
  user: { id: 101, name: 'Synthetic user', role: 'user' },
  groups: [], presets: [], limits: { max_output_tokens: 32768, max_timeout_seconds: 900 },
  version: 'synthetic', public_origin: 'https://gateway.example.com:8443'
});

test('self-test address defaults to the configured station without overwriting input', async (t) => {
  for (const previous of ['', 'https://other.example.org/v1']) {
    const f = fixture(t);
    const input = f.element('testForm').elements.namedItem('base_url');
    input.value = previous;
    const init = f.env.frontend.initialize();
    (await f.bodyPending()).resolve(bootstrap());
    await init;
    assert.deepEqual(f.notices, []);
    assert.equal(input.value, previous || bootstrap().public_origin);
    assert.equal(f.element('workspace').hidden, false);
  }
});

test('logout aborts a pending request and rejects its late JSON', async (t) => {
  const f = fixture(t);
  const pending = f.env.frontend.api('/bootstrap');
  const rejected = assert.rejects(pending, isSessionError);
  const request = await f.bodyPending();
  f.env.frontend.showAuth('Synthetic logout');
  assert.equal(request.signal.aborted, true);
  request.resolve(bootstrap());
  await rejected;
  assert.equal(f.element('workspace').hidden, true);
  assert.equal(f.env.frontend.pendingControllers.size, 0);
  assert.equal(f.values.get('auth_token'), 'synthetic-access-original');
});

test('post-JSON validation catches removed access or changed identity before a storage event', async (t) => {
  for (const changeSession of [
    (values) => values.delete('auth_token'),
    (values) => values.set('auth_user', JSON.stringify({ id: 202 }))
  ]) {
    const f = fixture(t);
    const pending = f.env.frontend.api('/bootstrap');
    const rejected = assert.rejects(pending, isSessionError);
    const request = await f.bodyPending();
    changeSession(f.values);
    request.resolve(bootstrap());
    await rejected;
    assert.equal(f.env.frontend.state.authExpired, true);
    assert.equal(f.element('workspace').hidden, true);
  }
});

test('a refresh body arriving after logout cannot persist replacement tokens', async (t) => {
  const f = fixture(t);
  const pending = f.env.frontend.refreshAccess('synthetic-access-original');
  const rejected = assert.rejects(pending, isSessionError);
  const request = await f.bodyPending();
  assert.equal(request.url, '/api/v1/auth/refresh');
  f.env.frontend.showAuth('Synthetic logout');
  assert.equal(request.signal.aborted, true);
  request.resolve({ code: 0, data: {
    access_token: 'synthetic-access-replacement', refresh_token: 'synthetic-refresh-replacement', expires_in: 3600
  } });
  await rejected;
  assert.equal(f.values.get('auth_token'), 'synthetic-access-original');
  assert.equal(f.values.get('refresh_token'), 'synthetic-refresh-original');
  assert.equal(f.env.frontend.pendingControllers.size, 0);
});

test('a response for the current session still succeeds and unregisters its controller', async (t) => {
  const f = fixture(t);
  const pending = f.env.frontend.api('/bootstrap');
  const request = await f.bodyPending();
  request.resolve(bootstrap());
  assert.equal((await pending).user.id, 101);
  assert.equal(request.signal.aborted, false);
  assert.equal(f.env.frontend.state.authExpired, false);
  assert.equal(f.env.frontend.pendingControllers.size, 0);
});

test('initialize cannot redisplay the workspace after logout during bootstrap or setup', async (t) => {
  const delayed = fixture(t);
  const pending = delayed.env.frontend.initialize();
  const request = await delayed.bodyPending();
  delayed.env.frontend.showAuth('Synthetic logout during bootstrap');
  request.resolve(bootstrap());
  await pending;
  assert.equal(delayed.element('workspace').hidden, true);
  assert.equal(delayed.env.frontend.state.bootstrap, null);
  assert.deepEqual(delayed.renders, { dashboard: 0, traffic: 0 });

  // This second timing point makes the real initializer's final display guard observable even
  // after api() has returned a valid response. Production form setup is replaced only as a hook.
  const duringSetup = fixture(t);
  duringSetup.env.updatePresets = () => duringSetup.env.frontend.showAuth('Synthetic logout during setup');
  const initialized = duringSetup.env.frontend.initialize();
  (await duringSetup.bodyPending()).resolve(bootstrap());
  await initialized;
  assert.equal(duringSetup.element('workspace').hidden, true);
  assert.equal(duringSetup.element('authGate').hidden, false);
  assert.deepEqual(duringSetup.renders, { dashboard: 0, traffic: 0 });
  assert.deepEqual(duringSetup.notices, []);
});

function sourceSection(from, to) {
  const begin = source.indexOf(from);
  const end = source.indexOf(to, begin);
  assert.ok(begin >= 0 && end > begin, 'Production UI section moved: ' + from);
  return source.slice(begin, end);
}

function uiFixture() {
  const elements = new Map();
  const makeField = (value = '') => {
    let stored = String(value);
    let markup = '';
    const details = { open: false };
    return {
      type: 'text', checked: false, readOnly: false, required: false, disabled: false,
      options: ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map(value => ({ value, disabled: false })),
      get value() { return stored; }, set value(next) { stored = String(next); },
      get innerHTML() { return markup; },
      set innerHTML(next) {
        markup = next;
        const choices = [...next.matchAll(/<option value="([^"]*)"/g)].map((match) => match[1]);
        if (!choices.includes(stored)) stored = choices[0] || '';
      },
      closest: () => details
    };
  };
  const makeForm = () => {
    const names = ['group_id', 'name', 'base_url', 'api_key', 'model', 'protocol', 'kind', 'effort', 'preset_id', 'prompt', 'expected_answer', 'interval_minutes', 'timeout_seconds', 'max_output_tokens', 'input_price', 'cached_price', 'output_price', 'display_multiplier', 'enabled', 'publish'];
    const fields = new Map(names.map((name) => [name, makeField()]));
    fields.get('enabled').type = 'checkbox'; fields.get('publish').type = 'checkbox';
    const sections = new Map([['.expected-field', { hidden: true }], ['.prompt-field', { hidden: true }], ['details.advanced', { open: false }]]);
    const form = {
      fields,
      elements: { namedItem: (name) => fields.get(name) || null, *[Symbol.iterator]() { for (const [name, field] of fields) yield {name, ...field}; } },
      querySelector: (selector) => sections.get(selector) || null,
      reset() {
        for (const field of fields.values()) { field.value = ''; field.checked = false; }
        fields.get('kind').value = 'reasoning';
        fields.get('protocol').value = 'responses';
        fields.get('max_output_tokens').value = 64;
        fields.get('effort').value = 'none';
      }
    };
    form.reset();
    return form;
  };
  elements.set('testForm', makeForm());
  elements.set('probeForm', makeForm());
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { id, innerHTML: '', textContent: '', hidden: true, open: false, showModal() {this.open=true;}, close() {this.open=false;}, replaceChildren() { this.innerHTML = ''; } });
    return elements.get(id);
  };
  const env = {
    URL,
    window: { matchMedia: () => ({ matches: false }) },
    document: { getElementById: element, querySelector:()=>[...elements.values()].find(el=>el.open)||null, body:{classList:{toggle(){}}},documentElement:{classList:{toggle(){},contains:()=>false}}, createElement(tag) { return { tagName: tag, attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } }; } },
    FormData: class {
      constructor(form) { this.form = form; }
      get(name) { return this.form.fields.get(name)?.value ?? null; }
      *[Symbol.iterator]() { for (const [name, field] of this.form.fields) if (field.type !== 'checkbox' || field.checked) yield [name, field.value]; }
    },
    formError() {}, observePreviews() {}, busy() {}, showError() {}, toast() {}, async loadProbes() {}, async loadDashboard() {}
  };
  const uiSource = source.slice(authStart, authEnd) + '\n' +
    sourceSection('  function metric(', '  async function loadDashboard(') + '\n' +
    sourceSection('  async function loadTraffic(', '  function runRows(') + '\n' +
    sourceSection('  function runRows(', '  async function loadMine(') + '\n' +
    sourceSection('  function artifactCanvas(', '  async function runDetail(') + '\n' +
    sourceSection('  function resultMetrics(', '  function renderRun(') + '\n' +
    sourceSection('  function syncModalScroll(', '  async function openDetail(') + '\n' +
    sourceSection('  function updatePresets(', '  async function submitTest(') + '\n' +
    sourceSection('  function editProbe(', '  async function probeAction(') + '\n' +
    'globalThis.ui = { state, updatePresets, changePreset, syncReasoningEffort, readTestFields, editProbe, saveProbe, renderDashboard, latestModelMetrics, resultMetrics, loadTraffic, artifactFrame, runRows };';
  vm.createContext(env);
  vm.runInContext(uiSource, env, { filename: appPath });
  env.api = async () => ({ models: ['Synthetic model'], reasoning_effort_blocks: {} });
  env.ui.state.bootstrap = {
    groups: [{ id: 1, name: 'Configured' }, { id: 2, name: 'Unconfigured' }, { id: 3, name: 'Paused' }],
    presets: [
      { id: 'candy', name: 'Candy', kind: 'reasoning', prompt: 'Fixed candy', expected_answer: '{"answer":21}' },
      { id: 'ping', name: 'Ping', kind: 'reasoning', prompt: 'Reply exactly OK.', expected_answer: 'OK' },
      { id: 'svg', name: 'SVG', kind: 'drawing', prompt: 'Draw compact SVG.', expected_answer: null },
      { id: 'custom_drawing', name: 'Custom drawing', kind: 'drawing', prompt: '', expected_answer: null },
      { id: 'custom', name: 'Custom', kind: 'reasoning', prompt: '', expected_answer: null }
    ],
    limits: { max_output_tokens: 32768, max_timeout_seconds: 900 }
  };
  return { ui: env.ui, element, setApi: (fn) => { env.api = fn; }, field: (form, name) => element(form).elements.namedItem(name) };
}

test('new self-service and admin forms select candy with 64 tokens and no reasoning', () => {
  const f = uiFixture();
  f.ui.updatePresets(f.element('testForm'), undefined, true);
  f.ui.editProbe(null);
  for (const form of ['testForm', 'probeForm']) {
    assert.equal(f.field(form, 'preset_id').value, 'candy');
    assert.equal(f.field(form, 'max_output_tokens').value, '64');
    assert.equal(f.field(form, 'effort').value, 'none');
    assert.equal(f.field(form, 'prompt').value, 'Fixed candy');
    assert.equal(f.field(form, 'prompt').readOnly, true);
    assert.equal(f.field(form, 'expected_answer').readOnly, true);
    assert.equal(f.element(form).querySelector('.prompt-field').hidden, true);
    assert.equal(f.element(form).querySelector('.expected-field').hidden, true);
  }
  assert.equal(f.field('probeForm', 'enabled').checked, false);
  assert.match(f.field('probeForm', 'group_id').innerHTML, /Unconfigured/);
});

test('reasoning choices exclude legacy connectivity metadata and preserve an existing manual budget', () => {
  const f = uiFixture();
  for (const form of ['testForm', 'probeForm']) {
    f.field(form, 'max_output_tokens').value = 4096;
    f.field(form, 'effort').value = 'high';
    f.ui.updatePresets(f.element(form), 'ping');
    assert.equal(f.field(form, 'preset_id').value, 'candy');
    assert.doesNotMatch(f.field(form, 'preset_id').innerHTML, /ping|Ping|连通性/);
    assert.match(f.field(form, 'preset_id').innerHTML, /value="custom"/);
    assert.equal(f.field(form, 'max_output_tokens').value, '4096');
    assert.equal(f.field(form, 'effort').value, 'high');
  }
});

test('self-service suggests the supported effort without changing protocol or budget', () => {
  const f = uiFixture(), form = f.element('testForm');
  const model = f.field('testForm', 'model'), effort = f.field('testForm', 'effort');
  f.ui.state.bootstrap.reasoning_effort_blocks = {
    'gpt-6.2-astra': ['none', 'minimal'], 'gpt-6.1-terra': ['minimal'],
  };
  model.value = 'openai/GPT_6.2_ASTRA';
  assert.equal(f.ui.syncReasoningEffort(form), false);
  assert.equal(effort.value, 'none');
  assert.equal(f.ui.syncReasoningEffort(form, true), true);
  assert.equal(effort.value, 'low');
  assert.deepEqual(effort.options.filter(option => option.disabled).map(option => option.value), ['none', 'minimal']);
  assert.equal(f.field('testForm', 'protocol').value, 'responses');
  assert.equal(f.field('testForm', 'max_output_tokens').value, '64');
  f.field('testForm', 'kind').value = 'drawing';
  f.ui.updatePresets(form, 'svg', true);
  assert.equal(effort.value, 'low');
  assert.equal(f.field('testForm', 'max_output_tokens').value, '1024');
  model.value = 'gpt-6.1-terra';
  f.ui.changePreset(form);
  assert.equal(effort.value, 'none');
  assert.deepEqual(effort.options.filter(option => option.disabled).map(option => option.value), ['minimal']);
  model.value = 'unknown-gateway-model';
  f.ui.syncReasoningEffort(form, true);
  assert.equal(effort.value, 'none');
  assert.ok(effort.options.every(option => !option.disabled));
});

test('unsupported model-name suffix remains invalid when the selected effort is allowed', () => {
  const f = uiFixture(), form = f.element('testForm');
  const model = f.field('testForm', 'model'), effort = f.field('testForm', 'effort');
  f.ui.state.bootstrap.reasoning_effort_blocks = {
    'gpt-6.2-astra-none': ['none', 'minimal'], 'gpt-6.1-terra-none': ['minimal'],
  };
  effort.value = 'low';
  model.value = 'openai/GPT_6.2_ASTRA_NONE';
  assert.equal(f.ui.syncReasoningEffort(form, true), false);
  assert.match(f.element('testEffortHint').textContent, /后缀/);
  assert.equal(effort.value, 'low');
  model.value = 'gpt-6.1-terra-none';
  assert.equal(f.ui.syncReasoningEffort(form), true);
  assert.equal(f.element('testEffortHint').hidden, true);
});

test('explicit preset changes apply small budgets and keep them editable', () => {
  const f = uiFixture();
  for (const form of ['testForm', 'probeForm']) {
    f.field(form, 'kind').value = 'drawing';
    f.ui.updatePresets(f.element(form), undefined, true);
    assert.equal(f.field(form, 'preset_id').value, 'svg');
    assert.equal(f.field(form, 'max_output_tokens').value, '1024');
    assert.equal(f.field(form, 'effort').value, 'none');
    assert.equal(f.element(form).querySelector('.prompt-field').hidden, false);
    assert.equal(f.element(form).querySelector('.expected-field').hidden, true);
    f.field(form, 'kind').value = 'reasoning';
    f.ui.updatePresets(f.element(form), 'candy', true);
    assert.equal(f.field(form, 'max_output_tokens').value, '64');
    assert.equal(f.field(form, 'effort').value, 'none');
    assert.equal(f.field(form, 'max_output_tokens').readOnly, false);
    assert.equal(f.field(form, 'effort').disabled, false);
    f.field(form, 'preset_id').value = 'custom';
    f.ui.changePreset(f.element(form));
    assert.equal(f.field(form, 'prompt').value, '');
    assert.equal(f.field(form, 'expected_answer').value, '');
    assert.equal(f.field(form, 'prompt').required, true);
    assert.equal(f.element(form).querySelector('.prompt-field').hidden, false);
    assert.equal(f.element(form).querySelector('.expected-field').hidden, false);
  }
});

test('custom drawing accepts HTML prompts and keeps the drawing budget', () => {
  const f = uiFixture();
  for (const form of ['testForm', 'probeForm']) {
    f.field(form, 'kind').value = 'drawing';
    f.ui.updatePresets(f.element(form), 'custom_drawing', true);
    assert.equal(f.field(form, 'preset_id').value, 'custom_drawing');
    assert.equal(f.field(form, 'max_output_tokens').value, '1024');
    assert.equal(f.field(form, 'prompt').required, true);
    assert.equal(f.element(form).querySelector('.prompt-field').hidden, false);
    f.field(form, 'prompt').value = 'Create a self-contained HTML illustration of a lighthouse.';
    const payload = f.ui.readTestFields(f.element(form));
    assert.equal(payload.preset_id, 'custom_drawing');
    assert.equal(payload.kind, 'drawing');
    assert.match(payload.prompt, /self-contained HTML illustration/);
  }
});

test('editing saved probes preserves budget, effort and custom content', () => {
  const f = uiFixture();
  f.ui.state.probes = [
    { id: 'saved-custom', group_id: 1, kind: 'reasoning', preset_id: 'custom', effort: 'high', max_output_tokens: 4096, prompt: 'Saved prompt', expected_answer: 'Saved answer' },
    { id: 'saved-svg', group_id: 1, kind: 'drawing', preset_id: 'svg', effort: 'medium', max_output_tokens: 8192, prompt: 'Saved SVG prompt' },
    { id: 'saved-candy', group_id: 3, kind: 'reasoning', preset_id: 'candy', effort: 'xhigh', max_output_tokens: 16384 }
  ];
  for (const probe of f.ui.state.probes) {
    f.ui.editProbe(probe.id);
    assert.equal(f.field('probeForm', 'max_output_tokens').value, String(probe.max_output_tokens));
    assert.equal(f.field('probeForm', 'effort').value, probe.effort);
    if (probe.prompt) assert.equal(f.field('probeForm', 'prompt').value, probe.prompt);
    if (probe.expected_answer) assert.equal(f.field('probeForm', 'expected_answer').value, probe.expected_answer);
  }
});

test('fixed candy payload omits answer overrides while custom answers can be cleared', () => {
  const f = uiFixture();
  f.ui.updatePresets(f.element('probeForm'), 'candy', true);
  const fixedPayload = f.ui.readTestFields(f.element('probeForm'));
  assert.equal(Object.hasOwn(fixedPayload, 'prompt'), false);
  assert.equal(Object.hasOwn(fixedPayload, 'expected_answer'), false);
  f.field('probeForm', 'preset_id').value = 'custom';
  f.ui.changePreset(f.element('probeForm'));
  f.field('probeForm', 'prompt').value = 'A custom question';
  const payload = f.ui.readTestFields(f.element('probeForm'));
  assert.equal(payload.prompt, 'A custom question');
  assert.equal(payload.expected_answer, '');
});

test('artwork thumbnails clip scrolling only inside the sandbox and full previews stay scrollable', () => {
  const f = uiFixture();
  const artifact = '<style>body{width:1200px;height:900px;overflow:scroll}</style><svg viewBox="0 0 1200 900"><rect width="1200" height="900"/></svg>';
  const thumbnail = f.ui.artifactFrame(artifact, 'Synthetic thumbnail');
  const full = f.ui.artifactFrame(artifact, 'Synthetic full preview', true);
  for (const frame of [thumbnail, full]) {
    assert.equal(frame.tagName, 'iframe');
    assert.equal(frame.attributes.sandbox, '');
    assert.equal(frame.attributes.referrerpolicy, 'no-referrer');
    assert.match(frame.srcdoc, /default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'/);
    assert.match(frame.srcdoc, /color-scheme:light/);
    assert.ok(frame.srcdoc.includes(artifact), 'The complete generated artwork remains inside srcdoc.');
    assert.doesNotMatch(frame.srcdoc, /<script\b|allow-same-origin|allow-scripts|https?:\/\//);
  }
  assert.equal(thumbnail.tabIndex, -1);
  assert.match(thumbnail.srcdoc.slice(thumbnail.srcdoc.lastIndexOf('<style>')), /overflow:hidden!important/);
  assert.match(thumbnail.srcdoc, /html:root body>svg\{[^}]*width:100%!important;height:auto!important/);
  assert.match(thumbnail.srcdoc, /scrollbar-width:none!important/);
  assert.equal(full.tabIndex, 0);
  assert.equal(full.className, 'artifact-large');
  const fullOverrides = full.srcdoc.slice(full.srcdoc.lastIndexOf('<style>'));
  assert.match(fullOverrides, /overflow:auto!important/);
  assert.doesNotMatch(fullOverrides, /overflow:hidden|scrollbar-width:none|height:100%!important/);
});

test('page and overflow containers share theme-aware thin scrollbars without hiding their content', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'web', 'styles.css'), 'utf8');
  assert.match(css, /\*\{scrollbar-width:thin;scrollbar-color:var\(--scrollbar\) transparent\}/);
  assert.match(css, /::-webkit-scrollbar\{width:6px;height:6px\}/);
  assert.match(css, /\.modal-body\{[^}]*overflow:auto/);
  assert.match(css, /\.output\{[^}]*overflow:auto/);
  assert.doesNotMatch(css, /(?:\.artifact-large|\.artwork-preview iframe)\{[^}]*background:white/);
  assert.match(css, /@media\(forced-colors:active\)\{\*\{scrollbar-color:auto\}\}/);
});

test('reference multipliers render once per uniform group and individually for mixed groups including zero', () => {
  const f = uiFixture();
  const model = (value) => ({ probe_id: String(value), name: 'Synthetic', model: 'Synthetic model', kind: 'reasoning', enabled: false, stats: {}, history: [], display_multiplier: value });
  const group = (value, models) => ({ id: 1, name: 'Synthetic group', stats: {}, trend: [], display_multiplier: value, models });
  f.ui.state.dashboard = { summary: {}, groups: [group(0.5, [model(0.5), model(0.5)])] };
  f.ui.renderDashboard();
  assert.equal((f.element('groupGrid').innerHTML.match(/参考倍率 ×0\.5/g) || []).length, 1);
  f.ui.state.dashboard.groups = [group(null, [model(0), model(2), model(null)])];
  f.ui.renderDashboard();
  assert.equal((f.element('groupGrid').innerHTML.match(/参考倍率 ×/g) || []).length, 2);
  assert.match(f.element('groupGrid').innerHTML, /参考倍率 ×0</);
  assert.match(f.element('groupGrid').innerHTML, /参考倍率 ×2</);
  assert.doesNotMatch(f.element('groupGrid').innerHTML, /参考倍率 ×(?:1|null|undefined)</);
  f.ui.state.dashboard.groups = [group(0, [model(0)])];
  f.ui.renderDashboard();
  assert.equal((f.element('groupGrid').innerHTML.match(/参考倍率 ×0</g) || []).length, 1);
});

test('admin multiplier overrides survive editing while clearing saves null', async () => {
  const f = uiFixture();
  for (const value of [0, 0.5, null]) {
    const probe = { id: 'saved', group_id: 1, name: 'Synthetic probe', model: 'Synthetic model', base_url: 'https://example.com', kind: 'reasoning', preset_id: 'candy', effort: 'high', max_output_tokens: 4096, display_multiplier: value };
    f.ui.state.probes = [probe];
    f.ui.editProbe('saved');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.field('probeForm', 'display_multiplier').value, value === null ? '' : String(value));
    let submitted;
    f.setApi(async (url, options) => { if (url.endsWith('/models')) return { models: ['Synthetic model'], reasoning_effort_blocks: {} }; submitted = { url, body: options.body }; return {}; });
    await f.ui.saveProbe({ preventDefault() {} });
    assert.equal(submitted.body.display_multiplier, value);
    assert.equal(submitted.body.max_output_tokens, 4096);
    assert.equal(submitted.body.effort, 'high');
    f.field('probeForm', 'display_multiplier').value = '';
    await f.ui.saveProbe({ preventDefault() {} });
    assert.equal(submitted.body.display_multiplier, null);
  }
});

test('overview excludes groups without probes and retains paused configured groups', () => {
  const f = uiFixture();
  const model = (id, enabled) => ({ probe_id: id, name: id, model: 'Synthetic model', kind: 'reasoning', effort: 'none', enabled, stats: {}, history: [] });
  f.ui.state.dashboard = { summary: {}, groups: [
    { id: 1, name: 'Configured', models: [model('active', true)], trend: [] },
    { id: 2, name: 'Unconfigured', models: [], trend: [] },
    { id: 3, name: 'Paused', models: [model('paused', false)], trend: [] }
  ] };
  f.ui.renderDashboard();
  assert.match(f.element('groupGrid').innerHTML, /Configured/);
  assert.match(f.element('groupGrid').innerHTML, /Paused/);
  assert.doesNotMatch(f.element('groupGrid').innerHTML, /Unconfigured/);
  f.ui.state.dashboard.groups = [];
  f.ui.renderDashboard();
  assert.match(f.element('groupGrid').innerHTML, /暂无探测配置/);
  assert.doesNotMatch(f.element('groupGrid').innerHTML, /group-card/);
});

test('result details omit unknown optional metrics but retain missing-usage state and known zero values', () => {
  const f = uiFixture();
  const missing = f.ui.resultMetrics({ status: 'completed', ttft_ms: null, total_tokens: null, cost_usd: null });
  assert.match(missing, /上游未返回用量/);
  assert.doesNotMatch(missing, /预估费用|缓存 Token|推理 Token|文本速率|—/);
  const measured = f.ui.resultMetrics({ status: 'passed', ttft_ms: 80, tokens_per_second: 12, total_tokens: 4, input_tokens: 2, output_tokens: 2, cached_tokens: 0, cost_usd: 0 });
  assert.match(measured, /首字延迟/);
  assert.match(measured, /文本速率/);
  assert.match(measured, /总 Token/);
  assert.match(measured, /<details class="disclosure usage-details"><summary>用量明细/);
  assert.match(measured, /<dt>缓存 Token<\/dt><dd>0<\/dd>/);
  assert.match(measured, /<dt>预估费用<\/dt><dd>\$0\.000000<\/dd>/);
  assert.doesNotMatch(measured, /上游未返回用量|推理 Token/);
});

test('fixed summary and recent completed model values are separate from channel windows and group selection', () => {
  const f = uiFixture();
  f.ui.state.dashboard = { summary: { pass_rate: 75, scored: 8, avg_duration_ms: 2000, total_tokens: 8000 }, groups: [{
    id: 1, name: 'Configured', summary: { pass_rate: 50, scored: 4, avg_duration_ms: 1000, total_tokens: 3000 }, stats: { success_rate: 90, avg_ttft_ms: 400, runs: 5 },
    models: [{ probe_id: 'paused', name: 'Paused', model: 'Synthetic', kind: 'reasoning', effort: 'none', enabled: false, stats: {pass_rate:80,scored:5}, history: [{ id: 'old-retained-run', status: 'passed', duration_ms:1234, total_tokens:567, created_at: '2020-01-01T00:00:00Z' }] }]
  }] };
  f.ui.renderDashboard();
  assert.match(f.element('summaryGrid').innerHTML, /综合不降智率[\s\S]*75\.0%/);
  assert.match(f.element('summaryGrid').innerHTML, /近期检测 Token[\s\S]*8,000/);
  assert.doesNotMatch(f.element('summaryGrid').innerHTML, /缓存|渠道成功率|首 T/);
  assert.match(f.element('groupGrid').innerHTML, /渠道成功率[\s\S]*90\.0%/);
  assert.match(f.element('groupGrid').innerHTML, /平均首 T[\s\S]*0\.40 s/);
  assert.match(f.element('groupGrid').innerHTML, /不降智率 <strong>80\.0%/);
  assert.match(f.element('groupGrid').innerHTML, /最近耗时 <strong>1\.23 s/);
  assert.match(f.element('groupGrid').innerHTML, /最近 Token <strong>567/);
  assert.doesNotMatch(f.element('groupGrid').innerHTML, /缓存|次答题检测/);
  assert.doesNotMatch(f.element('groupGrid').innerHTML.match(/class="model-row"[\s\S]*$/)[0], /近 \d+ 小时/);
  assert.match(f.element('groupGrid').innerHTML, />none</);
  assert.match(f.element('groupGrid').innerHTML, /最近 1 次运行/);
  assert.doesNotMatch(f.element('groupGrid').innerHTML, /polyline|mini-chart|监控趋势/);
  f.ui.state.hours = 24; f.ui.state.groupFilter = '1'; f.ui.renderDashboard();
  assert.match(f.element('summaryGrid').innerHTML, /50\.0%/);
  assert.doesNotMatch(f.element('summaryGrid').innerHTML, /75\.0%/);
  assert.equal(f.element('summaryScope').textContent,'Configured · 近 24 小时答题');
  assert.match(f.element('groupGrid').innerHTML, /最近 1 次运行/);
  assert.match(f.element('groupGrid').innerHTML, /近 24 小时/);
});

test('image probes remain at the bottom with successful newest-first works and no error cards or answer metrics', () => {
  const f=uiFixture();
  const history=Array.from({length:60},(_,index)=>({id:'image-'+index,model:'Image model',kind:'drawing',status:index===10?'error':'completed',has_artifact:index!==10,created_at:new Date(Date.UTC(2026,10,17,1,0,60-index)).toISOString()}));
  f.ui.state.dashboard={summary:{},groups:[{id:1,name:'Configured',summary:{},stats:{},models:[{probe_id:'qa',model:'QA model',stats:{},history:[]}],drawing_models:[{probe_id:'drawing',model:'Image model',history}]}]};
  f.ui.renderDashboard(); const markup=f.element('groupGrid').innerHTML;
  assert.ok(markup.indexOf('模型检测')<markup.indexOf('图片检测'));
  const drawing=markup.slice(markup.indexOf('class="drawing-section"'));
  assert.equal((drawing.match(/data-action="detail"/g)||[]).length,59);
  assert.doesNotMatch(drawing,/data-id="image-10"/);
  assert.ok(drawing.indexOf('data-id="image-0"')<drawing.indexOf('data-id="image-59"'));
  assert.match(drawing,/class="artwork-carousel"[^>]*tabindex="0"/);
  assert.match(drawing,/data-action="carousel-prev"/); assert.match(drawing,/data-action="carousel-next"/);
  assert.doesNotMatch(drawing,/status-pill|history-mark|不降智率|渠道成功率|缓存|首 T/);
  assert.doesNotMatch(drawing,/最新在前/);
  assert.doesNotMatch(drawing,/暂无作品|生成错误/);
  f.ui.state.dashboard.groups[0].models=[];f.ui.renderDashboard();
  assert.match(f.element('groupGrid').innerHTML,/Image model/);
  assert.doesNotMatch(f.element('groupGrid').innerHTML,/模型检测|渠道成功率/);
});

test('recent model metrics use one completed answer and ignore newer unfinished or unsuccessful requests', () => {
  const f = uiFixture();
  const history = [
    { status: 'passed', duration_ms: 1000, total_tokens: 100 },
    { status: 'failed', duration_ms: 2000, total_tokens: 200 },
    ...['queued', 'running', 'error', 'cancelled'].map(status => ({ status, duration_ms: 9000, total_tokens: 900 })),
    { kind: 'drawing', status: 'completed', duration_ms: 8000, total_tokens: 800 }
  ];
  const markup = f.ui.latestModelMetrics({ history });
  assert.match(markup, /最近耗时 <strong>2\.00 s/);
  assert.match(markup, /最近 Token <strong>200/);
  assert.doesNotMatch(markup, /1\.00|9\.00|8\.00|900|800/);
  f.ui.state.hours = 24;
  assert.equal(f.ui.latestModelMetrics({ history }), markup);
  assert.equal(history[0].total_tokens, 100, 'Reading recent metrics must not reorder history.');
});

test('recent completed unscored answers retain zero and never borrow missing values from an older answer', () => {
  const f = uiFixture();
  const older = { status: 'passed', duration_ms: 5000, total_tokens: 1000 };
  const zero = f.ui.latestModelMetrics({ history: [older, { status: 'completed', duration_ms: 0, total_tokens: 0 }] });
  assert.match(zero, /最近耗时 <strong>0\.00 s/);
  assert.match(zero, /最近 Token <strong>0/);
  const partial = f.ui.latestModelMetrics({ history: [older, { status: 'completed', duration_ms: null, total_tokens: 4 }] });
  assert.match(partial, /最近 Token <strong>4/);
  assert.doesNotMatch(partial, /最近耗时|5\.00|1,000/);
  assert.equal(f.ui.latestModelMetrics({ history: [older, { status: 'completed' }] }), '');
  assert.equal(f.ui.latestModelMetrics({ history: [{ status: 'error', duration_ms: 1 }] }), '');
});

test('ungrouped probes retain their null identity when selecting a single monitored group', () => {
  const f=uiFixture();
  const model=(id)=>({probe_id:id,model:'Synthetic',stats:{},history:[]});
  f.ui.state.dashboard={summary:{pass_rate:90},groups:[{id:null,name:'未分组',summary:{pass_rate:25,scored:4},models:[model('ungrouped')]},{id:1,name:'Named group',summary:{pass_rate:100},models:[model('named')]}]};
  f.ui.renderDashboard();
  assert.match(f.element('groupFilter').innerHTML, /<option value="null">未分组<\/option>/);
  f.ui.state.groupFilter='null';f.ui.renderDashboard();
  assert.equal(f.ui.state.groupFilter,'null');assert.equal(f.element('groupFilter').value,'null');
  assert.equal(f.element('summaryScope').textContent,'未分组 · 近 24 小时答题');
  assert.match(f.element('summaryGrid').innerHTML,/25\.0%/);assert.doesNotMatch(f.element('summaryGrid').innerHTML,/90\.0%/);
  assert.match(f.element('groupGrid').innerHTML,/未分组/);assert.doesNotMatch(f.element('groupGrid').innerHTML,/Named group/);
});

test('recent self-service answers show native effort, outcome, timestamp, input and output tokens, and terminal duration', () => {
  const f=uiFixture();
  const markup=f.ui.runRows([{id:'pass',model:'Native model',effort:'xhigh',status:'passed',created_at:'2026-11-17T01:02:03Z',input_tokens:0,output_tokens:120,duration_ms:1500},{id:'error',model:'Native model',effort:'none',status:'error',created_at:'2026-11-17T01:03:03Z',input_tokens:null,output_tokens:null}]);
  assert.match(markup,/Native model/);assert.match(markup,/>xhigh</);assert.match(markup,/>none</);
  assert.match(markup,/>不降智</);assert.match(markup,/>请求错误</);
  assert.match(markup,/输入 Token <strong>0/);assert.match(markup,/输出 Token <strong>120/);
  assert.match(markup,/11\/17/);assert.match(markup,/耗时 1\.50 s/);
  assert.match(markup,/输入 Token 未返回/);assert.doesNotMatch(markup,/答题通过|答案未通过|最高|耗时 —/);
});

test('disabled traffic monitoring stays hidden while v2 metrics remain available in a disclosure', async () => {
  const f = uiFixture();
  f.setApi(async () => ({ channel_monitor_enabled: true, channel_monitor_mode: 'v1' }));
  await f.ui.loadTraffic();
  assert.equal(f.element('trafficSection').hidden, true);
  f.setApi(async (url) => url === '/settings/public'
    ? { channel_monitor_enabled: true, channel_monitor_mode: 'v2' }
    : { metrics: { success_rate: .9, cache_rate: .32, ttft: { p50_ms: 100, p95_ms: 300 } }, coverage: { coverage_complete: true } });
  await f.ui.loadTraffic();
  assert.equal(f.element('trafficSection').hidden, false);
  assert.match(f.element('trafficContent').innerHTML, /90\.0%/);
});

test('form advanced parameters remain labeled and reachable with a shared site icon', () => {
  const markup = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const forms = [...markup.matchAll(/<form\b[^>]*id="(testForm|probeForm)"[\s\S]*?<\/form>/g)];
  assert.equal(forms.length, 2);
  for (const [form] of forms) {
    const advanced = form.match(/<details class="advanced[^\"]*">[\s\S]*?<\/details>/)?.[0];
    assert.ok(advanced);
    for (const field of ['protocol', 'effort', 'max_output_tokens', 'timeout_seconds']) assert.match(advanced, new RegExp('name="' + field + '"'));
    assert.match(form, /class="field full prompt-field" hidden/);
    assert.match(form, /class="field full expected-field" hidden/);
  }
  assert.equal((markup.match(/\$\{logo_path\}/g) || []).length, 2);
  assert.doesNotMatch(markup, /name="base_url"[^>]*\bvalue=/);
  assert.doesNotMatch(markup, /section-kicker|MODEL LAB|PLAYGROUND|RUN DETAIL/);
});
