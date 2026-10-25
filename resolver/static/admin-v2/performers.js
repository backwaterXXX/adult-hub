(() => {
  'use strict';

  const API = '/admin/api/v2/performers';
  const $ = (selector, root = document) => root.querySelector(selector);
  const PROPOSAL_PAGE_SIZE = 50;
  const state = { loading: false, loadSeq: 0, rows: [], proposalOffset: 0, proposalTotal: 0, proposalSeq: 0, busy: new Set(), fuzzyGroups: [], fuzzyOffset: 0, fuzzyLoading: false, fuzzyScanned: false, fuzzyMergeSeq: 0, fuzzyQueued: new Map(), fuzzyDismissed: new Set(), fuzzySwapped: new Map(), fuzzySelected: new Map() };
  const MAX_SELECTED_MERGES = 10;

  // Shared helpers live in admin-v2.js and are published through window.AdminV2.
  const helpers = () => window.AdminV2 || {};
  const node = (...args) => helpers().create(...args);
  function message(text, kind = 'status') { window.AdminV2.setFeedback($('#performersFeedback'), text, kind); }
  function errorText(error) { return error?.message || 'The request could not be completed.'; }

  async function get(url) {
    return window.AdminV2.getJson(url);
  }
  async function post(url, body) {
    return window.AdminV2.postJson(url, body);
  }
  async function loadCatalog(query, select, search, sequence) {
    const data = await get(`${API}/catalog?q=${encodeURIComponent(query)}&limit=100`);
    if (sequence !== Number(search.dataset.searchSequence)) return;
    const items = Array.isArray(data.items) ? data.items : [];
    select.replaceChildren(new Option(items.length ? 'Choose a matching performer…' : 'No matching performers', ''));
    items.forEach(item => select.add(new Option(item.name, item.id)));
    select.disabled = items.length === 0;
  }
  function render() {
    const rows = $('#performerProposalRows');
    rows.replaceChildren();
    $('#performersCount').textContent = `${Number(state.pendingTotal || 0).toLocaleString('en-US')} pending · ${state.proposalTotal.toLocaleString('en-US')} name groups`;
    renderProposalPagination();
    $('#performersState').hidden = state.rows.length > 0;
    if (!state.rows.length) {
      const empty = $('#performersState');
      empty.className = 'catalog-state';
      empty.replaceChildren(node('span', 'empty-icon', '✓'), node('strong', '', 'No pending proposals'), node('span', '', 'New performer candidates from Telegram will appear here.'));
      return;
    }
    for (const proposal of state.rows) rows.append(renderProposalGroup(proposal));
  }
  function renderProposalPagination() {
    const host = $('#performerProposalPagination');
    host.replaceChildren();
    host.hidden = state.proposalTotal <= PROPOSAL_PAGE_SIZE;
    if (host.hidden) return;
    const start = state.proposalOffset + 1;
    const end = Math.min(state.proposalOffset + state.rows.length, state.proposalTotal);
    host.append(node('span', 'catalog-feedback', `Showing ${start}–${end} name groups`));
    const previous = node('button', 'button button-quiet', 'Previous');
    previous.type = 'button'; previous.disabled = state.proposalOffset === 0;
    previous.addEventListener('click', () => loadProposalPage(Math.max(0, state.proposalOffset - PROPOSAL_PAGE_SIZE))
      .then(render).catch(error => message(errorText(error), 'error')));
    const next = node('button', 'button button-quiet', 'Next');
    next.type = 'button'; next.disabled = state.proposalOffset + state.rows.length >= state.proposalTotal;
    next.addEventListener('click', () => loadProposalPage(state.proposalOffset + PROPOSAL_PAGE_SIZE)
      .then(render).catch(error => message(errorText(error), 'error')));
    host.append(previous, next);
  }
  function renderProposalGroup(item) {
    const card = node('article', 'performer-proposal');
    card.dataset.proposalKey = item.key;
    const heading = node('div', 'performer-proposal-heading');
    const title = node('div', 'performer-proposal-title');
    const reasonLabels = { uncatalogued_credit: 'Unknown name', possible_typo: 'Possible spelling variant', ambiguous_catalog_alias: 'Ambiguous catalogue alias' };
    title.append(node('p', 'eyebrow', `${item.videos} videos · ${item.proposals} proposals`), node('h2', '', item.candidate || 'Unknown name'));
    title.title = item.example_title || '';
    heading.append(title, node('span', 'status-pill pill-warning', reasonLabels[item.reason] || item.reason));
    const details = node('dl', 'performer-proposal-details');
    const local = document.createElement('input');
    local.type = 'text'; local.maxLength = 160; local.placeholder = 'Canonical performer name';
    local.setAttribute('aria-label', 'New local performer name');
    for (const [label, value, key] of [
      ['Detected text', item.matched_text, 'matched'],
      ['Example title', item.example_title, 'reason'],
    ]) {
      const group = node('div', key ? `performer-detail-${key}` : '');
      const dd = node('dd', '', value || '—');
      group.append(node('dt', '', label), dd);
      details.append(group);
    }
    const form = node('div', 'performer-proposal-controls');
    const catalogField = node('label', 'performer-catalog-field');
    catalogField.append(node('span', '', 'Search catalogue & select match'));
    const search = document.createElement('input');
    search.type = 'search'; search.placeholder = 'Search catalogue…'; search.setAttribute('aria-label', 'Search performer catalogue');
    search.autocomplete = 'off';
    search.dataset.searchSequence = '0';
    search.addEventListener('input', () => {
      const query = search.value.trim();
      const sequence = Number(search.dataset.searchSequence) + 1;
      search.dataset.searchSequence = String(sequence);
      clearTimeout(search._catalogTimer);
      select.disabled = true;
      if (query.length < 2) {
        select.replaceChildren(new Option(query ? 'Type at least 2 characters…' : 'Search to see matching performers', ''));
        return;
      }
      select.replaceChildren(new Option('Searching catalogue…', ''));
      search._catalogTimer = setTimeout(() => loadCatalog(query, select, search, sequence).catch(() => {
        if (sequence !== Number(search.dataset.searchSequence)) return;
        select.replaceChildren(new Option('Could not load catalogue matches', ''));
      }), 180);
    });
    const select = document.createElement('select');
    select.dataset.performerCatalog = 'true'; select.setAttribute('aria-label', 'Choose matching catalogue performer');
    select.disabled = true;
    select.add(new Option('Search to see matching performers', ''));
    catalogField.append(search, select);
    const localField = node('label', 'performer-local-field');
    localField.append(node('span', '', 'Or enter the correct performer name'));
    localField.append(local);
    form.append(catalogField, localField);
    const actions = node('div', 'performer-proposal-actions');
    const batchSize = Math.min(Number(item.proposals) || 0, 100);
    const reject = node('button', 'button button-quiet', `Reject next ${batchSize}`); reject.type = 'button';
    const approve = node('button', 'button button-primary', `Apply to next ${batchSize}`); approve.type = 'button';
    reject.addEventListener('click', () => reviewGroup(item, { action: 'reject' }, card));
    approve.addEventListener('click', () => {
      const performerId = select.value;
      const canonical = local.value.trim();
      if (!performerId && !canonical) {
        message('Choose a catalogue performer or enter a local name first.');
        (select.disabled ? local : select).focus();
        return;
      }
      return reviewGroup(item, performerId ? { action: 'approve', performer_id: performerId } : { action: 'approve', canonical }, card);
    });
    actions.append(reject, approve);
    card.append(heading, details, form, actions);
    return card;
  }
  async function reviewGroup(item, body, card) {
    const key = `${item.reason}:${item.key}`;
    if (state.busy.has(key)) return;
    state.busy.add(key);
    card.querySelectorAll('button,input,select').forEach(control => { control.disabled = true; });
    message(body.action === 'reject' ? 'Rejecting this name group…' : 'Applying the decision to this name group…');
    try {
      const result = await post(`${API}/proposal-groups/${encodeURIComponent(item.reason)}/${encodeURIComponent(item.key)}`, body);
      state.pendingTotal = Math.max(0, Number(state.pendingTotal || 0) - Number(result.reviewed || 0));
      let refreshed = await loadProposalPage(state.proposalOffset);
      if (refreshed && !state.rows.length && state.proposalOffset > 0) refreshed = await loadProposalPage(Math.max(0, state.proposalOffset - PROPOSAL_PAGE_SIZE));
      render();
      message(`${body.action === 'reject' ? 'Rejected' : 'Reviewed'} ${result.reviewed} proposals in this group.`);
    } catch (error) {
      message(errorText(error), 'error');
      card.querySelectorAll('button,input,select').forEach(control => { control.disabled = false; });
    } finally { state.busy.delete(key); }
  }
  async function review(id, body, card) {
    if (state.busy.has(id)) return;
    const focusedControl = document.activeElement;
    const focusedBeforeRequest = card.contains(focusedControl);
    const reviewedIndex = state.rows.findIndex(item => String(item.id) === String(id));
    const originalOffset = state.proposalOffset;
    state.busy.add(id);
    card.querySelectorAll('button,input,select').forEach(control => { control.disabled = true; });
    message(body.action === 'reject' ? 'Rejecting proposal…' : 'Saving performer match…');
    try {
      const result = await post(`${API}/proposals/${encodeURIComponent(id)}`, body);
      const shouldRestoreFocus = focusedBeforeRequest
        && (card.contains(document.activeElement) || document.activeElement === document.body);
      state.rows = state.rows.filter(item => String(item.id) !== String(id));
      state.proposalTotal = Math.max(0, state.proposalTotal - 1);
      if (!state.rows.length && state.proposalOffset > 0) {
        await loadProposalPage(Math.max(0, state.proposalOffset - PROPOSAL_PAGE_SIZE));
      }
      render();
      if (shouldRestoreFocus) {
        const focusIndex = state.proposalOffset < originalOffset
          ? state.rows.length - 1
          : Math.min(reviewedIndex, state.rows.length - 1);
        focusProposal(focusIndex);
      }
      message(body.action === 'reject' ? 'Proposal rejected.' : result.refresh_pending === true
        ? 'Proposal approved. Metadata refresh queued.'
        : 'Proposal approved. Metadata refresh could not be queued; refresh the video manually.');
    } catch (error) {
      const shouldRestoreFocus = focusedBeforeRequest
        && (card.contains(document.activeElement) || document.activeElement === document.body);
      message(errorText(error), 'error');
      card.querySelectorAll('button,input,select').forEach(control => { control.disabled = false; });
      if (shouldRestoreFocus && focusedControl && card.contains(focusedControl)) focusedControl.focus({ preventScroll: true });
    } finally { state.busy.delete(id); }
  }
  function focusProposal(index) {
    const cards = $('#performerProposalRows').querySelectorAll('.performer-proposal');
    const card = index >= 0 ? cards[index] : null;
    const target = card?.querySelector('input,select,button:not(:disabled)') || $('#performersRefresh');
    target?.focus({ preventScroll: true });
  }
  async function load() {
    const sequence = ++state.loadSeq;
    state.loading = true;
    message('Loading repeated performer names…');
    try {
      state.proposalOffset = 0;
      const proposalData = await loadProposalPage(0);
      if (sequence !== state.loadSeq || !proposalData) return;
      render();
      message(proposalData.pending ? `${proposalData.pending} proposals grouped by repeated name. Decisions apply to at most 100 proposals at a time.` : 'Proposal queue is up to date.');
    } catch (error) {
      if (sequence !== state.loadSeq) return;
      message(errorText(error), 'error');
      const empty = $('#performersState');
      empty.hidden = false; empty.className = 'catalog-state state-error';
      empty.replaceChildren(node('span', 'empty-icon', '!'), node('strong', '', 'Could not load proposals'), node('span', '', errorText(error)));
    } finally { if (sequence === state.loadSeq) state.loading = false; }
  }
  async function loadProposalPage(offset) {
    const sequence = ++state.proposalSeq;
    let data;
    try { data = await get(`${API}/proposal-groups?limit=${PROPOSAL_PAGE_SIZE}&offset=${offset}`); }
    catch (error) { if (sequence !== state.proposalSeq) return null; throw error; }
    if (sequence !== state.proposalSeq) return null;
    state.rows = Array.isArray(data.items) ? data.items : [];
    state.proposalTotal = Number.isInteger(data.total) ? data.total : state.rows.length;
    state.pendingTotal = Number.isInteger(data.pending) ? data.pending : state.proposalTotal;
    state.proposalOffset = Number.isInteger(data.offset) ? data.offset : offset;
    return data;
  }

  async function reconcileClearMatches() {
    const button = $('#performersReconcile');
    if (button.disabled) return;
    button.disabled = true;
    let afterId = 0;
    let processed = 0;
    let resolved = 0;
    let refreshError = false;
    message('Checking pending proposals against current performer identities…');
    try {
      while (true) {
        const result = await post(`${API}/proposals/reconcile`, { after_id: afterId });
        processed += Number(result.processed || 0);
        resolved += Number(result.resolved || 0);
        refreshError = refreshError || result.refresh_error === true;
        afterId = Number(result.after_id || afterId);
        if (!result.has_more || !result.processed) break;
        message(`Checked ${processed.toLocaleString('en-US')} proposals; resolved ${resolved.toLocaleString('en-US')} clear matches…`);
      }
      await load();
      message(`Checked ${processed.toLocaleString('en-US')} proposals and resolved ${resolved.toLocaleString('en-US')} clear matches.${refreshError ? ' Some metadata refresh jobs could not be queued.' : ''} Ambiguous names remain grouped for review.`, refreshError ? 'warning' : 'status');
    } catch (error) {
      message(`Reconciliation stopped after ${processed.toLocaleString('en-US')} proposals. ${errorText(error)}`, 'error');
      await load().catch(() => {});
    } finally { button.disabled = false; }
  }

  function fuzzyRows() {
    const category = $('#fuzzyCategory')?.value || '';
    return state.fuzzyGroups.filter(group => !category || group.category === category)
      .flatMap(group => (Array.isArray(group.variants) ? group.variants : []).map(variant => ({ group, variant })))
      .filter(({ group, variant }) => {
        const keyA = `${variant.name}::${group.canonical}`;
        const keyB = `${group.canonical}::${variant.name}`;
        return !state.fuzzyDismissed.has(keyA) && !state.fuzzyDismissed.has(keyB);
      });
  }
  function addFilmExamples(target, label, examples) {
    const details = document.createElement('details');
    details.append(node('summary', '', `${label} examples (${examples.length})`));
    const list = document.createElement('ul');
    examples.forEach(example => {
      const title = example.title || 'Untitled video';
      const metadata = [example.provider, example.studio].filter(Boolean).join(' · ');
      list.append(node('li', '', metadata ? `${title} · ${metadata}` : title));
    });
    details.append(list);
    target.append(details);
  }
  function updateSelectedMergeButton() {
    const button = $('#fuzzyReviewSelected');
    const status = $('#fuzzySelectionStatus');
    if (!button) return;
    button.disabled = state.fuzzySelected.size === 0 || state.fuzzySelected.size > MAX_SELECTED_MERGES;
    button.textContent = `Review selected merges (${state.fuzzySelected.size})`;
    if (status) status.textContent = `Select up to ${MAX_SELECTED_MERGES} suggestions. Every merge needs a current preview and explicit confirmation.`;
  }
  function normalizedPerformerName(value) {
    return String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
  }
  function renderMergeImpact(target, item) {
    const impact = document.createElement('dl'); impact.className = 'performer-proposal-details';
    for (const [label, value] of [
      ['Variant', item.alias], ['Canonical target', item.canonical], ['Videos using variant', item.alias_videos],
      ['Videos using canonical name', item.canonical_videos], ['Other aliases affected', item.aliases_affected],
      ['Variant profile', [item.has_photo ? 'photo' : '', item.has_bio ? 'biography' : ''].filter(Boolean).join(' and ') || 'none'],
      ['Canonical profile', [item.canonical_has_photo ? 'photo' : '', item.canonical_has_bio ? 'biography' : ''].filter(Boolean).join(' and ') || 'none'],
    ]) {
      const field = node('div'); field.append(node('dt', '', label), node('dd', '', value)); impact.append(field);
    }
    target.append(impact);
  }
  async function reviewSelectedMerges() {
    if (!state.fuzzySelected.size || state.fuzzySelected.size > MAX_SELECTED_MERGES) return;
    const selected = [...state.fuzzySelected.entries()];
    const seenNames = new Map();
    const conflicts = [];
    selected.forEach(([, pair], index) => {
      const pairNames = new Map();
      for (const name of [pair.alias, pair.canonical]) {
        const key = normalizedPerformerName(name);
        if (key) pairNames.set(key, name);
      }
      for (const [key, name] of pairNames) {
        if (seenNames.has(key) && seenNames.get(key) !== index) conflicts.push(name);
      }
      for (const key of pairNames.keys()) seenNames.set(key, index);
    });
    const requestSeq = ++state.fuzzyMergeSeq;
    const dialog = $('#fuzzyMergeDialog');
    const content = $('#fuzzyMergeContent');
    const actions = $('#fuzzyMergeActions');
    dialog.addEventListener('close', () => { state.fuzzyMergeSeq += 1; }, { once: true });
    const heading = node('h2', '', 'Review selected merges'); heading.id = 'fuzzyMergeTitle';
    const outcomes = node('div', 'fuzzy-merge-outcomes');
    content.replaceChildren(node('p', 'eyebrow', 'SELECTED IDENTITY REVIEW'), heading,
      node('p', '', `${selected.length} explicit pair${selected.length === 1 ? '' : 's'} selected. Review each current identity, relationship impact, and warning before queueing.`), outcomes);
    actions.replaceChildren();
    const close = node('button', 'button button-quiet', 'Close'); close.type = 'button';
    close.addEventListener('click', () => window.AdminV2.closeDialog(dialog));
    actions.append(close);
    window.AdminV2.openDialog(dialog);
    if (conflicts.length) {
      outcomes.append(node('p', 'state-banner state-error',
        `Selection conflict: more than one selected merge uses the same performer identity (${[...new Set(conflicts)].join(', ')}). Deselect overlapping pairs and review again.`));
      return;
    }
    const prepared = [];
    for (const [pairKey, pair] of selected) {
      if (requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
      const card = node('section', 'operation-history-row fuzzy-merge-review-item');
      card.append(node('strong', '', `${pair.alias} → ${pair.canonical}`),
        node('p', '', `${pair.category} · similarity score ${pair.similarity} · ${pair.variantVideos} variant videos / ${pair.canonicalVideos} canonical videos`));
      if (pair.variantExamples.length) addFilmExamples(card, pair.alias, pair.variantExamples);
      if (pair.canonicalExamples.length) addFilmExamples(card, pair.canonical, pair.canonicalExamples);
      const status = node('p', 'catalog-feedback', 'Refreshing impact preview…'); card.append(status); outcomes.append(card);
      try {
        const preview = await post('/admin/api/v2/operations/preview', {
          type: 'performer_merge', alias: pair.alias, canonical: pair.canonical,
        });
        if (requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
        if (!Array.isArray(preview.items) || preview.items.length !== 1 || typeof preview.plan_id !== 'string') {
          throw new Error('The server did not return one current merge preview.');
        }
        renderMergeImpact(card, preview.items[0]);
        (Array.isArray(preview.warnings) ? preview.warnings : []).forEach(warning => card.append(node('p', 'state-banner state-warning', warning)));
        status.textContent = 'Current impact preview ready.';
        prepared.push({ pairKey, pair, planId: preview.plan_id, status: 'pending' });
      } catch (error) {
        status.className = 'state-banner state-error';
        status.textContent = `Preview failed: ${errorText(error)} This pair will not be queued.`;
      }
    }
    const ready = prepared.length === selected.length;
    const consentLabel = node('label', 'performer-proposal-controls');
    const consent = document.createElement('input'); consent.type = 'checkbox'; consent.disabled = !ready;
    consentLabel.append(consent, node('span', '', 'I reviewed every identity and impact preview. I understand performer merges cannot be undone from Jobs.'));
    content.append(consentLabel);
    const queue = node('button', 'button button-primary', 'Queue selected merges'); queue.type = 'button'; queue.disabled = true;
    consent.addEventListener('change', () => { queue.disabled = !consent.checked || !ready; });
    queue.addEventListener('click', async () => {
      if (!consent.checked || queue.disabled || requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
      queue.disabled = true;
      close.disabled = true;
      const resultList = node('ol', 'fuzzy-merge-results'); outcomes.append(resultList);
      for (const entry of prepared) {
        const result = node('li', '', `${entry.pair.alias} → ${entry.pair.canonical}: queueing…`); resultList.append(result);
        try {
          const queued = await post('/admin/api/v2/operations/execute', { plan_id: entry.planId });
          entry.status = 'queued';
          const jobId = /^[A-Za-z0-9_-]{1,128}$/.test(String(queued?.job_id || '')) ? queued.job_id : '';
          state.fuzzyQueued.set(entry.pairKey, jobId);
          result.textContent = `${entry.pair.alias} → ${entry.pair.canonical}: queued${jobId ? ` · task ${jobId}` : ''}`;
        } catch (error) {
          entry.status = 'failed';
          result.textContent = `${entry.pair.alias} → ${entry.pair.canonical}: outcome could not be confirmed. Check Tasks before retrying. ${errorText(error)}`;
        }
        state.fuzzySelected.delete(entry.pairKey);
      }
      renderFuzzy();
      updateSelectedMergeButton();
      const queuedCount = prepared.filter(entry => entry.status === 'queued').length;
      window.AdminV2.setFeedback($('#fuzzyFeedback'), `${queuedCount} of ${prepared.length} selected performer merges queued. Review individual outcomes and follow them in Tasks.`);
      close.disabled = false;
    });
    actions.append(queue);
  }
  function renderFuzzy() {
    const host = $('#fuzzyRows');
    const rows = fuzzyRows();
    host.replaceChildren();
    const total = rows.length;
    const pageSize = 50;
    const offset = Math.min(state.fuzzyOffset, Math.max(0, total - 1));
    const page = rows.slice(offset, offset + pageSize);
    if (!state.fuzzyScanned) {
      host.append(node('p', 'catalog-empty', 'Run a scan to review possible name variants.'));
    } else if (!state.fuzzyGroups.length) {
      host.append(node('p', 'catalog-empty', 'No likely name variants were found.'));
    } else if (!page.length) {
      host.append(node('p', 'catalog-empty', 'No name variants match this category.'));
    }
    page.forEach(({ group, variant }) => {
      const defaultAlias = variant.name;
      const defaultCanonical = group.canonical;
      const pairKey = `${defaultAlias}::${defaultCanonical}`;
      const isSwapped = Boolean(state.fuzzySwapped.get(pairKey));
      let currentAlias = isSwapped ? defaultCanonical : defaultAlias;
      let currentCanonical = isSwapped ? defaultAlias : defaultCanonical;

      const isQueued = state.fuzzyQueued.has(pairKey) || state.fuzzyQueued.has(`${defaultCanonical}::${defaultAlias}`);
      const jobId = state.fuzzyQueued.get(pairKey) || state.fuzzyQueued.get(`${defaultCanonical}::${defaultAlias}`);

      const row = node('article', isQueued ? 'operation-history-row is-completed' : 'operation-history-row');
      row.dataset.fuzzyPair = pairKey;

      const copy = node('div');
      const titleNode = node('strong', '', `${currentAlias} → ${currentCanonical}`);
      copy.append(titleNode);
      const similarity = Number(group.confidence);
      const similarityScore = Number.isFinite(similarity) ? similarity.toFixed(2) : 'unavailable';
      copy.append(node('p', '', `${group.category_label} · similarity score ${similarityScore} · ${variant.video_count || 0} variant videos / ${group.canonical_video_count || 0} canonical videos`));
      const canonicalSignals = [group.canonical_has_photo ? 'photo' : '', group.canonical_has_bio ? 'biography' : ''].filter(Boolean).join(' and ');
      const variantSignals = [variant.has_photo ? 'photo' : '', variant.has_bio ? 'biography' : ''].filter(Boolean).join(' and ');
      if (canonicalSignals || variantSignals) copy.append(node('p', 'detail-muted', `Existing profile data — canonical: ${canonicalSignals || 'none'}; variant: ${variantSignals || 'none'}.`));
      const examples = node('div', 'performer-proposal-details');
      addFilmExamples(examples, variant.name, Array.isArray(variant.video_examples) ? variant.video_examples : []);
      addFilmExamples(examples, group.canonical, Array.isArray(group.canonical_video_examples) ? group.canonical_video_examples : []);

      const actions = node('div', 'performer-proposal-actions');

      const selectionLabel = node('label', 'quality-select');
      const selection = document.createElement('input'); selection.type = 'checkbox';
      selection.checked = state.fuzzySelected.has(pairKey);
      selection.disabled = isQueued || (!selection.checked && state.fuzzySelected.size >= MAX_SELECTED_MERGES);
      selection.setAttribute('aria-label', `Select merge ${currentAlias} into ${currentCanonical}`);
      selection.addEventListener('change', () => {
        if (selection.checked) {
          state.fuzzySelected.set(pairKey, {
            alias: currentAlias, canonical: currentCanonical, category: group.category_label,
            similarity: similarityScore, variantVideos: variant.video_count || 0,
            canonicalVideos: group.canonical_video_count || 0,
            variantExamples: Array.isArray(variant.video_examples) ? variant.video_examples : [],
            canonicalExamples: Array.isArray(group.canonical_video_examples) ? group.canonical_video_examples : [],
          });
        } else state.fuzzySelected.delete(pairKey);
        updateSelectedMergeButton();
        renderFuzzy();
      });
      selectionLabel.append(selection, node('span', '', 'Select for merge review'));
      actions.append(selectionLabel);

      const review = node('button', 'button button-quiet', 'Review merge');
      review.type = 'button';
      review.addEventListener('click', () => reviewFuzzyMerge(currentAlias, currentCanonical));

      if (isQueued) {
        actions.append(review, node('span', 'status-pill pill-good', `✓ Queued (${jobId || 'job'})`));
      } else {
        const swap = node('button', 'button button-quiet', '⇄ Swap');
        swap.type = 'button';
        swap.title = 'Swap merge direction';
        if (isSwapped) swap.className = 'button button-quiet is-selected';
        swap.addEventListener('click', () => {
          const newSwapped = !state.fuzzySwapped.get(pairKey);
          state.fuzzySwapped.set(pairKey, newSwapped);
          currentAlias = newSwapped ? defaultCanonical : defaultAlias;
          currentCanonical = newSwapped ? defaultAlias : defaultCanonical;
          titleNode.textContent = `${currentAlias} → ${currentCanonical}`;
          if (selection.checked) state.fuzzySelected.set(pairKey, {
            alias: currentAlias, canonical: currentCanonical, category: group.category_label,
            similarity: similarityScore, variantVideos: variant.video_count || 0,
            canonicalVideos: group.canonical_video_count || 0,
            variantExamples: Array.isArray(variant.video_examples) ? variant.video_examples : [],
            canonicalExamples: Array.isArray(group.canonical_video_examples) ? group.canonical_video_examples : [],
          });
          swap.className = newSwapped ? 'button button-quiet is-selected' : 'button button-quiet';
        });

        const dismiss = node('button', 'button button-quiet', 'Dismiss');
        dismiss.type = 'button';
        dismiss.title = 'Dismiss suggestion';
        dismiss.addEventListener('click', () => {
          state.fuzzyDismissed.add(pairKey);
          state.fuzzySelected.delete(pairKey);
          renderFuzzy();
          window.AdminV2.setFeedback($('#fuzzyFeedback'), `Dismissed "${currentAlias} ↔ ${currentCanonical}".`);
        });

        actions.append(review, swap, dismiss);
      }

      row.append(copy, examples, actions);
      host.append(row);
    });
    const pagination = $('#fuzzyPagination');
    pagination.hidden = total <= pageSize;
    $('#fuzzyPageInfo').textContent = total ? `Showing ${offset + 1}–${Math.min(offset + pageSize, total)} of ${total}` : 'No suggestions';
    $('#fuzzyPrev').disabled = offset <= 0;
    $('#fuzzyNext').disabled = offset + pageSize >= total;
    state.fuzzyOffset = offset;
    updateSelectedMergeButton();
  }
  async function scanFuzzy() {
    if (state.fuzzyLoading) return;
    const threshold = Number($('#fuzzyThreshold').value);
    if (!Number.isFinite(threshold) || threshold < 0.80 || threshold > 0.99) {
      window.AdminV2.setFeedback($('#fuzzyFeedback'), 'Choose a similarity threshold from 0.80 to 0.99.', 'error');
      $('#fuzzyThreshold').focus();
      return;
    }
    state.fuzzyLoading = true;
    const button = $('#fuzzyScan'); button.disabled = true;
    window.AdminV2.setFeedback($('#fuzzyFeedback'), 'Scanning performer names…');
    try {
      const result = await get(`${API}/fuzzy?threshold=${encodeURIComponent(threshold)}`);
      state.fuzzyGroups = Array.isArray(result.groups) ? result.groups : [];
      state.fuzzyScanned = true;
      state.fuzzyOffset = 0;
      state.fuzzyQueued.clear();
      state.fuzzyDismissed.clear();
      state.fuzzySwapped.clear();
      state.fuzzySelected.clear();
      const stats = result.stats || {};
      window.AdminV2.setFeedback($('#fuzzyFeedback'), `${stats.total_unique_performers ?? '—'} unique performer names scanned; ${stats.total_groups ?? state.fuzzyGroups.length} candidate groups found. Review both names before merging.`);
      renderFuzzy();
    } catch (error) {
      state.fuzzyGroups = [];
      state.fuzzyScanned = false;
      state.fuzzyQueued.clear();
      state.fuzzyDismissed.clear();
      state.fuzzySwapped.clear();
      state.fuzzySelected.clear();
      renderFuzzy();
      window.AdminV2.setFeedback($('#fuzzyFeedback'), `Could not scan performer names. ${errorText(error)}`, 'error');
    } finally { state.fuzzyLoading = false; button.disabled = false; }
  }
  async function reviewFuzzyMerge(alias, canonical) {
    const requestSeq = ++state.fuzzyMergeSeq;
    const dialog = $('#fuzzyMergeDialog');
    const content = $('#fuzzyMergeContent');
    const actions = $('#fuzzyMergeActions');
    dialog.addEventListener('close', () => { state.fuzzyMergeSeq += 1; }, { once: true });
    const heading = node('h2', '', 'Review name merge'); heading.id = 'fuzzyMergeTitle';
    content.replaceChildren(node('p', 'eyebrow', 'MERGE PREVIEW'), heading, node('p', '', `${alias} will be merged into ${canonical}.`), node('p', 'catalog-feedback', 'Preparing a current impact preview…'));
    actions.replaceChildren();
    const close = node('button', 'button button-quiet', 'Close'); close.type = 'button'; close.addEventListener('click', () => window.AdminV2.closeDialog(dialog));
    actions.append(close);
    window.AdminV2.openDialog(dialog);
    try {
      const preview = await post('/admin/api/v2/operations/preview', { type: 'performer_merge', alias, canonical });
      if (requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
      if (!Array.isArray(preview.items) || !preview.items.length || typeof preview.plan_id !== 'string') throw new Error('The server returned an invalid merge preview.');
      const item = preview.items[0];
      const previewHeading = node('h2', '', 'Review name merge'); previewHeading.id = 'fuzzyMergeTitle';
      content.replaceChildren(node('p', 'eyebrow', 'MERGE PREVIEW'), previewHeading);
      const impact = document.createElement('dl'); impact.className = 'performer-proposal-details';
      for (const [label, value] of [
        ['Variant', item.alias], ['Canonical name', item.canonical], ['Videos using variant', item.alias_videos],
        ['Videos using canonical name', item.canonical_videos], ['Other aliases affected', item.aliases_affected],
        ['Variant profile', [item.has_photo ? 'photo' : '', item.has_bio ? 'biography' : ''].filter(Boolean).join(' and ') || 'none'],
        ['Canonical profile', [item.canonical_has_photo ? 'photo' : '', item.canonical_has_bio ? 'biography' : ''].filter(Boolean).join(' and ') || 'none'],
      ]) {
        const field = node('div'); field.append(node('dt', '', label), node('dd', '', value)); impact.append(field);
      }
      content.append(impact);
      (preview.warnings || []).forEach(warning => content.append(node('p', 'state-banner state-warning', warning)));
      content.append(node('p', 'catalog-feedback', 'The merge runs as a background job. There is no one-click undo for performer merges. Review the current backup and impact before queueing.'));
      const consentLabel = node('label', 'performer-proposal-controls');
      const consent = document.createElement('input'); consent.type = 'checkbox';
      consentLabel.append(consent, node('span', '', 'I reviewed the impact and understand the merge cannot be undone from Jobs.'));
      content.append(consentLabel);
      const queue = node('button', 'button button-primary', 'Queue merge'); queue.type = 'button'; queue.disabled = true;
      consent.addEventListener('change', () => { queue.disabled = !consent.checked; });
      queue.addEventListener('click', async () => {
        if (!consent.checked || queue.disabled || requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
        queue.disabled = true;
        try {
          const result = await post('/admin/api/v2/operations/execute', { plan_id: preview.plan_id });
          if (requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
          state.fuzzyQueued.set(`${alias}::${canonical}`, result.job_id);
          state.fuzzySelected.delete(`${alias}::${canonical}`);
          state.fuzzySelected.delete(`${canonical}::${alias}`);
          renderFuzzy();
          updateSelectedMergeButton();
          content.append(node('p', 'catalog-feedback', `Merge queued as job ${result.job_id}. The candidate remains listed until the job completes; check Jobs, then scan again.`));
          const jobs = node('a', 'button button-quiet', 'Open Jobs'); jobs.href = '#jobs';
          actions.replaceChildren(close, jobs);
          window.AdminV2.setFeedback($('#fuzzyFeedback'), `Merge queued as job ${result.job_id}.`);
        } catch (error) {
          if (requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
          content.append(node('p', 'state-banner state-error', `Could not queue merge. ${errorText(error)}`));
          queue.disabled = false;
        }
      });
      actions.append(queue);
    } catch (error) {
      if (requestSeq !== state.fuzzyMergeSeq || !dialog.open) return;
      const errorHeading = node('h2', '', 'Review name merge'); errorHeading.id = 'fuzzyMergeTitle';
      content.replaceChildren(node('p', 'eyebrow', 'MERGE PREVIEW'), errorHeading, node('p', 'state-banner state-error', `Could not create a current merge preview. ${errorText(error)}`));
    }
  }
  $('#performersRefresh')?.addEventListener('click', load);
  $('#performersReconcile')?.addEventListener('click', reconcileClearMatches);
  $('#fuzzyScan')?.addEventListener('click', scanFuzzy);
  $('#fuzzyReviewSelected')?.addEventListener('click', reviewSelectedMerges);
  $('#fuzzyCategory')?.addEventListener('change', () => { state.fuzzyOffset = 0; renderFuzzy(); });
  $('#fuzzyPrev')?.addEventListener('click', () => { state.fuzzyOffset = Math.max(0, state.fuzzyOffset - 50); renderFuzzy(); });
  $('#fuzzyNext')?.addEventListener('click', () => { state.fuzzyOffset += 50; renderFuzzy(); });
  $('#fuzzyMergeClose')?.addEventListener('click', () => window.AdminV2.closeDialog($('#fuzzyMergeDialog')));
  document.addEventListener('adminv2:navigate', event => { if (event.detail?.route === 'performers') load(); });
})();
