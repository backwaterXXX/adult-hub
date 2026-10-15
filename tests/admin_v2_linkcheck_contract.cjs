const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.textContent = ''; this.listeners = {}; this.disabled = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  get text() { return this.textContent + this.children.map(child => child.text || child.textContent || '').join(' '); }
}

const elements = new Map();
const documentListeners = {};
const document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  createElement(tag) { return new Element(tag); },
  addEventListener(name, fn) { documentListeners[name] = fn; }
};
let requested;
let response = { ok: true, available: true, exists: true, checked_at: '2026-10-06 12:01:00', empty: false,
  providers: [{ provider: 'telegram', status: 'broken', ok: 4, total: 5,
    failure_reasons: { 'Resolver request failed': 1 } }],
  samples: [
    { provider: 'telegram', timestamp: '2026-10-06 12:00:29', result: 'passed', media_id: 'private-id' },
    { provider: 'telegram', timestamp: '2026-10-06 12:00:30', result: 'failed', failure_reason: 'Resolver request failed',
      url: 'https://private.invalid/?token=secret' },
  ], samples_total: 2, samples_truncated: false };
const window = { AdminV2: { getJson: async url => {
  requested = url;
  return response;
} } };

const source = fs.readFileSync('resolver/static/admin-v2/linkcheck.js', 'utf8');
vm.runInNewContext(source, { document, window, Intl, Date, String, Object, Array, Number, console });

(async () => {
  await elements.get('#linkcheckRefresh').listeners.click();
  assert.strictEqual(requested, '/admin/api/v2/linkcheck');
  assert.match(elements.get('#linkcheckFeedback').textContent, /Latest report summary/);
  const rendered = elements.get('#linkcheckRows').children.map(row => row.text).join(' ');
  assert.match(rendered, /telegram/);
  assert.match(rendered, /4 of 5 sampled streams passed/);
  assert.match(rendered, /Resolver request failed \(1\)/);
  assert.doesNotMatch(rendered, /video-id|https?:\/\//);
  const samples = elements.get('#linkcheckSampleRows').children.map(row => row.text).join(' ');
  assert.match(elements.get('#linkcheckSamplesSummary').textContent, /showing 2 of 2/);
  assert.match(samples, /telegram · Passed/);
  assert.match(samples, /telegram · Failed/);
  assert.match(samples, /Resolver request failed/);
  assert.doesNotMatch(samples, /private-id|private\.invalid|token=secret/);
  assert.equal(elements.get('#linkcheckSamples').hidden, false);
  response = { available: true, exists: false, empty: false, providers: [] };
  await elements.get('#linkcheckRefresh').listeners.click();
  assert.match(elements.get('#linkcheckFeedback').textContent, /No link-check report has been recorded/);
  response = { available: true, exists: true, empty: true, providers: [] };
  await elements.get('#linkcheckRefresh').listeners.click();
  assert.match(elements.get('#linkcheckFeedback').textContent, /latest report is empty/);
  response = { available: false, exists: false, empty: false, providers: [] };
  await elements.get('#linkcheckRefresh').listeners.click();
  assert.match(elements.get('#linkcheckFeedback').textContent, /source is temporarily unavailable/);

  const deferred = () => {
    let resolve;
    const promise = new Promise(yes => { resolve = yes; });
    return { promise, resolve };
  };
  const first = deferred(), latest = deferred();
  let requestIndex = 0;
  window.AdminV2.getJson = () => (requestIndex++ === 0 ? first.promise : latest.promise);
  documentListeners['adminv2:navigate']({ detail: { route: 'quality', tab: 'issues' } });
  documentListeners['adminv2:navigate']({ detail: { route: 'quality', tab: 'issues' } });
  latest.resolve({ available: true, exists: true, checked_at: '2026-10-06 12:02:00', empty: false,
    providers: [{ provider: 'latest-provider', status: 'healthy', ok: 2, total: 2 }], samples: [] });
  await new Promise(resolve => setImmediate(resolve));
  first.resolve({ available: true, exists: true, checked_at: '2026-10-06 12:00:00', empty: false,
    providers: [{ provider: 'stale-provider', status: 'broken', ok: 0, total: 2 }], samples: [] });
  await new Promise(resolve => setImmediate(resolve));
  const raced = elements.get('#linkcheckRows').children.map(row => row.text).join(' ');
  assert.match(raced, /latest-provider/);
  assert.doesNotMatch(raced, /stale-provider/,
    'a late link-check response must not replace the report from the most recent Catalog Health view entry');
  assert.equal(elements.get('#linkcheckRefresh').disabled, false,
    'the latest response must release the refresh control even while an older request is pending');
  console.log('PASS: V2 link-check UI displays safe provider and failure summaries from the V2 endpoint');
})().catch(error => { console.error(error); process.exitCode = 1; });
