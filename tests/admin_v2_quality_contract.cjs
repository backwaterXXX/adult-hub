const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.hidden = false; this.value = ''; this.textContent = ''; this.className = ''; this.href = ''; this.attributes = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelectorAll(selector) {
    const collect = node => [node, ...(node.children || []).flatMap(collect)];
    const all = this.children.flatMap(collect);
    if (selector === 'input,textarea') return all.filter(item => ['input', 'textarea'].includes(item.tagName));
    const field = selector.match(/^\[data-proposal-field="([a-z_]+)"\]$/);
    return field ? all.filter(item => item.dataset.proposalField === field[1]) : [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const ids = ['metadataProposalsRefresh', 'metadataProposalsFeedback', 'metadataProposalsRows', 'metadataProposalsPagination', 'librarySuggestionsPane'];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'metadataProposalsRefresh' ? 'button' : 'div')]));
elements.librarySuggestionsPane.hidden = false;
const calls = [];
const events = {};
let pending = true;
let heldPromise;
let resolveHeld;
const proposal = { id: 12, video_id: 'video-1', title: 'Example film', source: 'catalog', confidence: 0.92, revision: 4,
  current: { plot: 'Current description' }, changes: { plot: 'Suggested description' }, created_at: '2026-10-12T10:00:00Z' };
const document = {
  querySelector: selector => elements[selector.slice(1)] || null,
  createElement: tag => new Element(tag),
  addEventListener(type, callback) { events[type] = callback; },
};
const window = { AdminV2: {
  currentRoute: () => ({ route: 'library', libraryTab: 'suggestions' }),
  async getJson(path) {
    calls.push({ method: 'GET', path });
    if (heldPromise) { const wait = heldPromise; heldPromise = null; return wait; }
    return { items: pending ? [proposal] : [], total: pending ? 1 : 0, limit: 10, offset: 0 };
  },
  async postJson(path, payload) {
    calls.push({ method: 'POST', path, payload });
    if (path.endsWith('/operations/preview')) return { plan_id: 'plan-1', fields: { plot: { before: 'Current description', after: payload.changes.plot } } };
    if (path.endsWith('/operations/execute')) { pending = false; return { job_id: 'task-1' }; }
    if (path.endsWith('/reject')) { pending = false; return { ok: true }; }
    throw new Error(`Unexpected POST ${path}`);
  },
  showQueued(payload, message) { calls.push({ method: 'QUEUED', payload, message }); },
} };
const context = { window, document, location: {}, history: {}, Date, URL, URLSearchParams, Promise, Set, Array, Object, String, Number, console };
const textOf = node => `${node.textContent || ''} ${(node.children || []).map(textOf).join(' ')}`;
const descendants = node => [node, ...(node.children || []).flatMap(descendants)];
(async () => {
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/quality.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 1);
  assert.match(calls[0].path, /quality\/proposals\?/);
  assert.match(textOf(elements.metadataProposalsRows), /Current: Current description/);
  assert.match(textOf(elements.metadataProposalsRows), /Suggested:/);
  const input = descendants(elements.metadataProposalsRows).find(item => item.dataset.proposalField === 'plot');
  input.value = 'Edited description';
  const preview = descendants(elements.metadataProposalsRows).find(item => item.textContent === 'Preview suggestion');
  await preview.listeners.click();
  const previewButton = descendants(elements.metadataProposalsRows).find(item => item.textContent === 'Queue approved metadata update');
  await previewButton.listeners.click();
  assert(calls.some(item => item.method === 'POST' && item.path.endsWith('/operations/preview') && item.payload.changes.plot === 'Edited description'));
  assert(calls.some(item => item.method === 'QUEUED' && item.message === 'Metadata update'));
  assert.match(elements.metadataProposalsFeedback.textContent, /0 suggestions waiting/);

  pending = true;
  const late = new Promise(resolve => { resolveHeld = resolve; });
  heldPromise = late;
  const before = calls.filter(item => item.method === 'GET').length;
  const refresh = elements.metadataProposalsRefresh.listeners.click();
  events['adminv2:navigate']({ detail: { route: 'system', systemTab: 'status' } });
  elements.librarySuggestionsPane.hidden = true;
  resolveHeld({ items: [proposal], total: 1, limit: 10, offset: 0 });
  await refresh;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(item => item.method === 'GET').length, before + 1);
  assert.equal(descendants(elements.metadataProposalsRows).some(item => item.dataset.proposalField === 'plot'), false,
    'a late suggestion response cannot restore editors after leaving Library');
  assert.equal(calls.some(item => item.path?.includes('/quality/summary') || item.path?.includes('/quality/items')),
    false, 'the retained suggestions queue does not poll removed catalog reports');
  console.log('PASS: Library suggestions retain current/suggested editing, preview and queued approval with route guards');
})().catch(error => { console.error(error); process.exitCode = 1; });
