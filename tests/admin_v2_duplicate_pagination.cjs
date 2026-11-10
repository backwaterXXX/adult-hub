const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { adminV2Source, partEndAfter } = require('./helpers/admin_v2_scripts.cjs');

const source = adminV2Source();
assert(source.includes('function showLibraryTab(tab, match) {'),
  'the Library tab controller must remain the single owner of catalog screens');
assert(source.includes('else loadDuplicateCandidates({ cacheOnly: true });'),
  'opening Duplicates must not start a catalog scan');
const visibilityStart = source.indexOf('  function duplicateGroupIsVisible(');
const visibilityEnd = source.indexOf('  function selectDuplicateMatch(', visibilityStart);
assert(visibilityStart >= 0 && visibilityEnd > visibilityStart, 'duplicate visibility gate must stay readable');
const start = source.indexOf('  async function loadDuplicateCandidates(');
const end = source.indexOf('  function renderDuplicateCandidates(', start);
assert(start >= 0 && end > start, 'duplicate scan loader must remain available for pagination tests');
const actionStart = source.indexOf('  async function previewDuplicate(');
const actionEnd = partEndAfter(source, actionStart);
assert(actionStart >= 0 && actionEnd > actionStart, 'duplicate actions must remain available for pagination tests');

const state = {
  route: 'library', libraryTab: 'duplicates', duplicateMatch: 'catalog',
  duplicateOffset: 0, duplicateTotal: 0, duplicateItems: [],
  duplicateBusy: false, duplicateRequestSeq: 0, duplicateScans: [], duplicateRequests: new Map(),
  visualRequestSeq: 0, operationRequestSeq: 0, duplicatePending: null, operationRequest: null, operationPlan: null,
  operationId: null, operationPreviewContext: null,
};
const elements = {
  '#duplicateFeedback': { textContent: '' },
  '#duplicateRows': { replaceChildren() {}, querySelectorAll: () => [currentTrigger].filter(Boolean) },
  '#duplicateNotice': { hidden: true, textContent: '' },
  '#duplicateCategory': { value: 'same_file' },
  '#duplicateState': { hidden: true, textContent: '' },
  '#libraryDuplicatesPane': { hidden: false },
  '#duplicateCatalogGroup': { hidden: false },
  '#duplicateVisualGroup': { hidden: true },
  '#operationDialog': { open: false },
};
const requests = [];
const rendered = [];
let fakeNow = 100_000;
let holdNextFetch = false;
let releaseFetch;
let resultTotal = 1200;
let currentTrigger = null;
const operationPosts = [];
const context = vm.createContext({
  showQueued() {}, taskLink() { return null; },
  state,
  V2_API: '/admin/api/v2',
  DUPLICATE_PAGE_SIZE: 25,
  DUPLICATE_SCAN_BATCH_SIZE: 200,
  DUPLICATE_SCAN_CACHE_TTL_MS: 30_000,
  Date: { now: () => fakeNow },
  URLSearchParams,
  Number,
  Math,
  $: selector => elements[selector],
  setText(element, value) { element.textContent = value; },
  getJson: async url => (await context.fetch(url)).json(),
  responseError(_payload, fallback) { return fallback; },
  renderOperationPreview(plan, previewContext) {
    state.renderedPlan = plan;
    state.renderedPreviewContext = previewContext;
  },
  openDialog(dialog) { dialog.open = true; },
  renderDuplicateCandidates(payload) {
    rendered.push({
      offset: state.duplicateOffset,
      ids: state.duplicateItems.map(pair => pair.id),
      total: payload.total,
    });
  },
  fetch: async url => {
    const parsed = new URL(url, 'http://test');
    requests.push({
      offset: Number(parsed.searchParams.get('offset')),
      limit: Number(parsed.searchParams.get('limit')),
      category: parsed.searchParams.get('category'),
      sameTelegramFile: parsed.searchParams.get('include_same_telegram_file'),
    });
    const offset = requests.at(-1).offset;
    const limit = requests.at(-1).limit;
    const pairs = Array.from({ length: Math.min(limit, Math.max(0, resultTotal - offset)) }, (_item, index) => ({ id: offset + index }));
    const response = { ok: true, async json() {
      return { ok: true, pairs, total: resultTotal, scanned: 50_400, complete: true, candidate_limit_reached: false };
    } };
    if (holdNextFetch) {
      holdNextFetch = false;
      return new Promise(resolve => { releaseFetch = () => resolve(response); });
    }
    return response;
  },
  beginOperationRequest: () => ++state.operationRequestSeq,
  operationRequestIsCurrent: (sequence, route = state.route) => sequence === state.operationRequestSeq && route === state.route,
  create: (_tag, _className, text) => ({ textContent: text, href: '' }),
  announce: () => {},
  window: {
    AdminV2: {
      postJson: async (url, body) => {
        operationPosts.push({ url, body });
        if (url.includes('/preview')) return { plan_id: 'plan-1', operation_type: 'hide', items: [] };
        if (url.includes('/execute')) return { jobs: [{ job_id: 'job-1' }] };
        return { job_id: 'job-1' };
      },
    },
  },
});
vm.runInContext(`${source.slice(visibilityStart, visibilityEnd)}\n${source.slice(start, end)}\n${source.slice(actionStart, actionEnd)}\nglobalThis.loadDuplicateCandidates = loadDuplicateCandidates;\nglobalThis.loadVisualDuplicateResults = loadVisualDuplicateResults;\nglobalThis.previewDuplicate = previewDuplicate;\nglobalThis.dismissDuplicateSuggestion = dismissDuplicateSuggestion;`, context);

