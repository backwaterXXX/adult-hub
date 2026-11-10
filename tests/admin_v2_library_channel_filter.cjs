const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { adminV2Source } = require('./helpers/admin_v2_scripts.cjs');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.value = '';
    this.textContent = '';
    this.attributes = {};
    this.listeners = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) {
    nodes.forEach(node => { node.parentElement = this; });
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    nodes.forEach(node => { node.parentElement = this; });
    this.children = [...nodes];
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  querySelector(selector) {
    const valMatch = selector.match(/^option\[value="([^"]*)"\]$/);
    if (valMatch) return this.children.find(node => node.tagName === 'option' && node.value === valMatch[1]) || null;
    return null;
  }
}

(async () => {
  const filterChannelSelect = new Element('select');
  filterChannelSelect.id = 'filterChannel';
  const defaultOption = new Element('option');
  defaultOption.value = '';
  defaultOption.textContent = 'All channels';
  filterChannelSelect.append(defaultOption);

  const elements = {
    filterChannel: filterChannelSelect,
  };

  // The actual response format from GET /admin/api/v2/sources/list
  const realSourcesListPayload = {
    ok: true,
    available: true,
    managed_revision: 0,
    desired_revision: 0,
    sources: [
      {
        provider: 'telegram',
        enabled: true,
        defaults: { stream_mode: 'proxy', max_pages: 50 },
        channels: [
          { id: '-1001484926034', studio: 'THE G BUNKER', enabled: true },
          { id: '-1001188284602', studio: 'Lukayan SHOW', enabled: true },
          { id: '-1001140295673', studio: 'Gay Porn Full Videos HD', enabled: true },
        ]
      },
      {
        provider: 'other_provider',
        enabled: true,
        channels: [
          { id: 'non-telegram-id', studio: 'Ignore Other Provider' }
        ]
      }
    ]
  };

  const documentListeners = new Map();
  const document = {
    querySelector(selector) {
      if (selector.startsWith('#')) return elements[selector.slice(1)] || null;
      return null;
    },
    createElement: tag => new Element(tag),
    addEventListener(type, callback) { documentListeners.set(type, callback); },
    dispatchEvent(event) {
      const listener = documentListeners.get(event.type);
      if (listener) listener(event);
    }
  };

  let getJsonCalls = [];
  const window = {
    AdminV2: {
      async getJson(path) {
        getJsonCalls.push(path);
        if (path === '/admin/api/v2/sources/list') {
          return realSourcesListPayload;
        }
        throw new Error(`Unexpected getJson call: ${path}`);
      }
    }
  };

  const source = adminV2Source();
  const start = source.indexOf('  let channelFilterLoaded = false;');
  assert.ok(start >= 0, 'loadChannelFilterOptions must exist in the Admin V2 parts');
  const end = source.indexOf('\n  function syncFiltersFromUrl(', start);
  assert.ok(end > start, 'syncFiltersFromUrl marker must follow loadChannelFilterOptions');
  const codeToRun = `
    const $ = (selector, root = document) => root.querySelector(selector);
    ${source.slice(start, end)}
    globalThis.loadChannelFilterOptions = loadChannelFilterOptions;
  `;

  const context = {
    window,
    document,
    Array,
    Object,
    String,
    Number,
    Boolean,
    console,
  };

  vm.runInNewContext(codeToRun, context);

  // Invoke channel filter loader with real data source shape
  await context.loadChannelFilterOptions();

  assert.equal(getJsonCalls.length, 1, 'fetched sources list exactly once');
  assert.equal(getJsonCalls[0], '/admin/api/v2/sources/list');

  // Assert options were populated from real payload structure
  const options = filterChannelSelect.children;
  assert.equal(options.length, 4, 'select has All channels + 3 telegram channels (excluding non-telegram)');
  assert.equal(options[0].value, '');
  assert.equal(options[0].textContent, 'All channels');

  // Verify alphabetical sorting by studio
  assert.equal(options[1].value, '-1001140295673');
  assert.equal(options[1].textContent, 'Gay Porn Full Videos HD (-1001140295673)');

  assert.equal(options[2].value, '-1001188284602');
  assert.equal(options[2].textContent, 'Lukayan SHOW (-1001188284602)');

  assert.equal(options[3].value, '-1001484926034');
  assert.equal(options[3].textContent, 'THE G BUNKER (-1001484926034)');

  // Verify that an unknown/external channel from URL is retained if present
  filterChannelSelect.value = '-1009999999999';
  document.dispatchEvent({ type: 'adminv2:sources-changed' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(filterChannelSelect.value, '-1009999999999', 'retains unlisted channel selection');
  assert.ok(filterChannelSelect.children.some(opt => opt.value === '-1009999999999'));

  process.stdout.write('PASS: Library channel filter populates from real sources/list payload and retains selections\n');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
