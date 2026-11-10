const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { adminV2Source, partEndAfter } = require('./helpers/admin_v2_scripts.cjs');

const source = adminV2Source();
function extract(name) {
  const marker = `  async function ${name}(`;
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `missing ${name}`);
  const end = source.indexOf('\n  async function ', start + marker.length);
  const nextSync = source.indexOf('\n  function ', start + marker.length);
  // Each part is its own IIFE, so a slice must also stop where the part that holds the function ends.
  const partEnd = partEndAfter(source, start + marker.length);
  const stop = [end, nextSync, partEnd].filter(index => index >= 0).sort((a, b) => a - b)[0];
  return source.slice(start, stop < 0 ? source.length : stop);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
class Element {
  constructor() { this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.style = {}; this.textContent = ''; this.disabled = false; this.open = false; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes ||= {}; this.attributes[name] = value; }
  getAttribute(name) { return this.attributes?.[name] ?? null; }
  append(...children) { this.children.push(...children); }
  insertBefore(child, reference) {
    const index = reference ? this.children.indexOf(reference) : -1;
    if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
    child.parentNode = this;
    return child;
  }
  get nextSibling() {
    const siblings = this.parentNode?.children || [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  contains(child) { return this.children.includes(child); }
  replaceChildren(...children) { this.children = children; }
  after(...children) { this.afterChildren = children; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
  focus() { this.focused = true; if (globalThis.document) globalThis.document.activeElement = this; }
}
const textOf = node => `${node.textContent || ''} ${(node.children || []).map(textOf).join(' ')}`;

(async () => {
  const elements = new Map();
  const get = selector => {
    const key = selector.replace(/^#/, '');
    if (!elements.has(key)) elements.set(key, new Element());
    return elements.get(key);
  };
  const state = {
    route: 'library', libraryTab: 'videos', duplicateMatch: 'catalog', performersTab: 'review',
    statusRequestSeq: 0, operationRequestSeq: 0, operationStatusSeq: 0, issueRequestSeq: 0,
    issues: [], issueLoading: false, issueFiltersInitialized: false,
    availabilityHistorySeq: 0, detailId: null,
    duplicateRequestSeq: 0, peopleRequestSeq: 0, quarantineRequestSeq: 0,
    duplicateScans: [], duplicateRequests: new Map(), duplicateItems: [], duplicateTotal: 0, duplicateBusy: false,
    personFilmographySeq: 0, personMergePreviewSeq: 0, pendingPersonMerge: null,
    metadataRequest: null, metadataPlanId: null, metadataDialogSeq: 0,
    selectedVideos: new Map(), isStale: false,
    operationRequest: null, operationPlan: null, operationId: null, operationPollTimer: null,
    operationRefreshing: false, availabilityRequest: null, quarantineOffset: 0, duplicateOffset: 0,
  };
  const announcements = [];
  const context = vm.createContext({
    showQueued(payload, message) { announcements.push('Queued ' + (payload.jobs?.[0]?.job_id || payload.job_id || '') + ' ' + message); }, taskLink() { return null; },
    state, V2_API: '/admin/api/v2', DUPLICATE_PAGE_SIZE: 25, DUPLICATE_SCAN_BATCH_SIZE: 200,
    DUPLICATE_SCAN_CACHE_TTL_MS: 30_000,
    window: { AdminV2: {} },
    $: get, $$: () => [],
    document: { body: {}, activeElement: null, createElement: () => new Element() }, URLSearchParams,
    setText: (node, value) => { node.textContent = String(value); },
    create: (_tag, _className, text) => { const node = new Element(); if (text != null) node.textContent = String(text); return node; },
    announce: message => announcements.push(message),
    renderOperationPreview: plan => { state.renderedPlan = plan; },
    renderDuplicateCandidates: () => { state.renderedDuplicates = state.duplicateScans[state.duplicateScans.length - 1]; },
    renderIssues: payload => { state.renderedIssues = payload; },
    issueFeedUrl: offset => `/admin/api/v2/issues?limit=50&offset=${offset || 0}`,
    readIssueFiltersFromUrl() {},
    renderStatus() {}, updateServices() {}, renderWriteAccessNotice() {},
    renderServiceChip() {}, renderOverviewCards() {},
    openDialog: dialog => { if (dialog) dialog.open = true; },
    closeDialog: dialog => { if (dialog) dialog.open = false; },
    requestJson: async (url, options = {}) => {
      const resp = await context.fetch(url, {
        method: options.method || 'GET',
        body: options.payload !== undefined ? JSON.stringify(options.payload) : undefined,
      });
      return resp.json();
    },
    getJson: async () => { throw new Error('Unexpected getJson'); },
    beginOperationRequest: () => ++state.operationRequestSeq,
    operationRequestIsCurrent: (sequence, route = state.route) => sequence === state.operationRequestSeq && route === state.route,
    responseError: (_body, fallback) => fallback,
    displayTime: value => String(value),
    titleCase: value => String(value),
    safeText: value => String(value), scheduleOperationPoll() {}, stopDetailPlayback() {},
    metadataPayloadFromEditor: () => ({ video_id: state.detailVideo?.id, changes: { title: 'Edited title' }, reset_fields: [] }),
    renderMetadataPreview: preview => { state.renderedMetadataPreview = preview; },
    bulkMetadataPayloadFromEditor: () => ({ type: 'metadata_update_batch', video_ids: [...(state.metadataBulkVideoIds || [])], changes: { studio: 'Studio' }, reset_fields: [] }),
    renderBulkMetadataPreview: preview => { state.renderedBulkMetadataPreview = preview; },
    updateBulkSelection() {}, loadQuarantine() {}, loadDuplicateCandidates() {}, loadVisualDuplicateResults() {}, refreshOperationStatus: async () => {},
    loadOrphanFileQuarantine: async () => {},
    fetch: async () => { throw new Error('Unexpected fetch'); },
    clearTimeout() {}, setTimeout: () => 1, encodeURIComponent, AbortController,
  });
  const run = (name, extras = '') => {
    vm.runInContext(`${extract(name)}\nglobalThis.__target = ${name};`, context);
    return context.__target;
  };
  const runSync = name => {
    const marker = `  function ${name}(`;
    const start = source.indexOf(marker);
    assert.notEqual(start, -1, `missing ${name}`);
    const end = source.indexOf('\n  function ', start + marker.length);
    const asyncEnd = source.indexOf('\n  async function ', start + marker.length);
    const partEnd = partEndAfter(source, start + marker.length);
    const stop = [end, asyncEnd, partEnd].filter(index => index >= 0).sort((a, b) => a - b)[0];
    vm.runInContext(`${source.slice(start, stop < 0 ? source.length : stop)}\nglobalThis.__target = ${name};`, context);
    return context.__target;
  };

  // Library selection supports availability, quarantine, and bulk metadata; its accessible name must describe that shared purpose.
  const selectedVideo = { id: 'video-7', title: 'A Shared Selection', studio: 'Studio' };
  context.itemId = video => String(video.id);
  context.first = (object, keys) => keys.map(key => object?.[key]).find(value => value != null);
  context.safeText = value => String(value ?? '');
  context.availability = () => ['Available', 'pill-success'];
  context.thumbnail = () => null;
  context.displayTime = () => '—';
  context.unavailableReason = () => '';
  context.openDetail = () => {};
  context.setVideoSelected = () => {};
  const cardCheckbox = runSync('cardFor')(selectedVideo).children[0].children[0];
  const tableCheckbox = runSync('tableRow')(selectedVideo).children[0].children[0];
  for (const checkbox of [cardCheckbox, tableCheckbox]) {
    assert.equal(checkbox.getAttribute('aria-label'), 'Select A Shared Selection for bulk actions');
    assert.doesNotMatch(checkbox.getAttribute('aria-label'), /quarantine/i,
      'selection must describe the shared bulk-action scope, not one specific operation');
  }

  // Closing and reopening the same video must invalidate its older history request.
  state.detailId = 'same-video';
  const historyA = deferred(), historyB = deferred();
  const historyResponses = [historyA, historyB];
  context.window.AdminV2.getJson = () => historyResponses.shift().promise;
  const loadAvailabilityHistory = run('loadAvailabilityHistory');
  const oldHistory = loadAvailabilityHistory('same-video');
  runSync('closeDetailVisual')();
  state.detailId = 'same-video';
  const currentHistory = loadAvailabilityHistory('same-video');
  historyB.resolve({ items: [{ status: 'available', checked_at: 'new-check', evidence: 'Fresh result' }] });
  await currentHistory;
  historyA.resolve({ items: [{ status: 'unavailable', checked_at: 'old-check', evidence: 'Stale result' }] });
  await oldHistory;
  assert.match(textOf(get('#detailContent')), /Fresh result/);
  assert.doesNotMatch(textOf(get('#detailContent')), /Stale result/,
    'a late history result from the closed detail must not append to the reopened video');

  // A detail-load failure must retain the heading named by the drawer's aria-labelledby.
  state.detailId = 'failed-video';
  context.getJson = async () => { throw new Error('Detail service unavailable.'); };
  await run('loadDetail')('failed-video');
  assert.equal(get('#detailContent').children[1].id, 'detailTitle',
    'the drawer failure state must retain its accessible-name target');
  assert.match(textOf(get('#detailContent')), /Could not open video/);
  assert.equal(get('.detail-drawer').focused, true, 'focus moves into the detail drawer after a load failure');

  // A delayed failure for an old detail must not replace the latest drawer state.
  const oldDetail = deferred(), currentDetail = deferred();
  let detailRequest = 0;
  context.getJson = () => (detailRequest++ === 0 ? oldDetail.promise : currentDetail.promise);
  state.detailId = 'old-video';
  const loadDetail = run('loadDetail');
  const oldDetailLoad = loadDetail('old-video');
  state.detailId = 'current-video';
  const currentDetailLoad = loadDetail('current-video');
  currentDetail.reject(new Error('Current detail failed.'));
  await currentDetailLoad;
  oldDetail.reject(new Error('Stale detail failed.'));
  await oldDetailLoad;
  assert.match(textOf(get('#detailContent')), /Current detail failed/);
  assert.doesNotMatch(textOf(get('#detailContent')), /Stale detail failed/,
    'a delayed error for the prior video must not replace the current detail state');

  // An action that refreshes the drawer after its own request finished must not
  // resurrect a detail view the user dismissed while that request was in flight.
  state.detailId = 'artwork-video';
  const artworkDetail = deferred();
  const detailSignals = [];
  let detailFetches = 0;
  context.getJson = async (_url, signal) => {
    detailSignals.push(signal);
    detailFetches += 1;
    return artworkDetail.promise;
  };
  context.renderDetail = video => { state.renderedDetail = video; };
  const artworkLoad = loadDetail('artwork-video');
  assert.equal(get('#detailOverlay').open, true, 'an opened detail shows its loading drawer');
  runSync('closeDetailVisual')();
  state.detailId = null;
  artworkDetail.resolve({ id: 'artwork-video', title: 'Artwork Video' });
  await artworkLoad;
  assert.equal(detailSignals.length, 1);
  assert.equal(detailSignals[0].aborted, true,
    'closing the drawer aborts the detail request it started');
  await loadDetail('artwork-video');
  assert.equal(detailFetches, 1,
    'a refresh for a closed detail must not fetch the video again');
  assert.equal(get('#detailOverlay').open, false,
    'a finished artwork task must not reopen the dismissed VIDEO DETAILS drawer');
  assert.equal(state.renderedDetail, undefined,
    'the abandoned refresh must not render into the closed drawer');

  // A slower old preview must not replace a newer availability selection.
  const availabilityA = deferred(), availabilityB = deferred();
  const availabilityCalls = [availabilityA, availabilityB];
  context.fetch = async (_url, options) => {
    const ids = JSON.parse(options.body).video_ids;
    const deferredResponse = availabilityCalls.shift();
    return deferredResponse.promise.then(title => ({ ok: true, status: 200, json: async () => ({
      ok: true, missing_count: 0, items: [{ video_id: ids[0], title, current: 'unknown', last_check: null }],
    }) }));
  };
  const reviewAvailability = run('reviewAvailability');
  const oldAvailability = reviewAvailability(['A']);
  const newAvailability = reviewAvailability(['B']);
  availabilityB.resolve('Video B'); await newAvailability;
  availabilityA.resolve('Video A'); await oldAvailability;
  assert.deepEqual(Array.from(state.availabilityRequest), ['B']);
  assert.match(textOf(get('#operationDialogContent')), /Video B/);
  assert.doesNotMatch(textOf(get('#operationDialogContent')), /Video A/);

  // A quarantine preview is bound to the exact selection it was created for.
  get('#operationDialog').close();
  state.selectedVideos = new Map([['A', 'Video A']]);
  const quarantineResponse = deferred();
  context.window.AdminV2.postJson = () => quarantineResponse.promise;
  const reviewQuarantine = run('reviewQuarantine');
  const quarantine = reviewQuarantine();
  state.selectedVideos = new Map([['B', 'Video B']]);
  context.beginOperationRequest();
  quarantineResponse.resolve({ plan_id: 'plan-A', operation_type: 'quarantine', items: [] });
  await quarantine;
  assert.equal(get('#operationDialog').open, false, 'old quarantine preview must not open after selection changes');
  assert.equal(state.operationRequest, null);

  // A late execute response must leave a newer operation plan and selection intact.
  let finishExecute;
  context.window.AdminV2.postJson = () => new Promise(resolve => { finishExecute = resolve; });
  state.operationPlan = { plan_id: 'plan-A', operation_type: 'quarantine' };
  state.operationRequest = { type: 'quarantine', video_ids: ['A'] };
  state.selectedVideos = new Map([['A', 'Video A'], ['B', 'Video B']]);
  const execute = run('executeOperationPlan');
  const executing = execute();
  state.operationPlan = { plan_id: 'plan-B', operation_type: 'quarantine' };
  state.operationRequest = { type: 'quarantine', video_ids: ['B'] };
  context.beginOperationRequest();
  finishExecute({ jobs: [{ job_id: 'job-A' }] });
  await executing;
  assert.equal(state.operationPlan.plan_id, 'plan-B');
  assert.equal(state.selectedVideos.has('A'), true);
  assert.equal(state.selectedVideos.has('B'), true);
  assert.match(announcements.at(-1), /job-A/);

  // Closing an old status dialog invalidates its in-flight poll before a new task opens.
  const statusA = deferred(), statusB = deferred();
  const statusResponses = [statusA, statusB];
  context.window.AdminV2.getJson = () => statusResponses.shift().promise;
  state.operationId = 'operation-A';
  state.operationPlan = { operation_type: 'restore', items: [{ video_id: 'A', title: 'Old video' }] };
  get('#operationDialog').open = true;
  const refreshStatus = run('refreshOperationStatus');
  const oldStatus = refreshStatus();
  state.operationRequestSeq += 1;
  state.operationStatusSeq += 1;
  state.operationRefreshing = false;
  get('#operationDialog').open = false;
  state.operationId = 'operation-B';
  state.operationPlan = { operation_type: 'restore', items: [{ video_id: 'B', title: 'New video' }] };
  get('#operationDialog').open = true;
  const newStatus = refreshStatus();
  statusB.resolve({ operation: { items: [{}], jobs: [{ video_id: 'B', job_id: 'job-B', state: 'queued' }] } }); await newStatus;
  statusA.resolve({ operation: { items: [{}], jobs: [{ video_id: 'A', job_id: 'job-A', state: 'done' }] } }); await oldStatus;
  assert.match(textOf(get('#operationDialogContent')), /New video/);
  assert.doesNotMatch(textOf(get('#operationDialogContent')), /Old video/);

  // Fast filter/page changes on Start and the Library tabs keep the latest request result.
  state.route = 'start';
  const issueA = deferred(), issueB = deferred();
  const issueResponses = [issueA, issueB];
  context.getJson = () => issueResponses.shift().promise;
  const loadIssues = run('loadIssues');
  const oldIssues = loadIssues();
  get('#issueSeverity').value = 'high';
  const newIssues = loadIssues();
  issueB.resolve({ marker: 'new-issues' }); await newIssues;
  issueA.resolve({ marker: 'old-issues' }); await oldIssues;
  assert.equal(state.renderedIssues.marker, 'new-issues');

  const initialIssues = deferred(), refreshedIssues = deferred();
  let issueCall = 0;
  context.getJson = path => path.includes('/issues?')
    ? (issueCall++ === 0 ? initialIssues.promise : refreshedIssues.promise)
    : Promise.resolve(path.endsWith('/status') ? { ok: true } : { actions_enabled: true });
  const loadStatus = run('loadStatus');
  const staleInitialStatus = loadStatus();
  const currentIssueLoad = loadIssues();
  refreshedIssues.resolve({ marker: 'filtered-issues' }); await currentIssueLoad;
  initialIssues.resolve({ marker: 'initial-home-issues' }); await staleInitialStatus;
  assert.equal(state.renderedIssues.marker, 'filtered-issues', 'late Home status load must not replace the current issue filter result');

  state.route = 'library';
  state.libraryTab = 'duplicates';
  state.duplicateMatch = 'catalog';
  get('#libraryDuplicatesPane').hidden = false;
  get('#duplicateCatalogGroup').hidden = false;
  get('#duplicateVisualGroup').hidden = true;
  context.duplicateGroupIsVisible = runSync('duplicateGroupIsVisible');
  get('#duplicateCategory').value = 'oshash_linked';
  const duplicateA = deferred(), duplicateB = deferred();
  const duplicateResponses = [duplicateA, duplicateB];
  context.fetch = () => duplicateResponses.shift().promise;
  context.getJson = async (url, signal) => (await context.fetch(url, { signal })).json();
  const loadDuplicates = run('loadDuplicateCandidates');
  const oldDuplicates = loadDuplicates();
  state.duplicateOffset = 25;
  const newDuplicates = loadDuplicates();
  duplicateB.resolve({ ok: true, status: 200, json: async () => ({ ok: true, marker: 'new-duplicates', pairs: [], total: 100, scanned: 0 }) }); await newDuplicates;
  duplicateA.resolve({ ok: true, status: 200, json: async () => ({ ok: true, marker: 'old-duplicates', pairs: [], total: 100, scanned: 0 }) }); await oldDuplicates;
  assert.equal(state.renderedDuplicates.marker, 'new-duplicates');

  // Switching away from the Duplicates tab must discard a request that lands afterwards.
  const lateDuplicates = deferred();
  context.fetch = () => lateDuplicates.promise;
  const abandonedDuplicates = loadDuplicates();
  state.libraryTab = 'quarantine';
  get('#libraryDuplicatesPane').hidden = true;
  lateDuplicates.resolve({ ok: true, status: 200, json: async () => ({ ok: true, marker: 'late-duplicates', pairs: [], total: 100, scanned: 0 }) });
  await abandonedDuplicates;
  assert.equal(state.renderedDuplicates.marker, 'new-duplicates',
    'a late duplicate result must not render after leaving the Duplicates tab');

  state.route = 'performers';
  state.performersTab = 'directory';
  get('#performerDirectoryPane').hidden = false;
  get('#peopleQuery').value = 'old query';
  const peopleA = deferred(), peopleB = deferred();
  const peopleResponses = [peopleA, peopleB];
  context.fetch = () => peopleResponses.shift().promise;
  const loadPeople = run('loadPeople');
  const oldPeople = loadPeople();
  get('#peopleQuery').value = 'new query';
  const newPeople = loadPeople();
  peopleB.resolve({ ok: true, status: 200, json: async () => ({ ok: true, available: true, items: [], total: 2 }) }); await newPeople;
  peopleA.resolve({ ok: true, status: 200, json: async () => ({ ok: true, available: true, items: [], total: 9 }) }); await oldPeople;
  assert.equal(get('#peopleFeedback').textContent, '');

  context.fetch = async () => ({ ok: true, status: 200, json: async () => ({
    ok: true, available: false,
  }) });
  await loadPeople();
  assert.equal(get('#peopleFeedback').textContent, 'People catalog unavailable');
  assert.equal(get('#peopleState').textContent, 'People data is unavailable. Check database migrations and refresh.');
  assert.equal(get('#peopleTableWrap').hidden, true);
  assert.equal(get('#peoplePagination').hidden, true);
  assert.equal(get('#peoplePageInfo').textContent, 'Unavailable');
  assert.equal(get('#peoplePrev').disabled, true);
  assert.equal(get('#peopleNext').disabled, true);

  context.fetch = async () => ({ ok: true, status: 200, json: async () => ({
    ok: true, available: true,
    items: [{ name: 'Taylor', aliases: ['Tay', 'Taylor R.'], video_count: 3, has_bio: true, has_photo: false }], total: 1,
  }) });
  await loadPeople();
  assert.equal(get('#peopleRows').children.length, 1);
  assert.equal(get('#peopleRows').children[0].children[1].textContent, 'Tay, Taylor R.');
  assert.equal(get('#peopleTableWrap').hidden, false);

  get('#peopleQuery').value = 'Tay';
  let peopleSearchUrl = '';
  context.fetch = async url => {
    peopleSearchUrl = String(url);
    return { ok: true, status: 200, json: async () => ({
      ok: true, available: true,
      items: [{ name: 'Taylor', aliases: ['Tay'], video_count: 3, has_bio: true, has_photo: false }], total: 1,
    }) };
  };
  await loadPeople();
  assert.equal(new URL(peopleSearchUrl, 'http://admin.test').searchParams.get('q'), 'Tay',
    'the visible alias search is sent to the People API');
  assert.equal(get('#peopleRows').children[0].children[0].textContent, 'Taylor',
    'searching an alias renders its canonical person');

  context.fetch = async () => ({ ok: true, status: 200, json: async () => ({
    ok: true, available: true, items: {}, total: 0,
  }) });
  await loadPeople();
  assert.equal(get('#peopleFeedback').textContent, 'The people catalog response is incomplete.');
  assert.equal(get('#peopleState').textContent, 'People could not be loaded. Refresh to try again.');
  assert.equal(get('#peopleRows').children.length, 1,
    'a malformed refresh preserves the previous successful rows instead of leaving an empty visible table');
  assert.equal(get('#peopleTableWrap').hidden, false);

  // Switching to Review matches must discard a directory response that lands afterwards.
  const latePeople = deferred();
  context.fetch = () => latePeople.promise;
  const abandonedPeople = loadPeople();
  state.performersTab = 'review';
  get('#performerDirectoryPane').hidden = true;
  latePeople.resolve({ ok: true, status: 200, json: async () => ({
    ok: true, available: true,
    items: [{ name: 'Late arrival', aliases: [], video_count: 1, has_bio: false, has_photo: false }], total: 99,
  }) });
  await abandonedPeople;
  assert.doesNotMatch(get('#peopleFeedback').textContent, /99 people/,
    'a late directory response must not update feedback after leaving the Directory tab');
  assert.equal(get('#peopleRows').children[0].children[0].textContent, 'Taylor',
    'a late directory response must not replace the rendered people after leaving the Directory tab');

  state.route = 'library';
  state.libraryTab = 'quarantine';
  get('#libraryQuarantinePane').hidden = false;
  const quarantineA = deferred(), quarantineB = deferred();
  const quarantineResponses = [quarantineA, quarantineB];
  context.fetch = () => quarantineResponses.shift().promise;
  const loadQuarantine = run('loadQuarantine');
  const oldQuarantine = loadQuarantine();
  state.quarantineOffset = 50;
  const newQuarantine = loadQuarantine();
  quarantineB.resolve({ ok: true, status: 200, json: async () => ({ ok: true, available: true, items: [], total: 70 }) }); await newQuarantine;
  quarantineA.resolve({ ok: true, status: 200, json: async () => ({ ok: true, available: true, items: [], total: 10 }) }); await oldQuarantine;
  assert.equal(get('#quarantineFeedback').textContent, '70 active decisions');

  // Switching back to the Videos tab must discard a quarantine list that lands afterwards.
  const lateQuarantine = deferred();
  context.fetch = () => lateQuarantine.promise;
  const abandonedQuarantine = loadQuarantine();
  state.libraryTab = 'videos';
  get('#libraryQuarantinePane').hidden = true;
  lateQuarantine.resolve({ ok: true, status: 200, json: async () => ({ ok: true, available: true, items: [{ id: 'late', title: 'Late decision' }], total: 3 }) });
  await abandonedQuarantine;
  assert.doesNotMatch(get('#quarantineFeedback').textContent, /3 active decisions/,
    'a late quarantine result must not update feedback after leaving the Quarantine tab');
  assert.equal(get('#quarantineRows').children.length, 0,
    'a late quarantine result must not render rows after leaving the Quarantine tab');

  // A late restore preview cannot replace the more recent restore selection.
  state.route = 'library';
  const restoreA = deferred(), restoreB = deferred();
  const restoreResponses = [restoreA, restoreB];
  context.window.AdminV2.postJson = () => restoreResponses.shift().promise;
  const restore = run('confirmQuarantineUndo');
  const oldRestore = restore({ operation_id: 'restore-A' });
  const newRestore = restore({ operation_id: 'restore-B' });
  restoreB.resolve({ plan_id: 'restore-plan-B', operation_type: 'restore', items: [] }); await newRestore;
  restoreA.resolve({ plan_id: 'restore-plan-A', operation_type: 'restore', items: [] }); await oldRestore;
  assert.equal(state.operationRequest.operation_id, 'restore-B');
  assert.equal(state.operationPlan.plan_id, 'restore-plan-B');
  assert.equal(state.renderedPlan.plan_id, 'restore-plan-B');
  assert.equal(get('#operationDialog').open, true);

  // A person-merge preview cannot populate a different person's reopened dialog.
  context.loadPersonFilmography = async () => {};
  const openPersonDetails = run('openPersonDetails');
  const openPersonMergePreview = run('previewPersonMerge');
  await openPersonDetails('Taylor');
  get('#personMergeCanonical').value = 'Morgan';
  const personPreviewResponse = deferred();
  context.window.AdminV2.postJson = () => personPreviewResponse.promise;
  const stalePersonPreview = openPersonMergePreview('Taylor');
  get('#personDetailDialog').close();
  await openPersonDetails('Morgan');
  personPreviewResponse.resolve({ plan_id: 'stale-person-plan', items: [{ alias: 'Taylor', canonical: 'Morgan', alias_videos: 4, canonical_videos: 2 }] });
  await stalePersonPreview;
  assert.equal(state.pendingPersonMerge, null, "closing/reopening another person's dialog invalidates the old merge preview");
  assert.doesNotMatch(textOf(get('#personDetailContent')), /Review name merge|stale-person-plan/,
    "a late preview must not replace the next person's filmography dialog");

  // Exact-file duplicate batching retains a fresh server preview but queues it
  // directly; this matches the no-confirmation preference for duplicate decisions.
  state.route = 'library';
  state.libraryTab = 'duplicates';
  const duplicateCalls = [];
  context.loadDuplicateCandidates = options => { duplicateCalls.push(options); };
  context.window.AdminV2.postJson = async (url, payload) => {
    if (url.endsWith('/operations/preview')) return {
      plan_id: 'exact-batch-1', operation_type: 'hide_batch',
      items: [{ video_id: 'duplicate-1', title: 'Duplicate 1' }],
    };
    if (url.endsWith('/operations/execute')) return { jobs: [{ job_id: 'job-exact-1' }] };
    throw new Error(`Unexpected operation request ${url}`);
  };
  get('#operationDialog').open = false;
  const announcementsBeforeExactMerge = announcements.length;
  await run('previewAutoMergeExact')();
  assert.equal(get('#operationDialog').open, false, 'batch duplicate merge does not open a confirmation screen');
  assert.equal(state.operationPlan.plan_id, 'exact-batch-1', 'the current server preview is used for execution');
  assert.ok(duplicateCalls.some(options => options?.refresh === true), 'the duplicate list refreshes after queue acceptance');
  assert.match(announcements.slice(announcementsBeforeExactMerge).join(' '), /queued/i);

  const queuedMergeResponse = deferred();
  context.window.AdminV2.postJson = () => queuedMergeResponse.promise;
  await openPersonDetails('Taylor');
  state.pendingPersonMerge = { plan_id: 'plan-taylor', alias: 'Taylor', canonical: 'Morgan' };
  get('#personMergeUndoConfirm').checked = true;
  const queuePersonMerge = run('queuePersonMerge');
  const oldMergeExecution = queuePersonMerge();
  get('#personDetailDialog').close();
  await openPersonDetails('Morgan');
  queuedMergeResponse.resolve({ jobs: [{ job_id: 'merge-job-taylor' }] });
  await oldMergeExecution;
  assert.equal(get('#personDetailDialog').open, true,
    'completion of a submitted merge must not close a newer performer dialog');
  assert.match(announcements.at(-1), /merge-job-taylor/,
    'the operator is still notified when the submitted merge completes after closing its dialog');

  // Metadata editor responses stay attached to the editor generation and video.
  const metadataPreviewA = deferred();
  context.window.AdminV2.postJson = () => metadataPreviewA.promise;
  state.detailVideo = { id: 'video-A' };
  state.metadataDialogSeq = 1;
  get('#metadataDialog').open = true;
  const previewMetadataChanges = run('previewMetadataChanges');
  const oldMetadataPreview = previewMetadataChanges();
  get('#metadataDialog').close();
  state.detailVideo = { id: 'video-B' };
  state.metadataDialogSeq += 1;
  get('#metadataDialog').open = true;
  metadataPreviewA.resolve({ plan_id: 'metadata-plan-A', fields: { title: { before: 'A', after: 'Edited A' } } });
  await oldMetadataPreview;
  assert.equal(state.metadataPlanId, null, 'a stale metadata preview must not install its plan into a reopened editor');
  assert.equal(state.renderedMetadataPreview, undefined, 'a stale metadata preview must not replace the new editor');

  const metadataExecuteA = deferred();
  context.window.AdminV2.postJson = () => metadataExecuteA.promise;
  state.metadataPlanId = 'metadata-plan-A';
  state.metadataDialogSeq += 1;
  const queueMetadataUpdate = run('queueMetadataUpdate');
  const oldMetadataExecution = queueMetadataUpdate();
  get('#metadataDialog').close();
  state.detailVideo = { id: 'video-C' };
  state.metadataDialogSeq += 1;
  get('#metadataDialog').open = true;
  metadataExecuteA.resolve({ jobs: [{ job_id: 'metadata-job-A' }] });
  await oldMetadataExecution;
  assert.equal(get('#metadataDialog').open, true, 'a stale metadata execution must not close the currently open editor');
  assert.doesNotMatch(announcements.at(-1), /metadata-job-A/, 'a metadata receipt must not overwrite the reopened editor after navigation');

  // Bulk metadata preview is bound to its selection and queues only the reviewed selection.
  state.route = 'library';
  state.selectedVideos = new Map([['bulk-A', 'Video A'], ['bulk-B', 'Video B']]);
  context.previewBulkMetadataChanges = () => {};
  context.queueBulkMetadataUpdate = () => {};
  const openBulkMetadataEditor = runSync('openBulkMetadataEditor');
  openBulkMetadataEditor();
  assert.equal(state.metadataBulkVideoIds.length, 2);
  const bulkSections = get('#metadataDialogContent').children.filter(node => node.children?.some(child => child.dataset?.bulkMetadataValue));
  assert.equal(bulkSections.length, 6, 'the bulk editor exposes all six supported metadata fields');
  for (const section of bulkSections) {
    const inputLabel = section.children[0];
    const input = section.children[2];
    assert.equal(inputLabel.htmlFor, input.id, 'each dynamic bulk field has a connected label');
  }
  state.metadataPlanId = null;
  const bulkFields = new Map();
  for (const field of ['title', 'plot', 'studio', 'performers', 'tags', 'date_released']) {
    bulkFields.set(`[data-bulk-metadata-override="${field}"]`, { checked: field === 'title' });
    bulkFields.set(`[data-bulk-metadata-value="${field}"]`, { value: field === 'title' ? '' : '' });
    bulkFields.set(`[data-bulk-metadata-reset="${field}"]`, { checked: field === 'tags' });
  }
  const originalSelector = context.$;
  context.$ = selector => bulkFields.get(selector) || get(selector);
  const bulkMetadataPayloadFromEditor = runSync('bulkMetadataPayloadFromEditor');
  const bulkPayload = bulkMetadataPayloadFromEditor();
  assert.deepEqual({ ...bulkPayload.changes }, { title: '' }, 'an explicitly selected empty value remains a clearing override');
  assert.deepEqual(Array.from(bulkPayload.reset_fields), ['tags'], 'reset is distinct from setting an empty value');
  context.$ = originalSelector;
  for (const field of ['title', 'plot', 'studio', 'performers', 'tags', 'date_released']) {
    get(`[data-bulk-metadata-override="${field}"]`).checked = field === 'title';
    get(`[data-bulk-metadata-value="${field}"]`).value = '';
    get(`[data-bulk-metadata-reset="${field}"]`).checked = field === 'tags';
  }
  const bulkPreviewA = deferred();
  context.window.AdminV2.postJson = (_path, body) => {
    assert.equal(body.type, 'metadata_update_batch');
    assert.deepEqual(Array.from(body.video_ids), ['bulk-A', 'bulk-B']);
    return bulkPreviewA.promise;
  };
  const previewBulkMetadataChanges = run('previewBulkMetadataChanges');
  const staleBulkPreview = previewBulkMetadataChanges();
  state.selectedVideos = new Map([['bulk-B', 'Video B'], ['bulk-C', 'Video C']]);
  bulkPreviewA.resolve({ plan_id: 'bulk-stale-plan', items: [] });
  await staleBulkPreview;
  assert.equal(state.metadataPlanId, null, 'bulk preview must be discarded if the selected videos change while it loads');
  assert.equal(get('#metadataDialog').open, false, 'stale selection closes the editor instead of leaving a disabled form open');

  state.selectedVideos = new Map([['bulk-A', 'Video A'], ['bulk-B', 'Video B']]);
  state.metadataBulkVideoIds = ['bulk-A', 'bulk-B'];
  state.metadataDialogSeq += 1;
  get('#metadataDialog').open = true;
  const bulkPreviewB = deferred();
  context.window.AdminV2.postJson = () => bulkPreviewB.promise;
  const currentBulkPreview = previewBulkMetadataChanges();
  bulkPreviewB.resolve({ plan_id: 'bulk-current-plan', items: [{ video_id: 'bulk-A' }, { video_id: 'bulk-B' }] });
  await currentBulkPreview;
  assert.equal(state.metadataPlanId, 'bulk-current-plan');

  state.selectedVideos = new Map([['bulk-C', 'Video C']]);
  context.window.AdminV2.postJson = () => { throw new Error('A changed bulk selection must not be submitted.'); };
  const queueBulkMetadataUpdate = run('queueBulkMetadataUpdate');
  await queueBulkMetadataUpdate();
  assert.equal(get('#metadataDialog').open, false, 'a preview cannot be executed after the selected videos change');
  assert.match(announcements.at(-1), /selection changed after preview/);

  state.selectedVideos = new Map([['bulk-A', 'Video A'], ['bulk-B', 'Video B']]);
  state.metadataBulkVideoIds = ['bulk-A', 'bulk-B'];
  state.metadataDialogSeq += 1;
  get('#metadataDialog').open = true;
  state.metadataPlanId = 'bulk-current-plan';
  const bulkExecute = deferred();
  context.window.AdminV2.postJson = (_path, body) => {
    assert.equal(body.plan_id, 'bulk-current-plan');
    return bulkExecute.promise;
  };
  const queuedBulk = queueBulkMetadataUpdate();
  bulkExecute.resolve({ jobs: [{ job_id: 'bulk-job-A' }, { job_id: 'bulk-job-B' }] });
  await queuedBulk;
  assert.equal(get('#metadataDialog').open, false, 'accepted bulk queue closes the reviewed editor');
  assert.equal(state.selectedVideos.size, 0, 'only the reviewed bulk selection is cleared after queue acceptance');
  assert.match(announcements.at(-1), /2 videos/);

  process.stdout.write('PASS: operation, duplicate, and availability-history views ignore stale responses\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
