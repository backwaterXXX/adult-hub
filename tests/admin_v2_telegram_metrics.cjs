const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { createAdminV2Helpers } = require('./helpers/admin_v2_shared_helpers.cjs');

let activeElement = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.isConnected = true;
    this.attributes = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { nodes.forEach(n => { n.parentElement = this; }); this.children.push(...nodes); }
  appendChild(node) { this.append(node); return node; }
  hasAttribute(name) { return name in this.attributes; }
  replaceChildren(...nodes) {
    this.children.forEach(n => { n.parentElement = null; });
    nodes.forEach(n => { n.parentElement = this; });
    this.children = nodes;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) {
    if (selector.startsWith('.')) {
      const cls = selector.slice(1);
      return this.children.find(n => (n.className || '').split(/\s+/).includes(cls)) || null;
    }
    return null;
  }
  querySelectorAll(selector) {
    const res = [];
    const walk = (node) => {
      for (const c of node.children) {
        if (selector.startsWith('.') && (c.className || '').split(/\s+/).includes(selector.slice(1))) {
          res.push(c);
        }
        walk(c);
      }
    };
    walk(this);
    return res;
  }
}

function descendants(root, predicate) {
  const result = [];
  const walk = (node) => {
    if (predicate(node)) result.push(node);
    for (const child of node.children) walk(child);
  };
  walk(root);
  return result;
}

async function run() {
  const ids = [
    'sourcesView', 'sourcesFilters', 'sourcesClearFilters', 'sourcesRefresh', 'sourcesAddChannel',
    'sourcesStatusFilter', 'sourcesRows', 'sourcesPagination', 'sourceDetailDialog', 'sourcesNotice',
    'sourcesFeedback', 'sourceDetailContent', 'sourceChannelsPane', 'sourceHistoryPane',
    'sourcesHistoryFeedback', 'sourcesHistoryRows', 'sourcesHistoryPagination', 'sourcesState',
    'sourcesTableWrap', 'sourcesQuery', 'liveMessage', 'crumbCurrent',
    'operationalEyebrow', 'operationalTitle', 'operationalIntro',
    'telegramLoginButton', 'telegramAccountsRefreshBtn', 'telegramAuthClose',
    'telegramAuthDialog', 'telegramAuthTitle', 'telegramAuthBanner', 'telegramAuthBody',
    'telegramAccountActionDialog', 'telegramAccountActionClose', 'telegramAccountActionTitle',
    'telegramAccountActionBanner', 'telegramAccountActionBody',
    'telegramAccountIntro', 'telegramAccountsContainer', 'telegramAccountBanner', 'telegramAccountCard'
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'sourcesFilters' ? 'form' : (id.includes('Dialog') ? 'dialog' : 'div'))]));

  const documentListeners = {};
  const windowListeners = {};

  const document = {
    get activeElement() { return activeElement; },
    querySelector(selector) {
      if (selector.startsWith('#')) {
        const id = selector.slice(1);
        if (elements[id]) return elements[id];
        for (const root of Object.values(elements)) {
          const match = descendants(root, node => node.id === id)[0];
          if (match) return match;
        }
        return null;
      }
      return null;
    },
    querySelectorAll: () => [],
    createElement: tag => new Element(tag),
    listeners: documentListeners,
    addEventListener(type, callback) {
      documentListeners[type] = documentListeners[type] || [];
      documentListeners[type].push(callback);
    },
  };

  const testAccounts = [
    {
      account_id: 'acc_alpha',
      label: 'Alpha Worker',
      phone_mask: '+1234****789',
      enabled: true,
      status: 'authorized',
      authorized: true,
      cooldown_seconds: 0,
      has_session_file: true,
      channel_summary: {
        accessible: 3,
        inaccessible: 1,
        total: 4,
      },
      metrics: {
        active_ops: 2,
        bytes_transferred: 5242880,
        throughput_mb_s: 3.5,
        buffer_init_latency_ms: 85.0,
        errors_count: 1,
        flood_wait_count: 1,
        cooldown_seconds: 0,
      },
    },
  ];

  const window = {
    listeners: windowListeners,
    addEventListener(type, callback) {
      windowListeners[type] = windowListeners[type] || [];
      windowListeners[type].push(callback);
    },
    prompt() {},
    confirm() {},
    AdminV2: {
      ...createAdminV2Helpers(document),
      formatBytes(bytes) {
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
      },
      async getJson(path) {
        if (path.endsWith('/capabilities')) return { token_required: false };
        if (path.endsWith('/sources/list')) return { available: true, managed_revision: 0, applied_revision: 0, sources: [] };
        if (path.endsWith('/sources/settings')) return { revision: 0, overrides: {} };
        if (path.includes('/sync-history')) return { runs: [] };
        if (path.endsWith('/telegram/accounts')) return { accounts: testAccounts };
        if (path.endsWith('/telegram/channels/access')) return { channels: [] };
        return {};
      },
      async postJson() { return { ok: true }; },
    },
  };

  const code = fs.readFileSync(require.resolve('../resolver/static/admin-v2/sources.js'), 'utf8');
  const location = { hash: '#sources', search: '', href: 'http://localhost/admin/v2/#sources' };
  const history = {
    replaceState() {},
    pushState() {},
  };

  const context = vm.createContext({
    window,
    document,
    location,
    history,
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    CustomEvent: class {},
    AbortController,
    URL,
    URLSearchParams,
  });
  vm.runInContext(code, context);

  // Trigger recorded listeners (DOMContentLoaded, adminv2:navigate, etc.)
  const dcl = documentListeners.DOMContentLoaded || windowListeners.DOMContentLoaded;
  if (dcl) {
    for (const cb of dcl) await cb();
  }
  const navListeners = documentListeners['adminv2:navigate'];
  if (navListeners) {
    for (const cb of navListeners) await cb();
  }

  // Allow microtasks to complete
  await new Promise(r => setTimeout(r, 20));

  // Assert event listeners were recorded
  assert.ok(documentListeners['adminv2:navigate']?.length > 0, 'adminv2:navigate listener must be recorded');
  assert.ok(windowListeners.popstate?.length > 0, 'popstate listener must be recorded');
  assert.ok(elements.telegramAccountsRefreshBtn.listeners.click, 'Refresh button click listener must be recorded');

  const container = elements.telegramAccountsContainer;
  assert.ok(container.children.length > 0, 'telegramAccountsContainer must have rendered content');

  const cardBody = descendants(container, n => (n.className || '').includes('telegram-account-card-body'))[0];
  assert.ok(cardBody, 'Card body must be rendered');

  const bodyText = cardBody.children.map(c => c.textContent).join(' | ');

  assert.ok(bodyText.includes('Verified Channels: 3 (1 inaccessible)'), `Expected channel breakdown in: ${bodyText}`);
  assert.ok(bodyText.includes('Active: 2 streams'), `Expected active streams in: ${bodyText}`);
  assert.ok(bodyText.includes('Transferred: 5.0 MB'), `Expected transferred bytes in: ${bodyText}`);
  assert.ok(bodyText.includes('Throughput: 3.5 MB/s'), `Expected throughput in: ${bodyText}`);
  assert.ok(bodyText.includes('TTFB: 85 ms'), `Expected TTFB in: ${bodyText}`);
  assert.ok(bodyText.includes('Errors: 1 (1 FloodWait)'), `Expected error counts in: ${bodyText}`);

  process.stdout.write('PASS: Admin UI correctly renders Telegram account metrics and channel status summaries\n');
}

run().catch(err => {
  console.error(err);
  process.exit(1);
});
