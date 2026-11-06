const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { createAdminV2Helpers } = require('./helpers/admin_v2_shared_helpers.cjs');

let activeElement = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.textContent = '';
    this.className = ''; this.isConnected = true; this.attributes = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children.push(...nodes); }
  replaceChildren(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) {
    if (selector.startsWith('#')) {
      const id = selector.slice(1);
      return descendants(this, node => node.id === id)[0] || null;
    }
    if (selector.startsWith('.')) return this.children.find(node => (node.className || '').split(/\s+/).includes(selector.slice(1))) || null;
    return null;
  }
  reportValidity() { return true; }
  select() {}
  get elements() {
    const inputs = descendants(this, node => ['input', 'select', 'textarea'].includes(node.tagName));
    const named = {};
    inputs.forEach(input => { if (input.name) named[input.name] = input; });
    return named;
  }
  closest(selector) {
    let curr = this;
    while (curr) {
      if (selector.startsWith('#') && curr.id === selector.slice(1)) return curr;
      if (selector.startsWith('.') && curr.className && curr.className.split(/\s+/).includes(selector.slice(1))) return curr;
      curr = curr.parentElement;
    }
    return null;
  }
  focus() { if (!this.disabled) activeElement = this; }
  querySelectorAll() { return []; }
}

function descendants(root, predicate) {
  return [
    ...(predicate(root) ? [root] : []),
    ...(root.children || []).flatMap(child => descendants(child, predicate)),
  ];
}

(async () => {
  const ids = [
    'sourcesView', 'sourcesFilters', 'sourcesClearFilters', 'sourcesRefresh', 'sourcesAddChannel',
    'sourcesStatusFilter',
    'sourcesRows', 'sourcesPagination', 'sourceDetailDialog', 'sourcesNotice', 'sourcesFeedback',
    'sourceDetailContent',
    'sourceChannelsPane', 'sourceHistoryPane', 'sourcesHistoryFeedback', 'sourcesHistoryRows', 'sourcesHistoryPagination',
    'sourcesState', 'sourcesTableWrap', 'sourcesQuery', 'liveMessage', 'crumbCurrent',
    'operationalEyebrow', 'operationalTitle', 'operationalIntro',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'sourcesFilters' ? 'form' : 'div')]));
  const documentListeners = {};
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
    addEventListener(type, callback) { documentListeners[type] = callback; },
  };

  const postCalls = [];
  const windowListeners = {};
  const window = {
    addEventListener(type, callback) { windowListeners[type] = callback; },
    AdminV2: {
      ...createAdminV2Helpers(document),
      async getJson(path) {
        if (path.endsWith('/sources/list')) {
          return {
            available: true,
            managed_revision: 0,
            applied_revision: 0,
            applied_revision_available: true,
            sources: [
              { provider: 'telegram', enabled: true, defaults: {}, channels: [] },
            ],
          };
        }
        if (path.endsWith('/sources/settings')) {
          return { revision: 0, overrides: {} };
        }
        if (path.includes('/sync-history')) {
          return { runs: [] };
        }
        throw new Error(`Unexpected URL ${path}`);
      },
      async postJson(path, payload) {
        postCalls.push({ path, payload });
        return { ok: true, message: 'Saved successfully', channel: payload.channel };
      },
      openDialog(dialog) { if (dialog) dialog.open = true; },
      closeDialog(dialog) { if (dialog) dialog.open = false; },
    },
  };

  const location = { hash: '#sources', search: '', href: 'http://localhost/admin/v2/#sources' };
  const history = {
    replaceState(_state, _title, value) {
      const url = new URL(String(value), location.href);
      location.href = url.href; location.search = url.search; location.hash = url.hash;
    },
    pushState(_state, _title, value) {
      const url = new URL(String(value), location.href);
      location.href = url.href; location.search = url.search; location.hash = url.hash;
    },
  };

  const context = {
    window, document, location, history, URL, URLSearchParams, AbortController,
    setTimeout, clearTimeout, Intl, Date, Map, Set, Array, Object, Number, String, Boolean, Math,
  };

  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'), context);
  await new Promise(resolve => setTimeout(resolve, 20));

  // 1. Verify empty state is displayed
  assert.equal(elements.sourcesTableWrap.hidden, true, 'table is hidden when channels are empty');
  assert.equal(elements.sourcesState.hidden, false, 'sources empty state is visible');
  assert.equal(elements.sourcesPagination.hidden, true, 'pagination is hidden when channels are empty');

  // Verify empty state messages
  const stateTitles = descendants(elements.sourcesState, node => node.tagName === 'strong');
  assert.equal(stateTitles.length, 1);
  assert.equal(stateTitles[0].textContent, 'No channels configured');

  const stateTexts = descendants(elements.sourcesState, node => node.tagName === 'span' && !node.className);
  assert.equal(stateTexts.length, 1);
  assert.match(stateTexts[0].textContent, /Add your first channel through Admin to start synchronizing/);

  // Verify the empty state action button exists
  const emptyAddBtn = descendants(elements.sourcesState, node => node.id === 'sourcesEmptyAddChannel')[0];
  assert.ok(emptyAddBtn, 'Empty state renders #sourcesEmptyAddChannel action button');
  assert.equal(emptyAddBtn.textContent, 'Add Telegram channel');

  // 2. Click the empty state action button to trigger openAddChannel()
  emptyAddBtn.listeners.click({ currentTarget: emptyAddBtn });
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(elements.sourceDetailDialog.open, true, 'clicking empty add button opens detail dialog');
  const inputs = descendants(elements.sourceDetailContent, node => node.tagName === 'input');
  assert.equal(inputs.length, 2, 'dialog contains channel ID and studio label inputs');

  // Fill in first channel
  inputs[0].value = '-1001234567890';
  inputs[1].value = 'First Run Studio';

  const saveBtn = descendants(elements.sourceDetailContent, node => node.id === 'sourceCreateSave')[0];
  assert.ok(saveBtn, 'Add channel form has save button');

  // Submit first channel
  elements.sourceDetailDialog.listeners.click({ target: saveBtn });
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(postCalls.length, 1, 'submitting form posts to sources endpoint');
  assert.equal(postCalls[0].path, '/admin/api/v2/sources');
  assert.equal(postCalls[0].payload.channel.id, '-1001234567890');
  assert.equal(postCalls[0].payload.channel.studio, 'First Run Studio');

  process.stdout.write('PASS: Zero channels empty state renders clear action and adds first channel cleanly\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
