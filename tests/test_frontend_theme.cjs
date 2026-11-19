'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/theme.js'), 'utf8');

function fixture({ saved = null, systemDark = false, embedded = false, parentDark = false, blockedStorage = false, externalParent = false } = {}) {
  const events = new Map();
  const docEvents = new Map();
  let mediaListener;
  let callback;
  let active = false;
  let storage = saved;
  const themeEvents = [];
  const classSet = new Set();
  const root = {
    dataset: {}, style: {},
    classList: {
      toggle(name, on) { if (on) classSet.add(name); else classSet.delete(name); },
      contains(name) { return classSet.has(name); },
    },
  };
  const parentClasses = new Set(parentDark ? ['dark'] : []);
  const parentRoot = { classList: { contains(name) { return parentClasses.has(name); } } };
  const themeColor = { content: '' };
  const media = { matches: systemDark, addEventListener(event, cb) { assert.equal(event, 'change'); mediaListener = cb; } };
  const window = {
    location: { origin: 'https://example.test' },
    matchMedia(query) { assert.equal(query, '(prefers-color-scheme: dark)'); return media; },
    localStorage: { getItem(key) { assert.equal(key, 'theme'); if (blockedStorage) throw new Error('blocked'); return storage; } },
    addEventListener(event, cb) { events.set(event, cb); },
    CustomEvent: class CustomEvent { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    dispatchEvent(event) { themeEvents.push({ type: event.type, theme: event.detail.theme }); },
  };
  window.parent = embedded ? { location: { origin: 'https://example.test' }, document: { documentElement: parentRoot } } : window;
  if (externalParent) Object.defineProperty(window, 'parent', { get() { throw new Error('Cross-origin access'); } });
  const document = { documentElement: root, hidden: false,
    querySelector(selector) { assert.equal(selector, 'meta[name="theme-color"]'); return themeColor; },
    addEventListener(event, cb) { docEvents.set(event, cb); } };
  class MutationObserver {
    constructor(cb) { callback = cb; }
    observe(target, options) { assert.equal(target, parentRoot); assert.deepEqual(Array.from(options.attributeFilter), ['class']); active = true; }
    disconnect() { active = false; }
  }
  vm.runInNewContext(source, { window, document, MutationObserver });
  return {
    dark: () => root.classList.contains('dark'),
    theme: () => root.dataset.theme,
    colorScheme: () => root.style.colorScheme,
    themeColor: () => themeColor.content,
    themeEvents: () => themeEvents,
    storage(value, key = 'theme') { storage = value; events.get('storage')({ key }); },
    system(value) { media.matches = value; mediaListener(); },
    parent(value) { if (value) parentClasses.add('dark'); else parentClasses.delete('dark'); if (active) callback(); },
    event(name) { events.get(name)(); },
    observerActive: () => active,
  };
}

const tests = [
  ['saved light wins over system dark on the first frame', () => {
    const f = fixture({ saved: 'light', systemDark: true });
    assert.equal(f.dark(), false); assert.equal(f.theme(), 'light'); assert.equal(f.colorScheme(), 'light'); assert.equal(f.themeColor(), '#f9fafb');
  }],
  ['saved dark wins over system light on the first frame', () => {
    const f = fixture({ saved: 'dark' }); assert.equal(f.dark(), true); assert.equal(f.colorScheme(), 'dark'); assert.equal(f.themeColor(), '#020617');
  }],
  ['without a saved preference the system controls standalone theme', () => {
    const f = fixture({ systemDark: true }); assert.equal(f.dark(), true); f.system(false); assert.equal(f.dark(), false);
  }],
  ['system changes do not override an explicit user preference', () => {
    const f = fixture({ saved: 'light' }); f.system(true); assert.equal(f.dark(), false);
  }],
  ['cross-tab storage changes update an already open standalone page', () => {
    const f = fixture(); f.storage('dark'); assert.equal(f.dark(), true); f.storage('light'); assert.equal(f.dark(), false);
  }],
  ['clearing storage restores the system preference', () => {
    const f = fixture({ saved: 'light', systemDark: true }); f.storage(null, null); assert.equal(f.dark(), true);
  }],
  ['blocked local storage still honors system preference', () => {
    assert.equal(fixture({ blockedStorage: true, systemDark: true }).dark(), true);
  }],
  ['same-origin iframe uses the actual parent class immediately', () => {
    const f = fixture({ embedded: true, parentDark: true, saved: 'light' }); assert.equal(f.dark(), true);
  }],
  ['same-origin parent changes update an already open iframe', () => {
    const f = fixture({ embedded: true }); f.parent(true); assert.equal(f.dark(), true); f.parent(false); assert.equal(f.dark(), false);
  }],
  ['storage does not desynchronize an iframe from its parent', () => {
    const f = fixture({ embedded: true }); f.storage('dark'); assert.equal(f.dark(), false);
  }],
  ['inaccessible external parent does not break theme initialization', () => {
    assert.equal(fixture({ externalParent: true, saved: 'dark' }).dark(), true);
  }],
  ['page restore reconnects the parent observer and catches missed changes', () => {
    const f = fixture({ embedded: true }); f.event('pagehide'); assert.equal(f.observerActive(), false);
    f.parent(true); assert.equal(f.dark(), false); f.event('pageshow'); assert.equal(f.observerActive(), true); assert.equal(f.dark(), true);
  }],
  ['only actual changes notify cached artwork surfaces', () => {
    const f = fixture({ saved: 'light' }); assert.equal(f.themeEvents().length, 1);
    f.storage('light'); assert.equal(f.themeEvents().length, 1);
    f.storage('dark'); assert.equal(f.themeEvents().length, 2);
    assert.equal(f.themeEvents()[1].type, 'geekpulse-themechange'); assert.equal(f.themeEvents()[1].theme, 'dark');
  }],
];
for (const [name, test] of tests) { test(); process.stdout.write(`PASS ${name}\n`); }
process.stdout.write(`${tests.length} theme tests passed\n`);
