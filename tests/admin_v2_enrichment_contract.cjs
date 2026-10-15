const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert.match(template, /One scan processes one bounded batch per provider/,
  'the Enrichment tab must set an accurate expectation for manual scan scope');
assert.match(template, /configured sync cadence is shown below; when the sync runner is active, later runs continue the queues automatically/,
  'the Enrichment tab must show automatic cadence while qualifying that the sync runner must be active');
assert.match(template, /Remaining counts are estimates because provider eligibility and retries vary/,
  'the Enrichment tab must label remaining-work counts as estimates');
assert.match(template, /href="#quality\?tab=enrichment"[^>]*data-section-tab="enrichment"/,
  'Enrichment must be a bookmarkable Catalog Health tab');
assert.match(template, /id="qualityEnrichmentPane"[^>]*data-quality-pane="enrichment"/,
  'enrichment progress and proposals must share the Catalog Health Enrichment pane');
assert.match(template, /id="enrichmentFreshnessAnnouncement"[^>]*role="status"[^>]*aria-live="polite"/,
  'freshness changes must be announced without making every 15-second timestamp update live');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this._textContent = ''; this.listeners = {}; this.disabled = false; this.attributes = {}; this.dataset = {}; }
  get textContent() { return this._textContent; }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
}

const elements = new Map();
const location = { hash: '#quality?tab=enrichment' };
let navigationListener;
let visibilityListener;
let pollCallback;
let pollInterval;
let clearedPoll = false;
const document = {
  querySelector(selector) { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
  createElement(tag) { return new Element(tag); },
  hidden: false,
  addEventListener(name, fn) {
    if (name === 'adminv2:navigate') navigationListener = fn;
    if (name === 'visibilitychange') visibilityListener = fn;
  }
};
let requested;
let getCalls = 0;
let checkedVideos = 50;
let batchLimits = { videos_per_run: 500, pbc_actors_per_run: 50,
  theporndb_actors_per_run: 40, stashdb_actors_per_run: 35, portraits_per_run: 25 };
let syncSchedule = { mode: 'interval', daily_at: null, interval_minutes: 60, run_on_startup: false };
const calls = [];
let responseJobId = 'metadata-job-1';
let responseJobState = 'queued';
let failNextGet = false;
const window = { confirm: () => true, AdminV2: { postJson: async (url, body) => {
  calls.push({ url, body });
  return { ok: true, job_id: responseJobId, state: responseJobState };
}, getJson: async url => {
  getCalls += 1;
  requested = url;
  if (failNextGet) { failNextGet = false; throw new Error('temporary network failure'); }
  return { ok: true, available: true, catalog: { videos: 10000, performers: 1000 },
    batch_limits: batchLimits,
    sync_schedule: syncSchedule,
    totals: { attempts: 70, found: 30, no_match: 35, errors: 5, due: 8 },
    sources: [{ entity_type: 'video', source: 'studio_scene', catalog_total: 10000,
      attempts: checkedVideos, found: 30, no_match: 18, errors: 2, due: 8, remaining: 9958 - (checkedVideos - 50) },
    { entity_type: 'performer', source: 'pbc', catalog_total: 1000,
      attempts: 50, found: 30, no_match: 18, errors: 2, due: 0, remaining: 950 },
    { entity_type: 'video', source: '  LUKAYAN_FINGERPRINT ', catalog_total: 10000,
      attempts: 12, found: 4, no_match: 7, errors: 1, due: 8, remaining: 9988 },
    { entity_type: 'video', source: 'telegram_fingerprint', catalog_total: 10000,
      attempts: 11, found: 4, no_match: 6, errors: 1, due: 8, remaining: 9989 },
    { entity_type: 'video', source: '  LUKAYAN_OSHASH ', catalog_total: 10000,
      attempts: 10, found: 4, no_match: 5, errors: 1, due: 8, remaining: 9990 },
    { entity_type: 'video', source: 'telegram_oshash', catalog_total: 10000,
      attempts: 9, found: 3, no_match: 5, errors: 1, due: 8, remaining: 9991 }] };
} } };

vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/enrichment.js', 'utf8'),
  { document, window, location, URLSearchParams, Intl, String, Number, console,
    setInterval: function(callback, delay) { pollCallback = callback; pollInterval = delay; return 1; },
    clearInterval: () => { clearedPoll = true; } });

