(() => {
  'use strict';
  // Admin V2 duplicate candidates, visual duplicate results, and OSHash links.
  const AV = window.AdminV2;
  const { $, DUPLICATE_PAGE_SIZE, DUPLICATE_SCAN_BATCH_SIZE, DUPLICATE_SCAN_CACHE_TTL_MS, V2_API, announce,
    appendTelegramId, create, duplicateGroupIsVisible, getJson, postJson, responseError, setText,
    showQueued, state } = AV;
  const dismissDuplicateSuggestion = (...args) => AV.dismissDuplicateSuggestion(...args);
  const openSideDetail = (...args) => AV.openSideDetail(...args);
  const previewDuplicate = (...args) => AV.previewDuplicate(...args);
  async function loadDuplicateCandidates({ refresh = false, cacheOnly = false } = {}) {
    if (!duplicateGroupIsVisible('catalog')) return;
    const sequence = ++state.duplicateRequestSeq;
    const category = $('#duplicateCategory').value;
    if (refresh) {
      state.duplicateScans = [];
      state.duplicateRequests.clear();
    }
    const scanOffset = category === 'oshash_linked'
      ? state.duplicateOffset
      : Math.floor(state.duplicateOffset / DUPLICATE_SCAN_BATCH_SIZE) * DUPLICATE_SCAN_BATCH_SIZE;
    let scan = state.duplicateScans.find(item => item.category === category && item.offset === scanOffset);
    if (scan && Date.now() - scan.fetchedAt > DUPLICATE_SCAN_CACHE_TTL_MS) {
      state.duplicateScans = [];
      scan = null;
    }
    const feedback = $('#duplicateFeedback'); const rows = $('#duplicateRows');
    $('#duplicateNotice').hidden = true;
    if (scan && scan.category === category && scan.offset === scanOffset) {
      state.duplicateScans = state.duplicateScans.filter(item => item !== scan);
      state.duplicateScans.push(scan);
      state.duplicateItems = scan.pairs.slice(state.duplicateOffset - scanOffset,
        state.duplicateOffset - scanOffset + DUPLICATE_PAGE_SIZE);
      state.duplicateTotal = scan.total;
      renderDuplicateCandidates(scan);
      setText(feedback, category === 'oshash_linked'
        ? `${state.duplicateTotal} active OSHASH-linked groups`
        : `${state.duplicateTotal} candidates · ${Number(scan.scanned) || 0} catalog items scanned`);
      if (category !== 'oshash_linked' && (scan.candidate_limit_reached || scan.complete === false)) {
        $('#duplicateNotice').hidden = false;
        $('#duplicateNotice').textContent = 'The candidate scan reached its safety limit. This is a partial review set; narrow the catalog or refresh later.';
      }
      state.duplicateBusy = false;
      return;
    }
    if (cacheOnly && category !== 'oshash_linked') {
      setText(feedback, 'Choose Scan catalog to find duplicate candidates. Scans run only when requested.');
      return;
    }
    state.duplicateBusy = true;
    setText(feedback, category === 'oshash_linked' ? 'Loading OSHASH-linked videos…' : 'Scanning duplicate candidates…');
    const requestKey = `${category}:${scanOffset}`;
    let pendingRequest = state.duplicateRequests.get(requestKey);
    try {
      if (!pendingRequest) {
        const linked = category === 'oshash_linked';
        const params = new URLSearchParams({ limit: String(linked ? DUPLICATE_PAGE_SIZE : DUPLICATE_SCAN_BATCH_SIZE), offset: String(scanOffset) });
        if (category && !linked) params.set('category', category);
        // Identical Telegram files are hidden by the automatic reconcile pass, so the
        // unfiltered view skips them; picking "Same file candidates" lists them again.
        if (!linked && !category) params.set('include_same_telegram_file', 'false');
        const endpoint = linked ? '/duplicates/linked' : '/duplicates';
        pendingRequest = getJson(`${V2_API}${endpoint}?${params}`).then(payload => ({ payload }));
        state.duplicateRequests.set(requestKey, pendingRequest);
      }
      const { payload } = await pendingRequest;
      if (state.duplicateRequests.get(requestKey) === pendingRequest) state.duplicateRequests.delete(requestKey);
      if (sequence !== state.duplicateRequestSeq || !duplicateGroupIsVisible('catalog')) return;
      if (!payload?.ok) throw new Error(responseError(payload, 'Could not scan duplicates.'));
      const total = Number(payload.total) || 0;
      if (total === 0) state.duplicateOffset = 0;
      else if (state.duplicateOffset >= total) {
        state.duplicateOffset = Math.floor((total - 1) / DUPLICATE_PAGE_SIZE) * DUPLICATE_PAGE_SIZE;
      }
      const effectiveScanOffset = category === 'oshash_linked'
        ? state.duplicateOffset
        : Math.floor(state.duplicateOffset / DUPLICATE_SCAN_BATCH_SIZE) * DUPLICATE_SCAN_BATCH_SIZE;
      if (total > 0 && effectiveScanOffset !== scanOffset) {
        await loadDuplicateCandidates({ refresh: true });
        return;
      }
      scan = {
        ...payload,
        category, offset: total === 0 ? 0 : scanOffset,
        pairs: (Array.isArray(payload.pairs) ? payload.pairs : Array.isArray(payload.groups) ? payload.groups : [])
          .map(pair => ({ ...pair, rules_version: pair.rules_version ?? payload.rules_version })),
        total,
        scanned: payload.scanned,
        complete: payload.complete,
        candidate_limit_reached: payload.candidate_limit_reached,
        fetchedAt: Date.now(),
      };
      state.duplicateScans = state.duplicateScans.filter(item => !(item.category === category && item.offset === scanOffset));
      state.duplicateScans.push(scan);
      if (state.duplicateScans.length > 3) state.duplicateScans.shift();
      state.duplicateItems = scan.pairs.slice(state.duplicateOffset - scanOffset,
        state.duplicateOffset - scanOffset + DUPLICATE_PAGE_SIZE);
      state.duplicateTotal = scan.total;
      renderDuplicateCandidates(scan);
      setText(feedback, category === 'oshash_linked'
        ? `${state.duplicateTotal} active OSHASH-linked groups`
        : `${state.duplicateTotal} candidates · ${Number(payload.scanned) || 0} catalog items scanned`);
      if (category !== 'oshash_linked' && (payload.candidate_limit_reached || payload.complete === false)) {
        $('#duplicateNotice').hidden = false;
        $('#duplicateNotice').textContent = 'The candidate scan reached its safety limit. This is a partial review set; narrow the catalog or refresh later.';
      }
    } catch (error) {
      if (state.duplicateRequests.get(requestKey) === pendingRequest) state.duplicateRequests.delete(requestKey);
      if (sequence !== state.duplicateRequestSeq || !duplicateGroupIsVisible('catalog')) return;
      rows.replaceChildren(); setText(feedback, error.message);
      $('#duplicateState').hidden = false; $('#duplicateState').textContent = 'Duplicate suggestions are unavailable. Refresh to try again.';
    } finally { if (sequence === state.duplicateRequestSeq) state.duplicateBusy = false; }
  }
  async function loadVisualDuplicateResults() {
    const host = $('#visualDuplicateRows');
    if (!host || !duplicateGroupIsVisible('visual')) return;
    const sequence = ++state.visualRequestSeq;
    const status = $('#visualDuplicateStatus');
    try {
      const payload = await getJson(`${V2_API}/duplicates/visual?limit=50`);
      if (sequence !== state.visualRequestSeq || !duplicateGroupIsVisible('visual')) return;
      if (!payload?.ok) throw new Error(responseError(payload, 'Could not load visual comparisons.'));
      const visualState = payload.state || {};
      const confirmedAt = Number(visualState.automatic_enabled_at || 0);
      const progress = `${payload.queue?.signatures_ready || 0}/${payload.queue?.signatures_total || 0} frame signatures ready · ${payload.queue?.signatures_pending || 0} queued`;
      setText(status, confirmedAt
        ? `Automatic merging is active. ${payload.queue?.confirmed || 0} confirmed · ${payload.queue?.review || 0} need review. ${progress}.`
        : `Automatic merging will activate on the next reconciliation run. ${payload.queue?.confirmed || 0} confirmed · ${payload.queue?.review || 0} need review · ${payload.queue?.queued || 0} comparisons queued. ${progress}.`);
      host.replaceChildren();
      for (const item of payload.items || []) {
        const card = create('article', 'duplicate-candidate-card');
        const heading = create('h3', '', `${item.state}: ${item.left_title || item.left_id} ↔ ${item.right_title || item.right_id}`);
        card.append(heading);
        const left = { id: item.left_id, title: item.left_title || item.left_id };
        const right = { id: item.right_id, title: item.right_title || item.right_id };
        const actions = create('div', 'visual-comparison-actions');
        for (const [side, video, label] of [['left', left, 'Left item'], ['right', right, 'Right item']]) {
          const details = create('button', 'button button-quiet', `Open ${label} details`);
          details.type = 'button';
          details.addEventListener('click', () => openSideDetail(video.id));
          actions.append(details);
          const keep = create('button', 'button button-primary', `Keep ${label.toLowerCase()} & hide other`);
          keep.type = 'button';
          keep.addEventListener('click', () => previewDuplicate({ left, right }, side, keep));
          actions.append(keep);
        }
        card.append(actions);
        appendTelegramId(card, left);
        appendTelegramId(card, right);
        const feedback = create('p', 'duplicate-action-feedback');
        feedback.hidden = true;
        feedback.setAttribute('role', 'status');
        card.append(feedback);
        const different = create('button', 'button button-quiet', 'Different videos');
        different.type = 'button';
        different.addEventListener('click', () => dismissDuplicateSuggestion({ left, right }, different, feedback));
        actions.append(different);
        const sheet = document.createElement('img');
        sheet.className = 'visual-comparison-sheet';
        sheet.alt = `25-frame comparison: ${item.left_title || item.left_id} and ${item.right_title || item.right_id}`;
        sheet.loading = 'lazy';
        sheet.src = `${V2_API}/duplicates/visual/${encodeURIComponent(item.id)}/sheet`;
        sheet.addEventListener('error', () => sheet.remove(), { once: true });
        card.append(sheet);
        card.append(create('p', 'detail-muted', `${Number(item.left_duration || 0).toFixed(1)} s · ${Number(item.right_duration || 0).toFixed(1)} s`));
        const evidence = item.evidence || {};
        const strictMatches = Number(evidence.strict_matches ?? evidence.matches?.filter(match => match.match).length) || 0;
        const looseMatches = Number(evidence.loose_matches ?? evidence.matches?.filter(match => match.loose).length) || 0;
        const informativeFrames = Number(evidence.informative_frames) || 0;
        const medianSsim = Number(evidence.median_ssim);
        const offset = Number(evidence.offset_fit?.intercept);
        const residual = Number(evidence.offset_fit?.max_residual_seconds);
        const metrics = [`${strictMatches}/${informativeFrames} strict matches`, `${looseMatches} loose`];
        if (Number.isFinite(medianSsim) && medianSsim > 0) metrics.push(`median SSIM ${medianSsim.toFixed(3)}`);
        if (Number.isFinite(offset)) metrics.push(`offset ${offset >= 0 ? '+' : ''}${offset.toFixed(2)} s`);
        if (Number.isFinite(residual) && residual < 90) metrics.push(`offset residual ${residual.toFixed(2)} s`);
        card.append(create('p', 'detail-muted', metrics.join(' · ')));
        if (evidence.sections) card.append(create('p', 'detail-muted', `Timeline coverage: ${Object.entries(evidence.sections).filter(([, found]) => found).map(([key]) => key).join(', ') || 'none'}`));
        if (item.state === 'review' || item.state === 'inconclusive') {
          const button = create('button', 'button button-quiet', 'Re-run visual comparison');
          button.type = 'button';
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              const result = await postJson(`${V2_API}/duplicates/visual/verify`, { left_id: item.left_id, right_id: item.right_id });
              if (!result?.ok) throw new Error(responseError(result, 'Could not queue comparison.'));
              if (duplicateGroupIsVisible('visual') && card.isConnected) showQueued(result, 'Visual comparison', undefined, 'visual');
              setTimeout(loadVisualDuplicateResults, 1500);
            } catch (error) { announce(error.message); }
            finally { button.disabled = false; }
          });
          card.append(button);
        }
        host.append(card);
      }
      if (!payload.items?.length) host.append(create('p', 'detail-muted', 'No visual comparisons are ready yet. Signatures and candidate checks run in the background.'));
    } catch (error) {
      if (sequence === state.visualRequestSeq && duplicateGroupIsVisible('visual')) setText(status, error.message);
    }
  }
  function appendDuplicateFingerprints(host, video) {
    const hashes = create('p', 'duplicate-fingerprints');
    hashes.append(document.createTextNode('OSHASH: '));
    hashes.append(video?.oshash ? create('code', '', video.oshash) : document.createTextNode('Not available'));
    hashes.append(document.createTextNode(' · PHASH: '));
    hashes.append(video?.phash ? create('code', '', video.phash) : document.createTextNode('Not calculated (subset)'));
    host.append(hashes);
  }
  function renderDuplicateCandidates() {
    const rows = $('#duplicateRows'); rows.replaceChildren();
    $('#duplicateState').hidden = state.duplicateItems.length > 0;
    if (!state.duplicateItems.length) $('#duplicateState').textContent = $('#duplicateCategory').value === 'oshash_linked'
      ? 'No active OSHASH-linked videos.' : 'No active duplicate candidates match this filter.';
    for (const pair of state.duplicateItems) {
      const card = create('article', 'duplicate-candidate');
      if ($('#duplicateCategory').value === 'oshash_linked') {
        const heading = create('div', 'duplicate-candidate-heading');
        heading.append(create('span', 'status-pill pill-neutral', 'Linked by OSHASH'),
          create('span', '', `${pair.losers.length} duplicate${pair.losers.length === 1 ? '' : 's'}`));
        card.append(heading);
        const hashLine = create('p', 'oshash-link-evidence', 'Matching OSHASH: ');
        for (const [index, hash] of (pair.oshashes || []).entries()) {
          if (index) hashLine.append(document.createTextNode(', '));
          hashLine.append(create('code', '', hash));
        }
        card.append(hashLine);
        const compare = create('div', 'duplicate-compare-grid');
        for (const [label, video] of [['Kept in catalog', pair.keeper], ...pair.losers.map(video => ['Linked duplicate', video])]) {
          const item = create('section', 'duplicate-compare-item');
          const artwork = create('div', 'duplicate-thumbnail');
          const showMissingArtwork = () => artwork.replaceChildren(create('span', 'duplicate-thumbnail-placeholder', 'No thumbnail available'));
          if (video?.thumbnail) {
            const image = document.createElement('img'); image.src = video.thumbnail; image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
            image.onerror = showMissingArtwork; artwork.append(image);
          } else showMissingArtwork();
          item.append(artwork, create('span', 'status-pill pill-neutral', label),
            create('h3', '', video?.title || 'Untitled'),
            create('p', '', `${video?.provider || 'Unknown source'} · ${video?.duration ? `${video.duration}s` : 'Duration unknown'}`));
          appendTelegramId(item, video);
          if (video?.studio) item.append(create('p', '', `Studio: ${video.studio}`));
          if (Array.isArray(video?.performers) && video.performers.length) item.append(create('p', '', `Cast: ${video.performers.join(', ')}`));
          appendDuplicateFingerprints(item, video);
          const open = create('button', 'button button-quiet duplicate-open-details', 'Open item details →');
          open.type = 'button'; open.disabled = !video?.id; open.addEventListener('click', () => openSideDetail(video?.id)); item.append(open);
          compare.append(item);
        }
        card.append(compare);
        const feedback = create('p', 'duplicate-action-feedback'); feedback.hidden = true; feedback.setAttribute('role', 'status');
        const undo = create('button', 'button button-quiet', `Undo link${pair.operation_size > 1 ? ` (${pair.operation_size} items)` : ''}`);
        undo.type = 'button'; undo.addEventListener('click', () => undoOshashLink(pair, undo, feedback));
        card.append(undo, feedback);
        rows.append(card);
        continue;
      }
      const heading = create('div', 'duplicate-candidate-heading');
      heading.append(create('span', 'status-pill pill-neutral', pair.category || 'Suggestion'),
        create('strong', '', `${Math.round(Number(pair.score) || 0)} / ${Number(pair.score_max) || 100}`));
      card.append(heading);
      const compare = create('div', 'duplicate-compare-grid');
      for (const [side, video] of [['left', pair.left], ['right', pair.right]]) {
        const item = create('section', 'duplicate-compare-item');
        const artwork = create('div', 'duplicate-thumbnail');
        const showMissingArtwork = () => artwork.replaceChildren(create('span', 'duplicate-thumbnail-placeholder', 'No thumbnail available'));
        if (video?.thumbnail) {
          const image = document.createElement('img'); image.src = video.thumbnail; image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
          image.onerror = showMissingArtwork;
          artwork.append(image);
        } else showMissingArtwork();
        item.append(artwork);
        const title = create('h3', '', video?.title || 'Untitled');
        item.append(title, create('p', '', `${video?.provider || 'Unknown source'} · ${video?.duration ? `${video.duration}s` : 'Duration unknown'}`));
        appendTelegramId(item, video);
        if (video?.studio) item.append(create('p', '', `Studio: ${video.studio}`));
        if (Array.isArray(video?.performers) && video.performers.length) item.append(create('p', '', `Cast: ${video.performers.join(', ')}`));
        appendDuplicateFingerprints(item, video);
        const open = create('button', 'button button-quiet duplicate-open-details', 'Open item details →');
        open.type = 'button'; open.disabled = !video?.id; open.addEventListener('click', () => openSideDetail(video?.id)); item.append(open);
        const keep = create('button', 'button button-quiet', 'Keep & hide other'); keep.type = 'button'; keep.addEventListener('click', () => previewDuplicate(pair, side, keep)); item.append(keep);
        compare.append(item);
      }
      card.append(compare);
      const reasons = Array.isArray(pair.reasons) ? pair.reasons : [];
      const reasonList = create('ul', 'duplicate-reasons'); reasons.forEach(reason => reasonList.append(create('li', '', reason)));
      if (reasons.length) card.append(reasonList);
      const actionFeedback = create('p', 'duplicate-action-feedback');
      actionFeedback.hidden = true;
      actionFeedback.setAttribute('role', 'status');
      actionFeedback.setAttribute('aria-live', 'polite');
      card.append(actionFeedback);
      const dismiss = create('button', 'button button-quiet', 'Dismiss suggestion'); dismiss.type = 'button';
      dismiss.addEventListener('click', () => dismissDuplicateSuggestion(pair, dismiss, actionFeedback)); card.append(dismiss);
      rows.append(card);
    }
    const start = state.duplicateOffset + 1; const end = Math.min(state.duplicateOffset + DUPLICATE_PAGE_SIZE, state.duplicateTotal);
    $('#duplicatePageInfo').textContent = state.duplicateTotal ? `${start}–${end} of ${state.duplicateTotal}` : 'No candidates';
    $('#duplicatePrev').disabled = state.duplicateOffset <= 0;
    $('#duplicateNext').disabled = state.duplicateOffset + DUPLICATE_PAGE_SIZE >= state.duplicateTotal;
    $('#duplicatePagination').hidden = state.duplicateTotal <= DUPLICATE_PAGE_SIZE;
  }
  async function undoOshashLink(group, button, feedback) {
    if (!group?.operation_id || button.disabled) return;
    const itemCount = Number(group.operation_size) || group.losers?.length || 1;
    if (!window.confirm(`Undo this OSHASH link operation and restore ${itemCount} video${itemCount === 1 ? '' : 's'}?`)) return;
    button.disabled = true;
    button.textContent = 'Queuing undo…';
    feedback.hidden = false;
    feedback.className = 'duplicate-action-feedback is-pending';
    feedback.textContent = 'Queueing the reversible restore operation…';
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/duplicates/actions`, {
        action: 'undo', operation_id: group.operation_id,
      });
      const jobId = String(result.job_id || '');
      feedback.className = 'duplicate-action-feedback is-queued';
      feedback.textContent = `Undo queued${jobId ? ` as task ${jobId}` : ''}. The linked items will return after the task completes.`;
      showQueued(result, 'Undo duplicate link');
      button.textContent = 'Undo queued';
      announce(feedback.textContent);
    } catch (error) {
      button.disabled = false;
      button.textContent = `Undo link${itemCount > 1 ? ` (${itemCount} items)` : ''}`;
      feedback.className = 'duplicate-action-feedback is-error';
      feedback.textContent = `Could not queue undo. ${error.message}`;
      announce(error.message);
    }
  }

  Object.assign(AV, { loadDuplicateCandidates, loadVisualDuplicateResults });
})();
