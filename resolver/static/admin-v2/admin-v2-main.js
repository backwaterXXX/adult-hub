(() => {
  'use strict';
  // Admin V2 theme, event wiring, and page start-up.
  const AV = window.AdminV2;
  const { $, $$, DUPLICATE_PAGE_SIZE, LIMIT, applySavedView, beginOperationRequest, closeDialog,
    commitFilterUrl, deleteSavedView, duplicateGroupIsVisible, first, go, loadChannelFilterOptions,
    renderLocation, renderSavedViews, resetCatalogPage, routeInfo, saveCurrentView, selectDuplicateMatch,
    state, writeIssueFiltersToUrl } = AV;
  const clearLibrarySelection = (...args) => AV.clearLibrarySelection(...args);
  const closeDetail = (...args) => AV.closeDetail(...args);
  const debounceLoad = (...args) => AV.debounceLoad(...args);
  const loadDuplicateCandidates = (...args) => AV.loadDuplicateCandidates(...args);
  const loadHeaderStatus = (...args) => AV.loadHeaderStatus(...args);
  const loadHeaderSyncStatus = (...args) => AV.loadHeaderSyncStatus(...args);
  const loadIssues = (...args) => AV.loadIssues(...args);
  const loadPeople = (...args) => AV.loadPeople(...args);
  const loadQuarantine = (...args) => AV.loadQuarantine(...args);
  const loadStatus = (...args) => AV.loadStatus(...args);
  const loadVideos = (...args) => AV.loadVideos(...args);
  const loadVisualDuplicateResults = (...args) => AV.loadVisualDuplicateResults(...args);
  const openBulkMetadataEditor = (...args) => AV.openBulkMetadataEditor(...args);
  const previewAutoMergeExact = (...args) => AV.previewAutoMergeExact(...args);
  const renderVideos = (...args) => AV.renderVideos(...args);
  const reviewAvailability = (...args) => AV.reviewAvailability(...args);
  const reviewQuarantine = (...args) => AV.reviewQuarantine(...args);
  function toggleTheme() {
    const root = document.documentElement;
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    root.dataset.theme = next; localStorage.setItem('adminV2Theme', next);
    $('#themeToggle').setAttribute('aria-label', next === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
  }
  function setupTheme() {
    const saved = localStorage.getItem('adminV2Theme');
    const preferred = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    document.documentElement.dataset.theme = saved || preferred;
    $('#themeToggle').setAttribute('aria-label', (saved || preferred) === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
  }
  function setupEvents() {
    addEventListener('popstate', renderLocation);
    addEventListener('hashchange', renderLocation);
    $('#themeToggle').addEventListener('click', toggleTheme);
    document.querySelectorAll('[data-close-dialog]').forEach(button => {
      button.addEventListener('click', () => {
        const dialog = document.getElementById(button.dataset.closeDialog);
        if (dialog) closeDialog(dialog);
      });
    });
    const changeIssueFilters = () => {
      state.issues = [];
      state.issueFiltersInitialized = true;
      writeIssueFiltersToUrl();
      loadIssues();
    };
    $('#issueSeverity').addEventListener('change', changeIssueFilters);
    $('#issueKind').addEventListener('change', changeIssueFilters);
    $('#issuesLoadMore').addEventListener('click', () => loadIssues({ append: true }));
    $('#duplicateRefresh').addEventListener('click', () => loadDuplicateCandidates({ refresh: true }));
    $('#autoMergeExact').addEventListener('click', previewAutoMergeExact);
    $('#duplicateCategory').addEventListener('change', () => {
      state.duplicateOffset = 0;
      $('#duplicateRows').replaceChildren();
      $('#duplicatePagination').hidden = true;
      loadDuplicateCandidates({ cacheOnly: true });
    });
    $('#duplicatePrev').addEventListener('click', () => { state.duplicateOffset = Math.max(0, state.duplicateOffset - DUPLICATE_PAGE_SIZE); loadDuplicateCandidates(); });
    $('#duplicateNext').addEventListener('click', () => { state.duplicateOffset += DUPLICATE_PAGE_SIZE; loadDuplicateCandidates(); });
    $('#duplicateMatchCatalog').addEventListener('click', () => selectDuplicateMatch('catalog'));
    $('#duplicateMatchVisual').addEventListener('click', () => selectDuplicateMatch('visual'));
    $('#peopleQuery').addEventListener('input', () => {
      clearTimeout(state.peopleTimer); state.peopleOffset = 0;
      state.peopleTimer = setTimeout(loadPeople, 250);
    });
    $('#peoplePrev').addEventListener('click', () => { state.peopleOffset = Math.max(0, state.peopleOffset - 50); loadPeople(); });
    $('#peopleNext').addEventListener('click', () => { state.peopleOffset += 50; loadPeople(); });
    $('#quarantinePrev').addEventListener('click', () => { state.quarantineOffset = Math.max(0, state.quarantineOffset - 50); loadQuarantine(); });
    $('#quarantineNext').addEventListener('click', () => { state.quarantineOffset += 50; loadQuarantine(); });
    $('#personDetailDialog').addEventListener('click', event => {
      if (event.target === $('#personDetailDialog')) closeDialog($('#personDetailDialog'));
    });
    $('#detailOverlay').addEventListener('cancel', event => {
      event.preventDefault(); closeDetail();
    });
    $('#detailOverlay').addEventListener('click', event => {
      if (event.target === $('#detailOverlay')) closeDetail();
    });
    $('#menuButton').addEventListener('click', () => {
      const open = !$('#sidebar').classList.contains('is-open'); $('#sidebar').classList.toggle('is-open', open); $('#menuButton').setAttribute('aria-expanded', String(open));
    });
    $('#libraryFilters').addEventListener('input', event => {
      if (event.target.name === 'q' || ['channel_id', 'studio', 'performer', 'tag'].includes(event.target.name)) debounceLoad();
    });
    $('#libraryFilters').addEventListener('change', event => {
      if (event.target.tagName === 'SELECT') debounceLoad();
    });
    $('#savedViews').addEventListener('change', event => {
      if (event.target.value) applySavedView(event.target.value);
      else $('#deleteView').disabled = true;
    });
    $('#saveView').addEventListener('click', saveCurrentView);
    $('#deleteView').addEventListener('click', deleteSavedView);
    $('#reviewQuarantine').addEventListener('click', reviewQuarantine);
    $('#checkAvailabilityBulk').addEventListener('click', () => reviewAvailability([...state.selectedVideos.keys()]));
    $('#bulkEditMetadata').addEventListener('click', () => openBulkMetadataEditor());
    $('#operationDialog').addEventListener('click', event => {
      if (event.target === $('#operationDialog')) closeDialog($('#operationDialog'));
    });
    $('#metadataDialog').addEventListener('click', event => {
      if (event.target === $('#metadataDialog')) closeDialog($('#metadataDialog'));
    });
    $('#metadataDialog').addEventListener('close', () => {
      state.metadataDialogSeq += 1;
      state.metadataPlanId = null;
    });
    $('#operationDialog').addEventListener('close', () => {
      beginOperationRequest();
      state.operationStatusSeq += 1;
      state.operationRefreshing = false;
      clearTimeout(state.operationPollTimer); state.operationPollTimer = null;
      state.operationPreviewContext = null;
      if (state.duplicatePending?.action === 'hide') {
        state.duplicatePending = null;
        $('#duplicateRows').querySelectorAll('button').forEach(button => { if (button.isConnected) button.disabled = false; });
      }
    });
    renderSavedViews();
    $('#clearFilters').addEventListener('click', () => {
      $('#libraryFilters').reset(); clearLibrarySelection(); resetCatalogPage(); commitFilterUrl(); loadVideos();
    });
    $('#viewToggle').addEventListener('click', event => {
      const button = event.currentTarget; const tableMode = button.getAttribute('aria-pressed') !== 'true'; button.setAttribute('aria-pressed', String(tableMode));
      button.querySelector('.view-toggle-label').textContent = tableMode ? 'Card view' : 'Table view';
      if (state.list) renderVideos({ items: state.list, page: { offset: state.offset, total: state.total, total_exact: state.total !== null, has_more: state.nextOffset !== null, next_cursor: state.nextCatalogCursor, previous_cursor: state.previousCatalogCursor }, data: { stale: state.isStale } });
    });
    $('#pagePrev').addEventListener('click', () => {
      if (state.previousCatalogCursor) {
        state.offset = Math.max(0, state.offset - LIMIT);
        state.catalogCursor = state.previousCatalogCursor;
        state.catalogCursorDirection = 'before';
      } else {
        state.offset = Math.max(0, state.offset - LIMIT);
        state.catalogCursor = null;
        state.catalogCursorDirection = 'after';
      }
      commitFilterUrl(); loadVideos();
    });
    $('#pageNext').addEventListener('click', () => {
      if (state.nextOffset === null) return;
      state.offset = state.nextOffset;
      state.catalogCursor = state.nextCatalogCursor;
      state.catalogCursorDirection = 'after';
      commitFilterUrl(); loadVideos();
    });
    document.addEventListener('click', event => {
      const action = event.target.closest('[data-action]')?.dataset.action;
      if (action === 'close-detail') closeDetail();
      if (action === 'refresh-status') loadStatus();
      const link = event.target.closest('a[data-route]');
      if (link && link.hash) {
        const route = link.hash.slice(1);
        if (routeInfo[route]) { event.preventDefault(); go(route); }
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Tab') {
        const dialogs = $$('dialog[open]');
        const modal = dialogs[dialogs.length - 1];
        if (modal) trapModalFocus(event, modal);
      }
    });
  }
  function trapModalFocus(event, dialog) {
    const focusable = $$('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])', dialog)
      .filter(element => element.getClientRects().length > 0 && !element.hidden);
    if (!focusable.length) { event.preventDefault(); dialog.focus(); return; }
    const first = focusable[0], last = focusable[focusable.length - 1];
    const activeIndex = focusable.indexOf(document.activeElement);
    if (!dialog.contains(document.activeElement) || activeIndex < 0) {
      event.preventDefault(); (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  }
  // Every Admin V2 script has published its bindings; freeze the namespace before start-up.
  Object.freeze(AV);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { loadHeaderStatus(); loadHeaderSyncStatus(); }
  });
  state.headerStatusTimer = setInterval(loadHeaderStatus, 15_000);
  state.headerSyncTimer = setInterval(loadHeaderSyncStatus, 60_000);
  state.visualResultsTimer = setInterval(() => {
    if (duplicateGroupIsVisible('visual')) loadVisualDuplicateResults();
  }, 30_000);
  setupTheme(); setupEvents();
  // Feature scripts register for adminv2:navigate after this router script.
  // Wait for DOMContentLoaded (all defer scripts have run) before the initial
  // route dispatch so direct links such as /admin/v2/#system load their data.
  if (document.readyState === 'complete') renderLocation();
  else document.addEventListener('DOMContentLoaded', renderLocation, { once: true });
  loadHeaderStatus();
  loadHeaderSyncStatus();
  loadStatus();
  void loadChannelFilterOptions();
})();
