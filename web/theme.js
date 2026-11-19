/* Sub2API v0.2.23: AppSidebar.vue stores light/dark in `theme` and uses html.dark. */
(() => {
  'use strict';
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let parentRoot = null;
  let observer = null;
  let currentTheme = null;

  try {
    if (window.parent !== window && window.parent.location.origin === window.location.origin) {
      parentRoot = window.parent.document.documentElement;
    }
  } catch (_) {
    // An external embed cannot read its parent; use this origin's saved preference.
  }

  function savedDark() {
    let saved = null;
    try { saved = window.localStorage.getItem('theme'); } catch (_) { /* Storage can be blocked. */ }
    return saved === 'dark' || (!saved && system.matches);
  }

  function synchronize() {
    const dark = parentRoot ? parentRoot.classList.contains('dark') : savedDark();
    const theme = dark ? 'dark' : 'light';
    const changed = currentTheme !== theme;
    root.classList.toggle('dark', dark);
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = dark ? '#020617' : '#f9fafb';
    currentTheme = theme;
    if (changed && typeof window.CustomEvent === 'function') {
      window.dispatchEvent(new window.CustomEvent('geekpulse-themechange', { detail: { theme } }));
    }
  }

  function observeParent() {
    if (parentRoot && !observer) {
      observer = new MutationObserver(synchronize);
      observer.observe(parentRoot, { attributes: true, attributeFilter: ['class'] });
    }
  }

  // This file runs synchronously before the stylesheet to choose the first frame.
  synchronize();
  observeParent();
  window.addEventListener('storage', event => {
    if (event.key === 'theme' || event.key === null) synchronize();
  });
  system.addEventListener('change', synchronize);
  window.addEventListener('pageshow', () => { observeParent(); synchronize(); });
  window.addEventListener('pagehide', () => {
    if (observer) { observer.disconnect(); observer = null; }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) synchronize();
  });
})();
