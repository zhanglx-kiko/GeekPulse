'use strict';
// Production handlers with synthetic DOM/transport. No browser or upstream calls.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const section = (a, b) => { const start = source.indexOf(a); const end = source.indexOf(b, start); assert.ok(start >= 0 && end > start); return source.slice(start, end); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const turn = () => new Promise((resolve) => setImmediate(resolve));
const updateTarget = { tag: 'v1.4.0', revision: 'a'.repeat(40) };
const installedUpdate = { phase: 'installed', tag: updateTarget.tag, updated_at: 'new' };
const updateView = { current: '1.3.0', revision: 'b'.repeat(40), source: 'ok', checked_at: 1, latest: updateTarget, available: true, managed: true, operation: null };
async function refreshUpdate(f, changes = {}) {
  const pending = f.ui.refreshUpdates();
  assert.equal(f.requests.at(-1).url, '/admin/updates');
  f.requests.at(-1).resolve({ ...updateView, ...changes });
  await pending;
}

test('the update card has one action and upgrades require confirmation, even with running probes', async () => {
  const f = modalFixture(), button = f.el('updateAction');
  const markup = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.equal((markup.match(/id="updateAction"/g) || []).length, 1);
  assert.doesNotMatch(markup, /id="(?:checkUpdates|installUpdate|reloadUpdate|releaseBadge)"/);
  f.ui.state.probes[0].enabled = true;
  f.ui.state.probeRequests.set('a', { runId: 'already-running' });
  await refreshUpdate(f);
  assert.equal(button.textContent, '升级到 v1.4.0');
  f.ui.updateAction();
  assert.equal(f.el('confirmCurrentVersion').textContent, 'v1.3.0');
  assert.equal(f.el('confirmTargetVersion').textContent, 'v1.4.0');
  assert.match(f.el('confirmDescription').textContent, /可能中断/);
  assert.equal(f.document.activeElement, f.el('confirmCancel'));
  f.ui.closeModal(f.el('confirmDialog')); f.flushCloses();
  assert.equal(f.requests.length, 1);
  f.ui.updateAction();
  const submit = f.ui.submitConfirmation();
  await f.ui.submitConfirmation();
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[1].url, '/admin/updates/install');
  assert.equal(JSON.stringify(f.requests[1].options.body), JSON.stringify(updateTarget));
  f.requests[1].resolve({ accepted: true });
  await submit; f.flushCloses();
  assert.equal(button.disabled, true);
  assert.equal(f.reloads, 0, 'Acceptance is not proof of activation.');
  assert.equal(f.ui.state.probes[0].enabled, true);
  f.requests[2].resolve({ ...updateView, operation: { ...installedUpdate, phase: 'installing' } });
  await turn();
  assert.equal(button.textContent, '升级中…');
});

test('checking uses the same action and waits for a new release result', async () => {
  const f = modalFixture(), button = f.el('updateAction');
  await refreshUpdate(f, { available: false });
  assert.equal(button.textContent, '检查更新');
  f.ui.updateAction();
  f.ui.updateAction();
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[1].url, '/admin/updates/check');
  assert.equal(button.textContent, '检查中…');
  f.requests[1].resolve({ accepted: true });
  await turn();
  f.requests[2].resolve({ ...updateView, available: false });
  await turn();
  assert.equal(button.disabled, true);
  await refreshUpdate(f, { checked_at: 2 });
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '升级到 v1.4.0');
});

test('a changed release or unavailable upgrader invalidates the open confirmation', async () => {
  for (const changed of [{ latest: { ...updateTarget, revision: 'c'.repeat(40) } }, { managed: false }, { operation: { phase: 'installing' } }]) {
    const f = modalFixture();
    await refreshUpdate(f);
    f.ui.updateAction();
    await refreshUpdate(f, changed);
    await f.ui.submitConfirmation();
    assert.equal(f.el('confirmDialog').open, true);
    assert.match(f.el('confirmError').textContent, /状态已变化/);
    assert.deepEqual(f.requests.map(req => req.url), ['/admin/updates', '/admin/updates']);
  }
});

test('a lost response and restart stay under observation until the requested commit is installed', async () => {
  const f = modalFixture();
  await refreshUpdate(f);
  f.ui.updateAction();
  const submit = f.ui.submitConfirmation();
  f.requests[1].reject(new Error('Connection lost'));
  await submit; f.flushCloses();
  f.requests[2].reject(new Error('Restarting'));
  await turn();
  assert.match(f.el('updateStatus').textContent, /等待 GeekPulse 恢复/);
  assert.equal(f.el('updateAction').disabled, true);
  assert.equal(f.lastDelay, 2000);
  await refreshUpdate(f, { operation: installedUpdate, current: '1.4.0', revision: 'c'.repeat(40) });
  await refreshUpdate(f, { operation: { ...installedUpdate, tag: 'v1.5.0' }, current: '1.4.0', revision: updateTarget.revision });
  assert.equal(f.reloads, 0);
  await refreshUpdate(f, { operation: installedUpdate, current: '1.4.0', revision: updateTarget.revision, available: false });
  assert.equal(f.reloads, 1);
  await f.ui.refreshUpdates();
  assert.equal(f.reloads, 1);
  assert.equal(f.requests.filter(req => req.options?.method === 'POST').length, 1);
});

test('rollback, upgrades by other pages and logout never reload this page', async () => {
  const f = modalFixture();
  await refreshUpdate(f, { operation: installedUpdate, current: '1.4.0', revision: updateTarget.revision, available: false });
  assert.equal(f.reloads, 0);
  const stale = { ...installedUpdate, phase: 'rolled_back', updated_at: 'old' };
  await refreshUpdate(f, { operation: stale });
  f.ui.updateAction();
  const submit = f.ui.submitConfirmation();
  f.requests[2].resolve({ accepted: true });
  await submit;
  f.requests[3].resolve({ ...updateView, operation: stale });
  await turn();
  assert.equal(f.el('updateAction').disabled, true, 'A previous rollback is not a result for this request.');
  await refreshUpdate(f, { operation: { ...stale, updated_at: 'retry' } });
  assert.equal(f.el('updateAction').disabled, false);
  assert.match(f.el('updateStatus').textContent, /已恢复原版本/);
  assert.equal(f.reloads, 0);
  f.flushCloses(); f.ui.updateAction();
  const toLogout = f.ui.submitConfirmation();
  f.requests[5].resolve({ accepted: true });
  await toLogout;
  f.ui.showAuth();
  f.requests[6].resolve({ ...updateView, operation: installedUpdate, current: '1.4.0', revision: updateTarget.revision });
  await turn();
  assert.equal(f.ui.state.updateAttempt, null);
  assert.equal(f.reloads, 0);
});

