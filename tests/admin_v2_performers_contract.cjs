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
  const ids = ['performersFeedback', 'performerProposalRows', 'performersCount', 'performersState', 'performersRefresh', 'performerProposalPagination',
    'fuzzyCategory', 'fuzzyRows', 'fuzzyPagination', 'fuzzyPageInfo', 'fuzzyPrev', 'fuzzyNext', 'fuzzyFeedback',
    'fuzzyThreshold', 'fuzzyScan', 'fuzzyReviewSelected', 'fuzzySelectionStatus', 'fuzzyMergeDialog', 'fuzzyMergeContent', 'fuzzyMergeActions', 'fuzzyMergeClose'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'fuzzyMergeDialog' ? 'dialog' : 'div')]));
  elements.fuzzyThreshold.value = '0.88';
  const calls = [];
  const catalogSelects = [];
  const proposals = Array.from({ length: 51 }, (_, index) => ({ id: index + 1, title: `Proposal ${index + 1}` }));
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
        if (path.includes('/proposals?')) {
          const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset'));
          return { items: proposals.slice(offset, offset + 50), total: proposals.length, offset, limit: 50 };
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
        const reviewMatch = path.match(/\/proposals\/(\d+)$/);
        if (reviewMatch) {
          if (deferReview) await new Promise((_, reject) => { rejectDeferredReview = () => reject(new Error('Review service unavailable.')); });
          if (failReview) { document.activeElement = document.body; throw new Error('Review service unavailable.'); }
          const index = proposals.findIndex(item => item.id === Number(reviewMatch[1]));
          if (index >= 0) proposals.splice(index, 1);
          return { ok: true, refresh_pending: true };
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
  await elements.performersRefresh.listeners.click();
  assert.match(calls[0].path, /\/proposals\?limit=50&offset=0$/);
  assert.equal(elements.performersCount.textContent, '51 pending');
  const nextPage = elements.performerProposalPagination.children.find(item => item.textContent === 'Next');
  await nextPage.listeners.click();
  assert.match(calls.find(call => call.method === 'GET' && /\/proposals\?limit=50&offset=50$/.test(call.path))?.path || '',
    /\/proposals\?limit=50&offset=50$/);
  assert.match(elements.performerProposalPagination.children[0].textContent, /Showing 51–51 of 51/);
  let card = elements.performerProposalRows.children[0];
  assert.equal(card.dataset.proposalId, 51);
  const originalGetJson = window.AdminV2.getJson;
  const stalePage = deferred(), currentPage = deferred();
  let pageRequest = 0;
  window.AdminV2.getJson = path => path.includes('/proposals?')
    ? (pageRequest++ === 0 ? stalePage.promise : currentPage.promise)
    : originalGetJson(path);
  const previous = elements.performerProposalPagination.children.find(item => item.textContent === 'Previous');
  const firstPageRequest = previous.listeners.click();
  const secondPageRequest = previous.listeners.click();
  currentPage.resolve({ items: proposals.slice(0, 50), total: proposals.length, offset: 0, limit: 50 });
  await secondPageRequest;
  stalePage.resolve({ items: [{ id: 999, title: 'Stale proposal' }], total: 1, offset: 0, limit: 50 });
  await firstPageRequest;
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalId, 1,
    'a delayed proposal page must not replace the result of a newer page request');
  window.AdminV2.getJson = originalGetJson;
  await elements.performerProposalPagination.children.find(item => item.textContent === 'Next').listeners.click();
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalId, 51);
  card = elements.performerProposalRows.children[0];
  const localInput = card.querySelectorAll('input').find(input => input.type === 'text'); localInput.value = 'Local performer';
  const approve = card.querySelectorAll('button').find(button => button.textContent === 'Approve');
  approve.focus();
  await approve.listeners.click();
  assert.match(calls.find(call => call.path.endsWith('/proposals/51')).path, /\/proposals\/51$/);
  assert.equal(calls.find(call => call.path.endsWith('/proposals/51')).payload.canonical, 'Local performer');
  card = elements.performerProposalRows.children.at(-1);
  assert.equal(card.dataset.proposalId, 50, 'after removing the only item on page two, the prior page is restored');
  assert.equal(document.activeElement, card.querySelector('input'), 'focus moves to the nearest remaining proposal after a page-boundary removal');
  const reject = card.querySelectorAll('button').find(button => button.textContent === 'Reject');
  reject.focus();
  await reject.listeners.click();
  card = elements.performerProposalRows.children.at(-1);
  assert.equal(card.dataset.proposalId, 49);
  assert.equal(document.activeElement, card.querySelector('input'), 'focus moves to the next proposal after rejection');
  const failureButton = card.querySelectorAll('button').find(button => button.textContent === 'Reject');
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
  const proposalApprove = find(elements.performerProposalRows, item => item.tagName === 'button' && item.textContent === 'Approve');
  await proposalApprove.listeners.click();
  assert.equal(document.activeElement, localName,
    'when catalogue search has no selectable match, an invalid approval focuses the active local-name field');
  assert.match(elements.performersFeedback.textContent, /Choose a catalogue performer/);
  await elements.fuzzyScan.listeners.click();
  assert.equal(calls.find(call => call.path.includes('/performers/fuzzy?')).path, '/admin/api/v2/performers/fuzzy?threshold=0.88');
  const review = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === 'Review merge');
  assert.ok(review, 'scan renders a review action for a name variant');
  const directMerge = find(elements.fuzzyRows, item => item.tagName === 'button' && item.textContent === 'Merge');
  assert.equal(directMerge, null, 'a candidate must never have a one-click merge action');
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
  assert.match(find(elements.fuzzyMergeContent, item => item.className === 'state-banner state-error')?.textContent || '', /Selection conflict/,
    'overlapping performer identities must be detected before any merge preview is queued');
  assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/operations/preview')).length, 0,
    'a conflicting selection must not create merge plans');
  elements.fuzzyMergeDialog.close();
  selectVariant('Alpha', false); selectVariant('Alphaa', false);
  selectVariant('Alpha'); selectVariant('Jamie Nort');
  const batchPreviewsBefore = calls.filter(call => call.method === 'POST' && call.path.endsWith('/operations/preview')).length;
  await elements.fuzzyReviewSelected.listeners.click();
  assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/operations/preview')).length, batchPreviewsBefore + 2,
    'each explicitly selected pair gets its own current server preview');
  const batchAliases = calls.filter(call => call.method === 'POST' && call.path.endsWith('/operations/preview'))
    .slice(batchPreviewsBefore).map(call => call.payload.alias);
  assert.deepEqual(batchAliases, ['Alpha', 'Jamie Nort'], 'unchecked candidates cannot enter the selected preview batch');
  assert.ok(find(elements.fuzzyMergeContent, item => item.tagName === 'dt' && item.textContent === 'Canonical target'));
  assert.ok(find(elements.fuzzyMergeContent, item => item.textContent === 'One data warning'));
  const batchQueue = find(elements.fuzzyMergeActions, item => item.tagName === 'button' && item.textContent === 'Queue selected merges');
  assert.ok(batchQueue.disabled, 'selected merges require explicit confirmation after all previews');
  const batchConsent = find(elements.fuzzyMergeContent, item => item.tagName === 'input');
  batchConsent.checked = true; batchConsent.listeners.change();
  await batchQueue.listeners.click();
  const batchResults = find(elements.fuzzyMergeContent, item => item.className === 'fuzzy-merge-results');
  assert.equal(batchResults.children.length, 2, 'batch review records a separate outcome for every selected pair');
  assert.ok(batchResults.children.every(item => /queued/.test(item.textContent)));
  elements.fuzzyMergeDialog.close();

  await review.listeners.click();
  const previewCall = calls.find(call => call.method === 'POST' && call.path === '/admin/api/v2/operations/preview');
  assert.equal(previewCall.path, '/admin/api/v2/operations/preview');
  assert.equal(JSON.stringify(previewCall.payload), JSON.stringify({ type: 'performer_merge', alias: 'Alpha', canonical: 'Álpha' }));
  const consent = find(elements.fuzzyMergeContent, item => item.tagName === 'input');
  assert.ok(find(elements.fuzzyMergeContent, item => item.tagName === 'label' && item.children.includes(consent)),
    'the irreversible merge confirmation checkbox has a wrapping label');
  const queue = find(elements.fuzzyMergeActions, item => item.tagName === 'button' && item.textContent === 'Queue merge');
  assert.ok(queue.disabled, 'merge cannot be queued before explicit confirmation');
  consent.checked = true;
  consent.listeners.change();
  await queue.listeners.click();
  const executeCall = calls.find(call => call.method === 'POST' && call.path === '/admin/api/v2/operations/execute');
  assert.equal(executeCall.path, '/admin/api/v2/operations/execute');
  assert.equal(JSON.stringify(executeCall.payload), JSON.stringify({ plan_id: 'plan-1' }));
  assert.match(elements.fuzzyFeedback.textContent, /job-7/);
  assert.ok(elements.fuzzyMergeContent.children.some(child => /no one-click undo/i.test(child.textContent)));
  elements.fuzzyMergeDialog.close();
  window.AdminV2.postJson = async () => { throw new Error('Preview service unavailable.'); };
  await review.listeners.click();
  const failedPreviewTitle = find(elements.fuzzyMergeContent, item => item.tagName === 'h2');
  assert.equal(failedPreviewTitle.id, 'fuzzyMergeTitle',
    'a failed merge preview must keep the dialog accessible name target in the DOM');
  assert.ok(find(elements.fuzzyMergeContent, item => item.tagName === 'p' && /Could not create a current merge preview/.test(item.textContent)),
    'the preview failure must remain visible in the dialog');
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

  elements.fuzzyMergeDialog.close();
  const stalePreview = deferred(), currentPreview = deferred();
  const pendingPreviews = [stalePreview, currentPreview];
  const executedPlans = [];
  window.AdminV2.postJson = (path, payload) => {
    if (path.endsWith('/operations/preview')) return pendingPreviews.shift().promise;
    if (path.endsWith('/operations/execute')) {
      executedPlans.push(payload.plan_id);
      return Promise.resolve({ job_id: 'fresh-job' });
    }
    throw new Error(`Unexpected performer write: ${path}`);
  };
  const staleMerge = review.listeners.click();
  elements.fuzzyMergeDialog.close();
  const currentMerge = review.listeners.click();
  currentPreview.resolve({ plan_id: 'plan-B', items: [{ alias: 'Current variant', canonical: 'Current canonical', alias_videos: 1, canonical_videos: 2 }] });
  await currentMerge;
  stalePreview.resolve({ plan_id: 'plan-A', items: [{ alias: 'Stale variant', canonical: 'Stale canonical', alias_videos: 9, canonical_videos: 8 }] });
  await staleMerge;
  const textOf = element => `${element.textContent || ''} ${(element.children || []).map(textOf).join(' ')}`;
  const fuzzyDialogText = () => textOf(elements.fuzzyMergeContent);
  assert.match(fuzzyDialogText(), /Current variant/);
  assert.doesNotMatch(fuzzyDialogText(), /Stale variant/,
    'a delayed preview must not replace a newer open merge dialog');
  const currentConsent = find(elements.fuzzyMergeContent, item => item.tagName === 'input');
  currentConsent.checked = true; currentConsent.listeners.change();
  const currentQueue = find(elements.fuzzyMergeActions, item => item.tagName === 'button' && item.textContent === 'Queue merge');
  await currentQueue.listeners.click();
  assert.deepEqual(executedPlans, ['plan-B'], 'only the preview belonging to the current dialog can be queued');

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
  const singleBatchConsent = find(elements.fuzzyMergeContent, item => item.tagName === 'input');
  singleBatchConsent.checked = true; singleBatchConsent.listeners.change();
  const mergeBtn = find(elements.fuzzyMergeActions, item => item.tagName === 'button' && item.textContent === 'Queue selected merges');
  await mergeBtn.listeners.click();
  assert.match(elements.fuzzyFeedback.textContent, /1 of 1 selected performer merges queued/);
  const queuedBadge = find(elements.fuzzyRows, item => item.className.includes('pill-good'));
  assert.ok(queuedBadge, 'a reviewed selection renders a queued status badge');
  assert.match(queuedBadge.textContent, /job-direct/);

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
    if (path.includes('/proposals?')) return proposalLoadCount++ === 0 ? oldProposals.promise : currentProposals.promise;
    if (path.includes('/catalog?')) return catalogLoadCount++ === 0 ? oldCatalogLoad.promise : currentCatalogLoad.promise;
    throw new Error(`Unexpected performer refresh request: ${path}`);
  };
  navigateListener({ detail: { route: 'performers' } });
  navigateListener({ detail: { route: 'library' } });
  navigateListener({ detail: { route: 'performers' } });
  assert.equal(proposalLoadCount, 2, 'returning to Performers starts a fresh proposal request while the old one is pending');
  currentProposals.resolve({ items: [{ id: 901, title: 'Current re-entry proposal' }], total: 1, offset: 0, limit: 50 });
  currentCatalogLoad.resolve({ items: [] });
  await new Promise(resolve => setImmediate(resolve));
  oldProposals.resolve({ items: [{ id: 902, title: 'Stale re-entry proposal' }], total: 1, offset: 0, limit: 50 });
  oldCatalogLoad.resolve({ items: [] });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.performerProposalRows.children[0].dataset.proposalId, 901,
    'a late proposal load from before navigation cannot overwrite the refreshed queue');
  assert.match(elements.performersFeedback.textContent, /Review each detected name/,
    'the stale load cannot replace feedback from the latest queue request');
  process.stdout.write('PASS: performer search and paging reject stale responses; review focus and explicit fuzzy-merge consent remain verified\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
