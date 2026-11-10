(() => {
  'use strict';
  // Sources list rendering, pagination, loading, sync actions, and URL state.
  const SRC = window.AdminV2Sources;
  const { $, HISTORY_URL, SOURCES_URL, SOURCE_CHECK_URL, SOURCE_PAGE_SIZE, SOURCE_SETTINGS_URL,
    TRIGGER_URL, V2_API, announce, appendRow, buildChannelHistory, buildHistoryIndex, buildItems,
    compareSourceItems, create, displayTime, helpers, isSourcesRoute, itemKey, matchesFilter, scrub,
    setFeedback, setNotice, setSourcesState, setText, sourcesTab, state, updateSortHeaders } = SRC;
  const closeSourceDetail = (...args) => SRC.closeSourceDetail(...args);
  const closeTelegramAccountActionDialog = (...args) => SRC.closeTelegramAccountActionDialog(...args);
  const closeTelegramAuthWizard = (...args) => SRC.closeTelegramAuthWizard(...args);
  const openAddChannel = (...args) => SRC.openAddChannel(...args);
  const renderSyncRuns = (...args) => SRC.renderSyncRuns(...args);
  const renderTelegramAccountCard = (...args) => SRC.renderTelegramAccountCard(...args);
  function render() {
    const tbody = $('#sourcesRows');
    if (!tbody) return;
    if (state.loading && !state.items.length) return;
    const matching = state.items.filter(matchesFilter).sort(compareSourceItems);
    updateSortHeaders();
    const pageCount = Math.max(1, Math.ceil(matching.length / SOURCE_PAGE_SIZE));
    const requestedPage = state.page;
    state.page = Math.min(state.page, pageCount - 1);
    if (state.page !== requestedPage) writeSourceUrl(true);
    const startIndex = state.page * SOURCE_PAGE_SIZE;
    const visible = matching.slice(startIndex, startIndex + SOURCE_PAGE_SIZE);
    const addChannel = $('#sourcesAddChannel');
    if (addChannel) addChannel.disabled = !state.settingsAvailable || state.configRevision === null;
    tbody.replaceChildren();

    if (!state.items.length) {
      const unavailable = !state.configAvailable;
      let action = null;
      if (!unavailable && state.settingsAvailable && state.configRevision !== null) {
        action = create('button', 'button button-primary empty-action-button', 'Add Telegram channel');
        action.type = 'button';
        action.id = 'sourcesEmptyAddChannel';
        action.addEventListener('click', event => openAddChannel(event.currentTarget));
      }
      setSourcesState(
        unavailable ? 'error' : 'empty',
        unavailable ? 'Sources unavailable' : 'No channels configured',
        unavailable
          ? 'The source configuration could not be loaded. Try refreshing in a moment.'
          : 'No Telegram channels are configured yet. Add your first channel through Admin to start synchronizing.',
        action,
      );
      setFeedback(unavailable ? 'Source configuration unavailable.' : 'No channels configured. Add your first channel to start.');
      return;
    }
    if (!visible.length) {
      setSourcesState('empty', 'No matching channels', 'Try a different search or clear the status filter.');
      renderPagination(0);
      setFeedback(`No channels match the current filters (${state.items.length} total).`);
      return;
    }

    visible.forEach(item => appendRow(tbody, item));
    $('#sourcesState').hidden = true;
    $('#sourcesTableWrap').hidden = false;
    renderPagination(matching.length);
    setFeedback(state.accessFeedback);
  }

  function renderPagination(total) {
    const host = $('#sourcesPagination');
    if (!host) return;
    host.replaceChildren();
    const pageCount = Math.max(1, Math.ceil(total / SOURCE_PAGE_SIZE));
    host.hidden = pageCount <= 1;
    if (host.hidden) return;
    const previous = create('button', 'button button-quiet', '← Previous');
    previous.type = 'button'; previous.disabled = state.page === 0; previous.dataset.sourcePage = 'previous';
    const info = create('span', '', `Page ${state.page + 1} of ${pageCount}`);
    info.id = 'sourcesPageInfo';
    info.tabIndex = -1;
    const next = create('button', 'button button-quiet', 'Next →');
    next.type = 'button'; next.disabled = state.page >= pageCount - 1; next.dataset.sourcePage = 'next';
    host.append(previous, info, next);
  }

  async function loadHistoryPage() {
    const sequence = ++state.historySeq;
    try {
      const payload = await window.AdminV2.getJson(`${V2_API}/sources/sync-history?limit=10&offset=${state.historyPage * 10}`);
      if (sequence !== state.historySeq || !isSourcesRoute() || sourcesTab() !== 'history') return;
      if (!payload.runs?.length && state.historyPage > 0 && payload.total >= 0) {
        state.historyPage = Math.max(0, Math.ceil(payload.total / 10) - 1); return loadHistoryPage();
      }
      renderSyncRuns(payload);
    } catch (error) { if (sequence === state.historySeq && isSourcesRoute() && sourcesTab() === 'history') renderSyncRuns(null); }
  }

  async function loadSources() {
    if (!isSourcesRoute()) return;
    if (state.abort) state.abort.abort();
    const controller = new AbortController();
    state.abort = controller;
    const sequence = ++state.seq;
    state.loading = true;
    setNotice('', '');
    if (!state.items.length) setSourcesState('loading', 'Loading sources', 'Fetching configured channels and sync history…');
    setFeedback('Loading sources…');
    try {
      const [sourcesResult, historyResult, settingsResult] = await Promise.allSettled([
        window.AdminV2.getJson(SOURCES_URL, controller.signal),
        window.AdminV2.getJson(HISTORY_URL, controller.signal),
        window.AdminV2.getJson(SOURCE_SETTINGS_URL, controller.signal)
      ]);
      if (sequence !== state.seq || !isSourcesRoute() || sourcesTab() !== 'channels') return;
      const historyPayload = historyResult.status === 'fulfilled' ? historyResult.value : null;
      if (sourcesTab() === 'history') loadHistoryPage();
      if (sourcesResult.status === 'rejected') throw sourcesResult.reason;
      const sourcesPayload = sourcesResult.value;
      const settingsPayload = settingsResult.status === 'fulfilled' ? settingsResult.value : null;
      state.configAvailable = !(sourcesPayload && sourcesPayload.available === false);
      state.telegramAuth = sourcesPayload?.telegram_auth || null;
      renderTelegramAccountCard(state.telegramAuth);
      state.configRevision = Number.isInteger(sourcesPayload && sourcesPayload.managed_revision)
        ? sourcesPayload.managed_revision : null;
      state.settingsAvailable = Boolean(settingsPayload
        && settingsPayload.revision === state.configRevision
        && settingsPayload.overrides && typeof settingsPayload.overrides === 'object');
      state.auditPending = settingsPayload?.audit_pending === true;
      state.overrides = state.settingsAvailable ? settingsPayload.overrides : {};
      state.historyAvailable = historyResult.status === 'fulfilled' && !(historyPayload && historyPayload.error);
      state.historyTruncated = state.historyAvailable && historyPayload?.truncated === true;
      const runs = Array.isArray(historyPayload && historyPayload.runs) ? historyPayload.runs : [];
      const sources = Array.isArray(sourcesPayload && sourcesPayload.sources) ? sourcesPayload.sources : [];
      state.channelHistory = state.historyAvailable ? buildChannelHistory(runs) : new Map();
      state.items = buildItems(sources, buildHistoryIndex(runs));
      if (!state.configAvailable) setNotice('warning', 'Source configuration could not be loaded. Showing configured channels where available.');
      else if (!state.settingsAvailable) setNotice('warning', 'Source editing is unavailable until settings and revision can be loaded together.');
      else if (state.auditPending) setNotice('warning', 'A source change audit is waiting for the database. It will be retried automatically; new source changes are paused until it is recorded.');
      else if (!state.historyAvailable) setNotice('warning', 'Sync history is unavailable, so the latest results are unknown.');
      state.loading = false;
      render();
    } catch (error) {
      if (error.name === 'AbortError' || sequence !== state.seq || !isSourcesRoute() || sourcesTab() !== 'channels') return;
      setSourcesState('error', 'Could not load sources', `${scrub(error.message) || 'The request failed.'} Please try again.`);
      setFeedback('Source list unavailable.');
    } finally {
      if (sequence === state.seq) state.loading = false;
    }
  }

  async function syncNow(id, mode, button) {
    const item = state.items.find(candidate => candidate.id === id);
    if (!item || !item.enabled || state.pendingSync.has(id)) return;
    state.pendingSync.add(id);
    const sequence = state.seq;
    const original = button.textContent;
    button.disabled = true;
    button.dataset.busy = 'true';
    button.textContent = 'Queueing…';
    setNotice('', '');
    try {
      const payload = await window.AdminV2.postJson(TRIGGER_URL, { source: id, sync_mode: mode });
      if (sequence !== state.seq || !isSourcesRoute()) return;
      if (!helpers().taskLink(payload?.job_id)) throw new Error('The server did not return a task reference.');
      helpers().showQueued(payload, 'Synchronization', $('#sourcesNotice'));
      // Keep the receipt visible when the originating dialog closes.
    } catch (error) {
      if (sequence !== state.seq || !isSourcesRoute()) return;
      setNotice('error', scrub(error.message) || 'Could not queue the sync.');
      announce('Could not queue the sync.');
    } finally {
      state.pendingSync.delete(id);
      delete button.dataset.busy;
      if (button.isConnected) { button.disabled = false; button.textContent = original; }
    }
  }

  async function checkSourceAccess(id, button) {
    const item = state.items.find(candidate => candidate.provider === 'telegram' && candidate.id === id);
    if (!item || !button) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    state.accessFeedback = `${item.studio}: Checking Telegram read access…`;
    setFeedback(state.accessFeedback);
    const resultNode = button.parentElement && button.parentElement.querySelector('.source-access-outcome');
    if (resultNode) {
      resultNode.hidden = false;
      resultNode.dataset.accessStatus = 'checking';
      resultNode.textContent = 'Checking Telegram read access…';
      resultNode.removeAttribute('title');
    }
    try {
      const result = await window.AdminV2.postJson(SOURCE_CHECK_URL, { source_id: id });
      const status = ['accessible', 'inaccessible', 'unknown'].includes(result.status) ? result.status : 'unknown';
      const outcome = {
        status,
        message: scrub(result.message) || 'Telegram could not confirm read access to this channel.',
        checkedAt: result.checked_at,
      };
      state.accessChecks.set(itemKey(item), outcome);
      if (resultNode) renderAccessOutcome(resultNode, outcome);
      applyAccessButtonOutcome(button, item, outcome);
      state.accessFeedback = accessFeedbackText(item, outcome);
      setFeedback(state.accessFeedback);
      announce(`${item.studio}: ${outcome.message} Checked ${displayTime(outcome.checkedAt)}.`);
    } catch (error) {
      const outcome = {
        status: 'error',
        message: scrub(error.message) || 'Could not check Telegram access.',
        checkedAt: null,
      };
      state.accessChecks.set(itemKey(item), outcome);
      if (resultNode) renderAccessOutcome(resultNode, outcome);
      applyAccessButtonOutcome(button, item, outcome);
      state.accessFeedback = accessFeedbackText(item, outcome);
      setFeedback(state.accessFeedback);
      announce(`${item.studio}: could not check Telegram access. ${outcome.message}`);
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }

  function accessStatusLabel(outcome) {
    return ({ accessible: 'Accessible', inaccessible: 'No access', unknown: 'Unknown', error: 'Check failed' })[outcome.status] || 'Unknown';
  }

  function accessFeedbackText(item, outcome) {
    return `${item.studio}: ${accessStatusLabel(outcome)} · ${outcome.message}`;
  }

  function applyAccessButtonOutcome(button, item, outcome) {
    const label = accessStatusLabel(outcome);
    const checked = displayTime(outcome.checkedAt);
    button.dataset.accessStatus = outcome.status;
    button.title = `${label} · ${outcome.message}${checked === '—' ? '' : ` · Checked ${checked}`}`;
    button.setAttribute('aria-label', `Check access for ${item.studio}. Last result: ${label}. ${outcome.message}`);
  }

  function renderAccessOutcome(node, outcome) {
    const labels = {
      accessible: 'Accessible', inaccessible: 'No access', unknown: 'Unknown', error: 'Check failed',
      checking: 'Checking',
    };
    const status = labels[outcome.status] ? outcome.status : 'unknown';
    const label = labels[status];
    const checked = displayTime(outcome.checkedAt);
    node.hidden = false;
    node.dataset.accessStatus = status;
    node.textContent = `${label} · ${outcome.message}${checked === '—' ? '' : ` · Checked ${checked}`}`;
    node.title = outcome.message;
  }

  function restoreSourceUrl() {
    const url = new URL(location.href);
    const params = url.searchParams;
    const deepLink = params.get('source_id');
    if (deepLink) {
      const separator = deepLink.indexOf(':');
      const query = separator >= 0 ? deepLink.slice(separator + 1) : deepLink;
      if (separator > 0) params.set('source_key', `${deepLink.slice(0, separator)}::${query}`);
      else params.delete('source_key');
      if (query) params.set('source_q', query);
      params.delete('source_id');
      url.search = params.toString();
      history.replaceState({}, '', url);
    }

    const current = new URLSearchParams(location.search || '');
    state.filter.q = current.get('source_q') || '';
    const status = current.get('source_status') || '';
    state.filter.status = ['enabled', 'disabled', 'succeeded', 'failed', 'never'].includes(status) ? status : '';
    state.filter.sourceKey = current.get('source_key') || '';
    const rawPage = Number(current.get('source_page'));
    state.page = Number.isSafeInteger(rawPage) && rawPage >= 0 ? rawPage : 0;
    const sort = current.get('source_sort') || 'channel';
    state.sort.key = ['channel', 'provider', 'status', 'videos', 'latest', 'last-sync'].includes(sort)
      ? sort : 'channel';
    state.sort.direction = current.get('source_dir') === 'desc' ? 'desc' : 'asc';
    const queryInput = $('#sourcesQuery');
    const statusInput = $('#sourcesStatusFilter');
    if (queryInput) queryInput.value = state.filter.q;
    if (statusInput) statusInput.value = state.filter.status;
  }

  function writeSourceUrl(replace = false) {
    const url = new URL(location.href);
    const params = url.searchParams;
    for (const key of ['source_id', 'source_q', 'source_status', 'source_key', 'source_page', 'source_sort', 'source_dir']) {
      params.delete(key);
    }
    if (state.filter.q.trim()) params.set('source_q', state.filter.q.trim());
    if (state.filter.status) params.set('source_status', state.filter.status);
    if (state.filter.sourceKey) params.set('source_key', state.filter.sourceKey);
    if (state.page > 0) params.set('source_page', String(state.page));
    if (state.sort.key !== 'channel') params.set('source_sort', state.sort.key);
    if (state.sort.direction === 'desc') params.set('source_dir', 'desc');
    url.search = params.toString();
    if (url.href === location.href) return;
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }

  function scheduleSourceUrlUpdate() {
    if (!state.urlEditActive) {
      history.pushState({}, '', location.href);
      state.urlEditActive = true;
    }
    clearTimeout(state.urlTimer);
    state.urlTimer = setTimeout(() => {
      state.urlTimer = null;
      state.urlEditActive = false;
    }, 300);
    writeSourceUrl(true);
  }

  function commitSourceUrl() {
    clearTimeout(state.urlTimer);
    state.urlTimer = null;
    state.urlEditActive = false;
    writeSourceUrl();
  }

  function restoreSourceStateFromHistory() {
    if (!isSourcesRoute()) return;
    clearTimeout(state.urlTimer);
    state.urlTimer = null;
    state.urlEditActive = false;
    restoreSourceUrl();
    render();
  }

  function sync() {
    const view = $('#sourcesView');
    if (!view) return;
    const active = isSourcesRoute();
    const tab = active ? sourcesTab() : '';
    const changed = state.activeTab !== tab;
    view.hidden = !active;
    if (changed) {
      state.seq += 1; state.historySeq += 1;
      state.abort?.abort();
      const dialog = $('#sourceDetailDialog');
      if (dialog?.open) closeSourceDetail(false);
      const tgAuthDialog = $('#telegramAuthDialog');
      if (tgAuthDialog?.open) closeTelegramAuthWizard();
      const tgActionDialog = $('#telegramAccountActionDialog');
      if (tgActionDialog?.open) closeTelegramAccountActionDialog();
    }
    state.activeTab = tab;
    const channels = $('#sourceChannelsPane'), history = $('#sourceHistoryPane');
    if (channels) channels.hidden = !active || tab !== 'channels';
    if (history) history.hidden = !active || tab !== 'history';
    document.querySelectorAll('[data-sources-tab]').forEach(link => {
      if (link.dataset.sourcesTab === tab) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    if (!active) { state.entered = false; return; }
    setText($('#crumbCurrent'), 'Sources');
    setText($('#operationalEyebrow'), 'SOURCES');
    setText($('#operationalTitle'), 'Sources');
    setText($('#operationalIntro'), tab === 'history' ? 'Synchronization outcomes and details.' : 'Manage channels and request synchronization.');
    if (!state.entered || changed) {
      state.entered = true;
      restoreSourceUrl();
      if (tab === 'history') loadHistoryPage();
      else loadSources();
    }
  }

  Object.assign(SRC, { applyAccessButtonOutcome, checkSourceAccess, commitSourceUrl, loadHistoryPage,
    loadSources, render, renderAccessOutcome, renderPagination, restoreSourceStateFromHistory,
    scheduleSourceUrlUpdate, sync, syncNow });
})();