(async () => {
  failNextGet = true;
  await elements.get('#enrichmentRefresh').listeners.click();
  assert.strictEqual(elements.get('#enrichmentFreshness').dataset.state, 'stale');
  assert.strictEqual(elements.get('#enrichmentFreshness').textContent,
    'Refresh failed. Metadata progress is unavailable.');
  assert.strictEqual(elements.get('#enrichmentFreshnessAnnouncement').textContent,
    'Metadata progress refresh failed. Progress is unavailable.');
  await elements.get('#enrichmentRefresh').listeners.click();
  assert.strictEqual(elements.get('#enrichmentFreshness').dataset.state, 'fresh');
  assert.strictEqual(elements.get('#enrichmentFreshnessAnnouncement').textContent,
    'Metadata progress refresh succeeded. Progress is current again.');
  await elements.get('#enrichmentRefresh').listeners.click();
  assert.strictEqual(requested, '/admin/api/v2/metadata/enrichment');
  assert.match(elements.get('#enrichmentFreshness').textContent, /Last successful progress refresh:/);
  assert.strictEqual(elements.get('#enrichmentFreshness').dataset.state, 'fresh');
  assert.match(elements.get('#enrichmentFeedback').textContent, /70 source records tracked/);
  assert.match(elements.get('#enrichmentBatchLimits').textContent,
    /500 videos · PBC 50 actors · ThePornDB 40 actors · StashDB 35 actors · 25 portraits/);
  assert.strictEqual(elements.get('#enrichmentSchedule').textContent,
    'Configured sync cadence: Every 60 minutes. Startup sync is disabled.');
  syncSchedule = { mode: 'daily', daily_at: '03:30', interval_minutes: null, run_on_startup: true };
  await elements.get('#enrichmentRefresh').listeners.click();
  assert.strictEqual(elements.get('#enrichmentSchedule').textContent,
    'Configured sync cadence: Daily at 03:30 runner local time. A sync is configured to run on startup.');
  assert.match(elements.get('#enrichmentSummary').children.map(card => card.children.map(child => child.textContent).join(' ')).join(' '), /1,000/);
  const row = elements.get('#enrichmentRows').children[0];
  assert.match(row.children[0].children[0].textContent, /Videos · Studio catalogs \+ ThePornDB/);
  assert.match(elements.get('#enrichmentRows').children[2].children[0].children[0].textContent,
    /^Videos · Telegram scene lookup$/);
  assert.match(elements.get('#enrichmentRows').children[3].children[0].children[0].textContent,
    /^Videos · Telegram scene lookup$/);
  assert.match(elements.get('#enrichmentRows').children[4].children[0].children[0].textContent,
    /^Videos · Telegram scene lookup$/);
  assert.match(elements.get('#enrichmentRows').children[5].children[0].children[0].textContent,
    /^Videos · Telegram scene lookup$/);
  for (const index of [2, 3, 4, 5]) {
    assert.doesNotMatch(elements.get('#enrichmentRows').children[index].children[0].children[0].textContent,
      /(?:lukayan|telegram)_/i);
  }
  assert.strictEqual(row.children[1].value, 50);
  assert.match(row.children[2].textContent, /9,958 remaining · about 20 batches at configured limit/);
  assert.match(elements.get('#enrichmentRows').children[1].children[2].textContent,
    /950 remaining · about 19 batches at configured limit/);
  await elements.get('#enrichmentScan').listeners.click({ currentTarget: elements.get('#enrichmentScan') });
  assert.deepEqual(calls, [{ url: '/admin/api/v2/metadata/enrichment/scan', body: {} }]);
  const feedback = elements.get('#enrichmentFeedback');
  assert.match(feedback.children[0].textContent, /is queued \(job metadata-job-1\)/);
  assert.strictEqual(feedback.children[1].textContent, 'Open task details');
  assert.strictEqual(feedback.children[1].href, '/admin/v2/?job_id=metadata-job-1#jobs');
  responseJobState = 'processing';
  await elements.get('#enrichmentScan').listeners.click({ currentTarget: elements.get('#enrichmentScan') });
  assert.match(feedback.children[0].textContent, /is already processing \(job metadata-job-1\)/);
  responseJobState = 'accepted';
  responseJobId = 'metadata-job-2';
  await elements.get('#enrichmentScan').listeners.click({ currentTarget: elements.get('#enrichmentScan') });
  assert.match(feedback.children[0].textContent, /was accepted; current state is unavailable \(job metadata-job-2\)/);
  responseJobId = '<img src=x>';
  await elements.get('#enrichmentScan').listeners.click({ currentTarget: elements.get('#enrichmentScan') });
  assert.deepEqual(calls, [
    { url: '/admin/api/v2/metadata/enrichment/scan', body: {} },
    { url: '/admin/api/v2/metadata/enrichment/scan', body: {} },
    { url: '/admin/api/v2/metadata/enrichment/scan', body: {} },
    { url: '/admin/api/v2/metadata/enrichment/scan', body: {} }
  ]);
  assert.strictEqual(feedback.textContent, 'Metadata scan request accepted; current task state is unavailable. Open Tasks to follow provider progress.');
  assert.strictEqual(feedback.children.length, 0);
  await navigationListener({ detail: { route: 'system' } });
  assert.strictEqual(pollInterval, 15000);
  await navigationListener({ detail: { route: 'quality', tab: 'enrichment' } });
  assert.strictEqual(pollInterval, 15000, 'the Enrichment tab starts bounded progress polling');
  feedback.replaceChildren(new Element('span'), new Element('a'));
  feedback.children[0].textContent = 'Metadata scan is queued (job metadata-job-1). ';
  feedback.children[1].textContent = 'Open task details';
  const beforePoll = getCalls;
  checkedVideos = 51;
  await pollCallback();
  assert.strictEqual(getCalls, beforePoll + 1);
  assert.strictEqual(elements.get('#enrichmentRows').children[0].children[1].value, 51);
  assert.match(feedback.children[0].textContent, /Metadata scan is queued \(job metadata-job-1\)/);
  const lastSuccessfulRefresh = elements.get('#enrichmentFreshness').textContent.match(/refresh: (.+) local time/i)[1];
  failNextGet = true;
  await pollCallback();
  assert.strictEqual(elements.get('#enrichmentFreshness').dataset.state, 'stale');
  assert.ok(elements.get('#enrichmentFreshness').textContent.includes(lastSuccessfulRefresh));
  const staleAnnouncement = elements.get('#enrichmentFreshnessAnnouncement').textContent;
  assert.match(staleAnnouncement, /refresh failed.*last successful data/i);
  assert.match(feedback.children[0].textContent, /Metadata scan is queued \(job metadata-job-1\)/,
    'a failed silent refresh must not erase task feedback');
  failNextGet = true;
  await pollCallback();
  assert.strictEqual(elements.get('#enrichmentFreshnessAnnouncement').textContent, staleAnnouncement,
    'repeated polling failures must not repeat the live announcement');
  const beforeHiddenPoll = getCalls;
  document.hidden = true;
  checkedVideos = 52;
  assert.strictEqual(pollCallback(), undefined);
  assert.strictEqual(getCalls, beforeHiddenPoll);
  assert.strictEqual(elements.get('#enrichmentRows').children[0].children[1].value, 51);
  document.hidden = false;
  checkedVideos = 53;
  await visibilityListener();
  assert.strictEqual(getCalls, beforeHiddenPoll + 1);
  assert.strictEqual(elements.get('#enrichmentRows').children[0].children[1].value, 53);
  assert.strictEqual(elements.get('#enrichmentFreshness').dataset.state, 'fresh');
  assert.match(elements.get('#enrichmentFreshnessAnnouncement').textContent, /refresh succeeded.*current again/i);
  await navigationListener({ detail: { route: 'jobs' } });
  assert.strictEqual(clearedPoll, true);
  batchLimits = { videos_per_run: -1 };
  syncSchedule = { mode: 'daily', daily_at: '25:99', run_on_startup: 'yes' };
  await elements.get('#enrichmentRefresh').listeners.click();
  assert.strictEqual(elements.get('#enrichmentBatchLimits').textContent,
    'Configured per-run batch limits are unavailable.');
  assert.doesNotMatch(elements.get('#enrichmentRows').children[0].children[2].textContent,
    /batches at configured limit/, 'unknown caps must not produce a misleading estimate');
  assert.strictEqual(elements.get('#enrichmentSchedule').textContent,
    'Configured sync schedule is unavailable.');
  console.log('PASS: V2 metadata enrichment progress and bounded scan action render safely');
})().catch(error => { console.error(error); process.exitCode = 1; });
