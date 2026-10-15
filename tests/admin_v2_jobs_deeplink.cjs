const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let focusedElement = null;

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.attributes = {}; this.hidden = false; this.disabled = false; this.textContent = '';
    this.className = ''; this.value = ''; this.isConnected = true; this.type = '';
    this.parentElement = null;
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...items) { for (const item of items) { item.parentElement = this; item.isConnected = this.isConnected; this.children.push(item); } }
  replaceChildren(...items) { for (const item of this.children) item.isConnected = false; this.children = []; this.append(...items); }
  after(item) { const siblings = this.parentElement?.children || []; const index = siblings.indexOf(this); if (index >= 0) { item.parentElement = this.parentElement; item.isConnected = this.isConnected; siblings.splice(index + 1, 0, item); } }
  remove() { const siblings = this.parentElement?.children || []; const index = siblings.indexOf(this); if (index >= 0) siblings.splice(index, 1); this.isConnected = false; }
  get nextElementSibling() { const siblings = this.parentElement?.children || []; return siblings[siblings.indexOf(this) + 1] || null; }
  get classList() { return { contains: name => this.className.split(/\s+/).includes(name) }; }
  closest(selector) { if (selector === '[data-operation-id]' && this.dataset.operationId) return this; if (selector === 'tr' && this.tagName === 'tr') return this; return this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) { return walk(this).filter(item => selector === '[data-operation-id]' ? item.dataset.operationId : false); }
  focus() { focusedElement = this; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  removeAttribute(name) { delete this.attributes[name]; }
  get firstElementChild() { return this.children[0] || null; }
}

function walk(root, output = []) {
  output.push(root);
  for (const child of root.children || []) walk(child, output);
  return output;
}

