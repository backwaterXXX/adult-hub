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
let navigationListener;
const document = {
  querySelector(selector) { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
  createElement(tag) { return new Element(tag); },
  addEventListener(name, fn) { if (name === 'adminv2:navigate') navigationListener = fn; }
};
let requested;
const window = { AdminV2: { getJson: async url => {
  requested = url;
  return { ok: true, available: true, exists: true, updated_at: '2026-10-06 12:00', omitted_entries: 2,
    entries: [
      { timestamp: '2026-10-06 12:00:00', level: 'info', message: 'Sync started.' },
      { timestamp: '2026-10-06 12:00:01', level: 'error', message: 'A Telegram source failed to sync.' },
      { timestamp: '2026-10-06 12:00:02', level: 'warning', message: 'Saved 8 of 9 catalog items with 1 write errors.' },
    ] };
} } };

vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/sync-activity.js', 'utf8'),
  { document, window, Intl, Date, String, Number, console });

(async () => {
  await elements.get('#syncActivityRefresh').listeners.click();
  assert.strictEqual(requested, '/admin/api/v2/sync/activity');
  const rendered = elements.get('#syncActivityRows').children.map(row => row.text).join(' ');
  assert.match(rendered, /Sync started/);
  assert.match(rendered, /A Telegram source failed to sync/);
  assert.match(rendered, /Saved 8 of 9 catalog items/);
  assert.doesNotMatch(rendered, /private-path|token=|https?:\/\//);
  assert.match(elements.get('#syncActivityFeedback').textContent, /2 detailed entries were hidden/);
  window.AdminV2.getJson = async () => ({ ok: true, available: true, exists: false, entries: [], omitted_entries: 0 });
  await elements.get('#syncActivityRefresh').listeners.click();
  assert.match(elements.get('#syncActivityFeedback').textContent, /No sync activity has been recorded/);
  window.AdminV2.getJson = async () => ({ ok: true, available: true, exists: true, entries: [], omitted_entries: 0 });
  await elements.get('#syncActivityRefresh').listeners.click();
  assert.match(elements.get('#syncActivityFeedback').textContent, /contains no recognized progress summaries/);
  window.AdminV2.getJson = async () => ({ ok: true, available: false, exists: false, entries: [], omitted_entries: 0 });
  await elements.get('#syncActivityRefresh').listeners.click();
  assert.match(elements.get('#syncActivityFeedback').textContent, /source is temporarily unavailable/);

  let resolveOld, resolveCurrent;
  let requestNumber = 0;
  window.AdminV2.getJson = () => new Promise(resolve => {
    requestNumber += 1;
    if (requestNumber === 1) resolveOld = resolve;
    else resolveCurrent = resolve;
  });
  navigationListener({ detail: { route: 'sources' } });
  navigationListener({ detail: { route: 'library' } });
  navigationListener({ detail: { route: 'sources' } });
  resolveCurrent({ ok: true, available: true, exists: true, updated_at: '2026-10-07 12:00', omitted_entries: 0,
    entries: [{ timestamp: '2026-10-07 12:00:00', level: 'info', message: 'Current sync result.' }] });
  await new Promise(resolve => setImmediate(resolve));
  resolveOld({ ok: true, available: true, exists: true, updated_at: '2026-10-07 11:00', omitted_entries: 0,
    entries: [{ timestamp: '2026-10-07 11:00:00', level: 'error', message: 'Stale sync result.' }] });
  await new Promise(resolve => setImmediate(resolve));
  const latest = elements.get('#syncActivityRows').children.map(row => row.text).join(' ');
  assert.match(latest, /Current sync result/);
  assert.doesNotMatch(latest, /Stale sync result/,
    'a slow request from before navigation must not overwrite the refreshed Sources view');
  assert.strictEqual(elements.get('#syncActivityRefresh').disabled, false,
    'the latest request should release the refresh button');
  console.log('PASS: V2 sync-activity UI renders only API-projected safe progress summaries');
})().catch(error => { console.error(error); process.exitCode = 1; });
