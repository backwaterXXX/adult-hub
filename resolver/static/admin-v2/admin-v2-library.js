(() => {
  'use strict';
  // Admin V2 library catalog: video list, selection, operation previews, availability checks, and operation status.
  const AV = window.AdminV2;
  const { $, $$, LIMIT, V2_API, announce, beginOperationRequest, closeDialog, create, filterParams, first,
    getJson, openDialog, operationRequestIsCurrent, renderLocation, requestJson, resetCatalogPage,
    responseError, safeText, setText, showQueued, state, taskLink, titleCase, urlFor, writeFilterUrl } = AV;
  const loadDuplicateCandidates = (...args) => AV.loadDuplicateCandidates(...args);
  const loadQuarantine = (...args) => AV.loadQuarantine(...args);
  function setLibraryState(kind, title, description) {
    const node = $('#libraryState'); node.hidden = false; node.className = `catalog-state${kind === 'error' ? ' state-error' : ''}`; node.replaceChildren();
    node.append(create('span', 'empty-icon', kind === 'loading' ? '◌' : kind === 'error' ? '!' : kind === 'empty' ? '⌕' : '▤'), create('strong', '', title), create('span', '', description));
    $('#videoGrid').hidden = true; $('#videoTableWrap').hidden = true; $('#pagination').hidden = true;
  }
  function setLibrarySummary(kind, text) { const node = $('#librarySummary'); node.dataset.state = kind; node.replaceChildren(create('span', 'summary-dot'), create('span', '', text)); }
  function displayTime(value) {
    if (!value) return '—';
    const dateValue = typeof value === 'number' && value < 100000000000 ? value * 1000 : value;
    const date = new Date(dateValue);
    if (Number.isNaN(date.getTime())) return safeText(value);
    return isoDate(date);
  }
  function isoDate(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }
  function availability(video) {
    const raw = first(video, ['availability', 'availability_status', 'playback_status', 'status', 'playable']);
    if (raw === true) return ['Available', 'pill-good'];
    if (raw === false) return ['Unavailable', 'pill-danger'];
    const value = raw === null || raw === undefined ? '' : String(raw).toLowerCase();
    if (['available', 'playable', 'ok', 'active', '1'].includes(value)) return ['Available', 'pill-good'];
    if (['unavailable', 'dead', 'failed', 'missing', '0'].includes(value)) return ['Unavailable', 'pill-danger'];
    return ['Unknown', 'pill-warning'];
  }
  function unavailableReason(video) {
    if (availability(video)[0] !== 'Unavailable') return '';
    return safeText(first(video, ['unavailable_reason']), video.playable === 0 ? 'Marked unplayable.' : 'Unavailable stream recorded.');
  }
  function itemId(video) { return safeText(first(video, ['id', 'video_id', 'videoId', 'key']), ''); }
  function thumbnail(video) {
    const value = first(video, ['thumbnail_url', 'artwork_url', 'poster_url', 'thumbnail']);
    if (!value || typeof value !== 'string') return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return null;
      return url.href;
    } catch { return null; }
  }
  function openDetail(id, opener) {
    if (!id) return;
    state.detailOpener = opener?.isConnected ? opener : null;
    // History state must be structured-cloneable; DOM nodes cannot be stored here.
    const stateData = { detailOpener: Boolean(opener) };
    history.pushState(stateData, '', urlFor(`film/${encodeURIComponent(id)}`));
    renderLocation();
  }
  function cardFor(video) {
    const id = itemId(video);
    const card = create('article', 'video-card'); card.dataset.videoId = id;
    const selectLabel = create('label', 'video-card-select');
    const select = create('input'); select.type = 'checkbox'; select.checked = state.selectedVideos.has(id);
    select.setAttribute('aria-label', `Select ${safeText(first(video, ['title', 'name']), 'video')} for bulk actions`);
    select.addEventListener('change', () => setVideoSelected(video, select.checked));
    selectLabel.append(select, create('span', '', 'Select'));
    const button = create('button', 'video-open'); button.type = 'button'; button.setAttribute('aria-label', `Open details: ${safeText(first(video, ['title', 'name']), 'Untitled video')}`);
    button.style.display = 'block'; button.style.width = '100%'; button.style.padding = '0'; button.style.border = '0'; button.style.color = 'inherit'; button.style.textAlign = 'left'; button.style.background = 'transparent';
    const thumb = create('div', 'video-thumb'); const imageUrl = thumbnail(video);
    if (imageUrl) { const image = document.createElement('img'); image.src = imageUrl; image.alt = ''; image.loading = 'lazy'; image.onerror = () => { image.remove(); thumb.append(create('span', 'video-thumb-placeholder', '▤')); }; thumb.append(image); }
    else thumb.append(create('span', 'video-thumb-placeholder', '▤'));
    const content = create('div', 'video-card-content'); content.append(create('span', 'video-title', safeText(first(video, ['title', 'name']), 'Untitled')));
    const meta = create('div', 'video-meta'); meta.append(create('span', '', safeText(first(video, ['studio', 'studio_name']), 'Unknown studio')));
    const [availabilityLabel, availabilityClass] = availability(video); meta.append(create('span', `status-pill ${availabilityClass}`, availabilityLabel));
    if (video.scene_match_source) meta.append(create('span', 'status-pill pill-good', 'StashDB'));
    const reason = unavailableReason(video);
    if (reason) meta.append(create('small', 'video-availability-reason', `${reason}${video.unavailable_detected_at ? ` · Detected ${displayTime(video.unavailable_detected_at)}` : ''}`));
    content.append(meta); button.append(thumb, content); button.addEventListener('click', () => openDetail(id, button)); card.append(selectLabel, button); return card;
  }
  function tableRow(video) {
    const row = create('tr'); row.dataset.videoId = itemId(video);
    const selectCell = create('td'); const select = create('input'); select.type = 'checkbox';
    select.checked = state.selectedVideos.has(itemId(video));
    select.setAttribute('aria-label', `Select ${safeText(first(video, ['title', 'name']), 'video')} for bulk actions`);
    select.addEventListener('change', () => setVideoSelected(video, select.checked)); selectCell.append(select); row.append(selectCell);
    const titleCell = create('td'); const open = create('button', 'table-title-button', safeText(first(video, ['title', 'name']), 'Untitled')); open.type = 'button'; open.addEventListener('click', () => openDetail(itemId(video), open)); titleCell.append(open); row.append(titleCell);
    row.append(create('td', '', safeText(first(video, ['studio', 'studio_name']))));
    row.append(create('td', '', safeText(first(video, ['channel_name', 'channel_id', 'source_name', 'provider']))));
    const availabilityCell = create('td'); const [label, css] = availability(video); availabilityCell.append(create('span', `status-pill ${css}`, label));
    if (video.scene_match_source) availabilityCell.append(create('span', 'status-pill pill-good', 'StashDB'));
    row.append(availabilityCell);
    row.append(create('td', 'video-availability-reason', unavailableReason(video) || '—'));
    row.append(create('td', '', video.unavailable_detected_at ? displayTime(video.unavailable_detected_at) : '—'));
    row.append(create('td', '', displayTime(first(video, ['first_seen', 'added_at', 'created_at', 'date_added']))));
    const detailCell = create('td'); const detail = create('button', 'button button-quiet', 'Details →'); detail.type = 'button'; detail.addEventListener('click', () => openDetail(itemId(video), detail)); detailCell.append(detail); row.append(detailCell);
    return row;
  }
  function renderVideos(payload) {
    const items = Array.isArray(payload.items) ? payload.items : Array.isArray(payload.videos) ? payload.videos : [];
    state.list = items;
    const page = payload.page || {};
    state.offset = Number.isFinite(Number(page.offset)) ? Number(page.offset) : state.offset;
    state.total = page.total_exact === false || page.total === null || page.total === undefined ? null : (Number.isFinite(Number(page.total)) ? Number(page.total) : null);
    const hasMore = state.total !== null
      ? state.offset + items.length < state.total
      : page.has_more === true;
    state.nextOffset = hasMore ? state.offset + items.length : null;
    state.nextCatalogCursor = typeof page.next_cursor === 'string' ? page.next_cursor : null;
    state.previousCatalogCursor = typeof page.previous_cursor === 'string' ? page.previous_cursor : null;
    const count = items.length;
    $('#libraryState').hidden = true;
    const grid = $('#videoGrid'), table = $('#videoTableWrap');
    const cardsMode = $('#viewToggle').getAttribute('aria-pressed') !== 'true';
    grid.replaceChildren(); $('#videoRows').replaceChildren();
    if (!count) {
      const data = payload.data || {};
      const stale = Boolean(data.stale || data.incomplete || data.available === false);
      state.isStale = stale;
      setLibraryState('empty', stale ? 'No results in the last available data' : 'No matching videos',
        stale ? 'The catalog may be out of date. Try again later or clear some filters.' : 'Try another search or clear some filters.');
      setLibrarySummary(stale ? 'stale' : 'ok', stale ? 'Data may be out of date' : 'No results');
      $('#catalogFeedback').textContent = stale ? 'Last available data · No results' : 'No results';
      updateBulkSelection();
      return;
    }
    items.forEach(video => { grid.append(cardFor(video)); $('#videoRows').append(tableRow(video)); });
    grid.hidden = !cardsMode; table.hidden = cardsMode;
    const stale = Boolean(payload.data && (payload.data.stale || payload.data.incomplete));
    state.isStale = stale || Boolean(payload.data && payload.data.available === false);
    const available = payload.data && payload.data.available === false;
    const prefix = stale ? 'Last available data · ' : available ? 'Source temporarily unavailable · ' : '';
    const totalText = state.total === null ? `${count} results on this page` : `${state.total.toLocaleString('en-US')} videos`;
    $('#catalogFeedback').textContent = `${prefix}${totalText}`;
    setLibrarySummary(stale || available ? 'stale' : 'ok', stale || available ? 'Data may be out of date' : totalText);
    const pagination = $('#pagination'); pagination.hidden = false;
    $('#pagePrev').disabled = state.offset <= 0 && !state.previousCatalogCursor;
    $('#pageNext').disabled = !hasMore;
    const pageNo = Math.floor(state.offset / LIMIT) + 1;
    $('#pageInfo').textContent = state.total === null ? `Page ${pageNo}` : `Page ${pageNo} of ${Math.max(1, Math.ceil(state.total / LIMIT))}`;
    if (stale || available) announce('Showing the last available data.');
    updateBulkSelection();
  }
  function setVideoSelected(video, checked) {
    const id = itemId(video);
    if (!id) return;
    if (checked && !state.selectedVideos.has(id) && state.selectedVideos.size >= 25) {
      announce('Select up to 25 videos per operation.');
      renderVideos({ items: state.list || [], page: { offset: state.offset, total: state.total, total_exact: state.total !== null, has_more: state.nextOffset !== null, next_cursor: state.nextCatalogCursor, previous_cursor: state.previousCatalogCursor }, data: { stale: state.isStale } });
      return;
    }
    const changed = checked ? !state.selectedVideos.has(id) : state.selectedVideos.has(id);
    if (checked) state.selectedVideos.set(id, safeText(first(video, ['title', 'name']), 'Untitled'));
    else state.selectedVideos.delete(id);
    if (changed) beginOperationRequest();
    updateBulkSelection();
  }
  function updateBulkSelection() {
    const count = state.selectedVideos.size;
    setText($('#bulkSelectionCount'), `${count} selected`);
    const button = $('#reviewQuarantine');
    if (button) button.disabled = count === 0 || state.isStale;
    const availabilityButton = $('#checkAvailabilityBulk');
    if (availabilityButton) {
      availabilityButton.disabled = count === 0 || count > 20 || state.isStale;
      availabilityButton.title = count > 20 ? 'Select up to 20 videos for one playback check task.' : '';
    }
    const metadataButton = $('#bulkEditMetadata');
    if (metadataButton) metadataButton.disabled = count === 0 || count > 25 || state.isStale;
    $$('[data-video-id] input[type="checkbox"]').forEach(input => {
      input.checked = state.selectedVideos.has(input.closest('[data-video-id]')?.dataset.videoId || '');
    });
  }
  function clearLibrarySelection() {
    if (!state.selectedVideos.size) return;
    state.selectedVideos.clear();
    updateBulkSelection();
  }
  async function reviewQuarantine() {
    if (!state.selectedVideos.size || state.isStale) return;
    const route = state.route;
    const sequence = beginOperationRequest();
    const request = { type: 'quarantine', video_ids: [...state.selectedVideos.keys()] };
    $('#reviewQuarantine').disabled = true;
    try {
      const plan = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (!operationRequestIsCurrent(sequence, route)) return;
      if (JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(request.video_ids)) {
        announce('The selection changed while the preview was loading. Review the selection and try again.');
        return;
      }
      state.operationRequest = request;
      state.operationPlan = plan; state.operationId = null;
      renderOperationPreview(plan);
      openDialog($('#operationDialog'));
    } catch (error) { if (operationRequestIsCurrent(sequence, route)) announce(error.message); }
    finally { if (operationRequestIsCurrent(sequence, route)) updateBulkSelection(); }
  }
  async function previewAutoMergeExact() {
    const button = $('#autoMergeExact'); if (button) button.disabled = true;
    const route = state.route;
    const sequence = beginOperationRequest();
    try {
      const request = { type: 'auto_merge_exact' };
      const plan = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (!operationRequestIsCurrent(sequence, route)) return;
      if (!plan.plan_id) { announce(plan.message || 'No exact duplicate groups are available to merge.'); return; }
      state.operationRequest = request; state.operationPlan = plan; state.operationId = null;
      announce(`Queueing ${plan.items.length} exact-file duplicate item${plan.items.length === 1 ? '' : 's'}; no source files will be deleted.`);
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: plan.plan_id });
      if (!operationRequestIsCurrent(sequence, route) || state.operationPlan !== plan || state.operationRequest !== request) {
        announce(`Exact-file duplicate task ${result.jobs?.[0]?.job_id || 'accepted'}. Open Tasks for progress.`);
        return;
      }
      state.operationId = plan.plan_id;
      loadDuplicateCandidates({ refresh: true });
      showQueued(result, 'Exact-file duplicates');
      await refreshOperationStatus();
    } catch (error) { if (operationRequestIsCurrent(sequence, route)) announce(error.message); }
    finally { if (button && operationRequestIsCurrent(sequence, route)) button.disabled = false; }
  }
  function renderOperationPreview(plan, context = state.operationPreviewContext || {}) {
    state.operationPreviewStale = false;
    clearTimeout(state.operationPollTimer); state.operationPollTimer = null;
    const content = $('#operationDialogContent'); content.replaceChildren();
    const isRestore = plan.operation_type === 'restore';
    const isHideBatch = plan.operation_type === 'hide_batch';
    const isHide = plan.operation_type === 'hide';
    const heading = create('h2', '', isRestore ? 'Restore recorded items' : isHideBatch ? 'Auto-merge exact duplicates' : isHide ? 'Keep selected item and hide duplicate' : 'Quarantine selected videos'); heading.id = 'operationDialogTitle';
    content.append(create('p', 'eyebrow', 'REVIEW OPERATION'), heading);
    content.append(create('p', 'detail-description', isRestore
      ? `${plan.items.length} item${plan.items.length === 1 ? '' : 's'} will be restored from the recorded decision. The worker checks conflicts before restoring saved files and requesting a Jellyfin refresh.`
      : isHideBatch
        ? `${plan.items.length} duplicate item${plan.items.length === 1 ? '' : 's'} across ${plan.scanned_groups || 'the scanned'} exact-file group${plan.scanned_groups === 1 ? '' : 's'} will be hidden. Each group runs as a separate task; no files are deleted.${plan.truncated ? ' The preview is capped at 25 items; run it again for another bounded batch.' : ''}`
        : isHide
          ? `${plan.items.length} duplicate item${plan.items.length === 1 ? '' : 's'} will be hidden from the active catalog. The selected keeper stays active. Review the affected item and file count before queueing; no files are deleted.`
          : `${plan.items.length} item${plan.items.length === 1 ? '' : 's'} will be moved out of the active library. Each item runs as a separate task and can fail independently.`));
    if (isHide && context.keeper) {
      const keeperTitle = typeof context.keeper.title === 'string' && context.keeper.title.trim()
        ? context.keeper.title.trim() : 'Untitled';
      const keeperId = typeof context.keeper.id === 'string' ? context.keeper.id : '';
      content.append(create('p', 'operation-keeper', `Kept in catalog: ${keeperTitle}${keeperId ? ` (ID ${keeperId})` : ''}`));
    }
    const list = create('ul', 'operation-item-list');
    for (const item of plan.items) {
      const row = create('li');
      const fileCount = Number(item.file_count) || 0;
      row.append(create('strong', '', item.title || 'Untitled'), create('code', 'operation-video-id', item.video_id),
        ...(item.keeper_id ? [create('span', '', `Keep: ${item.keeper_id}`)] : []),
        create('span', '', `${fileCount} library file${fileCount === 1 ? '' : 's'}${item.already_queued ? ' · A matching task is already queued' : ''}`));
      list.append(row);
    }
    content.append(list, create('p', 'operation-expiry', `Preview expires ${displayTime(plan.expires_at)}. Changes to selected items will invalidate it.`));
    const access = window.AdminV2HeaderStatus?.access(state.services?.checking ? null : state.services?.capabilities);
    if (access && access.state !== 'writable') {
      content.append(create('p', 'operation-readonly', access.state === 'unknown'
        ? 'Write access could not be verified. The admin checks it again before queueing this operation.'
        : `${access.label}: ${access.title}`));
    }
    const footer = $('#operationDialogActions'); footer.replaceChildren();
    const cancel = create('button', 'button button-quiet', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => closeDialog($('#operationDialog')));
    const execute = create('button', 'button button-primary', isRestore ? 'Queue restore' : isHideBatch ? 'Queue auto-merge' : isHide ? 'Queue hide' : 'Queue quarantine'); execute.type = 'button'; execute.addEventListener('click', executeOperationPlan);
    footer.append(cancel, execute);
  }
  async function executeOperationPlan() {
    const plan = state.operationPlan;
    const request = state.operationRequest;
    const sequence = state.operationRequestSeq;
    const planId = plan?.plan_id;
    if (!planId) return;
    const actions = $('#operationDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: planId });
      if (!operationRequestIsCurrent(sequence) || state.operationPlan !== plan || state.operationRequest !== request) {
        announce(`Task ${result.jobs?.[0]?.job_id || 'queued'} accepted. Open Tasks for progress.`);
        return;
      }
      state.operationId = planId;
      if (plan.operation_type === 'quarantine') {
        for (const videoId of request?.video_ids || []) state.selectedVideos.delete(videoId);
        updateBulkSelection();
        showQueued(result, 'Quarantine');
      } else {
        if (plan.operation_type === 'restore') {
          state.quarantineOffset = 0; loadQuarantine(); showQueued(result, 'Restore');
        } else {
          loadDuplicateCandidates({ refresh: true });
          showQueued(result, 'Duplicate review');
        }
      }
      await refreshOperationStatus();
    } catch (error) {
      if (!operationRequestIsCurrent(sequence) || state.operationPlan !== plan || state.operationRequest !== request) {
        announce(`The previous task request could not be confirmed. Check Tasks before retrying. ${error.message}`);
        return;
      }
      $('#operationDialogContent').append(create('p', 'operation-error', error.message));
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (error.status === 409 && state.operationRequest) {
        state.operationPreviewStale = true;
        const queue = [...actions.querySelectorAll('button')].find(button => button.textContent.startsWith('Queue '));
        if (queue) queue.disabled = true;
        const refresh = create('button', 'button button-primary', 'Refresh preview'); refresh.type = 'button';
        refresh.addEventListener('click', refreshOperationPreview);
        if (queue) queue.before(refresh); else actions.append(refresh);
        if (focusedAction && document.activeElement === document.body) refresh.focus({ preventScroll: true });
      } else if (focusedAction?.isConnected && document.activeElement === document.body) {
        focusedAction.focus({ preventScroll: true });
      }
    }
  }
  async function refreshOperationPreview() {
    const actions = $('#operationDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    const request = state.operationRequest;
    const priorPlan = state.operationPlan;
    const route = state.route;
    const sequence = beginOperationRequest();
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const plan = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (!operationRequestIsCurrent(sequence, route) || !$('#operationDialog')?.open
          || state.operationRequest !== request || state.operationPlan !== priorPlan) return;
      state.operationPlan = plan;
      renderOperationPreview(plan);
      const queue = [...$('#operationDialogActions').querySelectorAll('button')].find(button => button.textContent.startsWith('Queue '));
      if (focusedAction && document.activeElement === document.body) queue?.focus({ preventScroll: true });
    } catch (error) {
      if (!operationRequestIsCurrent(sequence, route) || !$('#operationDialog')?.open
          || state.operationRequest !== request || state.operationPlan !== priorPlan) return;
      $('#operationDialogContent').append(create('p', 'operation-error', error.message));
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (state.operationPreviewStale) {
        const queue = [...actions.querySelectorAll('button')].find(button => button.textContent.startsWith('Queue '));
        if (queue) queue.disabled = true;
      }
      if (focusedAction?.isConnected && document.activeElement === document.body) focusedAction.focus({ preventScroll: true });
    }
  }
  async function reviewAvailability(videoIds) {
    if (!Array.isArray(videoIds) || !videoIds.length || videoIds.length > 20) { announce('Choose 1 to 20 videos.'); return; }
    const unique = [...new Set(videoIds)];
    const route = state.route;
    const sequence = beginOperationRequest();
    try {
      const preview = await requestJson(`${V2_API}/availability/preview`, { method: 'POST', payload: { video_ids: unique } });
      if (!preview?.ok || preview.missing_count) throw new Error(responseError(preview, 'One or more videos changed. Refresh the catalog.'));
      if (!operationRequestIsCurrent(sequence, route)) return;
      state.availabilityRequest = unique;
      const content = $('#operationDialogContent'); content.replaceChildren();
      const heading = create('h2', '', 'Check playback availability'); heading.id = 'operationDialogTitle';
      content.append(create('p', 'eyebrow', 'REVIEW CHECK'), heading,
        create('p', 'detail-description', 'The worker requests only a short media byte range through the normal playback resolver. It does not download a video or quarantine any item. Temporary errors and inconclusive responses do not change availability.'));
      const list = create('ul', 'operation-item-list');
      for (const item of preview.items) list.append(create('li', '', `${item.title || item.video_id} · currently ${item.current} · last check ${item.last_check || 'never'}`));
      content.append(list, create('p', 'operation-expiry', 'A confirmed successful check clears an earlier unavailable mark. Confirmed unavailable results are recorded with evidence.'));
      const actions = $('#operationDialogActions'); actions.replaceChildren();
      const cancel = create('button', 'button button-quiet', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => closeDialog($('#operationDialog')));
      const queue = create('button', 'button button-primary', 'Queue check'); queue.type = 'button'; queue.addEventListener('click', executeAvailabilityCheck);
      actions.append(cancel, queue); openDialog($('#operationDialog'));
    } catch (error) { if (operationRequestIsCurrent(sequence, route)) announce(error.message); }
  }
  async function executeAvailabilityCheck() {
    const videoIds = state.availabilityRequest; if (!videoIds?.length) return;
    const sequence = state.operationRequestSeq;
    const actions = $('#operationDialogActions'); actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/availability/execute`, { video_ids: videoIds });
      if (!operationRequestIsCurrent(sequence) || state.availabilityRequest !== videoIds) {
        announce(`Availability check task ${result.job_id} queued. Open Tasks for progress.`);
        return;
      }
      closeDialog($('#operationDialog')); state.availabilityRequest = null;
      if (videoIds.length > 1) { state.selectedVideos.clear(); updateBulkSelection(); }
      showQueued(result, 'Availability check');
    } catch (error) {
      if (!operationRequestIsCurrent(sequence) || state.availabilityRequest !== videoIds) {
        announce(`The previous availability request could not be confirmed. Check Tasks before retrying. ${error.message}`);
        return;
      }
      $('#operationDialogContent').append(create('p', 'operation-error', error.message));
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  async function loadAvailabilityHistory(videoId) {
    const sequence = ++state.availabilityHistorySeq;
    const host = $('#detailContent'); const section = create('section', 'detail-section');
    section.setAttribute('aria-labelledby', 'detailAvailabilityHeading');
    const heading = create('h3', '', 'Availability & History'); heading.id = 'detailAvailabilityHeading';
    const feedback = create('p', 'detail-description', 'Loading availability history…');
    feedback.setAttribute('role', 'status');
    section.append(heading, feedback); host.append(section);
    try {
      const payload = await window.AdminV2.getJson(`${V2_API}/availability/history?video_id=${encodeURIComponent(videoId)}&limit=5`);
      if (sequence !== state.availabilityHistorySeq || state.detailId !== videoId) return;
      section.replaceChildren(heading);
      if (!payload.items.length) section.append(create('p', 'detail-description', 'No playback checks recorded.'));
      else for (const item of payload.items) section.append(create('p', 'detail-description', `${titleCase(item.status)} · ${displayTime(item.checked_at)} · ${item.evidence}`));
    } catch {
      if (sequence !== state.availabilityHistorySeq || state.detailId !== videoId) return;
      section.replaceChildren(heading, create('p', 'detail-description', 'Availability history could not be loaded.'));
    }
  }
  async function refreshOperationStatus() {
    if (!state.operationId || state.operationRefreshing || !$('#operationDialog').open) return;
    const sequence = state.operationRequestSeq;
    const statusSequence = ++state.operationStatusSeq;
    const operationId = state.operationId;
    const plan = state.operationPlan;
    state.operationRefreshing = true;
    clearTimeout(state.operationPollTimer); state.operationPollTimer = null;
    try {
      const payload = await window.AdminV2.getJson(`${V2_API}/operations/${encodeURIComponent(operationId)}`);
      if (statusSequence !== state.operationStatusSeq || !operationRequestIsCurrent(sequence) || state.operationId !== operationId
          || state.operationPlan !== plan || !$('#operationDialog').open) return;
      const content = $('#operationDialogContent'); content.replaceChildren();
      const isRestore = plan?.operation_type === 'restore';
      const isHide = plan?.operation_type === 'hide';
      const heading = create('h2', '', isRestore ? 'Restore task' : isHide ? 'Duplicate hide task' : 'Quarantine tasks'); heading.id = 'operationDialogTitle';
      content.append(create('p', 'eyebrow', 'OPERATION STATUS'), heading);
      const list = create('ul', 'operation-item-list');
      for (const item of payload.operation.jobs) {
        const row = create('li');
        const title = item.video_id
          ? (plan?.items?.find(entry => entry.video_id === item.video_id)?.title || item.video_id)
          : `${payload.operation.items.length} recorded item${payload.operation.items.length === 1 ? '' : 's'}`;
        row.append(create('strong', '', title), create('span', '', `${safeText(item.state)} · Task ${item.job_id}`));
        const link = taskLink(item.job_id); if (link) row.append(link);
        list.append(row);
      }
      const pending = payload.operation.jobs.some(item => ['queued', 'processing'].includes(item.state));
      content.append(list, create('p', 'operation-expiry', pending ? 'Status updates automatically while this dialog is open.' : 'All tasks have finished.'));
      const actions = $('#operationDialogActions'); actions.replaceChildren();
      const close = create('button', 'button button-quiet', 'Close'); close.type = 'button'; close.addEventListener('click', () => closeDialog($('#operationDialog')));
      actions.append(close);
      if (pending) scheduleOperationPoll();
    } catch (error) {
      if (statusSequence === state.operationStatusSeq && operationRequestIsCurrent(sequence) && state.operationId === operationId
          && state.operationPlan === plan && $('#operationDialog').open) {
        announce(`Could not refresh operation status: ${error.message}`);
        scheduleOperationPoll();
      }
    } finally {
      if (statusSequence === state.operationStatusSeq) {
        state.operationRefreshing = false;
        if (!operationRequestIsCurrent(sequence) && state.operationId && $('#operationDialog').open) scheduleOperationPoll();
      }
    }
  }
  function scheduleOperationPoll() {
    clearTimeout(state.operationPollTimer);
    state.operationPollTimer = setTimeout(() => {
      if (document.hidden) scheduleOperationPoll();
      else refreshOperationStatus();
    }, 5000);
  }
  async function loadVideos() {
    if (state.route !== 'library' || state.libraryTab !== 'videos') return;
    if (state.listAbort) state.listAbort.abort();
    const controller = new AbortController(); state.listAbort = controller;
    const sequence = ++state.requestSeq;
    const params = filterParams(); params.set('limit', String(LIMIT)); params.set('offset', String(state.offset));
    params.set('exact_total', 'false');
    const queryKey = params.toString();
    const url = `${V2_API}/library/videos?${params.toString()}`;
    writeFilterUrl(true);
    const matchingData = state.list !== null && state.lastSuccessfulQuery === queryKey;
    if (!matchingData) {
      state.list = null; state.total = null; state.nextOffset = null; state.isStale = false;
    }
    if (!matchingData || !state.list || !state.list.length) setLibraryState('loading', 'Loading catalog', 'Searching for videos that match your filters…');
    setLibrarySummary('loading', 'Loading catalog');
    try {
      const payload = await getJson(url, controller.signal);
      if (sequence !== state.requestSeq) return;
      state.lastSuccessfulQuery = queryKey;
      renderVideos(payload);
    } catch (error) {
      if (error.name === 'AbortError' || sequence !== state.requestSeq) return;
      if (matchingData && state.list && state.list.length) {
        state.isStale = true;
        setLibrarySummary('stale', 'Showing previous data');
        $('#catalogFeedback').textContent = `Could not refresh the catalog. Showing previous data. ${error.message}`;
      } else if (matchingData) {
        state.isStale = true;
        setLibraryState('empty', 'No matching videos', `The last successful search was empty. Refresh failed: ${error.message}`);
        setLibrarySummary('stale', 'Showing previous result');
        $('#catalogFeedback').textContent = `The last search was empty. Could not refresh the catalog. ${error.message}`;
      } else {
        setLibrarySummary('error', 'Catalog error');
        setLibraryState('error', 'Could not load the catalog', `${error.message} Please try again.`);
        $('#catalogFeedback').textContent = `Could not load the catalog. ${error.message}`;
      }
    }
  }
  function debounceLoad() {
    clearLibrarySelection();
    resetCatalogPage();
    const newHistoryEntry = !state.filterHistoryActive;
    state.filterHistoryActive = true;
    writeFilterUrl(!newHistoryEntry);
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.searchTimer = null;
      state.filterHistoryActive = false;
      loadVideos();
    }, 300);
  }
  Object.assign(AV, { availability, clearLibrarySelection, debounceLoad, displayTime,
    loadAvailabilityHistory, loadVideos, previewAutoMergeExact, renderOperationPreview, renderVideos,
    reviewAvailability, reviewQuarantine, thumbnail, unavailableReason, updateBulkSelection });
})();
