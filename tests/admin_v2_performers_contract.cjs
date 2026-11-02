const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { createAdminV2Helpers } = require('./helpers/admin_v2_shared_helpers.cjs');

let document;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.hidden = false; this.disabled = false; this.checked = false; this.value = '';
    this.textContent = ''; this.className = ''; this.attributes = {}; this.parentNode = null;
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  add(node) { this.append(node); }
  replaceChildren(...nodes) {
    for (const child of this.children) {
      if (child.contains?.(document.activeElement)) document.activeElement = document.body;
      child.parentNode = null;
    }
    this.children = [];
    this.append(...nodes);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { this.focused = true; document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
  contains(node) { return this === node || this.children.some(child => child.contains?.(node)); }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim());
    const matches = element => selectors.some(value => {
      if (value === '.performer-proposal') return String(element.className || '').split(/\s+/).includes('performer-proposal');
      if (!element.tagName) return false;
      const tag = value.split(/[:[]/, 1)[0].toLowerCase();
      if (element.tagName.toLowerCase() !== tag) return false;
      return !value.includes(':not(:disabled)') || !element.disabled;
    });
    const result = [];
    const visit = element => {
      for (const child of element.children || []) {
        if (matches(child)) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) {
    const result = find(child, predicate);
    if (result) return result;
  }
  return null;
}
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }

(async () => {
  const ids = ['taskReceipt', 'performerReviewPane', 'performerNameVariantsPane', 'performersFeedback', 'performerProposalRows', 'performersCount', 'performersState', 'performersRefresh', 'performersReconcile', 'performerProposalPagination',
    'fuzzyCategory', 'fuzzyRows', 'fuzzyPagination', 'fuzzyPageInfo', 'fuzzyPrev', 'fuzzyNext', 'fuzzyFeedback',
    'fuzzyThreshold', 'fuzzyScan', 'fuzzyReviewSelected', 'fuzzyMergeDialog', 'fuzzyMergeContent', 'fuzzyMergeActions', 'fuzzyMergeClose'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'fuzzyMergeDialog' ? 'dialog' : 'div')]));
  elements.fuzzyThreshold.value = '0.88';
  const calls = [];
  const catalogSelects = [];
  const proposals = Array.from({ length: 51 }, (_, index) => ({
    key: `group-${index + 1}`,
    reason: 'uncatalogued_credit',
    candidate: `Proposal ${index + 1}`,
    proposals: 1,
    videos: 1,
    matched_text: `Text ${index + 1}`,
    example_title: `Film ${index + 1}`,
  }));
  let failReview = false;
  let deferReview = false, rejectDeferredReview;
  let navigateListener;
  document = {
    body: new Element('body'), activeElement: null,
    querySelector: selector => elements[selector.slice(1)],
    querySelectorAll: selector => selector === '[data-performer-catalog]' ? catalogSelects : [],
    createElement: tag => { const element = new Element(tag); if (tag === 'select') catalogSelects.push(element); return element; },
    addEventListener(name, callback) { if (name === 'adminv2:navigate') navigateListener = callback; },
  };
  const window = {
    AdminV2: {
      ...createAdminV2Helpers(document),
      async getJson(path) {
        calls.push({ method: 'GET', path });
        if (path.includes('/proposal-groups?') || path.includes('/proposals?')) {
          const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset') || 0);
          return { items: proposals.slice(offset, offset + 50), total: proposals.length, pending: proposals.length, offset, limit: 50 };
        }
        if (path.includes('/catalog?')) return { items: [] };
        return { ok: true, stats: { total_unique_performers: 4, total_groups: 2 }, groups: [{
          category: 'diacritics', category_label: 'Diacritics', confidence: 0.95,
          canonical: 'Álpha', canonical_video_count: 3, canonical_has_photo: true,
          canonical_video_examples: [{ title: 'Canonical film', provider: 'telegram', studio: 'One' }],
          variants: [
            { name: 'Alpha', video_count: 2, has_bio: true, video_examples: [{ title: 'Variant film' }] },
            { name: 'Alphaa', video_count: 1, video_examples: [{ title: 'Overlapping variant' }] },
          ],
        }, {
          category: 'similarity', category_label: 'Similarity / typo', confidence: 0.88,
          canonical: 'Jamie North', canonical_video_count: 4,
          canonical_video_examples: [{ title: 'Jamie film' }],
          variants: [{ name: 'Jamie Nort', video_count: 1, video_examples: [{ title: 'Jamie variant' }] }],
        }] };
      },
      async postJson(path, payload) {
        calls.push({ method: 'POST', path, payload });
        const groupMatch = path.match(/\/proposal-groups\/([^/]+)\/([^/]+)$/);
        if (groupMatch) {
          if (deferReview) await new Promise((_, reject) => { rejectDeferredReview = () => reject(new Error('Review service unavailable.')); });
          if (failReview) { document.activeElement = document.body; throw new Error('Review service unavailable.'); }
          const groupKey = decodeURIComponent(groupMatch[2]);
          const index = proposals.findIndex(item => item.key === groupKey);
          if (index >= 0) proposals.splice(index, 1);
          return { ok: true, reviewed: 1, refresh_job_ids: payload.action === 'approve' ? ['performer-refresh-job'] : [] };
        }
        const reviewMatch = path.match(/\/proposals\/(\d+)$/);
        if (reviewMatch) {
          if (deferReview) await new Promise((_, reject) => { rejectDeferredReview = () => reject(new Error('Review service unavailable.')); });
          if (failReview) { document.activeElement = document.body; throw new Error('Review service unavailable.'); }
          const index = proposals.findIndex(item => item.id === Number(reviewMatch[1]));
          if (index >= 0) proposals.splice(index, 1);
          return { ok: true, refresh_pending: true };
        }
        if (path.endsWith('/proposals/reconcile')) {
          return { processed: 0, resolved: 0, after_id: 0, has_more: false };
        }
        if (path.endsWith('/operations/preview')) return { ok: true, plan_id: payload.alias === 'Alpha' ? 'plan-1' : `plan-${payload.alias}`, items: [{
          alias: payload.alias, canonical: payload.canonical, alias_videos: payload.alias === 'Alpha' ? 2 : 1, canonical_videos: 3,
          aliases_affected: 0, has_photo: false, canonical_has_photo: true, has_bio: true,
          canonical_has_bio: false,
        }], warnings: ['One data warning'] };
        return { ok: true, job_id: 'job-7' };
      },
      setFeedback(target, text) { if (target) target.textContent = text; },
      openDialog(dialog) { if (dialog) { dialog.open = true; dialog.showModal?.(); } },
      closeDialog(dialog) { if (dialog) { dialog.open = false; dialog.close?.(); } },
    },
  };
  const context = { window, document, Option: function Option(label, value) { this.textContent = label; this.value = value; },
    location: { hash: '' }, setTimeout, clearTimeout, encodeURIComponent, URLSearchParams };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/performers.js', 'utf8'), context);
  navigateListener({ detail: { route: 'performers', performersTab: 'review' } });
  await elements.performersRefresh.listeners.click();
  assert.match(calls[0].path, /\/proposal-groups\?limit=50&offset=0$/);
  assert.match(elements.performersCount.textContent, /^51 pending · 51 name groups$/);
  const nextPage = elements.performerProposalPagination.children.find(item => item.textContent === 'Next');
  await nextPage.listeners.click();
  assert.match(calls.find(call => call.method === 'GET' && /\/proposal-groups\?limit=50&offset=50$/.test(call.path))?.path || '',
    /\/proposal-groups\?limit=50&offset=50$/);
  assert.match(elements.performerProposalPagination.children[0].textContent, /Showing 51–51 name groups/);
  let card = elements.performerProposalRows.children[0];
  assert.equal(card.dataset.proposalKey, 'group-51');
  const originalGetJson = window.AdminV2.getJson;
  const stalePage = deferred(), currentPage = deferred();
  let pageRequest = 0;
  window.AdminV2.getJson = path => path.includes('/proposal-groups?') || path.includes('/proposals?')
    ? (pageRequest++ === 0 ? stalePage.promise : currentPage.promise)
    : originalGetJson(path);
  const previous = elements.performerProposalPagination.children.find(item => item.textContent === 'Previous');
  const firstPageRequest = previous.listeners.click();
  const secondPageRequest = previous.listeners.click();
  currentPage.resolve({ items: proposals.slice(0, 50), total: proposals.length, pending: proposals.length, offset: 0, limit: 50 });
  await secondPageRequest;
  stalePage.resolve({ items: [{ key: 'group-999', reason: 'uncatalogued_credit', candidate: 'Stale proposal', proposals: 1, videos: 1 }], total: 1, pending: 1, offset: 0, limit: 50 });
  await firstPageRequest;
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalKey, 'group-1',
    'a delayed proposal page must not replace the result of a newer page request');
  window.AdminV2.getJson = originalGetJson;
  await elements.performerProposalPagination.children.find(item => item.textContent === 'Next').listeners.click();
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalKey, 'group-51');
  card = elements.performerProposalRows.children[0];
  const localInput = card.querySelectorAll('input').find(input => input.type === 'text'); localInput.value = 'Local performer';
  const approve = card.querySelectorAll('button').find(button => button.textContent.startsWith('Apply to next'));
  approve.focus();
  await approve.listeners.click();
  assert.ok(find(elements.taskReceipt, item => item.tagName === 'a' && item.href.includes('job_id=performer-refresh-job')), 'recognition approval exposes its background refresh task');
  assert.match(calls.find(call => call.path.includes('/proposal-groups/uncatalogued_credit/group-51')).path, /\/proposal-groups\/uncatalogued_credit\/group-51$/);
  assert.equal(calls.find(call => call.path.includes('/proposal-groups/uncatalogued_credit/group-51')).payload.canonical, 'Local performer');
  card = elements.performerProposalRows.children.at(-1);
  assert.equal(card.dataset.proposalKey, 'group-50', 'after removing the only item on page two, the prior page is restored');
  assert.equal(document.activeElement, card.querySelector('input'), 'focus moves to the nearest remaining proposal after a page-boundary removal');
  const reject = card.querySelectorAll('button').find(button => button.textContent.startsWith('Reject next'));
  reject.focus();
  await reject.listeners.click();
  card = elements.performerProposalRows.children.at(-1);
  assert.equal(card.dataset.proposalKey, 'group-49');
  assert.equal(document.activeElement, card.querySelector('input'), 'focus moves to the next proposal after rejection');
  const failureButton = card.querySelectorAll('button').find(button => button.textContent.startsWith('Reject next'));
  failureButton.focus(); failReview = true;
  await failureButton.listeners.click();
  assert.equal(document.activeElement, failureButton, 'failed review leaves focus on the retry action');
  assert.equal(failureButton.disabled, false, 'failed review re-enables the action');
  assert.match(elements.performersFeedback.textContent, /Review service unavailable/);
  failReview = false; deferReview = true; failureButton.focus();
  const delayedReview = failureButton.listeners.click();
  document.activeElement = elements.performersRefresh;
  rejectDeferredReview();
  await delayedReview;
  assert.equal(document.activeElement, elements.performersRefresh, 'a delayed response does not steal focus after the operator moves elsewhere');
  const catalogSearch = find(elements.performerProposalRows, item => item.tagName === 'input' && item.type === 'search');
  const catalogSelect = find(elements.performerProposalRows, item => item.tagName === 'select');
  const localName = find(elements.performerProposalRows, item => item.tagName === 'input' && item.type === 'text');
  assert.equal(catalogSearch.attributes['aria-label'], 'Search performer catalogue');
  assert.equal(catalogSelect.attributes['aria-label'], 'Choose matching catalogue performer');
  assert.equal(localName.attributes['aria-label'], 'New local performer name');
  catalogSelect.disabled = true;
  localName.value = '';
  const proposalApprove = find(elements.performerProposalRows, item => item.tagName === 'button' && item.textContent.startsWith('Apply to next'));
  await proposalApprove.listeners.click();
  assert.equal(document.activeElement, localName,
    'when catalogue search has no selectable match, an invalid approval focuses the active local-name field');
  assert.match(elements.performersFeedback.textContent, /Choose a catalogue performer/);
  navigateListener({ detail: { route: 'performers', performersTab: 'name-variants' } });
  await elements.fuzzyScan.listeners.click();
  assert.equal(calls.find(call => call.path.includes('/performers/fuzzy?')).path, '/admin/api/v2/performers/fuzzy?threshold=0.88');
  const review = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === 'Merge');
  assert.ok(review, 'scan renders a direct merge action for a name variant');
  assert.match(find(elements.fuzzyRows, item => item.textContent.includes('similarity score'))?.textContent || '', /similarity score 0\.95/,
    'the fuzzy score is labeled as similarity, not probability or confidence');
  const swap = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === '⇄ Swap');
  assert.ok(swap, 'scan renders a swap direction action');
  const dismiss = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === 'Dismiss');
  assert.ok(dismiss, 'scan renders a dismiss action');

  const selectVariant = (name, checked = true) => {
    const row = elements.fuzzyRows.children.find(item => item.dataset.fuzzyPair?.startsWith(`${name}::`));
    const checkbox = find(row, item => item.tagName === 'input' && item.attributes['aria-label']?.startsWith('Select merge'));
    assert.ok(checkbox, `the ${name} candidate has an explicit selection control`);
    checkbox.checked = checked;
    checkbox.listeners.change();
    return checkbox;
  };
  selectVariant('Alpha'); selectVariant('Alphaa');
  await elements.fuzzyReviewSelected.listeners.click();
  assert.match(elements.fuzzyFeedback.textContent, /Selection conflict/);
  assert.equal(calls.filter(call => call.path.endsWith('/operations/preview')).length, 0,
    'overlapping identities are rejected before queuing');
  selectVariant('Alpha', false); selectVariant('Alphaa', false);
  selectVariant('Alpha'); selectVariant('Jamie Nort');
  assert.match(elements.fuzzyReviewSelected.textContent, /Merge selected \(2\)/);
  const batchBefore = calls.length;
  await elements.fuzzyReviewSelected.listeners.click();
  const batchCalls = calls.slice(batchBefore).filter(call => call.method === 'POST');
  assert.deepEqual(batchCalls.map(call => call.path.split('/').at(-1)), ['preview', 'execute', 'preview', 'execute'],
    'each selected merge is prepared and queued immediately');
  assert.deepEqual(batchCalls.filter(call => call.path.endsWith('/preview')).map(call => call.payload.alias), ['Alpha', 'Jamie Nort']);
  assert.match(elements.fuzzyFeedback.textContent, /2 of 2 selected merges queued/);
  assert.equal(elements.fuzzyMergeDialog.open, undefined, 'batch merge does not open a dialog');

  const executeBefore = calls.filter(call => call.path.endsWith('/operations/execute')).length;
  await review.listeners.click();
  const previewCall = calls.filter(call => call.path.endsWith('/operations/preview')).at(-1);
  assert.equal(JSON.stringify(previewCall.payload), JSON.stringify({ type: 'performer_merge', alias: 'Alpha', canonical: 'Álpha' }));
  const executions = calls.filter(call => call.path.endsWith('/operations/execute'));
  assert.equal(executions.length, executeBefore + 1, 'one click queues the merge after preparing a plan');
  assert.equal(JSON.stringify(executions.at(-1).payload), JSON.stringify({ plan_id: 'plan-1' }));
  assert.notEqual(elements.fuzzyMergeDialog.open, true, 'single merge does not open a confirmation dialog');
  assert.match(elements.fuzzyFeedback.textContent, /job-7/);
  window.AdminV2.postJson = async () => { throw new Error('Preview service unavailable.'); };
  const failedButton = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === 'Merge' && !item.disabled);
  await failedButton.listeners.click();
  assert.match(elements.fuzzyFeedback.textContent, /Could not prepare merge/);
  assert.equal(failedButton.disabled, false, 'a failed preview can be retried');
  const oldCatalog = deferred(), newCatalog = deferred();
  const catalogSelectInput = find(elements.performerProposalRows, item => item.tagName === 'input' && item.type === 'search');
  let catalogRequest = 0;
  window.AdminV2.getJson = path => {
    if (path.includes('/catalog?')) return (catalogRequest++ === 0 ? oldCatalog.promise : newCatalog.promise);
    throw new Error(`Unexpected catalog race request: ${path}`);
  };
  catalogSelectInput.value = 'old'; catalogSelectInput.listeners.input();
  await new Promise(resolve => setTimeout(resolve, 210));
  catalogSelectInput.value = 'new'; catalogSelectInput.listeners.input();
  await new Promise(resolve => setTimeout(resolve, 210));
  newCatalog.resolve({ items: [{ id: 'new-id', name: 'New Performer' }] });
  await new Promise(resolve => setImmediate(resolve));
  oldCatalog.resolve({ items: [{ id: 'old-id', name: 'Old Performer' }] });
  await new Promise(resolve => setImmediate(resolve));
  const currentCatalogSelect = find(catalogSelectInput.parentNode, item => item.tagName === 'select');
  assert.equal(currentCatalogSelect.children[1].textContent, 'New Performer',
    'a stale performer catalog search must not replace results for the current query');
  window.AdminV2.getJson = originalGetJson;

  window.AdminV2.postJson = (path, payload) => {
    if (path.endsWith('/operations/preview')) return Promise.resolve({ ok: true, plan_id: 'plan-direct', items: [{ alias: payload.alias, canonical: payload.canonical }] });
    if (path.endsWith('/operations/execute')) return Promise.resolve({ ok: true, job_id: 'job-direct' });
    throw new Error(`Unexpected postJson in test: ${path}`);
  };
  await elements.fuzzyScan.listeners.click();
  const rowTitle = find(elements.fuzzyRows, item => item.tagName === 'strong');
  assert.equal(rowTitle.textContent, 'Alpha → Álpha');
  const swapBtn = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === '⇄ Swap');
  await swapBtn.listeners.click();
  assert.equal(rowTitle.textContent, 'Álpha → Alpha', 'swap button flips alias and canonical direction');
  const selectedCheckbox = find(elements.fuzzyRows.children[0], item => item.tagName === 'input' && item.attributes['aria-label']?.startsWith('Select merge'));
  selectedCheckbox.checked = true; selectedCheckbox.listeners.change();
  await elements.fuzzyReviewSelected.listeners.click();
  assert.match(elements.fuzzyFeedback.textContent, /1 of 1 selected merges queued/);
  const queuedBadge = find(elements.fuzzyRows, item => item.className.includes('status-pill') && item.textContent === 'Queued');
  assert.ok(queuedBadge, 'a reviewed selection renders a queued status badge');
  assert.ok(find(elements.fuzzyRows, item => item.tagName === 'a' && item.href.includes('job_id=job-direct')), 'the queued merge links to its task');

  await elements.fuzzyScan.listeners.click();
  const dismissTarget = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === 'Dismiss');
  await dismissTarget.listeners.click();
  assert.equal(elements.fuzzyRows.children.some(item => item.dataset.fuzzyPair?.startsWith('Alpha::')), false,
    'dismissing a suggestion removes that pair while leaving other candidates intact');
  assert.match(elements.fuzzyFeedback.textContent, /Dismissed/);

  const oldProposals = deferred(), currentProposals = deferred();
  const oldCatalogLoad = deferred(), currentCatalogLoad = deferred();
  let proposalLoadCount = 0, catalogLoadCount = 0;
  window.AdminV2.getJson = path => {
    if (path.includes('/proposal-groups?') || path.includes('/proposals?')) return proposalLoadCount++ === 0 ? oldProposals.promise : currentProposals.promise;
    if (path.includes('/catalog?')) return catalogLoadCount++ === 0 ? oldCatalogLoad.promise : currentCatalogLoad.promise;
    throw new Error(`Unexpected performer refresh request: ${path}`);
  };
  navigateListener({ detail: { route: 'performers', performersTab: 'review' } });
  navigateListener({ detail: { route: 'library' } });
  navigateListener({ detail: { route: 'performers', performersTab: 'review' } });
  assert.equal(proposalLoadCount, 2, 'returning to Review matches starts a fresh proposal request while the old one is pending');
  currentProposals.resolve({ items: [{ key: 'group-901', reason: 'uncatalogued_credit', candidate: 'Current re-entry proposal', proposals: 1, videos: 1 }], total: 1, pending: 1, offset: 0, limit: 50 });
  currentCatalogLoad.resolve({ items: [] });
  await new Promise(resolve => setImmediate(resolve));
  oldProposals.resolve({ items: [{ key: 'group-902', reason: 'uncatalogued_credit', candidate: 'Stale re-entry proposal', proposals: 1, videos: 1 }], total: 1, pending: 1, offset: 0, limit: 50 });
  oldCatalogLoad.resolve({ items: [] });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalKey, 'group-901',
    'a late proposal load from before navigation cannot overwrite the refreshed queue');
  assert.match(elements.performersFeedback.textContent, /proposals grouped by repeated name/,
    'the stale load cannot replace feedback from the latest queue request');

  // Switching to the Directory tab while a proposal list is in flight must drop the late render.
  const lateProposals = deferred();
  window.AdminV2.getJson = path => path.includes('/catalog?')
    ? Promise.resolve({ items: [] }) : lateProposals.promise;
  const keyBeforeLate = elements.performerProposalRows.children[0].dataset.proposalKey;
  const lateReviewPane = elements.performerReviewPane;
  const lateRefresh = elements.performersRefresh.listeners.click();
  lateReviewPane.hidden = true;
  lateProposals.resolve({ items: [{ key: 'group-903', reason: 'uncatalogued_credit', candidate: 'Late proposal', proposals: 1, videos: 1 }], total: 1, pending: 1, offset: 0, limit: 50 });
  await lateRefresh;
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalKey, keyBeforeLate,
    'a late proposal list must not repaint after switching to the Directory tab');
  process.stdout.write('PASS: performer search and paging reject stale responses; review focus and direct fuzzy merges remain verified\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