(async () => {
  const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
  for (const tab of ['tasks', 'bulk', 'schedules']) {
    assert.match(template, new RegExp(`href="#jobs\\?tab=${tab}"`), `${tab} must have a bookmarkable navigation target`);
    assert.match(template, new RegExp(`data-jobs-pane="${tab}"`), `${tab} must have a content pane`);
  }
  const ids = [
    'jobsView', 'crumbCurrent', 'operationalEyebrow', 'operationalTitle', 'operationalIntro', 'jobsFilters',
    'jobsClearFilters', 'jobsRefresh', 'jobsPrev', 'jobsNext', 'jobsRows', 'operationHistoryRefresh',
    'operationHistoryPrev', 'operationHistoryNext', 'operationHistoryRows', 'jobsNotice', 'jobsFeedback',
    'jobsState', 'jobsTableWrap', 'jobsPagination', 'jobsPageInfo', 'operationHistoryFeedback',
    'operationHistoryState', 'operationHistoryTableWrap', 'operationHistoryPagination', 'operationHistoryPageInfo', 'jobQueueFilter',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'jobsFilters' ? 'form' : 'div')]));
  const jobsTabs = ['tasks', 'bulk', 'schedules'].map(tab => { const item = new Element('a'); item.dataset.jobsTab = tab; return item; });
  const jobsPanes = ['tasks', 'bulk', 'schedules'].map(tab => { const item = new Element('section'); item.dataset.jobsPane = tab; return item; });
  const filterValues = { queue: 'all', state: '', action: '' };
  elements.jobsFilters.elements = { namedItem: name => ({ get value() { return filterValues[name]; }, set value(value) { filterValues[name] = value; } }) };
  elements.jobsFilters.reset = () => { filterValues.queue = 'all'; filterValues.state = ''; filterValues.action = ''; };
  const document = {
    hidden: false,
    querySelector(selector) { return selector.startsWith('#') ? elements[selector.slice(1)] || null : null; },
    querySelectorAll(selector) {
      if (selector === '[data-job-detail]') return walk(elements.jobsRows).filter(item => item.dataset.jobDetail);
      if (selector === '[data-jobs-tab]') return jobsTabs;
      if (selector === '[data-jobs-pane]') return jobsPanes;
      return [];
    },
    getElementById(id) { return walk(elements.jobsRows).find(item => item.id === id) || null; },
    createElement(tag) { return new Element(tag); },
    addEventListener(type, callback) { documentListeners.set(type, callback); },
  };
  const documentListeners = new Map();
  const location = { hash: '#jobs', search: '?job_id=target-7', href: 'http://localhost/admin/v2/?job_id=target-7#jobs' };
  const windowListeners = new Map();
  let historyUrls = [location.href];
  let historyIndex = 0;
  let scheduledPollCallback = null;
  let timeoutSerial = 0;
  const setLocation = value => {
    const url = new URL(String(value), location.href);
    location.href = url.href; location.search = url.search; location.hash = url.hash;
  };
  const resetHistory = value => { setLocation(value); historyUrls = [location.href]; historyIndex = 0; };
  const history = {
    replaceState(_state, _title, value) {
      const url = new URL(String(value), location.href);
      historyUrls[historyIndex] = url.href; setLocation(url);
    },
    pushState(_state, _title, value) {
      const url = new URL(String(value), location.href);
      historyUrls = historyUrls.slice(0, historyIndex + 1);
      historyUrls.push(url.href); historyIndex += 1; setLocation(url);
    },
    back() {
      if (historyIndex <= 0) return;
      historyIndex -= 1; setLocation(historyUrls[historyIndex]); windowListeners.get('popstate')?.({ type: 'popstate' });
    },
    forward() {
      if (historyIndex + 1 >= historyUrls.length) return;
      historyIndex += 1; setLocation(historyUrls[historyIndex]); windowListeners.get('popstate')?.({ type: 'popstate' });
    },
  };
  const calls = [];
  const target = {
    id: 'target-7', queue: 'jobs', action: 'performer_refresh', state: 'processing', stage: 'writing_nfo',
    activity_stale: true,
    progress: { stage: 'writing_nfo', completed: 1, total: 1 },
    created_at: 1791367200, updated_at: Math.floor(Date.now() / 1000) - 180,
    events: [{ stage: 'writing_nfo', completed: 1, total: 1 }]
  };
  const metadataTask = {
    id: 'metadata-8', queue: 'jobs', action: 'lukayan_fingerprint', state: 'processing', stage: 'metadata_tpdb',
    activity_stale: false,
    progress: { stage: 'metadata_tpdb', completed: 2, total: 5 },
    created_at: 1791367200, updated_at: Math.floor(Date.now() / 1000),
    events: [{ stage: 'metadata_tpdb', completed: 2, total: 5 }]
  };
  const availabilityTask = {
    id: 'availability-9', queue: 'jobs', action: 'availability_check', state: 'done',
    result_summary: 'Playback check: 3 available, 1 unavailable, 2 temporary error, 4 inconclusive, 1 skipped.'
  };
  const phashTask = {
    queue: 'phash', id: 'p-hash-1', action: 'phash_generation', state: 'processing',
    title: 'Fixture clip', video_id: 'video-phash-1', attempts: 1,
    processing_time_ms: 45000, thumbnail_ready: false, error_code: null,
    created_at: '2026-10-14T10:00:00Z', updated_at: '2026-10-14T10:01:00Z',
  };
  const writes = [];
  let targetDetailCalls = 0;
  const operationDetailCalls = { 'plan-1': 0, 'plan-2': 0 };
  let releaseOperationDetail;
  let releaseFirstTargetDetail;
  let otherDetailCalls = 0;
  let releaseFirstOtherDetail;
  async function fetch(url) {
    calls.push(url);
    let body;
    if (url.endsWith('/capabilities')) body = { actions_enabled: true };
    else if (url.includes('/jobs?state=active')) body = { jobs: [], data: { available: true } };
    else if (url.includes('/tasks?queue=phash')) body = { tasks: [phashTask], total: 1, active_total: 1, workers: { phash: { available: true, worker_version: 'fixture', age_seconds: 2 } } };
    else if (url.includes('/tasks?')) {
      const tasks = [
        { id: 'other', queue: 'jobs', action: 'sync', state: 'done' },
        { id: 'failed-3', queue: 'jobs', action: 'sync', state: 'failed', error: 'A required operation timed out.' },
        { id: metadataTask.id, queue: 'jobs', action: metadataTask.action, state: metadataTask.state,
          stage: metadataTask.stage, progress: metadataTask.progress },
        availabilityTask,
      ];
      if (new URL(url, 'http://localhost').searchParams.get('action') !== 'sync') tasks.push(phashTask);
      body = { tasks, total: 71, workers: { phash: { available: true } } };
    }
    else if (url.endsWith('/tasks/phash/p-hash-1')) body = { task: phashTask };
    else if (url.endsWith('/tasks/jobs/failed-3')) body = { task: {
      id: 'failed-3', queue: 'jobs', action: 'sync', state: 'failed', source_id: 'telegram:-100123',
      error: 'A required operation timed out.',
    } };
    else if (url.endsWith('/tasks/jobs/metadata-8')) body = { task: metadataTask };
    else if (url.endsWith('/tasks/jobs/availability-9')) body = { task: availabilityTask };
    else if (url.endsWith('/tasks/jobs/target-7')) {
      targetDetailCalls += 1;
      if (targetDetailCalls === 1) {
        return new Promise(resolve => { releaseFirstTargetDetail = () => resolve({
          ok: true, status: 200, json: async () => ({ task: target }),
        }); });
      }
      body = { task: target };
    }
    else if (url.endsWith('/tasks/jobs/other')) {
      otherDetailCalls += 1;
      if (otherDetailCalls === 1) {
        return new Promise(resolve => { releaseFirstOtherDetail = () => resolve({
          ok: true, status: 200, json: async () => ({ task: { id: 'other', queue: 'jobs', action: 'sync', state: 'done', result_summary: 'Outdated result' } }),
        }); });
      }
      body = { task: { id: 'other', queue: 'jobs', action: 'sync', state: 'done', result_summary: 'Fresh result' } };
    }
    else if (url.includes('/operations?')) body = { items: [
      { plan_id: 'plan-1', operation_type: 'metadata_update_batch', state: 'previewed', item_count: 1 },
      { plan_id: 'plan-2', operation_type: 'metadata_update_batch', state: 'previewed', item_count: 1 },
    ], total: 2 };
    else if (url.endsWith('/operations/plan-1') || url.endsWith('/operations/plan-2')) {
      const planId = url.endsWith('/plan-1') ? 'plan-1' : 'plan-2';
      operationDetailCalls[planId] += 1;
      if (planId === 'plan-1' && operationDetailCalls[planId] <= 2) return new Promise(resolve => { releaseOperationDetail = () => resolve({
        ok: true, status: 200, json: async () => ({ operation: { items: [{ video_id: 'video-1', title: 'Test video' }], jobs: [] } }),
      }); });
      body = { operation: { items: [{ video_id: planId, title: `Test ${planId}` }], jobs: [] } };
    }
    else throw new Error(`Unexpected request ${url}`);
    return { ok: true, status: 200, json: async () => body };
  }
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/jobs.js', 'utf8'), {
    document, location, history, fetch, window: { confirm: () => true, AdminV2: {
      getJson: async (url, signal) => (await fetch(url, { signal })).json(),
      postJson: async url => { writes.push(url); return { ok: true, state: 'cancelled' }; },
    } }, URL, URLSearchParams,
    AbortController, setTimeout: () => 1, clearTimeout() {}, addEventListener() {},
  });
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(targetDetailCalls, 1, 'initial deep link reaches its delayed detail lookup');
  elements.jobsRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.ok(targetDetailCalls >= 2,
    'a refresh that supersedes deep-link loading must retry the linked task lookup');
  releaseFirstTargetDetail();
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(location.search, '', 'the one-shot task ID is removed after it is consumed');
  assert(calls.some(url => url.endsWith('/tasks/jobs/target-7')), 'Tasks must fetch the linked task even when it is not in the current page');
  const detailsButton = document.querySelectorAll('[data-job-detail]').find(button => button.dataset.jobDetail === 'target-7');
  assert.ok(detailsButton, 'the linked task must be included in the visible task page');
  assert.equal(detailsButton.getAttribute('aria-expanded'), 'true', 'the linked task details open automatically');
  assert.equal(detailsButton.type, 'button', 'task details must use a keyboard-activatable button');
  const actionLabels = walk(elements.jobsRows).filter(item => item.className === 'job-action').map(item => item.textContent);
  assert(actionLabels.includes('Telegram scene lookup'), 'legacy Telegram actions must use a readable generic label');
  assert(!actionLabels.some(label => label.includes('lukayan_fingerprint')),
    'the channel-specific implementation name must not leak into Admin V2');
  assert.equal(detailsButton.getAttribute('aria-controls'), 'job-detail-1');
  const detailRow = document.getElementById(detailsButton.getAttribute('aria-controls'));
  assert.equal(detailRow.hidden, false);
  const detailText = walk(detailRow).map(item => item.textContent).join(' ');
  assert.match(detailText, /Writing performer metadata files/);
  assert.match(detailText, /1 of 1 files handled/);
  assert.match(detailText, /No worker activity has been recorded for more than 90 seconds/,
    'processing tasks with a stale heartbeat must show an operator warning');
  assert.equal(walk(detailRow).some(item => item.dataset.jobRetry), false,
    'a stale warning must not automatically expose retry for a processing job');

  const metadataButton = document.querySelectorAll('[data-job-detail]').find(button => button.dataset.jobDetail === 'metadata-8');
  assert.ok(metadataButton, 'the metadata scan task must appear in the Jobs list');
  elements.jobsRows.listeners.click({
    target: { closest: selector => selector === '[data-job-detail]' ? metadataButton : null },
  });
  await new Promise(resolve => setTimeout(resolve, 0));
  const metadataRow = document.getElementById(metadataButton.getAttribute('aria-controls'));
  const metadataText = walk(metadataRow).map(item => item.textContent).join(' ');
  assert.match(metadataText, /Enriching actor and video metadata from ThePornDB/);
  assert.match(metadataText, /2 of 5 provider steps completed/);
  assert.doesNotMatch(metadataText, /files handled/,
    'provider pass counts must not be presented as file counts');

  const availabilityButton = document.querySelectorAll('[data-job-detail]').find(button => button.dataset.jobDetail === 'availability-9');
  assert.ok(availabilityButton, 'playback check tasks must appear in the Jobs list');
  const availabilityRow = availabilityButton.closest('tr');
  assert.match(walk(availabilityRow.children[4]).map(item => item.textContent).join(' '),
    /3 available, 1 unavailable, 2 temporary error, 4 inconclusive, 1 skipped/,
    'the result counts must be visible before opening task details');
  elements.jobsRows.listeners.click({
    target: { closest: selector => selector === '[data-job-detail]' ? availabilityButton : null },
  });
  await new Promise(resolve => setTimeout(resolve, 0));
  const availabilityDetail = document.getElementById(availabilityButton.getAttribute('aria-controls'));
  assert.match(walk(availabilityDetail).map(item => item.textContent).join(' '),
    /Playback check: 3 available, 1 unavailable, 2 temporary error, 4 inconclusive, 1 skipped/,
    'the result counts must also be included in task details');

  const failedButton = document.querySelectorAll('[data-job-detail]').find(button => button.dataset.jobDetail === 'failed-3');
  assert.ok(failedButton, 'failed tasks must remain available for diagnosis');
  assert.match(walk(failedButton.closest('tr').children[4]).map(item => item.textContent).join(' '), /A required operation timed out\./,
    'safe failure reasons must appear in the Jobs list before opening task details');
  elements.jobsRows.listeners.click({
    target: { closest: selector => selector === '[data-job-detail]' ? failedButton : null },
  });
  await new Promise(resolve => setTimeout(resolve, 0));
  const failedRow = document.getElementById(failedButton.getAttribute('aria-controls'));
  const failedText = walk(failedRow).map(item => item.textContent).join(' ');
  assert.match(failedText, /Failure/);
  assert.match(failedText, /A required operation timed out\./,
    'recognized safe failure categories must be visible in the task details');
  const sourceHistoryLink = walk(failedRow).find(item => item.tagName === 'a');
  assert.equal(sourceHistoryLink.href, '/admin/v2/?source_id=telegram%3A-100123#sources',
    'a sync task with a configured source ID links to its filtered source history');

  // Native buttons activate on Enter and Space. Feed that activation through
  // the real delegated Jobs click handler and verify the rendered disclosure
  // state while focus stays on the control.
  const activateFromKeyboard = key => {
    assert(['Enter', ' '].includes(key));
    detailsButton.focus();
    assert.equal(focusedElement, detailsButton, `${key} starts from the focused Details button`);
    elements.jobsRows.listeners.click({
      target: { closest: selector => selector === '[data-job-detail]' ? detailsButton : null },
    });
  };
  activateFromKeyboard(' ');
  assert.equal(detailsButton.getAttribute('aria-expanded'), 'false', 'Space collapses the focused task details');
  assert.equal(detailRow.hidden, true);
  assert.equal(focusedElement, detailsButton, 'collapsing details retains keyboard focus');
  activateFromKeyboard('Enter');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(detailsButton.getAttribute('aria-expanded'), 'true', 'Enter opens the focused task details');
  assert.equal(detailRow.hidden, false);
  assert.equal(focusedElement, detailsButton, 'opening details retains keyboard focus');

  const otherButton = document.querySelectorAll('[data-job-detail]').find(button => button.dataset.jobDetail === 'other');
  const activateDetail = () => elements.jobsRows.listeners.click({
    target: { closest: selector => selector === '[data-job-detail]' ? otherButton : null },
  });
  activateDetail();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(otherDetailCalls, 1, 'the first task detail request is pending');
  activateDetail(); // Close the detail while its request is pending.
  activateDetail(); // Reopen it, starting a newer request.
  await new Promise(resolve => setTimeout(resolve, 0));
  const otherRow = document.getElementById(otherButton.getAttribute('aria-controls'));
  assert.match(walk(otherRow).map(item => item.textContent).join(' '), /Fresh result/);
  releaseFirstOtherDetail();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.match(walk(otherRow).map(item => item.textContent).join(' '), /Fresh result/,
    'a late response from the closed detail request cannot overwrite the newer result');

  elements.operationHistoryRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  let operationButtons = elements.operationHistoryRows.querySelectorAll('[data-operation-id]');
  const oldOperationButton = operationButtons.find(button => button.dataset.operationId === 'plan-1');
  const secondOperationButton = operationButtons.find(button => button.dataset.operationId === 'plan-2');
  elements.operationHistoryRows.listeners.click({ target: oldOperationButton });
  assert.equal(operationDetailCalls['plan-1'], 1, 'the first operation detail request is pending');
  elements.operationHistoryRows.listeners.click({ target: secondOperationButton });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(secondOperationButton.getAttribute('aria-expanded'), 'true', 'the newer operation detail can load while the earlier request is pending');
  releaseOperationDetail();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(oldOperationButton.disabled, false);
  assert.equal(oldOperationButton.textContent, 'Details', 'a superseded detail request restores its button label');

  // Start another delayed detail, replace the operation rows, then prove its
  // response cannot mark the detached operation as open for the next refresh.
  elements.operationHistoryRows.listeners.click({ target: oldOperationButton });
  elements.operationHistoryRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const currentPlanTwo = elements.operationHistoryRows.querySelectorAll('[data-operation-id]').find(button => button.dataset.operationId === 'plan-2');
  assert.equal(operationDetailCalls['plan-2'], 2, 'the previously open operation is restored after row replacement');
  releaseOperationDetail();
  await new Promise(resolve => setTimeout(resolve, 0));
  elements.operationHistoryRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(operationDetailCalls['plan-1'], 2,
    'a detail response for a replaced operation row cannot mark that operation open and trigger a later stale auto-open');
  assert.equal(operationDetailCalls['plan-2'], 3, 'the current open operation remains the one restored on refresh');
  assert.equal(currentPlanTwo.closest('tr').isConnected, false, 'the replaced operation row is detached');

  location.search = '?job_state=queued&job_action=sync&home_panel=issues&issue_type=job_stalled';
  resetHistory(`http://localhost/admin/v2/${location.search}#jobs`);
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/jobs.js', 'utf8'), {
    document, location, history, fetch, window: { confirm: () => true, AdminV2: {
      getJson: async (url, signal) => (await fetch(url, { signal })).json(),
      postJson: async url => { writes.push(url); return { ok: true, state: 'cancelled' }; },
    } }, URL, URLSearchParams,
    AbortController,
    setTimeout: (callback, delay) => {
      if (delay === 0) callback();
      else scheduledPollCallback = callback;
      return ++timeoutSerial;
    },
    clearTimeout() { scheduledPollCallback = null; },
    addEventListener(type, callback) { windowListeners.set(type, callback); },
  });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(jobsPanes[0].hidden, false, 'the default Jobs route opens Tasks');
  assert.equal(jobsPanes[1].hidden, true);
  assert.equal(jobsTabs[0].attributes['aria-current'], 'page', 'the active Jobs tab is announced');
  const taskListRequestsBeforeBulk = calls.filter(url => url.includes('/tasks?queue=')).length;
  location.hash = '#jobs?tab=bulk';
  documentListeners.get('adminv2:navigate')({ detail: { route: 'jobs' } });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(jobsPanes[0].hidden, true, 'Bulk Operations hides the Tasks pane');
  assert.equal(jobsPanes[1].hidden, false, 'Bulk Operations shows its own pane');
  assert.equal(jobsTabs[1].attributes['aria-current'], 'page');
  assert(calls.some(url => url.includes('/operations?')), 'Bulk Operations loads operation history on entry');
  assert.equal(calls.filter(url => url.includes('/tasks?queue=')).length, taskListRequestsBeforeBulk,
    'entering Bulk Operations does not refetch the task queue');
  location.hash = '#jobs?tab=schedules';
  documentListeners.get('adminv2:navigate')({ detail: { route: 'jobs' } });
  assert.equal(jobsPanes[2].hidden, false, 'Schedules has its own bookmarkable pane');
  location.hash = '#jobs?tab=tasks';
  documentListeners.get('adminv2:navigate')({ detail: { route: 'jobs' } });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert(calls.some(url => url.includes('/tasks?queue=all&state=queued&action=sync&offset=0')),
    'a Home backlog link must apply the queued sync-job filters');
  filterValues.state = 'done';
  elements.jobsFilters.listeners.change();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(new URLSearchParams(location.search).get('job_state'), 'done', 'changing Jobs filters updates the URL');
  assert.equal(new URLSearchParams(location.search).get('home_panel'), 'issues', 'Jobs filter updates preserve unrelated Home URL state');
  assert.equal(new URLSearchParams(location.search).get('issue_type'), 'job_stalled', 'Jobs filter updates preserve unrelated issue deep links');
  assert.equal(historyUrls.length, 2, 'a deliberate Jobs filter change creates a history entry');
  assert(calls.some(url => url.includes('/tasks?queue=all&state=done&action=sync&offset=0')),
    'the changed Jobs filters request the matching task set');
  history.back();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(filterValues.state, 'queued', 'Back restores the previous Jobs filter controls');
  assert.equal(filterValues.action, 'sync');
  assert(calls.some(url => url.includes('/tasks?queue=all&state=queued&action=sync&offset=0')),
    'Back reloads the catalog for the restored Jobs filters');
  history.forward();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(filterValues.state, 'done', 'Forward restores the next Jobs filter state');
  elements.jobsNext.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(new URLSearchParams(location.search).get('job_offset'), '4', 'Jobs page navigation updates its URL');
  assert(calls.some(url => url.includes('/tasks?queue=all&state=done&action=sync&offset=4')),
    'the next Jobs page requests the matching offset');
  history.back();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(new URLSearchParams(location.search).get('job_offset'), null, 'Back restores the prior Jobs page');
  assert.equal(filterValues.state, 'done', 'page history retains the active filter');
  history.forward();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(new URLSearchParams(location.search).get('job_offset'), '4', 'Forward restores the next Jobs page');
  elements.jobsClearFilters.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(new URLSearchParams(location.search).get('job_state'), null, 'Clear filters removes Jobs filter state');
  assert.equal(new URLSearchParams(location.search).get('job_offset'), null, 'Clear filters removes Jobs pagination state');
  assert.equal(new URLSearchParams(location.search).get('home_panel'), 'issues', 'Clear filters preserves unrelated Home URL state');
  assert.equal(new URLSearchParams(location.search).get('issue_type'), 'job_stalled', 'Clear filters preserves unrelated issue deep links');
  history.back();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(filterValues.state, 'done', 'Back from Clear filters restores the previous Jobs controls');
  assert.equal(new URLSearchParams(location.search).get('job_offset'), '4', 'Back from Clear filters restores the previous Jobs page');
  const scheduledPoll = scheduledPollCallback;
  assert.equal(typeof scheduledPoll, 'function', 'Jobs schedules a background refresh while the view is active');
  const historyLengthBeforePoll = historyUrls.length;
  const urlBeforePoll = location.href;
  scheduledPollCallback = null;
  scheduledPoll();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(historyUrls.length, historyLengthBeforePoll, 'background polling does not add a history entry');
  assert.equal(location.href, urlBeforePoll, 'background polling preserves the current Jobs URL');
  filterValues.queue = 'phash';
  filterValues.state = 'active';
  filterValues.action = '';
  elements.jobsFilters.listeners.change();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert(calls.some(url => url.includes('/tasks?queue=phash')),
    'pHash jobs are selected through the shared Tasks queue filter');
  const phashCancel = walk(elements.jobsRows)
    .find(button => button.dataset.jobQueue === 'phash' && button.dataset.jobCancel === phashTask.id);
  assert.ok(phashCancel, `processing pHash tasks expose the queue-supported cancel action (${calls.filter(url => url.includes('/tasks?')).slice(-3).join(' | ')})`);
  const phashDetails = document.querySelectorAll('[data-job-detail]')
    .find(button => button.dataset.jobQueue === 'phash' && button.dataset.jobDetail === phashTask.id);
  elements.jobsRows.listeners.click({ target: { closest: selector => selector === '[data-job-detail]' ? phashDetails : null } });
  await new Promise(resolve => setTimeout(resolve, 0));
  const phashDetailRow = document.getElementById(phashDetails.getAttribute('aria-controls'));
  assert.match(walk(phashDetailRow).map(item => item.textContent).join(' '), /pHash task details/);
  assert.match(walk(phashDetailRow).map(item => item.textContent).join(' '), /Open video/);
  elements.jobsRows.listeners.click({ target: { closest: selector => selector === '[data-job-cancel]' ? phashCancel : null } });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert(writes.includes('/admin/api/v2/phash-jobs/p-hash-1/cancel'),
    'pHash task cancellation uses the existing pHash action endpoint');
  process.stdout.write('PASS: Jobs tab routing, task deep links, operation-history stale-detail races, off-page details, and filter/page Back/Forward history\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
