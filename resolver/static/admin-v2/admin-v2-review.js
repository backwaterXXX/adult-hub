(() => {
  'use strict';
  // Admin V2 review actions: orphan file quarantine and duplicate suggestions.
  const AV = window.AdminV2;
  const { $, V2_API, announce, beginOperationRequest, create, duplicateGroupIsVisible, getJson, openDialog,
    operationRequestIsCurrent, responseError, setText, showQueued, state, taskLink } = AV;
  const loadDuplicateCandidates = (...args) => AV.loadDuplicateCandidates(...args);
  const loadVisualDuplicateResults = (...args) => AV.loadVisualDuplicateResults(...args);
  const renderOperationPreview = (...args) => AV.renderOperationPreview(...args);
  async function loadQuarantine() {
    if (state.route !== 'library' || $('#libraryQuarantinePane').hidden) return;
    const sequence = ++state.quarantineRequestSeq;
    state.quarantineBusy = true; setText($('#quarantineFeedback'), 'Loading active decisions…');
    try {
      const params = new URLSearchParams({ offset: String(state.quarantineOffset), limit: '50' });
      const payload = await getJson(`${V2_API}/organize/quarantine?${params}`);
      if (sequence !== state.quarantineRequestSeq || state.route !== 'library' || $('#libraryQuarantinePane').hidden) return;
      if (!payload?.ok || payload.available === false) throw new Error(responseError(payload, 'Quarantine history is unavailable.'));
      state.quarantineTotal = Number(payload.total) || 0;
      const rows = $('#quarantineRows'); rows.replaceChildren(); $('#quarantineState').hidden = payload.items.length > 0;
      if (!payload.items.length) $('#quarantineState').textContent = 'No active hidden or quarantined items.';
      for (const item of payload.items || []) {
        const card = create('article', 'quarantine-review-item');
        const copy = create('div'); copy.append(create('strong', '', item.title || item.id), create('p', '', `${item.mode === 'quarantine' ? 'Quarantined' : 'Hidden'} · ${Number.isInteger(item.files) ? `${item.files} file(s)` : 'File count unknown'}`));
        const undo = create('button', 'button button-quiet', 'Restore'); undo.type = 'button'; undo.addEventListener('click', () => confirmQuarantineUndo(item));
        card.append(copy, undo); rows.append(card);
      }
      setText($('#quarantineFeedback'), `${state.quarantineTotal} active decisions`);
      $('#quarantinePageInfo').textContent = state.quarantineTotal ? `${state.quarantineOffset + 1}–${Math.min(state.quarantineOffset + 50, state.quarantineTotal)} of ${state.quarantineTotal}` : 'No decisions';
      $('#quarantinePrev').disabled = state.quarantineOffset <= 0; $('#quarantineNext').disabled = state.quarantineOffset + 50 >= state.quarantineTotal;
      $('#quarantinePagination').hidden = state.quarantineTotal <= 50;
      await loadOrphanFileQuarantine(sequence);
    } catch (error) {
      if (sequence !== state.quarantineRequestSeq || state.route !== 'library' || $('#libraryQuarantinePane').hidden) return;
      setText($('#quarantineFeedback'), error.message); $('#quarantineState').hidden = false; $('#quarantineState').textContent = 'Could not load decisions. Refresh to try again.';
    } finally { if (sequence === state.quarantineRequestSeq) state.quarantineBusy = false; }
  }
  async function loadOrphanFileQuarantine(sequence) {
    const feedback = $('#orphanQuarantineFeedback'), rows = $('#orphanQuarantineRows');
    if (!feedback || !rows) return;
    setText(feedback, 'Loading file quarantine…');
    try {
      const payload = await window.AdminV2.getJson(`${V2_API}/organize/quarantine/files?offset=0&limit=50`);
      if (sequence !== state.quarantineRequestSeq || state.route !== 'library' || $('#libraryQuarantinePane').hidden) return;
      rows.replaceChildren();
      if (!payload.items.length) rows.append(create('p', 'catalog-empty', 'No orphan files are currently quarantined.'));
      for (const item of payload.items) {
        const card = create('article', 'quarantine-review-item');
        const copy = create('div');
        copy.append(create('strong', '', `${item.file_count} quarantined file(s)`));
        copy.append(create('p', '', item.files.map(file => `${file.path} (${Number(file.size).toLocaleString('en-US')} bytes)`).join(' · ')));
        const restore = create('button', 'button button-quiet', 'Restore files');
        restore.type = 'button'; restore.addEventListener('click', () => restoreOrphanFiles(item));
        card.append(copy, restore); rows.append(card);
      }
      setText(feedback, `${payload.total} active file quarantine operation(s).`);
    } catch (error) {
      if (sequence === state.quarantineRequestSeq) setText(feedback, `Could not load file quarantine. ${error.message}`);
    }
  }
  async function restoreOrphanFiles(item) {
    const route = state.route;
    const sequence = beginOperationRequest();
    try {
      const plan = await window.AdminV2.postJson(`${V2_API}/operations/preview`,
        { type: 'orphan_file_restore', operation_id: item.operation_id });
      if (!operationRequestIsCurrent(sequence, route)) return;
      const paths = plan.items.map(file => file.path).join('\n');
      if (!window.confirm(`Restore ${plan.items.length} quarantined file(s) to their original library paths? Existing paths are never overwritten.\n\n${paths}`)) return;
      const queued = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: plan.plan_id });
      if (!operationRequestIsCurrent(sequence, route)) return;
      showQueued(queued, 'File restore');
    } catch (error) {
      if (operationRequestIsCurrent(sequence, route)) announce(`Could not preview or queue file restore. ${error.message}`);
    }
  }
  async function confirmQuarantineUndo(item) {
    const route = state.route;
    const sequence = beginOperationRequest();
    const request = { type: 'restore', operation_id: item.operation_id };
    try {
      const plan = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (!operationRequestIsCurrent(sequence, route)) return;
      state.operationRequest = request;
      state.operationPlan = plan; state.operationId = null;
      renderOperationPreview(plan);
      openDialog($('#operationDialog'));
    } catch (error) { if (operationRequestIsCurrent(sequence, route)) announce(`Could not preview restore. ${error.message}`); }
  }
  async function previewDuplicate(pair, keepSide, trigger) {
    const keeper = pair[keepSide]; const loser = pair[keepSide === 'left' ? 'right' : 'left'];
    if (!keeper?.id || !loser?.id || state.duplicatePending) return;
    const pending = { action: 'hide', keeper, loser };
    state.duplicatePending = pending;
    const route = state.route;
    const sequence = beginOperationRequest();
    const card = trigger?.closest('.duplicate-candidate, .duplicate-candidate-card');
    const match = state.duplicateMatch;
    const feedback = card?.querySelector('.duplicate-action-feedback');
    const actionButtons = card?.querySelectorAll('button') || [];
    actionButtons.forEach(button => { button.disabled = true; });
    if (trigger) trigger.textContent = 'Queuing…';
    if (feedback) {
      feedback.hidden = false;
      feedback.className = 'duplicate-action-feedback is-pending';
      feedback.textContent = `Keeping ${keeper.title || 'selected item'} and hiding ${loser.title || 'the other item'}…`;
    }
    announce('Queueing the selected duplicate action.');
    try {
      const request = { type: 'hide', keeper_id: keeper.id, loser_ids: [loser.id],
        ...(Number.isInteger(pair.rules_version) ? { rules_version: pair.rules_version } : {}) };
      const plan = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (!operationRequestIsCurrent(sequence, route) || state.duplicatePending !== pending
          || !duplicateGroupIsVisible(match) || !card?.isConnected) return;
      if (!plan.plan_id) throw new Error('The server did not return a review plan.');
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: plan.plan_id });
      if (!operationRequestIsCurrent(sequence, route) || state.duplicatePending !== pending) return;
      const jobId = result.jobs?.find(job => typeof job.job_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(job.job_id))?.job_id || '';
      const message = `Hide queued for ${loser.title || loser.id}; ${keeper.title || keeper.id} stays in the catalog. No files were deleted.${jobId ? ` Task ${jobId}.` : ' See Jobs & Automation for progress.'}`;
      if (feedback) {
        feedback.className = 'duplicate-action-feedback is-queued';
        feedback.textContent = message;
        if (jobId) {
          const taskLink = create('a', 'text-link', 'View task');
          taskLink.href = `/admin/v2/?job_id=${encodeURIComponent(jobId)}#jobs`;
          feedback.append(taskLink);
        }
      }
      showQueued(result, 'Duplicate decision');
      loadDuplicateCandidates({ refresh: true });
      loadVisualDuplicateResults();
    } catch (error) {
      if (operationRequestIsCurrent(sequence, route) && state.duplicatePending === pending) {
        if (feedback) {
          feedback.hidden = false;
          feedback.className = 'duplicate-action-feedback is-error';
          feedback.textContent = `Could not queue this duplicate action: ${error.message}`;
        }
        announce(feedback?.textContent || `Could not queue this duplicate action. ${error.message}`);
      }
    } finally {
      if (state.duplicatePending === pending) state.duplicatePending = null;
      if (trigger?.isConnected) trigger.textContent = 'Keep & hide other';
      actionButtons.forEach(button => { if (button.isConnected) button.disabled = false; });
    }
  }
  async function dismissDuplicateSuggestion(pair, button, feedback) {
    if (!pair?.left?.id || !pair?.right?.id || state.duplicatePending) return;
    const pending = { action: 'dismiss', left: pair.left, right: pair.right };
    state.duplicatePending = pending;
    button.disabled = true;
    feedback.hidden = true;
    let succeeded = false;
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/duplicates/actions`, {
        action: 'dismiss', left_id: pair.left.id, right_id: pair.right.id,
      });
      if (state.duplicatePending !== pending) return;
      succeeded = true;
      const jobId = typeof result.job_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(result.job_id) ? result.job_id : '';
      const message = `Decision queued: different videos.${jobId ? ` Task ${jobId}.` : ''}`;
      feedback.hidden = false;
      feedback.className = 'duplicate-action-feedback is-queued';
      feedback.textContent = message;
      if (jobId) {
        const taskLink = create('a', 'text-link', 'View task');
        taskLink.href = `/admin/v2/?job_id=${encodeURIComponent(jobId)}#jobs`;
        if (typeof feedback?.append === 'function') feedback.append(taskLink);
      }
      showQueued(result, 'Duplicate decision');
      loadDuplicateCandidates({ refresh: true });
      loadVisualDuplicateResults();
    } catch (error) {
      if (state.duplicatePending !== pending) return;
      feedback.hidden = false;
      feedback.className = 'duplicate-action-feedback is-error';
      feedback.textContent = `Could not dismiss this suggestion: ${error.message}`;
      announce(`Could not dismiss suggestion. ${error.message}`);
    } finally {
      if (state.duplicatePending === pending) state.duplicatePending = null;
      if (!succeeded && button.isConnected) button.disabled = false;
    }
  }
  Object.assign(AV, { dismissDuplicateSuggestion, loadQuarantine, previewDuplicate });
})();