function fixture() {
  const elements = new Map(), requests = [], storage = new Map(), observers = [], timers = new Map();
  let timerId = 0, reduced = false, dark = false;
  const node = () => ({ innerHTML: '', textContent: '', disabled: false, hidden: false, isConnected: true, dataset: {}, style: {}, children: [], attributes: {},
    setAttribute(k,v) { this.attributes[k]=v; }, querySelector() { return null; }, querySelectorAll() { return []; }, focus() {},
    insertAdjacentHTML(_, text) { this.innerHTML += text; }, prepend(child) { this.children.unshift(child); child.parent = this; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }, showModal() {}, close() {} });
  const el = (id) => { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); };
  const keyField = { value: 'test-only-key' };
  el('testForm').elements = { namedItem: () => keyField };
  const buttons = [el('probe-a'),el('probe-b')];
  buttons[0].dataset.id='a'; buttons[1].dataset.id='b';
  const env = { URL, crypto, console,
    document: { hidden:false, documentElement:{classList:{contains:()=>dark}}, getElementById:el, querySelectorAll:(selector)=>selector==='[data-action="probe-run"]'?buttons:[], createElement:node },
    window: { IntersectionObserver:true, matchMedia:()=>({matches:reduced}) },
    IntersectionObserver:class { constructor(callback) { this.callback=callback; observers.push(this); } observe(){} unobserve(){} },
    sessionStorage:{ setItem:(k,v)=>storage.set(k,v), getItem:(k)=>storage.get(k)??null, removeItem:(k)=>storage.delete(k) },
    setTimeout:(fn)=>{ timers.set(++timerId,fn); return timerId; }, clearTimeout:(id)=>timers.delete(id),
    confirm:()=>true, notice(){}, toast(){}, formError(){}, async loadMine(){}, async loadDashboard(){}, async loadProbes(){},
    async selectTab(){}, editProbe(){}, probePayload:()=>({}),
    syncReasoningEffort: () => true,
    readTestFields:()=>({base_url:'https://example.com',model:'test-model',api_key:keyField.value,kind:'reasoning',protocol:'responses',effort:'none'}),
    api(url,options={}) { const d=deferred(); requests.push({url,method:options.method||'GET',body:options.body?JSON.parse(JSON.stringify(options.body)):null,...d}); return d.promise; }
  };
  const code = section('  const $ =', '  let refreshPromise') +
    section('  function busy(', '  async function selectTab(') +
    section('  function metric(', '  function artifactCanvas(') +
    section('  function artifactCanvas(', '  function resultMetrics(') +
    section('  function resultMetrics(', '  async function openDetail(') +
    section('  function validateBaseURL(', '  async function loadProbes(') +
    section('  async function probeAction(', "  document.addEventListener('click'") +
    'globalThis.ui={state,submitTest,probeAction,recoverRequest,recoverProbeRequest,restorePendingRequest,pollRun,pollProbeRun,renderRun,artifactCanvas,fitPreview,artifactFrame,trackPreview,syncPreviews,rebuildPreviewFrames,previews,runDetail,artworkCard,runRows,historyStrip,loadMine};';
  vm.createContext(env); vm.runInContext(code,env);
  env.ui.state.bootstrap={user:{id:1,role:'admin'},receipt_namespace:'fixture-lab'};
  env.ui.state.probes=[{id:'a',model:'test-model',has_key:true},{id:'b',model:'test-model',has_key:true}];
  return {env,ui:env.ui,el,buttons,keyField,requests,storage,timers,node,setReduced:(v)=>{reduced=v;},setDark:(v)=>{dark=v;},intersect:(node,visible)=>observers[0].callback([{target:node,isIntersecting:visible}])};
}
const submit = (f) => f.ui.submitTest({preventDefault(){}});
test('rapid click and Enter share one submission and lock until terminal confirmation',async()=>{
  const f=fixture(); const first=submit(f); await submit(f); await submit(f);
  assert.equal(f.requests.length,1); assert.equal(f.el('submitTest').disabled,true);
  f.requests[0].resolve({id:'one',status:'queued'}); await first;
  assert.equal(f.requests.length,2); await submit(f); assert.equal(f.requests.length,2);
  assert.equal(f.el('submitTest').disabled,true);
  f.requests[1].resolve({id:'one',status:'passed',duration_ms:1200}); await turn();
  assert.equal(f.el('submitTest').disabled,false); assert.equal(f.ui.state.pendingRequest,null);
  assert.equal(f.storage.size,0);
});
test('unknown POST outcome remains locked; recovery reads original task without another POST',async()=>{
  const f=fixture(); const first=submit(f); f.requests[0].reject(new Error('offline')); await first;
  await submit(f); assert.equal(f.requests.length,1); assert.equal(f.el('submitTest').disabled,true);
  const recovery=f.ui.recoverRequest(); await f.ui.recoverRequest();
  assert.equal(f.requests[1].method,'GET'); assert.match(f.requests[1].url,/^\/requests\//);
  f.requests[1].resolve({id:'original',status:'running'}); await recovery;
  assert.equal(f.requests.filter(r=>r.method==='POST').length,1);
  assert.equal(f.ui.state.currentRun.id,'original');
});
test('not-found recovery permits explicit retry with same UUID and no stored key',async()=>{
  const f=fixture(); const first=submit(f); const id=f.requests[0].body.request_id;
  f.requests[0].reject(new Error('offline')); await first;
  assert.doesNotMatch([...f.storage.values()].join(''),/test-only-key|api_key|base_url|prompt/);
  const recovery=f.ui.recoverRequest(); f.requests[1].reject({status:404,code:'request_not_found'}); await recovery;
  assert.equal(f.el('submitTest').disabled,false); f.keyField.value='test-only-key';
  const retry=submit(f); assert.equal(f.requests[2].body.request_id,id);
  f.requests[2].reject({status:400,message:'invalid'}); await retry;
  assert.equal(f.ui.state.pendingRequest.requestId,id);
  assert.equal(f.ui.state.pendingRequest.retryAllowed,true);
});
test('different admin probes run concurrently while the same probe remains locked through rerender',async()=>{
  const f=fixture(); const first=f.ui.probeAction('probe-run','a',f.buttons[0]);
  await f.ui.probeAction('probe-run','a',f.node());
  const second=f.ui.probeAction('probe-run','b',f.buttons[1]);
  assert.equal(f.requests.length,2); assert.equal(f.buttons[0].disabled,true); assert.equal(f.buttons[1].disabled,true);
  assert.equal(f.el('submitTest').disabled,false);
  f.requests[0].resolve({id:'admin-one',status:'queued'}); await first;
  assert.equal(f.buttons[0].disabled,true); await f.ui.probeAction('probe-run','a',f.node());
  assert.equal(f.requests.filter(r=>r.method==='POST').length,2);
  f.requests[1].resolve({id:'admin-two',status:'queued'}); await second;
  f.requests[2].resolve({id:'admin-one',status:'completed'}); await turn();
  assert.equal(f.buttons[0].disabled,false);
  assert.equal(f.buttons[1].disabled,true);
  f.requests[3].resolve({id:'admin-two',status:'passed'}); await turn();
  assert.equal(f.buttons[1].disabled,false); assert.equal(f.storage.size,0);
});
test('receipt restores after reload using GET only and never under a different user',async()=>{
  const f=fixture(); const receipt={requestId:crypto.randomUUID(),userId:'1',probeId:null};
  f.storage.set('fixture-lab-pending',JSON.stringify(receipt));
  const restore=f.ui.restorePendingRequest(); await turn(); assert.equal(f.requests[0].method,'GET');
  f.requests[0].resolve({id:'restored',status:'running'}); await restore;
  assert.equal(f.ui.state.currentRun.id,'restored');
  const other=fixture(); other.storage.set('fixture-lab-pending',JSON.stringify({...receipt,userId:'2'}));
  await other.ui.restorePendingRequest(); assert.equal(other.requests.length,0);
});
test('reload restores multiple admin probes in parallel without blocking self-service or creating paid requests',async()=>{
  const f=fixture();
  f.storage.set('fixture-lab-probes',JSON.stringify({userId:'1',requests:[{requestId:crypto.randomUUID(),probeId:'a'},{requestId:crypto.randomUUID(),probeId:'b'}]}));
  const restore=f.ui.restorePendingRequest(); await turn();
  assert.equal(f.requests.length,2); assert.ok(f.requests.every(r=>r.method==='GET'));
  assert.equal(f.el('submitTest').disabled,false);
  f.requests[0].resolve({id:'a-run',status:'running'}); f.requests[1].resolve({id:'b-run',status:'running'}); await restore;
  assert.equal(f.ui.state.probeRequests.size,2); assert.equal(f.requests.length,4);
  f.requests[2].resolve({id:'a-run',status:'passed'}); f.requests[3].resolve({id:'b-run',status:'passed'}); await turn();
  assert.equal(f.ui.state.probeRequests.size,0); assert.equal(f.storage.size,0);
  assert.ok(f.requests.every(r=>r.method==='GET'));
});
test('unknown admin submission can be recovered or explicitly resubmitted with its original receipt',async()=>{
  const f=fixture(); const first=f.ui.probeAction('probe-run','a',f.buttons[0]); const requestId=f.requests[0].body.request_id;
  f.requests[0].reject(new Error('offline')); await first;
  assert.equal(f.buttons[0].disabled,true); assert.equal(f.buttons[1].disabled,false);
  const recover=f.ui.recoverProbeRequest('a'); f.requests[1].reject({status:404,code:'request_not_found'}); await recover;
  const retry=f.ui.probeAction('probe-run','a',f.node(),true); assert.equal(f.requests[2].body.request_id,requestId);
  assert.doesNotMatch([...f.storage.values()].join(''),/api_key|test-only-key|prompt|base_url/);
  f.requests[2].resolve({id:'a-run',status:'running'}); await retry;
  f.requests[3].resolve({id:'a-run',status:'passed'}); await turn();
  assert.equal(f.ui.state.probeRequests.size,0);
});
test('poll retries are single-flight',async()=>{
  const f=fixture(); f.ui.state.currentRun={id:'one',status:'running'};
  const first=f.ui.pollRun(); await f.ui.pollRun(); assert.equal(f.requests.length,1);
  f.requests[0].resolve({id:'one',status:'running'}); await first; assert.equal(f.timers.size,1);
});
test('duration appears only below terminal status and running has no elapsed ticker',()=>{
  const f=fixture(), target=f.node();
  f.ui.renderRun(target,{model:'test',status:'running',duration_ms:1200});
  assert.doesNotMatch(target.innerHTML,/data-elapsed|result-duration|耗时/);
  f.ui.renderRun(target,{model:'test',status:'passed',duration_ms:1200});
  assert.match(target.innerHTML,/class="result-status">[\s\S]*不降智<\/span><span class="result-duration">耗时 1\.20 s/);
  assert.doesNotMatch(source,/elapsedTimer|updateElapsed|setInterval/);
});
test('SVG animation remains sandboxed and reduced-motion removes SMIL and disables CSS motion',()=>{
  const f=fixture(); const art='<svg><g><animateTransform attributeName="transform" type="rotate" dur="2s"></animateTransform><circle r="20"></circle></g></svg>';
  const frame=f.ui.artifactFrame(art,'test');
  assert.match(frame.srcdoc,/<animateTransform/); assert.equal(frame.attributes.sandbox,'');
  assert.doesNotMatch(frame.srcdoc,/<script|allow-scripts|allow-same-origin/);
  f.setReduced(true); const reduced=f.ui.artifactFrame(art,'test');
  assert.doesNotMatch(reduced.srcdoc,/<animateTransform/); assert.match(reduced.srcdoc,/<circle/);
  assert.match(reduced.srcdoc,/animation:none!important/);
});
test('leaving viewport or hiding page removes animation frame and restores cached srcdoc without requests',()=>{
  const f=fixture(), host=f.node();
  f.ui.trackPreview(host,{artifact:'<svg><circle r="20"/></svg>',title:'test'});
  assert.equal(host.children.length,0); f.intersect(host,true); assert.equal(host.children.length,1);
  f.intersect(host,false); assert.equal(host.children.length,0);
  f.intersect(host,true); assert.equal(host.children.length,1);
  f.env.document.hidden=true; f.ui.syncPreviews(); assert.equal(host.children.length,0);
  f.env.document.hidden=false; f.ui.syncPreviews(); assert.equal(host.children.length,1);
  assert.equal(f.requests.length,0); assert.equal(f.timers.size,0);
});
test('lazy preview detail loads once and animation cycles never fetch it again',async()=>{
  const f=fixture(), host=f.node(); host.dataset.preview='art';
  f.ui.trackPreview(host); f.intersect(host,true); f.intersect(host,true);
  assert.equal(f.requests.length,1);
  f.requests[0].resolve({id:'art',status:'completed',model:'test',artifact_html:'<svg><circle r="20"/></svg>'}); await turn();
  for(let i=0;i<10;i++){f.intersect(host,false);f.intersect(host,true);}
  assert.equal(f.requests.length,1); assert.equal(host.children.length,1);
});
test('full HTML layouts are rendered on a complete virtual canvas and scaled together inside the sandbox',()=>{
  const f=fixture(),host=f.node(); host.clientWidth=220;host.clientHeight=150;
  const art='<style>body{padding:24px}main{width:1000px}header{height:80px}svg{width:100%}.wheel{animation:spin 1s infinite}</style><main><header>Scene title</header><svg viewBox="0 0 1000 620"><g class="wheel"><circle r="30"/></g></svg><footer>Scene footer</footer></main>';
  f.ui.trackPreview(host,{artifact:art,title:'HTML layout'}); f.intersect(host,true);
  const preview=f.ui.previews.get(host),frame=host.children[0];
  assert.ok(preview.canvas.width>=1000); assert.ok(preview.canvas.height>=820);
  assert.match(frame.style.transform,/^scale\(0\./); assert.equal(frame.style.transformOrigin,'top left');
  assert.ok(frame.srcdoc.includes(art)); assert.equal(frame.attributes.sandbox,'');
  assert.doesNotMatch(frame.srcdoc,/allow-scripts|allow-same-origin|<script\b/);
  f.intersect(host,false);f.intersect(host,true);assert.equal(f.requests.length,0);
});
test('theme changes rebuild visible artwork frames from memory without fetching details or changing original artwork styles',()=>{
  const f=fixture(),host=f.node(),art='<style>body{background:#fedcba;color:#123456}</style><svg><circle r="20"/></svg>';
  f.ui.trackPreview(host,{artifact:art,title:'Artwork'});f.intersect(host,true);
  assert.match(host.children[0].srcdoc,/color-scheme:light/);
  f.setDark(true);f.ui.rebuildPreviewFrames();
  assert.match(host.children[0].srcdoc,/color-scheme:dark/);
  assert.ok(host.children[0].srcdoc.includes(art));assert.equal(f.requests.length,0);
  f.intersect(host,false);f.setDark(false);f.ui.rebuildPreviewFrames();assert.equal(host.children.length,0);assert.equal(f.requests.length,0);
});

function modalFixture() {
  const elements = new Map(), closes = [], requests = [], rendered = [], errors = [], locks = new Set();
  let maxOpen = 0, deferCatalog = false, reloads = 0, lastDelay = 0;
  const classList = (prefix) => ({ toggle(name, value) { if (value) locks.add(prefix + name); else locks.delete(prefix + name); } });
  const document = { activeElement: null, body: { classList: classList('body:') }, documentElement: { classList: classList('html:') } };
  const node = (id) => ({ id, open: false, isConnected: true, innerHTML: '', textContent: '', hidden: false, disabled: false, dataset: {}, attributes: {}, scrollTop: 0, listeners: new Map(),
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(fn); },
    emit(type, event = {}) { for (const fn of this.listeners.get(type) || []) fn({ target: this, ...event }); },
    showModal() { this.open = true; document.activeElement = this; maxOpen = Math.max(maxOpen, [...elements.values()].filter(el => el.open).length); },
    close() { if (this.open) { this.open = false; closes.push(() => this.emit('close')); } },
    focus(options) { document.activeElement = this; this.focusOptions = options; },
    replaceChildren() { this.innerHTML = ''; },
    getBoundingClientRect() { return { left: 100, right: 500, top: 100, bottom: 500 }; }
  });
  const el = (id) => { if (!elements.has(id)) elements.set(id, node(id)); return elements.get(id); };
  const fields = new Map(['group_id','name','model','protocol','kind','effort','preset_id','prompt','expected_answer','interval_minutes','timeout_seconds','max_output_tokens','input_price','cached_price','output_price','display_multiplier','enabled','publish'].map(name => {
    let value = '', markup = '';
    return [name, { name, type: ['enabled','publish'].includes(name) ? 'checkbox' : 'text', checked: false,
      options: ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map(value => ({ value, disabled: false })),
      get value() { return value; }, set value(next) { value = String(next); },
      get innerHTML() { return markup; }, set innerHTML(next) { markup = next; const choices = [...next.matchAll(/<option value="([^"]*)"/g)].map(match => match[1]); if (!choices.includes(value)) value = choices[0] || ''; }
    }];
  }));
  const advanced = { open: false }, sections = new Map([['details.advanced', advanced], ['.prompt-field', {}], ['.expected-field', {}]]);
  const form = el('probeForm');
  form.elements = { namedItem: name => fields.get(name) || null, *[Symbol.iterator]() { yield* fields.values(); } };
  form.querySelector = selector => sections.get(selector);
  form.reset = () => { for (const field of fields.values()) { field.value = ''; field.checked = false; } fields.get('kind').value = 'reasoning'; };
  document.getElementById = el;
  document.querySelector = selector => selector === 'dialog[open]' ? [...elements.values()].find(el => el.open) || null : null;
  document.querySelectorAll = selector => selector === 'dialog[open]' ? [...elements.values()].filter(el => el.open) : [];
  const env = { document, URL, console, window: { location: { reload() { reloads++; } } },
    setTimeout(_callback, delay) { lastDelay = delay; return 1; },
    FormData: class { constructor(form) { this.form = form; } get(name) { return this.form.elements.namedItem(name)?.value ?? null; } *[Symbol.iterator]() { for (const field of this.form.elements) if (!field.disabled && (field.type !== 'checkbox' || field.checked)) yield [field.name, field.value]; } },
    localStorage: { setItem() { throw new Error('Modal drafts must not be persisted.'); } },
    sessionStorage: { setItem() { throw new Error('Modal drafts must not be persisted.'); } },
    prunePreviews() {}, stopPolling() {}, syncPreviews() {}, toast() {},
    busy(button, value) { button.disabled = value; },
    formError(id, message) { errors.push({ id, message }); },
    renderRun(target, run) { target.innerHTML = run.id; rendered.push(run.id); },
    showError(target, error) { target.innerHTML = error.message; errors.push(error); },
    runRows: runs => runs.map(run => run.id).join(','),
    runDetail(id) { const pending = deferred(); requests.push({ id, ...pending }); return pending.promise; },
    api(url, options) { if (/^\/admin\/groups\/.*\/models$/.test(url) && !deferCatalog) return Promise.resolve({ models: ['Model a', 'Model b'], reasoning_effort_blocks: {} }); const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; },
    async loadProbes() {}, async loadDashboard() {}, async loadMine() {}, async loadArtworks() {}, saveProbeRequests() {}, syncTestButtons() {}, clearTimeout() {}
  };
  const code = section('  const $ =', '  let refreshPromise') +
    'let sessionEpoch=0;const pendingControllers=new Set();' +
    section('  function showAuth(', '  async function api(') +
    section('  function syncModalScroll(', '  function updatePresets(') +
    section('  function updatePresets(', '  async function submitTest(') +
    section('  function cancelRun(', '  async function loadProbes(') +
    section('  function updateActive(', '  async function probeAction(') +
    section('  async function probeAction(', "  document.addEventListener('click'") +
    'globalThis.ui={state,bindModal,openModal,closeModal,openHistory,openDetail,editProbe,saveProbe,showAuth,openConfirmation,submitConfirmation,requestRunDeletion,requestProbeDeletion,probeAction,loadProbeModels,syncProbeModelSelection,readProbeFields,defaultProbeName,renderProbeList,updateAction,refreshUpdates,cancelRun};';
  vm.createContext(env); vm.runInContext(code, env);
  for (const id of ['historyDialog','detailDialog','probeDialog','confirmDialog']) env.ui.bindModal(el(id));
  env.ui.state.bootstrap = { version: '1.3.0', user: { role: 'admin' }, groups: [{ id: 1, name: 'Group' }], limits: { max_timeout_seconds: 900 }, presets: [
    { id: 'candy', kind: 'reasoning', name: 'Candy', prompt: 'Fixed candy', expected_answer: '21' },
    { id: 'custom', kind: 'reasoning', name: 'Custom', prompt: '' }, { id: 'svg', kind: 'drawing', name: 'SVG', prompt: '<svg/>' }
  ] };
  env.ui.state.probes = ['a','b'].map(id => ({ id, group_id: 1, name: 'Probe ' + id, model: 'Model ' + id, base_url: 'https://example.com', has_key: true, kind: 'reasoning', preset_id: 'custom', prompt: 'Original ' + id, effort: 'high', max_output_tokens: 64 }));
  env.ui.state.dashboard = { groups: [{ models: [{ probe_id: 'a', model: 'Model A', history: [{ id: 'old', status: 'passed' }, { id: 'latest', status: 'failed' }] }] }] };
  return { ui: env.ui, el, document, requests, rendered, errors, locks, advanced, field: name => fields.get(name), deferCatalog: () => { deferCatalog = true; },
    flushCloses() { while (closes.length) closes.shift()(); },
    outside(dialog, type, changes = {}) { dialog.emit(type, { button: 0, isPrimary: true, pointerId: 1, clientX: 50, clientY: 50, ...changes }); },
    get maxOpen() { return maxOpen; }, get reloads() { return reloads; }, get lastDelay() { return lastDelay; }
  };
}

test('dialog dismisses only a short primary pointer gesture that starts and ends outside', () => {
  const f = modalFixture(), dialog = f.el('detailDialog'), opener = f.el('pageOpener');
  f.ui.openModal(dialog, opener);
  assert.ok(f.locks.has('html:modal-open'));
  const inside = f.el('inside');
  f.outside(dialog, 'pointerdown', { target: inside, clientX: 200, clientY: 200 });
  f.outside(dialog, 'pointerup');
  assert.equal(dialog.open, true, 'A selection dragged from inside must not dismiss.');
  f.outside(dialog, 'pointerdown'); f.outside(dialog, 'pointerup', { target: inside, clientX: 200, clientY: 200 });
  assert.equal(dialog.open, true, 'A gesture ending inside must not dismiss.');
  f.outside(dialog, 'pointerdown'); f.outside(dialog, 'pointerup', { clientX: 70 });
  assert.equal(dialog.open, true, 'Dragging across the backdrop must not dismiss.');
  f.outside(dialog, 'pointerdown'); dialog.emit('pointercancel'); f.outside(dialog, 'pointerup');
  assert.equal(dialog.open, true);
  f.outside(dialog, 'pointerdown', { button: 2 }); f.outside(dialog, 'pointerup');
  assert.equal(dialog.open, true);
  f.outside(dialog, 'pointerdown'); f.outside(dialog, 'pointerup', { clientX: 52 }); f.flushCloses();
  assert.equal(dialog.open, false); assert.equal(f.document.activeElement, opener); assert.equal(f.locks.size, 0);
});

test('history opens detail as one modal and returns to its scroll position and focused row', async () => {
  const f = modalFixture(), pageOpener = f.el('pageOpener'), row = f.el('historyRow');
  pageOpener.focus(); f.ui.openHistory('a');
  assert.equal(f.el('historyContent').innerHTML, 'latest,old');
  f.el('historyContent').scrollTop = 273;
  row.focus(); const pending = f.ui.openDetail('latest', row); f.flushCloses();
  assert.equal(f.el('historyDialog').open, false); assert.equal(f.el('detailDialog').open, true);
  assert.equal(f.el('detailBack').hidden, false); assert.equal(f.maxOpen, 1);
  f.requests[0].resolve({ id: 'latest', kind: 'reasoning' }); await pending;
  assert.deepEqual(f.rendered, ['latest']);
  f.el('historyContent').scrollTop = 0;
  // The browser's native Escape action ends with close; it uses the same production cleanup.
  f.el('detailDialog').close(); f.flushCloses();
  assert.equal(f.el('detailDialog').open, false); assert.equal(f.el('historyDialog').open, true);
  assert.equal(f.el('historyContent').scrollTop, 273); assert.equal(f.document.activeElement, row);
  assert.equal(f.el('detailContent').innerHTML, ''); assert.equal(f.el('detailBack').hidden, true);
  f.ui.closeModal(f.el('historyDialog')); f.flushCloses();
  assert.equal(f.document.activeElement, pageOpener); assert.equal(f.locks.size, 0);
});

test('closed detail ignores late success and error responses', async () => {
  for (const failure of [false, true]) {
    const f = modalFixture(), pending = f.ui.openDetail('late');
    f.ui.closeModal(f.el('detailDialog')); f.flushCloses();
    if (failure) f.requests[0].reject(new Error('late error')); else f.requests[0].resolve({ id: 'late', kind: 'drawing' });
    await pending;
    assert.equal(f.el('detailContent').innerHTML, ''); assert.deepEqual(f.rendered, []); assert.deepEqual(f.errors, []);
  }
});

test('probe dismissal restores its form draft only in memory without key or upstream fields', () => {
  const f = modalFixture(); f.ui.editProbe('a');
  f.field('prompt').value = 'Unsaved prompt'; f.field('effort').value = 'xhigh'; f.field('max_output_tokens').value = '2048';
  f.field('enabled').checked = true; f.advanced.open = true;
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  assert.equal(f.field('api_key'), undefined); assert.equal(f.field('base_url'), undefined);
  assert.doesNotMatch(JSON.stringify([...f.ui.state.probeDrafts.values()]), /api_key|base_url/);
  f.ui.editProbe('b'); assert.equal(f.field('prompt').value, 'Original b');
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  f.ui.editProbe('a');
  assert.equal(f.field('prompt').value, 'Unsaved prompt'); assert.equal(f.field('effort').value, 'xhigh');
  assert.equal(f.field('max_output_tokens').value, '2048'); assert.equal(f.field('enabled').checked, true);
  assert.equal(f.advanced.open, true);
});

test('an earlier probe save cannot close or overwrite a subsequently opened editor', async () => {
  const f = modalFixture(); f.ui.editProbe('a');
  await turn();
  const pending = f.ui.saveProbe({ preventDefault() {} });
  assert.equal(f.requests[0].url, '/admin/probes/a');
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses(); f.ui.editProbe('b');
  f.field('prompt').value = 'Current editor content';
  f.requests[0].resolve({}); await pending; f.flushCloses();
  assert.equal(f.el('probeDialog').open, true); assert.equal(f.field('prompt').value, 'Current editor content');
  assert.equal(f.ui.state.editingProbe.id, 'b'); assert.equal(f.el('saveProbe').disabled, false);
  assert.equal(f.ui.state.probeDrafts.has('a'), false);
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  assert.equal(f.ui.state.probeDrafts.get('b').values.prompt, 'Current editor content');
});

test('session expiry clears drafts, closes detail and prevents suspended history from returning', async () => {
  const f = modalFixture(); f.ui.editProbe('a');
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  f.el('pageOpener').focus(); f.ui.openHistory('a'); const pending = f.ui.openDetail('late', f.el('historyRow')); f.flushCloses();
  f.ui.showAuth('Expired'); f.flushCloses();
  f.requests[0].resolve({ id: 'late', kind: 'reasoning' }); await pending;
  assert.equal(f.el('historyDialog').open, false); assert.equal(f.el('detailDialog').open, false);
  assert.equal(f.ui.state.probeDrafts.size, 0); assert.equal(f.ui.state.historyReturn, null);
  assert.equal(f.field('api_key'), undefined); assert.equal(f.locks.size, 0); assert.deepEqual(f.rendered, []);
});

test('an earlier save preserves a newer unsaved draft of the same probe', async () => {
  const f = modalFixture(); f.ui.editProbe('a');
  await turn();
  f.field('prompt').value = 'Submitted content';
  const pending = f.ui.saveProbe({ preventDefault() {} });
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  f.ui.editProbe('a'); f.field('prompt').value = 'New unsaved content';
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  f.requests[0].resolve({}); await pending;
  assert.equal(f.ui.state.probeDrafts.get('a').values.prompt, 'New unsaved content');
  f.ui.editProbe('a');
  await turn();
  assert.equal(f.field('prompt').value, 'New unsaved content');
  const current = f.ui.saveProbe({ preventDefault() {} });
  f.requests[1].resolve({}); await current; f.flushCloses();
  assert.equal(f.ui.state.probeDrafts.has('a'), false, 'Saving this draft clears it.');
});

test('deletion dialog names the target, focuses cancel and dismisses without a request', async () => {
  const f = modalFixture(), opener = f.el('deleteButton');
  const run = { id: 'art', kind: 'drawing', model: '<Model & name>', can_delete: true, status: 'completed', created_at: '2026-11-18T00:00:00Z' };
  f.ui.state.runSummaries.set('art', run);
  f.ui.requestRunDeletion('art', opener);
  assert.equal(f.el('confirmDialog').open, true);
  assert.equal(f.el('confirmName').textContent, '<Model & name>');
  assert.match(f.el('confirmDescription').textContent, /公开展示会一并删除/);
  assert.equal(f.document.activeElement, f.el('confirmCancel'));
  assert.equal(f.requests.length, 0);
  f.outside(f.el('confirmDialog'), 'pointerdown'); f.outside(f.el('confirmDialog'), 'pointerup'); f.flushCloses();
  assert.equal(f.el('confirmDialog').open, false); assert.equal(f.ui.state.confirmation, null);
  assert.equal(f.document.activeElement, opener); assert.equal(f.requests.length, 0);
});

test('deletion confirmation is single-flight, prevents dismissal while pending and keeps errors retryable', async () => {
  const f = modalFixture(); f.ui.requestProbeDeletion(f.ui.state.probes[0], f.el('deleteButton'));
  const first = f.ui.submitConfirmation(); await f.ui.submitConfirmation();
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].options.method, 'DELETE');
  assert.equal(f.el('confirmCancel').disabled, true); assert.equal(f.el('confirmSubmit').disabled, true);
  f.ui.closeModal(f.el('confirmDialog')); assert.equal(f.el('confirmDialog').open, true);
  let prevented = false; f.el('confirmDialog').emit('cancel', { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  f.requests[0].reject(new Error('Connection lost')); await first;
  assert.equal(f.el('confirmDialog').open, true); assert.equal(f.el('confirmError').textContent, 'Connection lost');
  assert.equal(f.el('confirmSubmit').disabled, false); assert.equal(f.ui.state.deletedProbes.size, 0);
  const retry = f.ui.submitConfirmation(); assert.equal(f.requests.length, 2);
  f.requests[1].resolve({ ok: true }); await retry; f.flushCloses();
  assert.equal(f.el('confirmDialog').open, false); assert.equal(f.ui.state.deletedProbes.has('a'), true);
  assert.equal(f.ui.state.probes.some(probe => probe.id === 'a'), false);
});

test('only can_delete records expose deletion and deleting a source invalidates cached public details', async () => {
  const f = fixture();
  const drawing = { id: 'art', model: 'Model', kind: 'drawing', status: 'completed', has_artifact: true, can_delete: true };
  assert.match(f.ui.artworkCard(drawing), /data-action="run-delete"/);
  assert.doesNotMatch(f.ui.artworkCard({ ...drawing, can_delete: false }), /data-action="run-delete"/);
  assert.doesNotMatch(f.ui.artworkCard(drawing, true), /data-action="run-delete"/);
  assert.match(f.ui.runRows([{ ...drawing, kind: 'reasoning', status: 'failed' }]), /data-action="run-delete"/);
  const m = modalFixture(); m.ui.state.runSummaries.set('art', drawing); m.ui.state.runCache.set('art', { ...drawing, artifact_html: '<svg/>' });
  const detail = m.ui.openDetail('art'); m.requests[0].resolve(drawing); await detail;
  m.ui.requestRunDeletion('art', m.el('deleteButton'));
  const pending = m.ui.submitConfirmation(); assert.equal(m.requests[1].url, '/runs/art');
  m.requests[1].resolve({ ok: true }); await pending; m.flushCloses();
  assert.equal(m.ui.state.deletedRuns.has('art'), true); assert.equal(m.ui.state.runCache.size, 0);
  assert.equal(m.ui.state.runSummaries.has('art'), false); assert.equal(m.el('detailDialog').open, false);
});

test('late detail and older mine list responses cannot restore deleted records', async () => {
  const f = fixture();
  const pendingDetail = f.ui.runDetail('removed');
  f.ui.state.recordsVersion++; f.ui.state.deletedRuns.add('removed');
  f.requests[0].resolve({ id: 'removed', status: 'completed', kind: 'drawing', artifact_html: '<svg/>' });
  await assert.rejects(pendingDetail, /记录已更新/); assert.equal(f.ui.state.runCache.size, 0);
  assert.equal(f.ui.artworkCard({ id: 'removed' }), '');
  const first = f.ui.loadMine(), second = f.ui.loadMine();
  f.requests[2].resolve({ runs: [{ id: 'current', model: 'Current', status: 'passed' }] }); await second;
  f.requests[1].resolve({ runs: [{ id: 'removed', model: 'Removed', status: 'passed' }] }); await first;
  assert.match(f.el('myRuns').innerHTML, /Current/); assert.doesNotMatch(f.el('myRuns').innerHTML, /Removed/);
});

test('group history excludes request errors and cancellation while wrong answers remain red', () => {
  const f = fixture();
  const history = ['passed', 'failed', 'completed', 'error', 'cancelled', 'running'].map((status, i) => ({ id: String(i), status }));
  const markup = f.ui.historyStrip({ probe_id: 'a', model: 'Model', history });
  assert.match(markup, /history-mark failed/);
  assert.doesNotMatch(markup, /history-mark (error|cancelled|running)/);
  assert.equal((markup.match(/history-mark /g) || []).length, 3);
});

test('management honors group alias restrictions while preserving a paused edit', async () => {
  const f = modalFixture();
  f.deferCatalog();
  f.ui.editProbe(null);
  const catalog = { models: ['group-alias', 'plain'], reasoning_effort_blocks: { 'group-alias': ['none', 'minimal'] } };
  f.requests[0].resolve(catalog); await turn();
  assert.equal(f.field('model').value, '');
  f.field('model').value = 'group-alias';
  f.ui.syncProbeModelSelection(true);
  assert.equal(f.field('effort').value, 'low');
  assert.equal(f.field('effort').options.find(option => option.value === 'none').disabled, true);
  f.field('effort').value = 'max';
  f.ui.syncProbeModelSelection();
  assert.equal(f.ui.readProbeFields(f.el('probeForm')).effort, 'max');
  f.ui.state.probes[0].model = 'group-alias';
  f.ui.state.probes[0].effort = 'none';
  f.ui.editProbe('a');
  f.requests[1].resolve(catalog); await turn();
  assert.equal(f.field('effort').value, 'none');
  f.field('enabled').checked = true;
  await f.ui.saveProbe({preventDefault(){}});
  assert.equal(f.requests.length, 2);
  assert.equal(f.advanced.open, true);
  f.field('enabled').checked = false;
  const saving = f.ui.saveProbe({preventDefault(){}});
  assert.equal(f.requests[2].options.body.effort, 'none');
  f.requests[2].resolve({}); await saving;
});

test('group model catalog ignores out-of-order group and closed-editor responses', async () => {
  const f = modalFixture(); f.deferCatalog(); f.ui.editProbe('a');
  assert.equal(f.requests[0].url, '/admin/groups/1/models');
  f.field('group_id').value = '2'; const switched = f.ui.loadProbeModels('');
  f.requests[1].resolve({ models: ['Second group model'], reasoning_effort_blocks: {} }); await switched;
  assert.match(f.field('model').innerHTML, /Second group model/); assert.equal(f.field('model').value, '');
  f.requests[0].resolve({ models: ['Stale first group model'], reasoning_effort_blocks: {} }); await turn();
  assert.doesNotMatch(f.field('model').innerHTML, /Stale first group model/);
  const late = f.ui.loadProbeModels('');
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses();
  f.requests[2].resolve({ models: ['Closed editor model'], reasoning_effort_blocks: {} }); await late;
  assert.doesNotMatch(f.field('model').innerHTML, /Closed editor model/);
  assert.equal(f.ui.state.probeModels, null);
});

test('missing original models are retained visibly and cannot silently switch or save', async () => {
  const f = modalFixture(); f.deferCatalog(); f.ui.editProbe('a');
  f.requests[0].resolve({ models: ['Different model'], reasoning_effort_blocks: {} }); await turn();
  assert.equal(f.field('model').value, 'Model a');
  assert.match(f.field('model').innerHTML, /Model a（当前不可用）/);
  assert.equal(f.el('saveProbe').disabled, true);
  await f.ui.saveProbe({ preventDefault() {} }); assert.equal(f.requests.length, 1);
  f.field('model').value = 'Different model'; f.ui.syncProbeModelSelection();
  assert.equal(f.el('saveProbe').disabled, false); assert.equal(f.el('probeModelHint').textContent, '');
  const save = f.ui.saveProbe({ preventDefault() {} });
  assert.equal(f.requests[1].options.body.model, 'Different model');
  assert.equal(f.requests[1].options.body.group_id, 1);
  assert.doesNotMatch(JSON.stringify(f.requests[1].options.body), /api_key|base_url/);
  f.ui.syncProbeModelSelection(); assert.equal(f.el('saveProbe').disabled, true, 'Model updates cannot unlock an in-flight save.');
  f.requests[1].resolve({ ok: true }); await save;
});

test('a previous editor catalog cannot overwrite the current editor model options', async () => {
  const f = modalFixture(); f.deferCatalog(); f.ui.editProbe('a');
  f.ui.closeModal(f.el('probeDialog')); f.flushCloses(); f.ui.editProbe('b');
  f.requests[1].resolve({ models: ['Model b'], reasoning_effort_blocks: {} }); await turn();
  assert.equal(f.field('model').value, 'Model b');
  assert.equal(f.el('saveProbe').disabled, false);
  f.requests[0].resolve({ models: ['Model a'], reasoning_effort_blocks: {} }); await turn();
  assert.equal(f.field('model').value, 'Model b');
  assert.doesNotMatch(f.field('model').innerHTML, /Model a/);
  assert.equal(f.ui.state.editingProbe.id, 'b');
  assert.equal(f.el('saveProbe').disabled, false);
});

test('empty model catalogs show the server explanation as text without a manual model fallback', async () => {
  const f = modalFixture(); f.deferCatalog(); f.ui.editProbe(null);
  const message = '原站账号未明确配置模型列表。<img src=x onerror=alert(1)>';
  f.requests[0].resolve({ models: [], reasoning_effort_blocks: {}, message }); await turn();
  assert.equal(f.el('probeModelHint').textContent, message);
  assert.equal(f.field('model').disabled, true); assert.equal(f.el('saveProbe').disabled, true);
  assert.doesNotMatch(f.field('model').innerHTML, /img|onerror/);
  const edited = f.ui.loadProbeModels('Old model');
  f.requests[1].resolve({ models: [], reasoning_effort_blocks: {}, message }); await edited;
  assert.equal(f.el('probeModelHint').textContent, message, 'The empty catalog explanation also applies to existing models.');
  assert.equal(f.field('model').value, 'Old model'); assert.equal(f.el('saveProbe').disabled, true);
});

test('empty and failed model catalogs keep saving blocked and allow an explicit reload', async () => {
  const f = modalFixture(); f.deferCatalog(); f.ui.editProbe(null);
  f.requests[0].resolve({ models: [], reasoning_effort_blocks: {} }); await turn();
  assert.match(f.el('probeModelHint').textContent, /未返回可用模型/);
  assert.equal(f.field('model').disabled, true); assert.equal(f.el('saveProbe').disabled, true);
  assert.equal(f.el('retryProbeModels').hidden, false);
  const failure = f.ui.loadProbeModels(''); f.requests[1].reject(new Error('此账号无权绑定分组')); await failure;
  assert.equal(f.el('probeModelHint').textContent, '此账号无权绑定分组');
  assert.equal(f.el('saveProbe').disabled, true);
  const retry = f.ui.loadProbeModels(''); f.requests[2].resolve({ models: ['Available model'], reasoning_effort_blocks: {} }); await retry;
  assert.equal(f.field('model').disabled, false); assert.equal(f.field('model').value, '');
  assert.equal(f.el('saveProbe').disabled, true, 'A new probe requires an explicit model choice.');
});

test('locked retired model needs no catalog to edit its interval; next new form unlocks', async () => {
  const f = modalFixture(); f.deferCatalog();
  Object.assign(f.ui.state.probes[0], { identity_locked:true, interval_minutes:60, enabled:true, publish:true, effort:'default', timeout_seconds:900, protocol:'responses' });
  f.ui.editProbe('a');
  for (const field of ['model','group_id','kind','preset_id','prompt','expected_answer']) assert.equal(f.field(field).disabled, true);
  assert.equal(f.requests.length, 0); assert.equal(f.field('model').value, 'Model a');
  f.field('interval_minutes').value = '15';
  const saving = f.ui.saveProbe({ preventDefault() {} });
  const request = f.requests.at(-1); assert.equal(request.url, '/admin/probes/a');
  assert.equal(request.options.body.interval_minutes, 15);
  for (const key of ['group_id','model','kind','preset_id','prompt','expected_answer','effort','enabled']) assert.equal(Object.hasOwn(request.options.body, key), false);
  request.resolve({}); await saving; f.flushCloses(); f.ui.editProbe(null);
  assert.equal(f.field('kind').disabled, false); assert.match(f.requests.at(-1).url, /models$/);
});
test('name follows the selected group until typed, and matching names keep independent actions', () => {
  const f = modalFixture(); f.ui.state.bootstrap.groups.push({ id:2, name:'Second' });
  f.ui.editProbe(null); assert.equal(f.field('name').value, 'Group');
  f.field('group_id').value = '2'; f.ui.defaultProbeName(); assert.equal(f.field('name').value,'Second');
  f.field('name').value = 'Manual'; f.field('group_id').value = '1'; f.ui.defaultProbeName(); assert.equal(f.field('name').value,'Manual');
  f.ui.state.probes.push({ id:'c', name:'Other' });
  f.ui.state.probes[0].name = f.ui.state.probes[1].name = 'Same';
  f.ui.state.probeNameFilter = 'Same'; f.ui.renderProbeList();
  assert.equal((f.el('probeNameFilter').innerHTML.match(/value="Same"/g) || []).length, 1);
  assert.match(f.el('probeList').innerHTML, /data-id="a"/); assert.match(f.el('probeList').innerHTML, /data-id="b"/);
  assert.doesNotMatch(f.el('probeList').innerHTML, /data-id="c"/);
  f.ui.state.probes = f.ui.state.probes.slice(-1); f.ui.renderProbeList();
  assert.equal(f.el('probeNameFilter').value, ''); assert.match(f.el('probeList').innerHTML, /data-id="c"/);
});