let visualRefreshes = 0;
const origLoadVisual = context.loadVisualDuplicateResults;
context.loadVisualDuplicateResults = (...args) => {
  visualRefreshes++;
  return origLoadVisual(...args);
};

(async () => {
  await context.loadDuplicateCandidates({ cacheOnly: true });
  assert.equal(requests.length, 0, 'opening an unscanned tab performs no expensive scan');
  await context.loadDuplicateCandidates();
  assert.deepEqual(requests, [{ offset: 0, limit: 200, category: 'same_file', sameTelegramFile: null }]);
  assert.deepEqual(rendered.at(-1).ids, Array.from({ length: 25 }, (_item, index) => index));

  state.duplicateOffset = 25;
  await context.loadDuplicateCandidates();
  assert.equal(requests.length, 1, 'next visible page must reuse the fetched scan batch');
  assert.deepEqual(rendered.at(-1).ids, Array.from({ length: 25 }, (_item, index) => index + 25));

  state.duplicateOffset = 175;
  await context.loadDuplicateCandidates();
  assert.equal(requests.length, 1, 'the final visible page in a batch must be served locally');
  assert.deepEqual(rendered.at(-1).ids, Array.from({ length: 25 }, (_item, index) => index + 175));

  state.duplicateOffset = 200;
  await context.loadDuplicateCandidates();
  assert.deepEqual(requests.at(-1), { offset: 200, limit: 200, category: 'same_file', sameTelegramFile: null });
  state.duplicateOffset = 175;
  await context.loadDuplicateCandidates();
  assert.equal(requests.length, 2, 'recent prior scan batches should remain available for Previous');
  assert.deepEqual(rendered.at(-1).ids, Array.from({ length: 25 }, (_item, index) => index + 175));

  await context.loadDuplicateCandidates({ refresh: true });
  assert.equal(requests.length, 3, 'Refresh must force a new server scan');
  assert.equal(requests.at(-1).offset, 0);

  elements['#duplicateCategory'].value = 'strong';
  state.duplicateOffset = 0;
  await context.loadDuplicateCandidates({ refresh: true });
  assert.deepEqual(requests.at(-1), { offset: 0, limit: 200, category: 'strong', sameTelegramFile: null });
  assert.equal(state.duplicateScans.length, 1, 'changing category must discard prior scan batches');

  fakeNow += 30_001;
  state.duplicateOffset = 25;
  await context.loadDuplicateCandidates();
  assert.deepEqual(requests.at(-1), { offset: 0, limit: 200, category: 'strong', sameTelegramFile: null },
    'expired batches must be rescanned before serving another page');

  const beforeTraversal = requests.length;
  elements['#duplicateCategory'].value = 'same_file';
  for (let offset = 0; offset < 1200; offset += 25) {
    state.duplicateOffset = offset;
    await context.loadDuplicateCandidates({ refresh: offset === 0 });
    assert.equal(state.duplicateItems.length, 25, `page at ${offset} should contain 25 candidates`);
  }
  assert.equal(requests.length - beforeTraversal, 6,
    '1,200 candidates should require six complete scans instead of 48 page scans');

  elements['#duplicateCategory'].value = '';
  state.duplicateOffset = 0;
  await context.loadDuplicateCandidates({ refresh: true });
  assert.deepEqual(requests.at(-1), { offset: 0, limit: 200, category: null, sameTelegramFile: 'false' },
    'the unfiltered view must leave Telegram pairs that reconcile merges automatically');

  elements['#duplicateCategory'].value = 'review';
  state.duplicateOffset = 0;
  const beforeConcurrent = requests.length;
  holdNextFetch = true;
  const olderPage = context.loadDuplicateCandidates({ refresh: true });
  state.duplicateOffset = 25;
  const newestPage = context.loadDuplicateCandidates();
  assert.equal(requests.length - beforeConcurrent, 1,
    'in-flight requests for the same category and scan batch must be shared');
  releaseFetch();
  await Promise.all([olderPage, newestPage]);
  assert.equal(requests.length - beforeConcurrent, 1);
  assert.deepEqual(rendered.at(-1).ids, Array.from({ length: 25 }, (_item, index) => index + 25),
    'the newest requested offset must render after a shared in-flight scan');

  resultTotal = 100;
  state.duplicateOffset = 200;
  const beforeShrink = requests.length;
  await context.loadDuplicateCandidates({ refresh: true });
  assert.deepEqual(requests.slice(beforeShrink).map(request => request.offset), [200, 0],
    'a refresh whose total shrinks below the current batch must fetch the new last page');
  assert.equal(state.duplicateOffset, 75, 'the visible offset should clamp to the last existing page');
  assert.deepEqual(rendered.at(-1).ids, Array.from({ length: 25 }, (_item, index) => index + 75));

  // A duplicate choice must stop at the visible, scoped preview until an operator queues it.
  state.duplicateOffset = 50;
  resultTotal = 1200;
  const cardFeedback = { hidden: true, className: '', textContent: '', appended: [], append(node) { this.appended.push(node); } };
  const card = {
    isConnected: true,
    querySelector: sel => sel === '.duplicate-action-feedback' ? cardFeedback : null,
    querySelectorAll: () => [],
  };
  const trigger = { isConnected: true, disabled: false, textContent: 'Keep this item', closest: () => card };
  currentTrigger = trigger;
  await context.previewDuplicate({ left: { id: 'v1' }, right: { id: 'v2' } }, 'left', trigger);
  assert.equal(state.duplicateOffset, 50, 'opening a duplicate preview on page 2+ must preserve the current page offset');
  assert.equal(elements['#operationDialog'].open, false, 'a duplicate action must not open a confirmation screen');
  assert.equal(trigger.textContent, 'Keep & hide other');
  assert.equal(trigger.disabled, false, 'duplicate row actions are re-enabled after the request completes');
  assert.deepEqual(JSON.parse(JSON.stringify(operationPosts)), [{
    url: '/admin/api/v2/operations/preview',
    body: { type: 'hide', keeper_id: 'v1', loser_ids: ['v2'] },
  }, {
    url: '/admin/api/v2/operations/execute',
    body: { plan_id: 'plan-1' },
  }], 'one clearly labeled duplicate action previews and queues the reversible hide without a confirmation screen');
  assert.equal(cardFeedback.className, 'duplicate-action-feedback is-queued');
  assert.match(cardFeedback.textContent, /No files were deleted/);
  assert.equal(cardFeedback.appended[0]?.textContent, 'View task');
  assert.equal(cardFeedback.appended[0]?.href, '/admin/v2/?job_id=job-1#jobs');
  assert.equal(state.duplicatePending, null, 'duplicate action lock is released after the request completes');

  state.duplicateMatch = 'visual';
  elements['#duplicateCatalogGroup'].hidden = true;
  elements['#duplicateVisualGroup'].hidden = false;
  // Refreshing results is covered separately; this assertion verifies the write flow.
  const originalVisualLoader = context.loadVisualDuplicateResults;
  context.loadVisualDuplicateResults = () => {};
  const beforeVisualChoice = operationPosts.length;
  await context.previewDuplicate({ left: { id: 'v1' }, right: { id: 'v2' } }, 'right', trigger);
  assert.equal(operationPosts.length, beforeVisualChoice + 2, 'visual decisions preview and execute');
  assert.equal(operationPosts.at(-2).body.keeper_id, 'v2');
  context.loadVisualDuplicateResults = originalVisualLoader;
  state.duplicateMatch = 'catalog';
  elements['#duplicateCatalogGroup'].hidden = false;
  elements['#duplicateVisualGroup'].hidden = true;

  context.window.AdminV2.postJson = async () => {
    const error = new Error('Duplicate identity has not been confirmed, or duration/part safeguards failed.');
    error.status = 409;
    throw error;
  };
  await context.previewDuplicate({ left: { id: 'v1' }, right: { id: 'v2' } }, 'left', trigger);
  assert.match(cardFeedback.textContent, /Duplicate identity has not been confirmed/);
  assert.doesNotMatch(cardFeedback.textContent, /suggestion changed/);
  context.window.AdminV2.postJson = async () => ({ job_id: 'job-1' });

  state.duplicateOffset = 50;
  const dismissButton = { isConnected: true, disabled: false };
  const dismissFeedback = { hidden: true, className: '', textContent: '', appended: [], append(node) { this.appended.push(node); } };
  const initialVisualRefreshes = visualRefreshes;
  await context.dismissDuplicateSuggestion({ left: { id: 'v1' }, right: { id: 'v2' } }, dismissButton, dismissFeedback);
  assert.equal(state.duplicateOffset, 50, 'dismissing/rejecting a duplicate on page 2+ must preserve current page offset');
  assert.equal(visualRefreshes, initialVisualRefreshes + 1, 'dismissing duplicate suggestion must refresh visual duplicate results');
  assert.equal(dismissButton.disabled, true, 'successful duplicate dismissal keeps button disabled to avoid duplicate execution');

  context.window.AdminV2.postJson = async () => { throw new Error('network down'); };
  await context.dismissDuplicateSuggestion({ left: { id: 'v1' }, right: { id: 'v2' } }, dismissButton, dismissFeedback);
  assert.equal(dismissButton.disabled, false, 'failed duplicate dismissal re-enables button for retry');

  // Visual lists use the same stale-response guarantees as catalog lists.
  state.duplicateMatch = 'visual';
  elements['#duplicateCatalogGroup'].hidden = true;
  elements['#duplicateVisualGroup'].hidden = false;
  const visualRows = { children: [], replaceChildren() { this.children = []; }, append(item) { this.children.push(item); } };
  elements['#visualDuplicateRows'] = visualRows;
  elements['#visualDuplicateStatus'] = { textContent: '' };
  let resolveOldVisual, resolveNewVisual;
  const oldVisual = new Promise(resolve => { resolveOldVisual = resolve; });
  const newVisual = new Promise(resolve => { resolveNewVisual = resolve; });
  let visualCalls = 0;
  const catalogGet = context.getJson;
  context.getJson = () => visualCalls++ === 0 ? oldVisual : newVisual;
  const oldVisualLoad = context.loadVisualDuplicateResults();
  const newVisualLoad = context.loadVisualDuplicateResults();
  resolveNewVisual({ ok: true, items: [], queue: { confirmed: 7 } });
  await newVisualLoad;
  const visualStatus = elements['#visualDuplicateStatus'].textContent;
  resolveOldVisual({ ok: true, items: [], queue: { confirmed: 1 } });
  await oldVisualLoad;
  assert.equal(elements['#visualDuplicateStatus'].textContent, visualStatus, 'older visual results cannot overwrite the latest response');
  let resolveHiddenVisual;
  context.getJson = () => new Promise(resolve => { resolveHiddenVisual = resolve; });
  const hiddenVisualLoad = context.loadVisualDuplicateResults();
  elements['#duplicateVisualGroup'].hidden = true;
  resolveHiddenVisual({ ok: true, items: [], queue: { confirmed: 99 } });
  await hiddenVisualLoad;
  assert.equal(elements['#visualDuplicateStatus'].textContent, visualStatus, 'visual results cannot repaint after leaving the group');
  context.getJson = catalogGet;
  state.duplicateMatch = 'catalog';

  // A scan that is still running when the operator leaves the tab must not paint it.
  const renderedBeforeLeaving = rendered.length;
  const requestsBeforeLeaving = requests.length;
  elements['#duplicateCatalogGroup'].hidden = false;
  elements['#duplicateVisualGroup'].hidden = true;
  holdNextFetch = true;
  const abandonedScan = context.loadDuplicateCandidates({ refresh: true });
  assert.equal(requests.length, requestsBeforeLeaving + 1, 'the tab still starts the requested scan');
  state.libraryTab = 'quarantine';
  elements['#libraryDuplicatesPane'].hidden = true;
  elements['#duplicateCatalogGroup'].hidden = true;
  releaseFetch();
  await abandonedScan;
  assert.equal(rendered.length, renderedBeforeLeaving,
    'a late duplicate scan response must not render after the tab changed');

  process.stdout.write('PASS: duplicate results keep 25-card pages while reusing 200-result scan batches\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
