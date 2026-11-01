(() => {
  'use strict';

  const V2_API = '/admin/api/v2';
  const LIMIT = 50;
  const DUPLICATE_PAGE_SIZE = 25;
  const DUPLICATE_SCAN_BATCH_SIZE = 200;
  const DUPLICATE_SCAN_CACHE_TTL_MS = 30_000;
  const SAVED_VIEWS_KEY = 'adminV2SavedViews';
  const ISSUE_SEVERITIES = new Set(['critical', 'high', 'medium', 'low']);
  const ISSUE_KINDS = new Set(['sync_failure', 'sync_never_run', 'sync_backlog', 'job_failure', 'job_stalled', 'unavailable_video', 'missing_metadata', 'missing_nfo', 'invalid_nfo', 'nfo_metadata_mismatch', 'missing_artwork', 'backup', 'quality_finding', 'quality_audit', 'system']);
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { route: 'start', detailId: null, libraryTab: 'videos', duplicateMatch: 'catalog', performersTab: 'review', performerArea: '', qualityTab: null, systemTab: null, peopleDirectoryQuery: '', status: null, statusLoaded: false, statusRequestSeq: 0,
    services: { checking: true }, problemSummary: null, systemStatusInFlight: false,
    headerStatusInFlight: false, headerStatusTimer: null, headerSyncInFlight: false, headerSyncTimer: null,
    list: null, lastSuccessfulQuery: null, offset: 0, total: null, nextOffset: null,
    catalogCursor: null, catalogCursorDirection: 'after', nextCatalogCursor: null, previousCatalogCursor: null, requestSeq: 0,
    listAbort: null, detailAbort: null, availabilityHistorySeq: 0, searchTimer: null,
    filterHistoryActive: false, isStale: false,
    issues: [], issueTotal: 0, issueTruncated: false, issueLoading: false, issueRequestSeq: 0, issueFiltersInitialized: false,
    selectedVideos: new Map(), operationPlan: null, operationRequest: null, operationId: null, operationPreviewContext: null, operationPreviewStale: false,
    operationRequestSeq: 0, operationStatusSeq: 0, detailVideo: null,
    metadataRequest: null, metadataPlanId: null, metadataDialogSeq: 0, metadataBulkVideoIds: null, metadataBulkDraft: null,
    duplicateOffset: 0, duplicateTotal: 0, duplicateItems: [], duplicateBusy: false, duplicateRequestSeq: 0,
    duplicateScans: [], duplicateRequests: new Map(),
    duplicatePending: null, visualRequestSeq: 0,
    peopleOffset: 0, peopleTotal: 0, peopleBusy: false, peopleRequestSeq: 0, peopleTimer: null,
    quarantineOffset: 0, quarantineTotal: 0, quarantineBusy: false, quarantineRequestSeq: 0,
    pendingPersonMerge: null,
    personMergePreviewSeq: 0,
    personFilmographySeq: 0,
    availabilityRequest: null,
    operationPollTimer: null, operationRefreshing: false, detailOpener: null, sideDetailId: null };
  const routeInfo = {
    start: { title: 'Overview', intro: 'Catalog size, synchronization, tasks and open problems at a glance.' },
    library: { title: 'Library', intro: 'Find videos and check their source and availability.' },
    sources: { title: 'Sources', intro: 'Channels and synchronization history.' },
    performers: { title: 'Performers', intro: 'Browse the performer catalog, or review the names that still need a decision.' },
    jobs: { title: 'Tasks', intro: 'The operation queue and history.' },
    schedules: { title: 'Schedules', intro: 'Recurring tasks and their schedules.' },
    system: { title: 'System', intro: 'Service status, backups and maintenance.' }
  };
  const LIBRARY_TABS = ['videos', 'duplicates', 'suggestions', 'quarantine'];
  const PERFORMER_TABS = ['directory', 'review'];
  const PERFORMER_AREAS = ['proposals', 'name-variants'];
  const DUPLICATE_MATCHES = ['catalog', 'visual'];
  const navOwner = route => route;

  function safeText(value, fallback = '—') {
    if (value === null || value === undefined || value === '') return fallback;
    if (Array.isArray(value)) return value.map(item => safeText(item, '')).filter(Boolean).join(', ') || fallback;
    if (typeof value === 'object') return value.name || value.title || value.label || fallback;
    return String(value);
  }
  function first(object, keys) {
    for (const key of keys) if (object && object[key] !== undefined && object[key] !== null && object[key] !== '') return object[key];
    return null;
  }
  function beginOperationRequest() { state.operationRequestSeq += 1; return state.operationRequestSeq; }
  function operationRequestIsCurrent(sequence, route = state.route) {
    return state.operationRequestSeq === sequence && state.route === route;
  }
  function responseError(payload, fallback) {
    const error = payload && payload.error;
    if (typeof error === 'string' && error.trim()) return error.trim();
    if (error && typeof error === 'object' && typeof error.message === 'string' && error.message.trim()) return error.message.trim();
    return fallback;
  }
  function urlFor(route, extra = {}) {
    const url = new URL(location.href);
    for (const [key, value] of Object.entries(extra)) {
      if (value === null || value === undefined || value === '') url.searchParams.delete(key);
      else url.searchParams.set(key, String(value));
    }
    url.hash = route;
    return url;
  }
  function go(route, extra = {}, replace = false) {
    const url = urlFor(route, extra);
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
    renderLocation();
  }
  // Stage 3 moved screens into Library and Performers tabs; old addresses are
  // rewritten to the canonical one so bookmarks and issue links keep working.
  let legacyScreenNotice = '';
  function legacyHashRewrite(hash) {
    if (hash.startsWith('film/')) return null;
    const [route, query = ''] = hash.split('?', 2);
    const params = new URLSearchParams(query);
    const tab = params.get('tab') || '';
    const target = (nextRoute, nextTab, extra) => {
      params.delete('tab'); params.delete('match'); params.delete('area');
      if (nextTab) params.set('tab', nextTab);
      for (const [key, value] of Object.entries(extra || {})) params.set(key, value);
      return `#${nextRoute}${params.toString() ? `?${params}` : ''}`;
    };
    const retired = (nextRoute = 'system', nextTab = 'status') => {
      legacyScreenNotice = 'This screen has been retired. The related automatic work continues in the background.';
      return target(nextRoute, nextTab === 'status' ? '' : nextTab);
    };
    if (route === 'jobs' && tab === 'schedules') {
      legacyScreenNotice = 'Schedules now have their own section.';
      return target('schedules');
    }
    if (route === 'organize') {
      return tab === 'people' ? target('performers', 'directory')
        : target('library', tab === 'quarantine' ? 'quarantine' : 'duplicates');
    }
    if (route === 'library' && tab === 'visual') return target('library', 'duplicates', { match: 'visual' });
    if (route === 'library' && tab === 'proposals') return target('library', 'suggestions');
    if (route === 'quality' && tab === 'visual') return target('library', 'duplicates', { match: 'visual' });
    if (route === 'quality' && tab === 'enrichment') return target('library', 'suggestions');
    if (route === 'quality' && ['duplicates', 'quarantine'].includes(tab)) return target('library', tab);
    if (route === 'quality' || ['catalog-health', 'cataloghealth', 'diagnostics', 'playback-diagnostics', 'admin-history', 'activity', 'linkcheck', 'enrichment-progress', 'audit-history', 'trends'].includes(route)) return retired();
    if (route === 'system' && ['diagnostics', 'playback', 'playback-diagnostics', 'activity', 'admin-history', 'audit', 'audits', 'audit-history', 'consistency', 'linkcheck', 'links', 'availability', 'enrichment-progress', 'trends'].includes(tab)) return retired();
    if (route === 'system' && tab === 'automation') { legacyScreenNotice = 'Schedules are available in their own section.'; return '#schedules'; }
    if (route === 'performers' && ['proposals', 'matching'].includes(tab)) {
      return target('performers', 'review', { area: tab === 'matching' ? 'name-variants' : 'proposals' });
    }
    return null;
  }
  function libraryHash(tab, match) {
    const params = new URLSearchParams();
    if (tab && tab !== 'videos') params.set('tab', tab);
    if (tab === 'duplicates' && match === 'visual') params.set('match', 'visual');
    const query = params.toString();
    return query ? `library?${query}` : 'library';
  }
  function currentRoute() {
    const rawHash = location.hash.replace(/^#/, '');
    const rewritten = legacyHashRewrite(rawHash);
    if (rewritten) history.replaceState({}, '', rewritten);
    const hash = rewritten ? rewritten.slice(1) : rawHash;
    const previous = { libraryTab: state.libraryTab || 'videos', duplicateMatch: state.duplicateMatch || 'catalog' };
    if (hash.startsWith('film/')) {
      try { return { route: 'library', primaryRoute: 'library', detailId: decodeURIComponent(hash.slice(5)), ...previous }; } catch { return { route: 'library', primaryRoute: 'library', detailId: hash.slice(5), ...previous }; }
    }
    const [route, query = ''] = hash.split('?', 2);
    const params = new URLSearchParams(query);
    const tab = params.get('tab') || '';
    if (route === 'system' && ['sync', 'sync-activity', 'sync_activity', 'history'].includes(tab)) {
      history.replaceState({}, '', '#sources?tab=history');
      return { route: 'sources', primaryRoute: 'sources', sourcesTab: 'history', detailId: null };
    }
    if (route === 'system') {
      return { route, primaryRoute: route, systemTab: ['backups', 'maintenance'].includes(tab) ? tab : 'status', detailId: null };
    }
    if (route === 'library') {
      const libraryTab = LIBRARY_TABS.includes(tab) ? tab : 'videos';
      const duplicateMatch = libraryTab === 'duplicates' && params.get('match') === 'visual' ? 'visual' : 'catalog';
      return { route, primaryRoute: 'library', detailId: null, libraryTab, duplicateMatch,
        focusTarget: duplicateMatch === 'visual' ? '#visualDuplicateTitle' : null };
    }
    if (route === 'performers') {
      const performersTab = PERFORMER_TABS.includes(tab) ? tab : 'review';
      const area = params.get('area') || '';
      const performerArea = performersTab === 'review' && PERFORMER_AREAS.includes(area) ? area : '';
      return { route, primaryRoute: 'performers', detailId: null, performersTab, performerArea,
        focusTarget: performerArea === 'name-variants' ? '#fuzzyTitle' : performerArea ? '#performerProposalsTitle' : null };
    }
    return routeInfo[route] ? { route, primaryRoute: route, detailId: null } : { route: 'start', primaryRoute: 'start', detailId: null };
  }
  function currentIssueFilterKey() {
    const params = new URLSearchParams();
    const severity = $('#issueSeverity').value;
    const kind = $('#issueKind').value;
    if (severity) params.set('issue_severity', severity);
    if (kind) params.set('issue_kind', kind);
    return params.toString();
  }
  function readIssueFiltersFromUrl() {
    const params = new URLSearchParams(location.search);
    const severity = params.get('issue_severity') || '';
    const kind = params.get('issue_kind') || '';
    $('#issueSeverity').value = ISSUE_SEVERITIES.has(severity) ? severity : '';
    $('#issueKind').value = ISSUE_KINDS.has(kind) ? kind : '';
    writeIssueFiltersToUrl(true);
  }
  function writeIssueFiltersToUrl(replace = false) {
    const url = new URL(location.href);
    url.searchParams.delete('issue_severity');
    url.searchParams.delete('issue_kind');
    const severity = $('#issueSeverity').value;
    const kind = $('#issueKind').value;
    if (ISSUE_SEVERITIES.has(severity)) url.searchParams.set('issue_severity', severity);
    if (ISSUE_KINDS.has(kind)) url.searchParams.set('issue_kind', kind);
    if (url.href === location.href) return;
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }
  function issueFeedUrl(offset = 0) {
    const params = new URLSearchParams({ limit: '50', offset: String(offset) });
    const severity = $('#issueSeverity').value;
    const kind = $('#issueKind').value;
    if (ISSUE_SEVERITIES.has(severity)) params.set('severity', severity);
    if (ISSUE_KINDS.has(kind)) params.set('kind', kind);
    return `${V2_API}/issues?${params}`;
  }
  const SECTION_TAB_PANES = {
    libraryTabList: { videos: '#libraryVideosPane', duplicates: '#libraryDuplicatesPane',
      suggestions: '#librarySuggestionsPane', quarantine: '#libraryQuarantinePane' },
    performersTabList: { directory: '#performerDirectoryPane', review: '#performerReviewPane' }
  };
  // Each tab anchor keeps its panel id in sync, following the <name>Tab / <name>Pane convention.
  function syncSectionTabs(listId, activeTab) {
    for (const [name, selector] of Object.entries(SECTION_TAB_PANES[listId] || {})) {
      const selected = name === activeTab;
      const pane = $(selector);
      if (pane) pane.hidden = !selected;
      const tab = $(selector.replace(/Pane$/, 'Tab'));
      if (tab) tab.setAttribute('aria-selected', String(selected));
    }
  }
  function setDuplicateMatch(match) {
    const active = match === 'visual' ? 'visual' : 'catalog';
    for (const name of DUPLICATE_MATCHES) {
      const button = $(name === 'visual' ? '#duplicateMatchVisual' : '#duplicateMatchCatalog');
      if (button) button.setAttribute('aria-pressed', String(name === active));
      const group = $(name === 'visual' ? '#duplicateVisualGroup' : '#duplicateCatalogGroup');
      if (group) group.hidden = name !== active;
    }
    return active;
  }
  function duplicateGroupIsVisible(match) {
    const group = $(match === 'visual' ? '#duplicateVisualGroup' : '#duplicateCatalogGroup');
    return state.route === 'library' && !$('#libraryDuplicatesPane').hidden && group && !group.hidden;
  }
  function selectDuplicateMatch(match) {
    if (state.duplicateMatch === match) return;
    history.pushState({}, '', urlFor(libraryHash('duplicates', match)));
    renderLocation();
  }
  const ADVANCED_FILTER_NAMES = ['studio', 'performer', 'tag', 'missing', 'sort'];
  function updateMoreFilterCount() {
    const badge = $('#moreFilterCount');
    if (!badge) return;
    const form = $('#libraryFilters');
    const active = ADVANCED_FILTER_NAMES.filter(name => {
      const field = form.elements.namedItem(name);
      const value = field ? field.value.trim() : '';
      return value && !(name === 'sort' && value === 'first_seen');
    }).length;
    badge.hidden = active === 0;
    badge.textContent = active ? String(active) : '';
  }
  function renderLocation() {
    const next = currentRoute();
    setHidden('#taskReceipt', true);
    const routeChanged = next.route !== state.route;
    const detailChanged = next.detailId !== state.detailId;
    const libraryTabChanged = (next.libraryTab || 'videos') !== state.libraryTab;
    const duplicateMatchChanged = (next.duplicateMatch || 'catalog') !== state.duplicateMatch;
    const performersTabChanged = (next.performersTab || 'review') !== state.performersTab;
    const systemTabChanged = (next.systemTab || null) !== state.systemTab;
    const peopleDirectoryQuery = next.route === 'performers' && next.performersTab === 'directory'
      ? routeQueryParams().get('person') || '' : '';
    const peopleDirectoryQueryChanged = peopleDirectoryQuery !== state.peopleDirectoryQuery;
    const previousIssueFilterKey = state.route === 'start' && state.issueFiltersInitialized
      ? currentIssueFilterKey() : null;
    const previousLibraryParams = state.route === 'library' ? filterParams() : null;
    const previousLibraryQuery = previousLibraryParams?.toString() ?? null;
    const filterStateKey = params => {
      const filters = new URLSearchParams(params);
      filters.delete('offset');
      filters.delete('cursor');
      filters.delete('cursor_direction');
      return filters.toString();
    };
    const previousLibraryFilters = previousLibraryParams ? filterStateKey(previousLibraryParams) : null;
    clearTimeout(state.searchTimer);
    state.searchTimer = null;
    state.filterHistoryActive = false;
    if (routeChanged || libraryTabChanged || duplicateMatchChanged || performersTabChanged) {
      beginOperationRequest();
      state.visualRequestSeq += 1;
      state.duplicateRequestSeq += 1;
      state.peopleRequestSeq += 1;
      state.quarantineRequestSeq += 1;
      state.requestSeq += 1;
      state.listAbort?.abort();
      if ($('#operationDialog')?.open) closeDialog($('#operationDialog'));
      if ($('#metadataDialog')?.open) closeDialog($('#metadataDialog'));
    }
    if (routeChanged && state.route === 'library' && next.route !== 'library') clearLibrarySelection();
    const restoreDetailFocus = detailChanged && !next.detailId && next.route === 'library'
      ? state.detailOpener : null;
    if (detailChanged && !next.detailId) state.detailOpener = null;
    state.route = next.route;
    state.detailId = next.detailId;
    state.libraryTab = next.libraryTab || 'videos';
    state.duplicateMatch = next.duplicateMatch || 'catalog';
    state.performersTab = next.performersTab || 'review';
    state.performerArea = next.performerArea || '';
    state.qualityTab = next.qualityTab || null;
    state.systemTab = next.systemTab || null;
    state.peopleDirectoryQuery = peopleDirectoryQuery;
    if (peopleDirectoryQueryChanged) {
      $('#peopleQuery').value = peopleDirectoryQuery;
      state.peopleOffset = 0;
    }
    if (next.route === 'start') {
      readIssueFiltersFromUrl();
      state.issueFiltersInitialized = true;
    }
    $$('.view[data-view]').forEach(node => { node.hidden = node.dataset.view !== (['start', 'library', 'performers'].includes(state.route) ? state.route : 'operational'); });
    $$('.primary-nav a[data-route]').forEach(link => {
      // Legacy screens stay reachable; the menu marks the section that owns them.
      const active = link.dataset.route === navOwner(next.primaryRoute || state.route);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    setText($('#crumbCurrent'), next.detailId ? 'Video details' : routeInfo[next.primaryRoute || state.route].title);
    $('#systemSectionTabs').hidden = next.route !== 'system';
    $$('#systemSectionTabs [data-system-tab]').forEach(link => {
      if (link.dataset.systemTab === (next.systemTab || 'status')) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    syncSectionTabs('libraryTabList', state.libraryTab);
    setHidden('#librarySummary', state.libraryTab !== 'videos');
    syncSectionTabs('performersTabList', state.performersTab);
    setDuplicateMatch(state.duplicateMatch);
    if (!['start', 'library', 'performers'].includes(next.route)) {
      // Keep the canonical route in state; only the view key is shared.
      state.route = next.route;
      showOperationalView(next);
    }
    const sidebar = $('#sidebar');
    sidebar.classList.remove('is-open');
    $('#menuButton').setAttribute('aria-expanded', 'false');
    if (routeChanged && next.route === 'start') loadStatus();
    else if (next.route === 'start' && previousIssueFilterKey !== null
      && previousIssueFilterKey !== currentIssueFilterKey()) {
      state.issues = [];
      loadIssues();
    }
    // The service-state panel lives in System, so its own read starts there.
    if (legacyScreenNotice) {
      const notice = $('#legacyScreenNotice'); notice.hidden = false; notice.textContent = legacyScreenNotice;
      legacyScreenNotice = '';
    } else setHidden('#legacyScreenNotice', true);
    if (next.route === 'system' && (next.systemTab || 'status') === 'status'
      && (routeChanged || systemTabChanged)) void loadSystemStatus();
    if (next.route === 'library') {
      syncFiltersFromUrl();
      const currentLibraryParams = filterParams();
      const libraryQueryChanged = previousLibraryQuery !== null
        && previousLibraryQuery !== currentLibraryParams.toString();
      const libraryFiltersChanged = previousLibraryFilters !== null
        && previousLibraryFilters !== filterStateKey(currentLibraryParams);
      if (libraryFiltersChanged) clearLibrarySelection();
      if (detailChanged && next.detailId) loadDetail(next.detailId);
      else if (detailChanged && !next.detailId) closeDetailVisual();
      if ((routeChanged || libraryTabChanged || libraryQueryChanged) && !next.detailId) loadVideos();
      else if (routeChanged && next.detailId && $('#detailOverlay').open) loadVideos();
    } else {
      closeDetailVisual();
    }
    if (next.route === 'library' && (routeChanged || libraryTabChanged || duplicateMatchChanged)) {
      showLibraryTab(state.libraryTab, state.duplicateMatch);
    }
    if (next.route === 'performers' && (routeChanged || performersTabChanged || peopleDirectoryQueryChanged)) {
      showPerformersTab(state.performersTab);
    }
    document.dispatchEvent(new CustomEvent('adminv2:navigate', {
      detail: {
        route: state.route, detailId: state.detailId, tab: state.qualityTab,
        libraryTab: state.route === 'library' ? state.libraryTab : null,
        duplicateMatch: state.route === 'library' && state.libraryTab === 'duplicates' ? state.duplicateMatch : null,
        performersTab: state.route === 'performers' ? state.performersTab : null,
        performerArea: state.route === 'performers' ? state.performerArea : null,
        systemTab: state.systemTab, sourcesTab: next.sourcesTab || null
      }
    }));
    if (next.focusTarget) requestAnimationFrame(() => {
      const target = $(next.focusTarget);
      target?.focus({ preventScroll: true });
      if (next.duplicateMatch === 'visual') target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    if (restoreDetailFocus?.isConnected) requestAnimationFrame(() => restoreDetailFocus.focus({ preventScroll: true }));
  }
  function showLibraryTab(tab, match) {
    if (tab === 'duplicates') {
      if (match === 'visual') loadVisualDuplicateResults();
      else loadDuplicateCandidates({ cacheOnly: true });
      return;
    }
    if (tab === 'quarantine') loadQuarantine();
  }
  function showPerformersTab(tab) {
    if (tab === 'directory') loadPeople();
  }
  function showOperationalView(routeState) {
    const route = routeState.route;
    setText($('#operationalTitle'), routeInfo[route].title);
    setText($('#operationalIntro'), routeInfo[route].intro);
    setText($('#operationalEyebrow'), routeInfo[route].title.toUpperCase());
    $('#systemSchedulesView').hidden = route !== 'schedules';
    const systemTab = routeState.systemTab || 'status';
    $('#systemStatusView').hidden = route !== 'system' || systemTab !== 'status';
    $('#systemBackupView').hidden = route !== 'system' || systemTab !== 'backups';
    $('#systemMaintenanceView').hidden = route !== 'system' || systemTab !== 'maintenance';
  }
  function syncFiltersFromUrl() {
    const params = routeQueryParams();
    const form = $('#libraryFilters');
    for (const name of ['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort']) {
      const field = form.elements.namedItem(name);
      if (field) field.value = params.get(name) || (name === 'sort' ? 'first_seen' : '');
    }
    state.offset = Math.max(0, Number(params.get('offset')) || 0);
    state.catalogCursor = params.get('cursor') || null;
    state.catalogCursorDirection = params.get('cursor_direction') === 'before' ? 'before' : 'after';
    updateMoreFilterCount();
  }
  function routeQueryParams() {
    const params = new URLSearchParams(location.search);
    const hashQuery = location.hash.includes('?') ? location.hash.slice(location.hash.indexOf('?') + 1) : '';
    for (const [key, value] of new URLSearchParams(hashQuery)) if (!params.has(key)) params.set(key, value);
    return params;
  }
  function filterParams() {
    const form = $('#libraryFilters');
    const result = new URLSearchParams();
    for (const name of ['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort']) {
      const field = form.elements.namedItem(name);
      const value = field ? field.value.trim() : '';
      if (value) result.set(name, value);
    }
    if (state.offset > 0) result.set('offset', String(state.offset));
    if (state.catalogCursor) {
      result.set('cursor', state.catalogCursor);
      if (state.catalogCursorDirection === 'before') result.set('cursor_direction', 'before');
    }
    return result;
  }
  function resetCatalogPage() {
    state.offset = 0;
    state.catalogCursor = null;
    state.catalogCursorDirection = 'after';
    state.nextCatalogCursor = null;
    state.previousCatalogCursor = null;
  }
  function readSavedViews() {
    try {
      const parsed = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.filter(view => view && typeof view.name === 'string' && view.filters && typeof view.filters === 'object').slice(0, 20) : [];
    } catch { return []; }
  }
  function renderSavedViews(selected = '') {
    const select = $('#savedViews');
    if (!select) return;
    const views = readSavedViews();
    select.replaceChildren(new Option('Choose a saved view', ''));
    views.forEach((view, index) => select.add(new Option(view.name.slice(0, 80), String(index))));
    select.value = selected;
    $('#deleteView').disabled = !selected || !views[Number(selected)];
  }
  function applySavedView(index) {
    const view = readSavedViews()[Number(index)];
    if (!view || !view.filters) return;
    clearLibrarySelection();
    const form = $('#libraryFilters');
    for (const name of ['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort']) {
      const field = form.elements.namedItem(name);
      if (field) field.value = typeof view.filters[name] === 'string' ? view.filters[name] : '';
    }
    resetCatalogPage();
    updateMoreFilterCount();
    commitFilterUrl();
    loadVideos();
  }
  function saveCurrentView() {
    const name = (prompt('Name this saved view:') || '').trim().slice(0, 80);
    if (!name) return;
    const filters = Object.fromEntries(filterParams());
    delete filters.offset; delete filters.cursor; delete filters.cursor_direction;
    const views = readSavedViews();
    const existing = views.findIndex(view => view.name.toLocaleLowerCase() === name.toLocaleLowerCase());
    if (existing >= 0) views[existing] = { name, filters };
    else if (views.length < 20) views.push({ name, filters });
    else { announce('You can save up to 20 views. Delete one before saving another.'); return; }
    try { localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views)); }
    catch { announce('Your browser could not save this view.'); return; }
    renderSavedViews(String(existing >= 0 ? existing : views.length - 1));
    announce(`Saved view “${name}”.`);
  }
  function deleteSavedView() {
    const index = Number($('#savedViews').value);
    const views = readSavedViews();
    if (!Number.isInteger(index) || !views[index]) return;
    const name = views[index].name;
    if (!confirm(`Delete saved view “${name}”?`)) return;
    views.splice(index, 1);
    try { localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views)); }
    catch { announce('Your browser could not update saved views.'); return; }
    renderSavedViews();
    announce(`Deleted saved view “${name}”.`);
  }
  function writeFilterUrl(replace = true) {
    const url = new URL(location.href);
    const hashQuery = new URLSearchParams(url.hash.includes('?') ? url.hash.slice(url.hash.indexOf('?') + 1) : '');
    const route = url.hash.replace(/^#/, '').split('?', 2)[0];
    url.hash = route;
    const keep = new Set(['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort', 'offset', 'cursor', 'cursor_direction']);
    for (const key of keep) url.searchParams.delete(key);
    for (const [key, value] of filterParams()) url.searchParams.set(key, value);
    // Library keeps its selected tab in the hash, so filter writes must not drop it.
    if (route === 'library') url.hash = libraryHash(hashQuery.get('tab'), hashQuery.get('match'));
    if (url.href === location.href) return;
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }
  function commitFilterUrl() {
    clearTimeout(state.searchTimer);
    state.searchTimer = null;
    state.filterHistoryActive = false;
    writeFilterUrl(false);
  }
  async function requestJson(url, { method = 'GET', payload, headers, signal } = {}) {
    const requestHeaders = new Headers({ 'Accept': 'application/json' });
    new Headers(headers || {}).forEach((value, key) => requestHeaders.set(key, value));
    if (payload !== undefined && !requestHeaders.has('Content-Type')) requestHeaders.set('Content-Type', 'application/json');
    const response = await fetch(url, {
      method, credentials: 'same-origin', headers: requestHeaders, signal,
      ...(payload !== undefined ? { body: JSON.stringify(payload) } : {})
    });
    let body;
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok || !body || body.ok === false) {
      const message = body && (body.message || (body.error && body.error.message) || body.error);
      const error = new Error(typeof message === 'string' ? message : `Could not complete the request (HTTP ${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return body;
  }
  const getJson = (url, signal) => requestJson(url, { signal });
  async function postJson(url, payload) {
    const capabilities = await getJson(`${V2_API}/capabilities`);
    if (capabilities.actions_enabled !== true) {
      if (['worker_unavailable', 'worker_incompatible'].includes(capabilities.read_only_reason)) {
        throw new Error('Admin actions are paused because the sync worker is unavailable or does not support all required actions.');
      }
      throw new Error(capabilities.read_only_reason === 'admin_disabled'
        ? 'Admin actions are disabled.'
        : 'This panel is read-only.');
    }
    const headers = new Headers({ 'Accept': 'application/json' });
    if (capabilities.token_required === true) {
      let token = sessionStorage.getItem('adminWriteToken') || sessionStorage.getItem('duplicateActionToken') || '';
      if (!token) token = prompt('Enter the admin write token:') || '';
      token = token.trim();
      if (!token) throw new Error('An admin write token is required.');
      sessionStorage.setItem('adminWriteToken', token);
      headers.set('Authorization', `Bearer ${token}`);
    }
    try {
      return await requestJson(url, { method: 'POST', payload, headers });
    } catch (error) {
      if (error.status === 403 && capabilities.token_required === true) {
        sessionStorage.removeItem('adminWriteToken');
        sessionStorage.removeItem('duplicateActionToken');
      }
      throw error;
    }
  }
  async function putJson(url, payload) {
    const capabilities = await getJson(`${V2_API}/capabilities`);
    if (capabilities.actions_enabled !== true) {
      if (['worker_unavailable', 'worker_incompatible'].includes(capabilities.read_only_reason)) {
        throw new Error('Admin actions are paused because the sync worker is unavailable or does not support all required actions.');
      }
      throw new Error(capabilities.read_only_reason === 'admin_disabled'
        ? 'Admin actions are disabled.' : 'This panel is read-only.');
    }
    const headers = new Headers({ Accept: 'application/json' });
    if (capabilities.token_required === true) {
      let token = sessionStorage.getItem('adminWriteToken') || sessionStorage.getItem('duplicateActionToken') || '';
      if (!token) token = prompt('Enter the admin write token:') || '';
      token = token.trim();
      if (!token) throw new Error('An admin write token is required.');
      sessionStorage.setItem('adminWriteToken', token);
      headers.set('Authorization', `Bearer ${token}`);
    }
    try {
      return await requestJson(url, { method: 'PUT', payload, headers });
    } catch (error) {
      if (error.status === 403 && capabilities.token_required === true) {
        sessionStorage.removeItem('adminWriteToken');
        sessionStorage.removeItem('duplicateActionToken');
      }
      throw error;
    }
  }
  async function deleteJson(url) {
    const capabilities = await getJson(`${V2_API}/capabilities`);
    if (capabilities.actions_enabled !== true) {
      if (['worker_unavailable', 'worker_incompatible'].includes(capabilities.read_only_reason)) {
        throw new Error('Admin actions are paused because the sync worker is unavailable or does not support all required actions.');
      }
      throw new Error(capabilities.read_only_reason === 'admin_disabled' ? 'Admin actions are disabled.' : 'This panel is read-only.');
    }
    const headers = new Headers({ 'Accept': 'application/json' });
    if (capabilities.token_required === true) {
      let token = sessionStorage.getItem('adminWriteToken') || sessionStorage.getItem('duplicateActionToken') || '';
      if (!token) token = prompt('Enter the admin write token:') || '';
      token = token.trim();
      if (!token) throw new Error('An admin write token is required.');
      sessionStorage.setItem('adminWriteToken', token);
      headers.set('Authorization', `Bearer ${token}`);
    }
    try {
      return await requestJson(url, { method: 'DELETE', headers });
    } catch (error) {
      if (error.status === 403 && capabilities.token_required === true) {
        sessionStorage.removeItem('adminWriteToken');
        sessionStorage.removeItem('duplicateActionToken');
      }
      throw error;
    }
  }
  const dialogOpeners = new WeakMap();
  const dialogCloseHandlers = new WeakSet();
  function openDialog(dialog, opener = document.activeElement) {
    if (!dialog) return;
    dialog.querySelector?.('.task-receipt')?.remove();
    if (dialog.open) return;
    if (opener && typeof opener.focus === 'function') dialogOpeners.set(dialog, opener);
    if (!dialogCloseHandlers.has(dialog)) {
      dialog.addEventListener('close', () => {
        const previous = dialogOpeners.get(dialog);
        dialogOpeners.delete(dialog);
        if (previous?.isConnected && typeof previous.focus === 'function') previous.focus();
      });
      dialogCloseHandlers.add(dialog);
    }
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }
  function closeDialog(dialog, restoreFocus = true) {
    if (!dialog) return;
    if (typeof dialog.close === 'function') {
      if (!restoreFocus) dialogOpeners.delete(dialog);
      if (dialog.open) dialog.close();
      else dialogOpeners.delete(dialog);
      return;
    }
    const previous = dialogOpeners.get(dialog);
    dialogOpeners.delete(dialog);
    dialog.removeAttribute('open');
    if (restoreFocus && previous?.isConnected && typeof previous.focus === 'function') {
      previous.focus({ preventScroll: true });
    }
  }
  function setFeedback(target, message, kind = 'status') {
    if (!target) return;
    const isError = kind === 'error';
    target.setAttribute('role', isError ? 'alert' : 'status');
    target.setAttribute('aria-live', isError ? 'assertive' : 'polite');
    target.setAttribute('aria-atomic', 'true');
    target.textContent = message;
  }
  function downloadBlob(filename, blob) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.style.position = 'fixed';
    link.style.left = '-10000px';
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  // Shared Admin V2 helpers. The other Admin V2 modules use them through window.AdminV2.
  function create(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function setText(target, value) { if (target) target.textContent = value; }
  function setHidden(selector, hidden) { const element = $(selector); if (element) element.hidden = Boolean(hidden); }
  function announce(message) { setText($('#liveMessage'), message); }
  async function copyToClipboard(value) {
    if (navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(value); return; } catch { /* Use the local fallback below. */ }
    }
    const field = document.createElement('textarea');
    field.value = value;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.left = '-10000px';
    document.body.append(field);
    field.select();
    const copied = document.execCommand('copy');
    field.remove();
    if (!copied) throw new Error('Clipboard access is unavailable.');
  }
  function appendTelegramId(host, video) {
    const id = typeof video?.id === 'string' ? video.id : '';
    if (!id.startsWith('telegram:')) return;
    const row = create('p', 'duplicate-telegram-id');
    row.append(create('span', '', 'Telegram ID: '));
    const button = create('button', '', id);
    button.type = 'button';
    button.title = 'Copy Telegram ID';
    button.setAttribute('aria-label', `Copy Telegram ID ${id}`);
    button.addEventListener('click', async () => {
      try { await copyToClipboard(id); announce('Telegram ID copied to clipboard.'); }
      catch { announce('Could not copy the Telegram ID.'); }
    });
    row.append(button);
    host.append(row);
  }
  function safeId(value) { return value === null || value === undefined || value === '' ? '' : String(value); }
  // Never surface raw errors, secrets, URLs, or filesystem paths in the panel.
  function scrub(value) {
    let text = value === null || value === undefined ? '' : String(value);
    text = text.replace(/\b[a-zA-Z][a-zA-Z0-9+.\-]*:\/\/[^\s"'<>)]+/g, '[link hidden]');
    text = text.replace(/\b[A-Za-z]:\\[^\s"'<>]*/g, '[path hidden]');
    text = text.replace(/(^|[\s("'=:])\/(?:[\w.\-]+\/)+[\w.\-]*/g, (match, prefix) => `${prefix}[path hidden]`);
    text = text.replace(/\b(pass(?:word|wd)?|secret|token|api[_-]?key|credentials?|authorization)\b\s*[:=]\s*\S+/gi, '$1: [hidden]');
    return text.trim();
  }
  function titleCase(value) {
    return safeId(value).replace(/[_-]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
  }
  function detailField(label, value) {
    const wrapper = create('div', 'detail-field');
    wrapper.append(create('dt', '', label), create('dd', '', value));
    return wrapper;
  }
  function formatDuration(ms, state) {
    const total = Math.max(0, Math.floor(Number(ms) / 1000));
    if (!total) return state === 'processing' ? '0s' : '—';
    const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
    if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
    if (minutes) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
    return `${seconds}s`;
  }
  const syncTimers = new WeakMap();
  function scheduleSync(callback) {
    if (typeof callback !== 'function') return;
    const previous = syncTimers.get(callback);
    if (previous !== undefined) clearTimeout(previous);
    syncTimers.set(callback, setTimeout(callback, 0));
  }
  function taskLink(id, queue = 'jobs') {
    const value = String(id || '');
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(value)) return null;
    const link = create('a', 'notice-link', 'View task');
    link.href = `/admin/v2/?job_id=${encodeURIComponent(value)}${['phash', 'visual'].includes(queue) ? '&job_queue=' + queue : ''}#jobs`;
    return link;
  }
  function showQueued(payload, message = '', target = $('#taskReceipt'), queue = 'jobs') {
    const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
    const ids = [...new Set([payload?.job_id, ...(payload?.job_ids || []), ...jobs.map(job => job.job_id)].filter(Boolean))];
    const label = `Queued${message ? ' · ' + message : ''}`;
    const targets = target ? [target] : [];
    const dialog = document.querySelector('dialog[open]');
    if (dialog && !dialog.contains(target)) {
      let receipt = dialog.querySelector('.task-receipt');
      if (!receipt) {
        receipt = create('div', 'state-banner task-receipt');
        receipt.setAttribute('role', 'status');
        (dialog.firstElementChild || dialog).append(receipt);
      }
      targets.push(receipt);
    }
    for (const node of targets) {
      node.hidden = false;
      node.classList?.remove?.('state-error', 'state-success', 'state-warning');
      node.setAttribute?.('role', 'status');
      node.replaceChildren(create('span', '', label));
      for (const id of ids) { const link = taskLink(id, queue); if (link) node.append(link); }
    }
    targets.at(-1)?.scrollIntoView?.({ block: 'nearest' });
    announce(label);
  }
  window.AdminV2 = Object.freeze({
    requestJson, getJson, postJson, putJson, deleteJson, openDialog, closeDialog, setFeedback, downloadBlob,
    create, el: create, setText, announce, safeId, scrub, titleCase, detailField, formatDuration, scheduleSync,
    currentRoute, taskLink, showQueued
  });
  function renderServiceChip() {
    const element = $('#serviceStatus');
    if (!element) return;
    const status = window.AdminV2HeaderStatus.serviceStatus(state.services);
    const label = $('span:last-child', element);
    if (element.dataset.state === status.state && label && label.textContent === status.label) return;
    element.dataset.state = status.state;
    element.title = status.title;
    setText(label, status.label);
    element.setAttribute('aria-label', `${status.label}. Open System.`);
  }
  function updateServices(patch) {
    state.services = { ...state.services, checking: false, ...patch };
    renderServiceChip();
  }
  function renderWriteAccessNotice(capabilities, error) {
    const notice = $('#writeAccessNotice');
    if (!notice) return;
    const status = window.AdminV2HeaderStatus.access(error ? null : capabilities);
    if (status.state === 'writable') {
      notice.hidden = true;
      notice.textContent = '';
      return;
    }
    notice.hidden = false;
    notice.className = 'state-banner state-stale';
    notice.textContent = status.state === 'token'
      ? 'Writes are enabled, but every action needs the admin write token.'
      : `${status.label}: ${status.title}`;
  }
  async function loadHeaderStatus() {
    if (document.hidden || state.headerStatusInFlight) return;
    state.headerStatusInFlight = true;
    try {
      const [capabilityResult, jobsResult] = await Promise.allSettled([
        getJson(`${V2_API}/capabilities`),
        getJson(`${V2_API}/jobs?state=active&limit=1&offset=0`)
      ]);
      updateServices({
        capabilities: capabilityResult.status === 'fulfilled'
          ? (capabilityResult.value.capabilities || capabilityResult.value) : null,
        activeTasks: jobsResult.status === 'fulfilled' ? jobsResult.value : null
      });
    } finally {
      state.headerStatusInFlight = false;
    }
  }
  async function loadHeaderSyncStatus() {
    if (document.hidden || state.headerSyncInFlight) return;
    state.headerSyncInFlight = true;
    try {
      const payload = await getJson(`${V2_API}/status/sync`);
      updateServices({ sync: (payload && payload.sync) || null });
    } catch {
      updateServices({ sync: null });
    } finally {
      state.headerSyncInFlight = false;
    }
  }
  async function loadStatus() {
    readIssueFiltersFromUrl();
    state.issueFiltersInitialized = true;
    const sequence = ++state.statusRequestSeq;
    const issueSequence = ++state.issueRequestSeq;
    state.issueLoading = false;
    $('#issuesLoadMore').disabled = false;
    const banner = $('#statusState');
    if (!state.statusLoaded) {
      banner.hidden = false; banner.className = 'state-banner state-loading';
      banner.replaceChildren(create('span', 'spinner'), create('span', '', 'Checking system status…'));
    }
    try {
      const [statusResult, capabilityResult, issues] = await Promise.all([
        getJson(`${V2_API}/status`).then(value => ({ value }), error => ({ error })),
        getJson(`${V2_API}/capabilities`).then(value => ({ value }), error => ({ error })),
        getJson(issueFeedUrl()).then(value => ({ value }), error => ({ error }))
      ]);
      if (sequence !== state.statusRequestSeq) return;
      if (statusResult.error) throw statusResult.error;
      const status = statusResult.value;
      state.status = status;
      state.statusLoaded = true;
      const capabilityData = capabilitiesPayload(capabilityResult.value);
      updateServices({ connection: true, capabilities: capabilityData, sync: (status && status.sync) || null });
      if (issueSequence === state.issueRequestSeq && state.route === 'start') {
        if (issues.value) renderIssues(issues.value);
        else renderIssues(null, issues.error);
      }
      renderStatus(status, capabilityData);
      renderWriteAccessNotice(capabilityData, capabilityResult.error);
      banner.hidden = !capabilityResult.error;
      if (capabilityResult.error) {
        banner.className = 'state-banner state-stale';
        banner.replaceChildren(create('span', '', 'System status is current, but write access could not be verified.'));
      }
      $('#statusContent').hidden = false;
      announce(capabilityResult.error ? 'System status updated. Write access could not be verified.' : 'System status updated.');
    } catch (error) {
      if (sequence !== state.statusRequestSeq) return;
      updateServices({ connection: false });
      if (state.statusLoaded && state.status) {
        banner.hidden = false; banner.className = 'state-banner state-stale';
        banner.replaceChildren(create('span', '', 'Data may be out of date. ' + error.message));
        $('#statusContent').hidden = false;
      } else {
        banner.hidden = false; banner.className = 'state-banner state-error';
        banner.replaceChildren(create('span', '', `Could not load system status. ${error.message}`));
      }
    }
  }
  async function loadSystemStatus() {
    if (state.systemStatusInFlight) return;
    state.systemStatusInFlight = true;
    const sequence = ++state.statusRequestSeq;
    try {
      const [statusResult, capabilityResult] = await Promise.all([
        getJson(`${V2_API}/status`).then(value => ({ value }), error => ({ error })),
        getJson(`${V2_API}/capabilities`).then(value => ({ value }), error => ({ error }))
      ]);
      if (sequence !== state.statusRequestSeq) return;
      const capabilityData = capabilitiesPayload(capabilityResult.value);
      if (statusResult.error) {
        // Never show the previous service rows as current after a failed read.
        renderWriteAccessNotice(capabilityData, capabilityResult.error || statusResult.error);
        updateServices({ connection: false, capabilities: capabilityData });
        return;
      }
      const status = statusResult.value;
      state.status = status;
      state.statusLoaded = true;
      updateServices({ connection: true, capabilities: capabilityData, sync: (status && status.sync) || null });
      renderWriteAccessNotice(capabilityData, capabilityResult.error);
    } finally {
      state.systemStatusInFlight = false;
    }
  }
  document.addEventListener('adminv2:refresh-system-status', () => { if (state.route === 'system' && state.systemTab === 'status') void loadSystemStatus(); });
  function capabilitiesPayload(payload) {
    if (!payload) return null;
    return payload.capabilities || payload;
  }
  function metric(label, value, caption, icon) {
    const card = create('article', 'metric-card');
    const top = create('div', 'metric-top'); top.append(create('span', '', label), create('span', 'metric-icon', icon));
    card.append(top, create('strong', 'metric-value', value), create('span', 'metric-caption', caption));
    return card;
  }
  function lastSyncMetric(sync) {
    if (!sync || sync.available === false) {
      return metric('Last sync', 'No data', 'The synchronization status could not be read.', '◷');
    }
    if (sync.running === true) {
      return metric('Last sync', 'In progress', 'A synchronization run is executing now.', '◷');
    }
    if (sync.history_available === false) {
      return metric('Last sync', 'No data', 'The synchronization history could not be read.', '◷');
    }
    const run = sync.last_run && typeof sync.last_run === 'object' ? sync.last_run : null;
    const finishedAt = run ? (run.ended_at || run.started_at) : null;
    if (!finishedAt) {
      return metric('Last sync', 'No history', 'No completed synchronization run is recorded yet.', '◷');
    }
    const outcome = run.interrupted === true ? 'The recorded run was interrupted'
      : run.success === true ? 'The recorded run completed'
        : run.success === false ? 'The recorded run failed'
          : 'The recorded outcome is unknown';
    return metric('Last sync', displayDateTime(finishedAt),
      `${outcome}. Started ${run.started_at ? displayDateTime(run.started_at) : 'unknown'}.`, '◷');
  }
  function renderOverviewCards(data) {
    const db = data.db || {};
    const attention = data.attention || {};
    const videos = first(db, ['videos_total', 'videos', 'video_count', 'total_videos', 'total'])
      ?? first(data, ['video_count', 'videos_count']);
    const queue = first(attention, ['pending_tasks', 'pending', 'active_jobs'])
      ?? first(data.sync || {}, ['queued', 'queue_length']);
    const problems = state.problemSummary;
    const problemTotal = problems && Number.isSafeInteger(problems.total_open) && problems.total_open >= 0
      ? problems.total_open : null;
    const cards = $('#statusCards');
    if (!cards) return;
    cards.replaceChildren(
      metric('Videos in library', safeText(videos), 'Catalog records', '▤'),
      lastSyncMetric(data.sync || {}),
      metric('Active tasks', safeText(queue, 'No data'), 'Queued or processing', '◷'),
      metric('Problems', problemTotal === null ? 'No data' : safeText(problemTotal),
        problemTotal === null ? 'The problem sources could not be read'
          : problems.incomplete ? 'Unresolved problems; some sources are unavailable'
            : 'Unresolved problems', '!')
    );
  }
  function renderStatus(data, capabilities) {
    renderOverviewCards(data);
  }
  function renderIssues(payload, error, append = false) {
    const completeness = $('#issueCompleteness');
    const count = $('#issueCount');
    const host = $('#attentionList');
    if (!payload) {
      state.problemSummary = null;
      renderProblemGroups(null, error);
      if (state.status) renderOverviewCards(state.status);
      count.hidden = false;
      count.textContent = state.issues.length
        ? 'Issue count unavailable; showing previously loaded results.'
        : 'Issue count unavailable.';
      completeness.hidden = false;
      completeness.textContent = 'The issue list is temporarily unavailable. Summary data may be out of date.';
      if (state.issues.length) host.replaceChildren(...state.issues.map(renderIssueRow));
      else host.replaceChildren(create('p', 'empty-attention', `Could not load the issue list${error ? `: ${error.message}` : '.'}`));
      return;
    }
    const hasItems = Array.isArray(payload.items) || Array.isArray(payload.issues);
    const incoming = Array.isArray(payload.items) ? payload.items : Array.isArray(payload.issues) ? payload.issues : null;
    const validTotal = Number.isSafeInteger(payload.total) && payload.total >= 0;
    const validTruncated = typeof payload.truncated === 'boolean';
    const validCompleteness = payload.data && typeof payload.data.incomplete === 'boolean';
    if (!hasItems || !incoming || !validTotal || !validTruncated || !validCompleteness) {
      renderIssues(null, new Error('The issue feed returned an invalid response.'));
      return;
    }
    state.issues = append ? state.issues.concat(incoming) : incoming;
    state.issueTotal = payload.total;
    state.issueTruncated = payload.truncated;
    state.problemSummary = payload.summary && typeof payload.summary === 'object' ? payload.summary : null;
    renderProblemGroups(state.problemSummary, null);
    if (state.status) renderOverviewCards(state.status);
    const incomplete = payload.data.incomplete;
    count.hidden = false;
    if (incomplete) count.textContent = `Showing ${state.issues.length} returned ${state.issues.length === 1 ? 'issue' : 'issues'}; the total may be incomplete.`;
    else if (state.issues.length === state.issueTotal) count.textContent = `Showing ${state.issueTotal} ${state.issueTotal === 1 ? 'issue' : 'issues'}.`;
    else count.textContent = `Showing ${state.issues.length} of ${state.issueTotal} issues.`;
    completeness.hidden = !incomplete;
    completeness.textContent = incomplete ? 'Partial list: some issue sources are unavailable or history is truncated.' : '';
    host.replaceChildren();
    if (!state.issues.length) {
      host.append(create('p', 'empty-attention', incomplete ? 'No issues were returned. This list is incomplete; check System for service status.' : 'No issues need attention.'));
    } else state.issues.forEach(issue => host.append(renderIssueRow(issue)));
    $('#issuesLoadMore').hidden = !state.issueTruncated;
    $('#issuesLoadMore').disabled = state.issueLoading;
  }
  const ISSUE_ROUTE_PATTERNS = [
    /^\/?(?:admin\/v2\/)?#(?:library|sources|jobs|system|performers|schedules)(?:[?&/]|$)/,
    /^\/admin\/v2\/\?source_id=[A-Za-z0-9%._~-]+#sources$/,
    /^\/admin\/v2\/\?job_id=[A-Za-z0-9%._~-]{1,128}#jobs$/,
    /^\/admin\/v2\/\?job_state=(?:active|queued|processing|done|failed|cancelled)#jobs$/,
    /^\/admin\/v2\/\?job_state=(?:active|queued|processing|done|failed|cancelled)&job_action=[A-Za-z0-9%._~-]{1,64}#jobs$/
  ];
  function safeIssueRoute(route) {
    return typeof route === 'string' && ISSUE_ROUTE_PATTERNS.some(pattern => pattern.test(route)) ? route : null;
  }
  function issueHref(route) {
    if (!route) return null;
    return route.startsWith('#') ? `/admin/v2/${route}` : (route.startsWith('/') ? route : `/admin/v2/${route}`);
  }
  function renderProblemGroups(summary, error) {
    const host = $('#problemGroups');
    if (!host) return;
    const groups = summary && Array.isArray(summary.groups) ? summary.groups : null;
    const completeness = $('#problemCompleteness');
    host.replaceChildren();
    if (!groups) {
      setHidden('#problemCompleteness', false);
      setText(completeness, error
        ? `The problem summary could not be loaded: ${error.message}`
        : 'The problem summary is unavailable; the full list below may still be loaded.');
      host.append(create('p', 'empty-attention', 'Problem summaries unavailable.'));
      return;
    }
    setHidden('#problemCompleteness', !summary.incomplete);
    setText(completeness, summary.incomplete
      ? 'Some problem sources are unavailable, so this summary may be incomplete.' : '');
    if (!groups.length) {
      host.append(create('p', 'empty-attention', summary.incomplete
        ? 'No problems were returned. This summary is incomplete; check System for service status.'
        : 'No problems need attention.'));
    } else groups.forEach(group => host.append(problemGroupRow(group)));
    const hidden = Number.isSafeInteger(summary.groups_total)
      ? summary.groups_total - groups.length : 0;
    if (hidden > 0) {
      host.append(create('p', 'problem-groups-more', `${hidden} more problem ${hidden === 1 ? 'group' : 'groups'} in All problems.`));
    }
  }
  function problemGroupRow(group) {
    const severity = String(group.severity || '').toLowerCase();
    const danger = ['critical', 'high', 'error'].includes(severity);
    const row = create('div', 'attention-row problem-group-row');
    row.append(create('span', `attention-mark${danger ? ' is-danger' : ''}`, danger ? '!' : '↗'));
    const copy = create('div', 'attention-copy');
    copy.append(create('strong', '', safeText(group.label, 'Problem')));
    const occurrences = Number.isSafeInteger(group.occurrences) && group.occurrences >= 0 ? group.occurrences : 1;
    const objects = Number.isSafeInteger(group.objects) && group.objects > 1 ? group.objects : null;
    copy.append(create('small', '', `${occurrences} ${occurrences === 1 ? 'occurrence' : 'occurrences'}`
      + (objects ? ` · ${objects} items` : '')));
    copy.append(create('small', 'issue-time', group.last_seen
      ? `Last detected ${displayTime(group.last_seen)}` : 'Time unavailable'));
    row.append(copy, create('span', `issue-severity severity-${severity || 'unknown'}`, severity || 'Unknown'));
    const action = group.action && typeof group.action === 'object' ? group.action : null;
    const route = safeIssueRoute(action ? action.route : null);
    if (route) {
      const label = safeText(action.label, 'Open →');
      const link = create('a', 'issue-action', label);
      link.href = issueHref(route);
      link.setAttribute('aria-label', `${label}: ${safeText(group.label, 'Problem')}`);
      row.append(link);
    }
    return row;
  }
  function renderIssueRow(issue) {
      const kind = safeText(first(issue, ['kind', 'type', 'code']), 'System issue');
      const title = safeText(first(issue, ['title', 'message', 'label']), kind);
      const detail = safeText(first(issue, ['message', 'description', 'reason', 'object_label', 'object_id']), 'Needs review.');
      const count = first(issue, ['count', 'affected_count', 'total']);
      const severity = String(first(issue, ['severity', 'level']) || '').toLowerCase();
      const row = create('div', 'attention-row');
      row.append(create('span', `attention-mark${['critical', 'error'].includes(severity) ? ' is-danger' : ''}`, ['critical', 'error'].includes(severity) ? '!' : '↗'));
      const copy = create('div', 'attention-copy');
      copy.append(create('strong', '', title), create('small', '', detail));
      const when = first(issue, ['last_seen', 'first_seen']);
      const timeLabel = issue.kind === 'sync_never_run' ? 'No prior run recorded' : when ? `Last detected ${displayTime(when)}` : 'Time unavailable';
      copy.append(create('small', 'issue-time', timeLabel));
      row.append(copy, create('span', `issue-severity severity-${severity || 'unknown'}`, severity || 'Unknown'));
      if (count !== null) {
        const countLabel = kind === 'job_failure'
          ? `${safeText(count)} ${Number(count) === 1 ? 'attempt' : 'attempts'}`
          : safeText(count);
        row.append(create('strong', 'attention-value', countLabel));
      }
      const action = issue.action && typeof issue.action === 'object' ? issue.action : null;
      const actionRoute = safeIssueRoute(action ? action.route : null);
      if (actionRoute) {
        const link = create('a', 'issue-action', safeText(action.label, 'Open →'));
        link.href = issueHref(actionRoute);
        link.setAttribute('aria-label', `${safeText(action.label, 'Open')}: ${title}`); row.append(link);
      }
      return row;
  }
  async function loadIssues({ append = false } = {}) {
    const sequence = ++state.issueRequestSeq;
    state.issueLoading = true;
    $('#issuesLoadMore').disabled = true;
    try {
      const payload = await getJson(issueFeedUrl(append ? state.issues.length : 0));
      if (sequence !== state.issueRequestSeq || state.route !== 'start') return;
      renderIssues(payload, null, append);
    } catch (error) {
      if (sequence !== state.issueRequestSeq || state.route !== 'start') return;
      if (!state.issues.length) renderIssues(null, error);
      else announce(`Could not load more issues: ${error.message}`);
    } finally {
      if (sequence === state.issueRequestSeq) {
        state.issueLoading = false;
        $('#issuesLoadMore').disabled = false;
      }
    }
  }

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
    return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium' }).format(date);
  }
  function displayDateTime(value) {
    if (!value) return '—';
    const dateValue = typeof value === 'number' && value < 100000000000 ? value * 1000 : value;
    const date = new Date(dateValue);
    if (Number.isNaN(date.getTime())) return safeText(value);
    return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
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
      const refresh = create('button', 'button button-primary', 'Refresh status'); refresh.type = 'button'; refresh.addEventListener('click', refreshOperationStatus);
      actions.append(close, refresh);
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
  function closeDetailVisual() {
    state.availabilityHistorySeq += 1;
    state.sideDetailId = null;
    stopDetailPlayback();
    if (state.detailAbort) {
      state.detailAbort.abort();
      state.detailAbort = null;
    }
    const dialog = $('#detailOverlay');
    if (dialog.open) closeDialog(dialog);
  }
  function stopDetailPlayback() {
    const player = $('#detailContent video');
    if (!player) return;
    player.pause();
    player.removeAttribute('src');
    player.load();
  }
  function safeExternalUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try { const parsed = new URL(value, location.origin); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null; } catch { return null; }
  }
  // Detail fields that accept DOM nodes keep their own builder; window.AdminV2.detailField serves the plain-text variant.
  function detailFieldValue(label, value) {
    const wrapper = create('div', 'detail-field');
    const dd = create('dd');
    if (value instanceof Node) dd.append(value);
    else dd.textContent = safeText(value);
    wrapper.append(create('dt', '', label), dd);
    return wrapper;
  }
  function localFileLabel(value) {
    return ({ present: 'Present', missing: 'Missing', unknown: 'Unknown' })[value] || 'Unknown';
  }
  function renderDetail(video) {
    const isCurrentDetail = () => state.detailVideo === video && (state.detailId === video.id || state.sideDetailId === video.id);
    state.availabilityHistorySeq += 1;
    state.detailVideo = video;
    stopDetailPlayback();
    const host = $('#detailContent'); host.replaceChildren();
    const title = safeText(first(video, ['title', 'name']), 'Untitled');
    const heading = create('h2', 'detail-title', title); heading.id = 'detailTitle';
    host.append(create('p', 'detail-kicker', 'VIDEO DETAILS'), heading);
    const [availabilityLabel, availabilityClass] = availability(video); host.append(create('span', `status-pill detail-state ${availabilityClass}`, availabilityLabel));
    const artwork = thumbnail(video);
    if (artwork) {
      const art = create('div', 'detail-art');
      const img = document.createElement('img');
      img.src = artwork;
      img.alt = `Artwork: ${title}`;
      img.loading = 'lazy';
      img.onerror = () => {
        art.replaceChildren(create('span', 'video-thumb-placeholder', 'No artwork preview available'));
      };
      art.append(img);
      host.append(art);
    }
    const preview = create('section', 'detail-preview');
    const play = create('button', 'button button-primary', '▶ Play preview');
    const playableId = first(video, ['id', 'video_id', 'videoId']);
    play.type = 'button';
    play.disabled = !playableId;
    const playerHost = create('div', 'detail-preview-player');
    play.addEventListener('click', async () => {
      if (!playerHost.querySelector('video')) {
        const player = document.createElement('video');
        player.className = 'detail-video-player';
        player.controls = true;
        player.playsInline = true;
        player.preload = 'metadata';
        if (artwork) player.poster = artwork;
        player.src = `/admin/play/${encodeURIComponent(String(playableId))}/stream?mode=proxy`;
        player.addEventListener('error', () => {
          player.pause();
          player.removeAttribute('src');
          player.load();
          playerHost.replaceChildren();
          play.textContent = '▶ Retry preview';
        }, { once: true });
        playerHost.replaceChildren(player);
        play.textContent = '■ Stop preview';
        try { await player.play(); } catch { /* Native controls remain available when autoplay is blocked. */ }
        return;
      }
      stopDetailPlayback();
      playerHost.replaceChildren();
      play.textContent = '▶ Play preview';
    });
    preview.append(play, playerHost);
    host.append(preview);
    const overview = create('section', 'detail-section');
    overview.setAttribute('aria-labelledby', 'detailOverviewHeading');
    const overviewHeading = create('h3', '', 'Overview'); overviewHeading.id = 'detailOverviewHeading';
    overview.append(overviewHeading);
    const fields = [
      ['ID', first(video, ['id', 'video_id', 'videoId'])], ['Studio', first(video, ['studio', 'studio_name'])],
      ['Channel', first(video, ['channel_name', 'channel_id', 'source_name'])], ['Added', displayTime(first(video, ['first_seen', 'added_at', 'created_at', 'date_added']))],
      ['Availability reason', unavailableReason(video)], ['Unavailable since', displayTime(video.unavailable_detected_at)],
      ['Duration', duration(first(video, ['duration_seconds', 'duration']))], ['Missing data', first(video, ['missing', 'missing_fields'])],
      ['Scene match', video.scene_match_source ? `${video.scene_match_source}${video.scene_match_external_id ? ` · ${video.scene_match_external_id}` : ''}` : '']
    ];
    const overviewFields = create('dl', 'detail-fields');
    fields.forEach(([label, value]) => overviewFields.append(detailFieldValue(label, value)));
    overview.append(overviewFields);
    const people = first(video, ['performers', 'people', 'cast']);
    if (people) overview.append(create('h4', 'detail-subheading', 'Performers'), create('p', 'detail-description', safeText(people)));
    const tags = first(video, ['tags', 'genres']);
    if (tags) overview.append(create('h4', 'detail-subheading', 'Tags'), create('p', 'detail-description', safeText(tags)));
    const description = first(video, ['description', 'overview', 'plot']);
    if (description) overview.append(create('h4', 'detail-subheading', 'Description'), create('p', 'detail-description', safeText(description)));
    const sourceRecords = Array.isArray(video.sources) ? video.sources : [];
    if (sourceRecords.length) {
      overview.append(create('h4', 'detail-subheading', 'Source records'));
      sourceRecords.forEach(source => {
        const line = create('p', 'detail-description');
        const sourceLabel = `${safeText(source.provider, 'Source')}${source.channel_id ? ` · Channel ${safeText(source.channel_id)}` : ''}`;
        line.textContent = `${sourceLabel} · ${source.active ? 'Active' : 'Inactive'} · Priority ${safeText(source.priority, '—')}`;
        overview.append(line);
      });
    }
    const links = [];
    for (const [label, keys] of [
      ['Open Telegram post', ['telegram_url', 'source_url', 'post_url']],
      ['Open in Jellyfin', ['jellyfin_url']],
      ['Open in StashDB', ['scene_match_url']],
    ]) {
      const url = safeExternalUrl(first(video, keys)); if (url) links.push([label, url]);
    }
    if (links.length) {
      const row = create('div', 'detail-links');
      links.forEach(([label, href]) => { const link = create('a', 'detail-link', `${label} ↗`); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link); });
      overview.append(create('h4', 'detail-subheading', 'Related links'), row);
    }
    host.append(overview);

    const metadata = create('section', 'detail-section');
    metadata.setAttribute('aria-labelledby', 'detailMetadataHeading');
    const metadataHeading = create('h3', '', 'Metadata'); metadataHeading.id = 'detailMetadataHeading';
    const revision = Number(video.metadata_revision);
    const revisionLabel = video.metadata_revision !== null && video.metadata_revision !== undefined
      && video.metadata_revision !== '' && Number.isInteger(revision) && revision >= 0 ? revision : 'Unavailable';
    metadata.append(metadataHeading, create('p', 'detail-description', `Current metadata revision: ${revisionLabel}. Manual overrides are compared against this revision before they are queued.`));
    const edit = create('button', 'button button-quiet', 'Edit metadata');
    edit.type = 'button'; edit.addEventListener('click', () => openMetadataEditor());
    metadata.append(edit);
    host.append(metadata);

    const files = create('section', 'detail-section');
    files.setAttribute('aria-labelledby', 'detailFilesHeading');
    const filesHeading = create('h3', '', 'Files'); filesHeading.id = 'detailFilesHeading';
    files.append(filesHeading);
    let artworkValue = localFileLabel(video.local_files?.artwork);
    if (video.artwork_url && video.local_files?.artwork === 'present') {
      const span = document.createElement('span');
      span.textContent = `${artworkValue} · `;
      const a = create('a', 'text-link', 'Open artwork ↗');
      a.href = video.artwork_url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      span.append(a);
      artworkValue = span;
    }
    const fileFields = create('dl', 'detail-fields');
    [
      ['OSHASH', video.oshash || 'Not available'],
      ['pHash', video.phash || 'Not calculated (available for a subset of videos)'],
      ['Video size', fileSize(video.file_size_bytes)],
      ['Stream file', localFileLabel(video.local_files?.stream)],
      ['NFO file', localFileLabel(video.local_files?.nfo)],
      ['Artwork file', artworkValue]
    ].forEach(([label, value]) => fileFields.append(detailFieldValue(label, value)));
    files.append(fileFields);

    const genArtwork = create('button', 'button button-quiet', 'Generate artwork');
    genArtwork.id = 'detailGenerateArtwork';
    genArtwork.type = 'button';
    genArtwork.addEventListener('click', async () => {
      if (genArtwork.disabled) return;
      genArtwork.disabled = true;
      const prevText = genArtwork.textContent;
      genArtwork.textContent = 'Queueing artwork generation…';
      try {
        const result = await postJson(`${V2_API}/library/videos/${encodeURIComponent(video.id)}/generate-artwork`, { offset_seconds: 60 });
        genArtwork.textContent = 'Artwork generation queued';
        if (isCurrentDetail()) showQueued(result, 'Artwork generation');
      } catch (err) {
        alert(`Failed to generate artwork: ${err.message || err}`);
        genArtwork.disabled = false;
        genArtwork.textContent = prevText;
      }
    });

    const downloadArtwork = create('button', 'button button-quiet', 'Download artwork');
    downloadArtwork.id = 'detailDownloadArtwork';
    downloadArtwork.type = 'button';
    downloadArtwork.addEventListener('click', async () => {
      if (downloadArtwork.disabled) return;
      downloadArtwork.disabled = true;
      const prevText = downloadArtwork.textContent;
      downloadArtwork.textContent = 'Queueing artwork download…';
      try {
        const result = await postJson(`${V2_API}/library/videos/${encodeURIComponent(video.id)}/download-artwork`, {});
        downloadArtwork.textContent = 'Artwork download queued';
        if (isCurrentDetail()) showQueued(result, 'Artwork download');
      } catch (err) {
        alert(`Failed to download artwork: ${err.message || err}`);
        downloadArtwork.disabled = false;
        downloadArtwork.textContent = prevText;
      }
    });

    const genPhash = create('button', 'button button-quiet', video.phash ? 'Regenerate pHash' : 'Generate pHash');
    genPhash.id = 'detailGeneratePhash';
    genPhash.type = 'button';
    genPhash.addEventListener('click', async () => {
      if (genPhash.disabled) return;
      genPhash.disabled = true;
      const prevText = genPhash.textContent;
      genPhash.textContent = 'Queueing pHash…';
      try {
        const payload = video.phash ? { force: true } : {};
        const res = await postJson(`${V2_API}/library/videos/${encodeURIComponent(video.id)}/generate-phash`, payload);
        if (res.already_computed) {
          announce('pHash is already computed.');
        } else if (res.job_id) {
          if (isCurrentDetail()) showQueued(res, 'pHash generation', undefined, 'phash');
        } else {
          announce('pHash task queued.');
        }
        if (res.already_computed) {
          genPhash.textContent = 'pHash already computed';
          await loadDetail(video.id);
        } else {
          genPhash.textContent = 'pHash queued';
        }
      } catch (err) {
        alert(`Failed to generate pHash: ${err.message || err}`);
        genPhash.disabled = false;
        genPhash.textContent = prevText;
      }
    });

    const filesActions = create('div', 'detail-actions');
    filesActions.append(genArtwork, downloadArtwork, genPhash);
    files.append(filesActions);
    host.append(files);

    const checkAvailability = create('button', 'button button-quiet', 'Check availability');
    checkAvailability.id = 'detailCheckAvailability';
    checkAvailability.type = 'button'; checkAvailability.addEventListener('click', () => reviewAvailability([String(video.id)]));
    const availabilityActions = create('div', 'detail-actions'); availabilityActions.append(checkAvailability); host.append(availabilityActions);
    loadAvailabilityHistory(String(video.id));
  }
  function fileSize(value) {
    const bytes = Number(value);
    if (value === null || value === undefined || !Number.isFinite(bytes) || bytes <= 0) return 'Not available';
    return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${(bytes / 1e6).toFixed(2)} MB`;
  }
  function duration(value) {
    if (value === null || value === undefined || value === '') return '—';
    const n = Number(value); if (!Number.isFinite(n) || n < 0) return safeText(value);
    const hours = Math.floor(n / 3600); const minutes = Math.floor((n % 3600) / 60);
    return hours ? `${hours} hour${hours === 1 ? '' : 's'}${minutes ? ` ${minutes} min` : ''}` : `${minutes} min`;
  }
  async function loadDetail(id) {
    // An action button refreshes the drawer after its own request finishes, and
    // the drawer may have been closed while that request was in flight. Bailing
    // out before touching the dialog keeps a finished task from resurrecting a
    // detail view the user dismissed, where its loading banner would be stranded
    // by the stale-response guard below.
    const isCurrentDetail = () => state.detailId === id || state.sideDetailId === id;
    if (!isCurrentDetail()) return;
    if (state.detailAbort) state.detailAbort.abort();
    const controller = new AbortController(); state.detailAbort = controller;
    const dialog = $('#detailOverlay');
    if (!dialog.open) openDialog(dialog);
    const host = $('#detailContent');
    const heading = create('h2', 'detail-title', 'Loading…'); heading.id = 'detailTitle';
    host.replaceChildren(create('p', 'detail-kicker', 'VIDEO DETAILS'), heading, create('div', 'state-banner state-loading', 'Loading video details…'));
    try {
      const payload = await getJson(`${V2_API}/library/videos/${encodeURIComponent(id)}`, controller.signal);
      if (!isCurrentDetail()) return;
      const video = payload.item || payload.video || payload.data || payload;
      renderDetail(video);
      $('.detail-drawer').focus({ preventScroll: true });
    } catch (error) {
      if (error.name === 'AbortError' || !isCurrentDetail()) return;
      const errorHeading = create('h2', 'detail-title', 'Could not open video'); errorHeading.id = 'detailTitle';
      host.replaceChildren(create('p', 'detail-kicker', 'VIDEO DETAILS'), errorHeading, create('p', 'detail-description', error.message));
      $('.detail-drawer').focus({ preventScroll: true });
    }
  }
  function closeDetail() {
    if (state.sideDetailId !== null) {
      state.sideDetailId = null;
      closeDetailVisual();
      return;
    }
    const url = new URL(location.href);
    if (url.hash.startsWith('#film/')) {
      if (history.state && history.state.detailOpener) history.back();
      else { history.replaceState({}, '', urlFor(libraryHash(state.libraryTab, state.duplicateMatch))); renderLocation(); }
    }
  }
  function openSideDetail(id) {
    if (!id) return;
    state.sideDetailId = String(id);
    loadDetail(state.sideDetailId);
  }
  function metadataPayloadFromEditor() {
    const video = state.detailVideo;
    const changes = {}, reset_fields = [];
    for (const field of ['title', 'plot', 'studio', 'performers', 'tags', 'date_released']) {
      const enabled = $(`[data-metadata-override="${field}"]`).checked;
      const input = $(`[data-metadata-value="${field}"]`);
      const wasOverridden = Boolean(video.metadata_fields?.[field]?.overridden);
      if (enabled) {
        let value = input.value;
        if (field === 'performers' || field === 'tags') value = value.split('\n').map(item => item.trim()).filter(Boolean);
        if (!wasOverridden || JSON.stringify(value) !== JSON.stringify(video[field] ?? (Array.isArray(value) ? [] : ''))) changes[field] = value;
      } else if (wasOverridden) reset_fields.push(field);
    }
    return { video_id: video.id, expected_revision: Number(video.metadata_revision) || 0, changes, reset_fields };
  }
  function openMetadataEditor(restoreDraft = false) {
    const video = state.detailVideo;
    if (!video?.id) return;
    state.metadataDialogSeq += 1;
    state.metadataBulkVideoIds = null;
    const draft = restoreDraft ? state.metadataRequest : null;
    if (!restoreDraft) state.metadataRequest = null;
    state.metadataPlanId = null;
    const dialog = $('#metadataDialog'); const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Edit metadata'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'MANUAL OVERRIDES'), heading,
      create('p', 'detail-description', 'Manual values stay in place while source metadata continues to update. Clear an override to use the latest source value.'));
    for (const [field, label] of [['title', 'Title'], ['plot', 'Description'], ['studio', 'Studio'], ['performers', 'Performers'], ['tags', 'Tags'], ['date_released', 'Release date']]) {
      const stateField = video.metadata_fields?.[field] || { source: video[field] ?? '', effective: video[field] ?? '', overridden: false };
      const hasDraftChange = Object.hasOwn(draft?.changes || {}, field);
      const draftReset = draft?.reset_fields?.includes(field) === true;
      const section = create('section', 'metadata-edit-field');
      const inputLabel = create('label', '', label); inputLabel.htmlFor = `metadata-${field}`;
      const overrideLabel = create('label', 'metadata-override-toggle');
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.dataset.metadataOverride = field;
      checkbox.checked = hasDraftChange || (!draftReset && Boolean(stateField.overridden));
      overrideLabel.append(checkbox, create('span', '', 'Use a manual value'));
      const input = ['plot', 'performers', 'tags'].includes(field) ? document.createElement('textarea') : document.createElement('input');
      if (field === 'date_released') input.type = 'date';
      const value = hasDraftChange ? draft.changes[field] : draftReset ? stateField.source : stateField.effective;
      input.id = `metadata-${field}`; input.dataset.metadataValue = field;
      input.value = Array.isArray(value) ? value.join('\n') : String(value ?? '');
      input.maxLength = field === 'title' ? 500 : field === 'studio' ? 300 : field === 'plot' ? 10000 : 10000; input.disabled = !checkbox.checked;
      if (field === 'plot') input.rows = 5;
      if (field === 'performers' || field === 'tags') { input.rows = 4; input.maxLength = 20000; input.placeholder = 'One value per line'; }
      checkbox.addEventListener('change', () => { input.disabled = !checkbox.checked; if (checkbox.checked) input.focus(); });
      section.append(inputLabel, overrideLabel, input,
        create('small', 'metadata-source-value', `Current source: ${stateField.source === '' || stateField.source === null || (Array.isArray(stateField.source) && !stateField.source.length) ? '(empty)' : safeText(Array.isArray(stateField.source) ? stateField.source.join(', ') : stateField.source)}`));
      content.append(section);
    }
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const cancel = create('button', 'button button-quiet', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => closeDialog(dialog));
    const preview = create('button', 'button button-primary', 'Preview changes'); preview.type = 'button'; preview.addEventListener('click', previewMetadataChanges);
    actions.append(cancel, preview);
    openDialog(dialog);
  }
  async function previewMetadataChanges() {
    const sequence = state.metadataDialogSeq;
    const videoId = String(state.detailVideo?.id ?? '');
    if (!videoId || !$('#metadataDialog').open) return;
    const request = metadataPayloadFromEditor();
    if (!Object.keys(request.changes).length && !request.reset_fields.length) { announce('No metadata changes to preview.'); return; }
    const actions = $('#metadataDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const payload = await window.AdminV2.postJson(`${V2_API}/operations/preview`, { type: 'metadata_update', ...request });
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      state.metadataRequest = request; state.metadataPlanId = payload.plan_id; renderMetadataPreview(payload);
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      announce(error.message);
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  function renderMetadataPreview(preview) {
    const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Review metadata changes'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'PREVIEW'), heading);
    const list = create('dl', 'metadata-diff-list');
    for (const [field, values] of Object.entries(preview.fields || {})) {
      const labels = { title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Performers', tags: 'Tags', date_released: 'Release date' };
      const displayValue = value => Array.isArray(value) ? value.join(', ') : value;
      list.append(create('dt', '', labels[field] || field));
      const valuesNode = create('dd');
      valuesNode.append(create('span', 'metadata-diff-before', values.before === '' || values.before === null || (Array.isArray(values.before) && !values.before.length) ? '(empty)' : String(displayValue(values.before))),
        create('span', 'metadata-diff-arrow', '→'),
        create('strong', 'metadata-diff-after', values.after === '' || values.after === null || (Array.isArray(values.after) && !values.after.length) ? '(empty)' : String(displayValue(values.after))));
      list.append(valuesNode);
    }
    content.append(list, create('p', 'detail-description', 'The worker saves the local override, writes updated NFO files atomically, then requests a Jellyfin refresh.'));
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const back = create('button', 'button button-quiet', 'Back to editor'); back.type = 'button'; back.addEventListener('click', () => openMetadataEditor(true));
    const queue = create('button', 'button button-primary', 'Queue metadata update'); queue.type = 'button'; queue.addEventListener('click', queueMetadataUpdate);
    actions.append(back, queue);
  }
  async function queueMetadataUpdate() {
    const planId = state.metadataPlanId;
    if (!planId) return;
    const sequence = state.metadataDialogSeq;
    const videoId = String(state.detailVideo?.id ?? '');
    if (!videoId || !$('#metadataDialog').open) return;
    const actions = $('#metadataDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const payload = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: planId });
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      closeDialog($('#metadataDialog'));
      showQueued(payload, 'Metadata update');
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      announce(error.message);
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (error.status === 409 && state.metadataRequest) {
        state.metadataPlanId = null;
        const queue = [...actions.querySelectorAll('button')].find(button => button.textContent === 'Queue metadata update');
        if (queue) queue.disabled = true;
        const refresh = create('button', 'button button-primary', 'Refresh preview'); refresh.type = 'button';
        refresh.addEventListener('click', async () => {
          const refreshSequence = state.metadataDialogSeq;
          const refreshVideoId = String(state.detailVideo?.id ?? '');
          const request = state.metadataRequest;
          if (!refreshVideoId || !request || !$('#metadataDialog').open) return;
          const restoreRefreshFocus = document.activeElement === refresh;
          refresh.disabled = true;
          try {
            const refreshed = await window.AdminV2.postJson(`${V2_API}/operations/preview`, {
              type: 'metadata_update', ...request
            });
            if (refreshSequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== refreshVideoId || !$('#metadataDialog').open) return;
            state.metadataPlanId = refreshed.plan_id;
            renderMetadataPreview(refreshed);
            const newQueue = [...$('#metadataDialogActions').querySelectorAll('button')].find(button => button.textContent === 'Queue metadata update');
            if (restoreRefreshFocus && document.activeElement === document.body) newQueue?.focus({ preventScroll: true });
          } catch (refreshError) {
            if (refreshSequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== refreshVideoId || !$('#metadataDialog').open) return;
            announce(refreshError.message); refresh.disabled = false;
            if (restoreRefreshFocus && document.activeElement === document.body) refresh.focus({ preventScroll: true });
          }
        });
        if (queue) queue.before(refresh); else actions.append(refresh);
        if (focusedAction && document.activeElement === document.body) refresh.focus({ preventScroll: true });
      } else if (focusedAction?.isConnected && document.activeElement === document.body) {
        focusedAction.focus({ preventScroll: true });
      }
    }
  }
  function openBulkMetadataEditor(restoreDraft = false) {
    if (state.route !== 'library' || state.isStale || !state.selectedVideos.size || state.selectedVideos.size > 25) return;
    const videoIds = [...state.selectedVideos.keys()];
    state.metadataDialogSeq += 1;
    state.metadataBulkVideoIds = videoIds;
    if (!restoreDraft) state.metadataBulkDraft = null;
    state.metadataRequest = null; state.metadataPlanId = null;
    const dialog = $('#metadataDialog'); const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Edit metadata for selected videos'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'BULK METADATA EDIT'), heading,
      create('p', 'detail-description', `Set the same selected fields for ${videoIds.length} videos, or reset fields to each video's latest source value. Each video is checked against its own revision before any task is queued.`));
    for (const [field, label] of [['title', 'Title'], ['plot', 'Description'], ['studio', 'Studio'], ['performers', 'Performers'], ['tags', 'Tags'], ['date_released', 'Release date']]) {
      const section = create('section', 'metadata-edit-field');
      const inputLabel = create('label', '', label); inputLabel.htmlFor = `bulk-metadata-${field}`;
      const setLabel = create('label', 'metadata-override-toggle');
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.dataset.bulkMetadataOverride = field;
      checkbox.checked = Object.hasOwn(state.metadataBulkDraft?.changes || {}, field);
      setLabel.append(checkbox, create('span', '', 'Set this value for all selected videos'));
      const input = ['plot', 'performers', 'tags'].includes(field) ? document.createElement('textarea') : document.createElement('input');
      if (field === 'date_released') input.type = 'date';
      input.id = `bulk-metadata-${field}`; input.dataset.bulkMetadataValue = field;
      const draftReset = state.metadataBulkDraft?.reset_fields?.includes(field) === true;
      input.value = checkbox.checked
        ? (Array.isArray(state.metadataBulkDraft.changes[field]) ? state.metadataBulkDraft.changes[field].join('\n') : String(state.metadataBulkDraft.changes[field] ?? ''))
        : '';
      input.disabled = !checkbox.checked; input.maxLength = field === 'title' ? 500 : field === 'studio' ? 300 : field === 'plot' ? 10000 : field === 'date_released' ? 10 : 20000;
      if (field === 'plot') input.rows = 4;
      if (field === 'performers' || field === 'tags') { input.rows = 3; input.placeholder = 'One value per line'; }
      checkbox.addEventListener('change', () => { input.disabled = !checkbox.checked; });
      const resetLabel = create('label', 'metadata-override-toggle');
      const reset = document.createElement('input'); reset.type = 'checkbox'; reset.dataset.bulkMetadataReset = field;
      reset.checked = draftReset;
      reset.addEventListener('change', () => { if (reset.checked) { checkbox.checked = false; input.disabled = true; } });
      checkbox.addEventListener('change', () => { if (checkbox.checked) reset.checked = false; });
      resetLabel.append(reset, create('span', '', 'Reset to each video’s latest source value'));
      section.append(inputLabel, setLabel, input, resetLabel);
      content.append(section);
    }
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const cancel = create('button', 'button button-quiet', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => closeDialog(dialog));
    const preview = create('button', 'button button-primary', 'Preview changes'); preview.type = 'button'; preview.addEventListener('click', previewBulkMetadataChanges);
    actions.append(cancel, preview); openDialog(dialog);
  }
  function bulkMetadataPayloadFromEditor() {
    const changes = {}, reset_fields = [];
    for (const field of ['title', 'plot', 'studio', 'performers', 'tags', 'date_released']) {
      if ($(`[data-bulk-metadata-override="${field}"]`).checked) {
        const input = $(`[data-bulk-metadata-value="${field}"]`);
        changes[field] = ['performers', 'tags'].includes(field)
          ? input.value.split('\n').map(item => item.trim()).filter(Boolean) : input.value;
      } else if ($(`[data-bulk-metadata-reset="${field}"]`).checked) reset_fields.push(field);
    }
    return { type: 'metadata_update_batch', video_ids: [...(state.metadataBulkVideoIds || [])], changes, reset_fields };
  }
  async function previewBulkMetadataChanges() {
    const sequence = state.metadataDialogSeq;
    const videoIds = [...(state.metadataBulkVideoIds || [])];
    const request = bulkMetadataPayloadFromEditor();
    if (!videoIds.length || !$('#metadataDialog').open) return;
    if (!Object.keys(request.changes).length && !request.reset_fields.length) { announce('Choose at least one field to set or reset.'); return; }
    if (JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(videoIds)) { announce('The selection changed. Reopen the bulk editor to review the current selection.'); return; }
    state.metadataBulkDraft = { changes: request.changes, reset_fields: request.reset_fields };
    const actions = $('#metadataDialogActions'); actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const preview = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (sequence !== state.metadataDialogSeq || !$('#metadataDialog').open) return;
      if (state.route !== 'library'
          || JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(videoIds)) {
        closeDialog($('#metadataDialog'));
        announce('The selection changed while the preview was loading. Reopen the bulk editor to review the current selection.');
        return;
      }
      state.metadataPlanId = preview.plan_id; renderBulkMetadataPreview(preview);
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || !$('#metadataDialog').open) return;
      announce(error.message); actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  function renderBulkMetadataPreview(preview) {
    const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Review bulk metadata changes'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'PREVIEW'), heading,
      create('p', 'detail-description', `${preview.items.length} videos will receive the selected override changes. Each update runs as a separate task; stale revisions prevent the batch from being queued.`));
    const list = create('ul', 'operation-item-list');
    for (const item of preview.items || []) {
      const row = create('li');
      row.append(create('strong', '', item.title || 'Untitled'), create('code', 'operation-video-id', item.video_id));
      for (const [field, values] of Object.entries(item.fields || {})) {
        const before = Array.isArray(values.before) ? values.before.join(', ') : values.before;
        const after = Array.isArray(values.after) ? values.after.join(', ') : values.after;
        const labels = { title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Performers', tags: 'Tags', date_released: 'Release date' };
        row.append(create('span', '', `${labels[field] || field}: ${before == null || before === '' ? '(empty)' : before} → ${after == null || after === '' ? '(empty)' : after}`));
      }
      list.append(row);
    }
    content.append(list, create('p', 'operation-expiry', `Preview expires ${displayTime(preview.expires_at)}. Any selected video changing its metadata invalidates the batch.`));
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const back = create('button', 'button button-quiet', 'Back to editor'); back.type = 'button'; back.addEventListener('click', () => openBulkMetadataEditor(true));
    const queue = create('button', 'button button-primary', `Queue ${preview.items.length} updates`); queue.type = 'button'; queue.addEventListener('click', queueBulkMetadataUpdate);
    actions.append(back, queue);
  }
  async function queueBulkMetadataUpdate() {
    const planId = state.metadataPlanId;
    const sequence = state.metadataDialogSeq;
    const videoIds = [...(state.metadataBulkVideoIds || [])];
    if (!planId || !videoIds.length || !$('#metadataDialog').open) return;
    if (state.route !== 'library' || JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(videoIds)) {
      closeDialog($('#metadataDialog'));
      announce('The selection changed after preview. Reopen the bulk editor to review the current selection.');
      return;
    }
    const actions = $('#metadataDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    const queue = [...actions.querySelectorAll('button')].find(button => button.textContent.startsWith('Queue '));
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: planId });
      const current = sequence === state.metadataDialogSeq && $('#metadataDialog').open
        && JSON.stringify([...state.selectedVideos.keys()]) === JSON.stringify(videoIds);
      if (current) {
        for (const videoId of videoIds) state.selectedVideos.delete(videoId);
        updateBulkSelection(); closeDialog($('#metadataDialog'));
      }
      if (current) showQueued(result, `Bulk metadata update for ${result.jobs?.length || videoIds.length} videos`);
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || !$('#metadataDialog').open) return;
      announce(error.message); actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (error.status === 409) {
        state.metadataPlanId = null;
        if (queue) queue.disabled = true;
        const back = create('button', 'button button-primary', 'Review current selection'); back.type = 'button';
        back.addEventListener('click', () => openBulkMetadataEditor(true));
        if (queue) queue.before(back); else actions.append(back);
        if (focusedAction && document.activeElement === document.body) back.focus({ preventScroll: true });
      } else if (focusedAction?.isConnected && document.activeElement === document.body) {
        focusedAction.focus({ preventScroll: true });
      }
    }
  }
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
  function renderDuplicateCandidates(payload) {
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

  async function loadPeople() {
    if (state.route !== 'performers' || $('#performerDirectoryPane').hidden) return;
    const sequence = ++state.peopleRequestSeq;
    state.peopleBusy = true;
    const query = $('#peopleQuery').value.trim(); setText($('#peopleFeedback'), 'Loading people…');
    try {
      const params = new URLSearchParams({ q: query, offset: String(state.peopleOffset), limit: '50' });
      const payload = await getJson(`${V2_API}/organize/people?${params}`);
      if (sequence !== state.peopleRequestSeq || state.route !== 'performers' || $('#performerDirectoryPane').hidden) return;
      if (!payload?.ok) throw new Error(responseError(payload, 'Could not load the people catalog.'));
      if (typeof payload.available !== 'boolean') {
        throw new Error('The people catalog response is incomplete.');
      }
      if (!payload.available) {
        $('#peopleRows').replaceChildren();
        state.peopleTotal = 0;
        $('#peopleState').hidden = false;
        $('#peopleState').textContent = 'People data is unavailable. Check database migrations and refresh.';
        $('#peopleTableWrap').hidden = true;
        setText($('#peopleFeedback'), 'People catalog unavailable');
        $('#peoplePageInfo').textContent = 'Unavailable';
        $('#peoplePrev').disabled = true;
        $('#peopleNext').disabled = true;
        $('#peoplePagination').hidden = true;
        return;
      }
      if (!Array.isArray(payload.items)) {
        throw new Error('The people catalog response is incomplete.');
      }
      const rows = $('#peopleRows'); rows.replaceChildren();
      state.peopleTotal = Number(payload.total) || 0;
      $('#peopleState').hidden = payload.items.length > 0;
      $('#peopleTableWrap').hidden = !payload.items.length;
      if (!payload.items.length) $('#peopleState').textContent = query ? 'No people match this search.' : 'No performers are in the catalog yet.';
      for (const person of payload.items || []) {
        const row = document.createElement('tr');
        const aliases = Array.isArray(person.aliases)
          ? person.aliases.filter(alias => typeof alias === 'string' && alias.trim()).join(', ')
          : '';
        row.append(create('td', 'person-name', person.name), create('td', 'person-aliases', aliases || '—'), create('td', '', person.video_count),
          create('td', '', person.has_bio ? 'Available' : 'Missing'), create('td', '', person.has_photo ? 'Available' : 'Missing'));
        const actionCell = document.createElement('td'); const details = create('button', 'button button-quiet', 'Details'); details.type = 'button'; details.addEventListener('click', () => openPersonDetails(person.name)); actionCell.append(details); row.append(actionCell); rows.append(row);
      }
      setText($('#peopleFeedback'), `${state.peopleTotal} people`);
      $('#peoplePageInfo').textContent = state.peopleTotal ? `${state.peopleOffset + 1}–${Math.min(state.peopleOffset + 50, state.peopleTotal)} of ${state.peopleTotal}` : 'No people';
      $('#peoplePrev').disabled = state.peopleOffset <= 0; $('#peopleNext').disabled = state.peopleOffset + 50 >= state.peopleTotal;
      $('#peoplePagination').hidden = state.peopleTotal <= 50;
    } catch (error) {
      if (sequence !== state.peopleRequestSeq || state.route !== 'performers' || $('#performerDirectoryPane').hidden) return;
      setText($('#peopleFeedback'), error.message); $('#peopleState').hidden = false; $('#peopleState').textContent = 'People could not be loaded. Refresh to try again.';
    } finally { if (sequence === state.peopleRequestSeq) state.peopleBusy = false; }
  }
  async function openPersonDetails(name) {
    const dialog = $('#personDetailDialog'); const content = $('#personDetailContent'); content.replaceChildren();
    state.personMergePreviewSeq += 1;
    state.pendingPersonMerge = null;
    const heading = create('h2', '', name); heading.id = 'personDetailTitle'; content.append(create('p', 'eyebrow', 'FILMOGRAPHY'), heading, create('p', 'catalog-feedback', 'Loading filmography…')); openDialog(dialog);
    dialog.addEventListener('close', () => {
      state.personFilmographySeq += 1;
      state.personMergePreviewSeq += 1;
      state.pendingPersonMerge = null;
    }, { once: true });
    const mergeSection = create('section', 'person-merge-panel');
    mergeSection.append(create('h3', '', 'Merge this name into another performer'));
    const label = create('label', '', 'Canonical performer name'); label.htmlFor = 'personMergeCanonical';
    const input = document.createElement('input'); input.id = 'personMergeCanonical'; input.type = 'text'; input.maxLength = 200; input.autocomplete = 'off';
    const previewButton = create('button', 'button button-quiet', 'Preview merge'); previewButton.id = 'personMergePreview'; previewButton.type = 'button'; previewButton.addEventListener('click', () => previewPersonMerge(name));
    const feedback = create('p', 'catalog-feedback'); feedback.id = 'personMergeFeedback';
    mergeSection.append(label, input, previewButton, feedback);
    const profileSection = create('section', 'person-profile-panel');
    profileSection.append(create('h3', '', 'Performer profile'));
    const profileStatus = create('p', 'catalog-feedback', 'Loading profile…');
    profileStatus.id = 'personProfileFeedback'; profileStatus.setAttribute('role', 'status'); profileStatus.setAttribute('aria-live', 'polite');
    const profileLinks = create('div', 'detail-links person-profile-links');
    profileLinks.id = 'personProfileLinks';
    profileSection.append(profileStatus, profileLinks);
    const profileFields = create('div', 'person-profile-fields');
    const bioLabel = create('label', '', 'Biography');
    const bio = document.createElement('textarea'); bio.id = 'personProfileBio'; bio.maxLength = 12000; bio.rows = 5; bio.setAttribute('aria-describedby', 'personProfileBioHelp');
    bioLabel.append(bio);
    const bioHelp = create('small', 'detail-muted', 'Manual biographies are protected from provider updates until reset.'); bioHelp.id = 'personProfileBioHelp'; bioLabel.append(bioHelp);
    const photoLabel = create('label', '', 'Artwork URL (HTTPS)');
    const photo = document.createElement('input'); photo.id = 'personProfilePhoto'; photo.type = 'url'; photo.maxLength = 2048; photo.autocomplete = 'url'; photo.placeholder = 'https://…'; photo.setAttribute('aria-describedby', 'personProfilePhotoHelp');
    photoLabel.append(photo);
    const photoHelp = create('small', 'detail-muted', 'Public HTTPS artwork URLs only. Credentials, local hosts, secret query parameters, and URL fragments are rejected.'); photoHelp.id = 'personProfilePhotoHelp'; photoLabel.append(photoHelp);
    const aliasesLabel = create('label', '', 'Aliases (one per line)');
    const aliases = document.createElement('textarea'); aliases.id = 'personProfileAliases'; aliases.maxLength = 24000; aliases.rows = 5; aliases.setAttribute('aria-describedby', 'personProfileAliasesHelp');
    aliasesLabel.append(aliases);
    const aliasesHelp = create('small', 'detail-muted', 'Aliases are used for performer matching. Ambiguous or duplicate identities are rejected.'); aliasesHelp.id = 'personProfileAliasesHelp'; aliasesLabel.append(aliasesHelp);
    profileFields.append(bioLabel, photoLabel, aliasesLabel);
    const profileActions = create('div', 'person-profile-actions');
    const saveProfile = create('button', 'button button-primary', 'Save profile'); saveProfile.id = 'personProfileSave'; saveProfile.type = 'button';
    const resetProfile = create('button', 'button button-quiet', 'Reset to provider data'); resetProfile.id = 'personProfileReset'; resetProfile.type = 'button';
    const reloadProfile = create('button', 'button button-quiet', 'Reload profile'); reloadProfile.id = 'personProfileReload'; reloadProfile.type = 'button';
    profileActions.append(saveProfile, resetProfile, reloadProfile);
    profileSection.append(profileFields, profileActions);
    content.insertBefore(profileSection, heading.nextSibling);
    const profileUrl = `${V2_API}/organize/people/${encodeURIComponent(name)}/profile`;
    const loadProfile = async () => {
      profileStatus.textContent = 'Loading profile…';
      saveProfile.disabled = resetProfile.disabled = reloadProfile.disabled = true;
      try {
        const payload = await getJson(profileUrl);
        if (!dialog.open || !payload?.ok) throw new Error(responseError(payload, 'Could not load performer profile.'));
        const profile = payload.profile;
        bio.value = profile.bio || ''; photo.value = profile.photo || ''; aliases.value = (profile.aliases || []).join('\n');
        profileLinks.replaceChildren();
        if (profile.stashdb_url) {
          const a = create('a', 'detail-link', 'StashDB ↗');
          a.href = profile.stashdb_url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          profileLinks.append(a);
        }
        if (profile.pbc_url) {
          const a = create('a', 'detail-link', 'PBC ↗');
          a.href = profile.pbc_url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          profileLinks.append(a);
        }
        profileSection.dataset.revision = String(profile.revision || 0);
        profileStatus.textContent = profile.overrides?.length
          ? `Revision ${profile.revision}. Manual overrides: ${profile.overrides.join(', ')}.`
          : `Revision ${profile.revision}. Profile values are inherited from provider data.`;
        const capabilities = await getJson(`${V2_API}/capabilities`);
        const canWrite = capabilities.actions_enabled === true;
        saveProfile.disabled = resetProfile.disabled = !canWrite;
        if (!canWrite) profileStatus.textContent += ' Editing is unavailable in READ-ONLY mode.';
      } catch (error) {
        if (!dialog.open) return;
        profileStatus.textContent = `Could not load performer profile. ${error.message}`;
        reloadProfile.disabled = false;
      }
    };
    saveProfile.addEventListener('click', async () => {
      saveProfile.disabled = resetProfile.disabled = true;
      profileStatus.textContent = 'Saving profile…';
      try {
        const result = await window.AdminV2.putJson(profileUrl, {
          expected_revision: Number(profileSection.dataset.revision),
          bio: bio.value,
          photo: photo.value,
          aliases: aliases.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
        });
        if (!dialog.open) return;
        profileSection.dataset.revision = String(result.profile.revision);
        profileStatus.textContent = `Saved revision ${result.profile.revision}. Manual overrides: ${result.profile.overrides.join(', ') || 'none'}.`;
        loadPersonFilmography(name, 0, heading, content, mergeSection);
      } catch (error) {
        profileStatus.textContent = error.status === 409
          ? `Profile changed elsewhere. Reload the current profile before saving again. ${error.message}`
          : `Could not save performer profile. ${error.message}`;
        saveProfile.disabled = false;
      } finally {
        if (dialog.open) {
          const capabilities = await getJson(`${V2_API}/capabilities`).catch(() => ({}));
          resetProfile.disabled = capabilities.actions_enabled !== true;
        }
      }
    });
    resetProfile.addEventListener('click', async () => {
      if (!window.confirm('Reset biography, artwork, and aliases to their saved provider values?')) return;
      saveProfile.disabled = resetProfile.disabled = true;
      profileStatus.textContent = 'Restoring provider values…';
      try {
        const result = await window.AdminV2.putJson(profileUrl, {
          expected_revision: Number(profileSection.dataset.revision),
          bio: bio.value, photo: photo.value,
          aliases: aliases.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
          reset_fields: ['bio', 'photo', 'aliases'],
        });
        if (!dialog.open) return;
        profileSection.dataset.revision = String(result.profile.revision);
        bio.value = result.profile.bio || ''; photo.value = result.profile.photo || ''; aliases.value = (result.profile.aliases || []).join('\n');
        profileStatus.textContent = `Restored saved provider values at revision ${result.profile.revision}.`;
      } catch (error) {
        profileStatus.textContent = error.status === 409
          ? `Profile changed elsewhere. Reload before resetting. ${error.message}`
          : `Could not reset performer profile. ${error.message}`;
      } finally { if (dialog.open) await loadProfile(); }
    });
    reloadProfile.addEventListener('click', loadProfile);
    try {
      void loadProfile();
      await loadPersonFilmography(name, 0, heading, content, mergeSection);
    } catch (error) { content.append(create('p', 'state-banner', error.message)); }
  }
  async function loadPersonFilmography(name, offset, heading, content, mergeSection) {
    const requestSeq = ++state.personFilmographySeq;
    content.querySelector('.filmography-content')?.remove();
    const status = create('p', 'catalog-feedback', 'Loading filmography…');
    status.className = 'catalog-feedback filmography-content';
    content.insertBefore(status, mergeSection.isConnected ? mergeSection : null);
    try {
      const params = new URLSearchParams({ offset: String(offset), limit: '50' });
      const payload = await getJson(`${V2_API}/organize/people/${encodeURIComponent(name)}?${params}`);
      if (requestSeq !== state.personFilmographySeq) return;
      if (!payload?.ok) throw new Error(responseError(payload, 'Could not load filmography.'));
      const section = create('div', 'filmography-content');
      section.append(create('p', 'detail-description', `${payload.total} catalog videos`));
      const list = create('ul', 'person-filmography');
      for (const video of payload.filmography) {
        const item = create('li'); const link = create('a', 'text-link', video.title || 'Untitled'); link.href = `#film/${encodeURIComponent(video.id)}`; item.append(link);
        if (video.studio) item.append(create('span', 'detail-muted', ` · ${video.studio}`)); list.append(item);
      }
      section.append(list);
      const range = create('p', 'catalog-feedback', payload.total
        ? `${payload.offset + 1}–${Math.min(payload.offset + payload.filmography.length, payload.total)} of ${payload.total}`
        : 'No videos');
      range.setAttribute('aria-live', 'polite'); range.className = 'catalog-feedback filmography-range';
      section.append(range);
      if (payload.total > 50) {
        const pagination = create('div', 'pagination filmography-pagination');
        const previous = create('button', 'button button-quiet', 'Previous'); previous.type = 'button'; previous.disabled = payload.offset <= 0;
        previous.addEventListener('click', () => loadPersonFilmography(name, Math.max(0, payload.offset - payload.limit), heading, content, mergeSection));
        const next = create('button', 'button button-quiet', 'Next'); next.type = 'button'; next.disabled = payload.offset + payload.filmography.length >= payload.total;
        next.addEventListener('click', () => loadPersonFilmography(name, payload.offset + payload.limit, heading, content, mergeSection));
        pagination.append(previous, next); section.append(pagination);
      }
      content.querySelector('.filmography-content')?.remove();
      content.insertBefore(section, mergeSection.isConnected ? mergeSection : null);
      if (!mergeSection.isConnected) content.append(mergeSection);
    } catch (error) {
      if (requestSeq !== state.personFilmographySeq) return;
      const errorState = create('p', 'state-banner filmography-content', error.message);
      content.querySelector('.filmography-content')?.replaceWith(errorState);
      if (!errorState.isConnected) content.append(errorState);
    }
  }
  async function previewPersonMerge(alias) {
    const previewSeq = ++state.personMergePreviewSeq;
    const canonical = $('#personMergeCanonical').value.trim();
    const feedback = $('#personMergeFeedback');
    state.pendingPersonMerge = null;
    const existingConfirmation = $('#personMergeUndoConfirm');
    if (existingConfirmation) existingConfirmation.checked = false;
    const existingQueue = $('#personMergeQueue');
    if (existingQueue) existingQueue.disabled = true;
    if (!canonical || canonical.toLocaleLowerCase() === alias.toLocaleLowerCase()) { feedback.textContent = 'Enter a different canonical performer name.'; return; }
    feedback.textContent = 'Preparing merge preview…';
    try {
      const preview = await window.AdminV2.postJson(`${V2_API}/operations/preview`, { type: 'performer_merge', alias, canonical });
      if (previewSeq !== state.personMergePreviewSeq || !$('#personDetailDialog').open) return;
      const item = preview.items?.[0];
      if (!preview.plan_id || !item) throw new Error('Could not prepare merge preview.');
      state.pendingPersonMerge = { plan_id: preview.plan_id, alias: item.alias, canonical: item.canonical };
      const warnings = Array.isArray(preview.warnings) ? ` ${preview.warnings.join(' ')}` : '';
      feedback.textContent = `Preview: ${Number(item.alias_videos) || 0} videos use “${item.alias}”; ${Number(item.canonical_videos) || 0} use “${item.canonical}”. The worker will merge metadata and then refresh Jellyfin.${warnings}`;
      let confirmationLabel = $('#personMergeUndoWarning');
      if (!confirmationLabel) {
        confirmationLabel = create('label', 'state-banner person-merge-undo-warning');
        confirmationLabel.id = 'personMergeUndoWarning';
        const confirmation = document.createElement('input'); confirmation.id = 'personMergeUndoConfirm'; confirmation.type = 'checkbox';
        confirmation.addEventListener('change', () => { const queueButton = $('#personMergeQueue'); if (queueButton) queueButton.disabled = !confirmation.checked || !state.pendingPersonMerge; });
        confirmationLabel.append(confirmation, document.createTextNode(' I understand this performer merge cannot be undone in Admin v2.'));
        feedback.after(confirmationLabel);
      }
      let queue = $('#personMergeQueue');
      if (!queue) { queue = create('button', 'button button-primary', 'Queue performer merge'); queue.id = 'personMergeQueue'; queue.type = 'button'; queue.disabled = true; queue.addEventListener('click', queuePersonMerge); confirmationLabel.after(queue); }
    } catch (error) {
      if (previewSeq !== state.personMergePreviewSeq || !$('#personDetailDialog').open) return;
      feedback.textContent = error.message; state.pendingPersonMerge = null;
      const queue = $('#personMergeQueue'); if (queue) queue.disabled = true;
    }
  }
  async function queuePersonMerge() {
    const merge = state.pendingPersonMerge; if (!merge || !$('#personMergeUndoConfirm')?.checked) return;
    const requestSeq = state.personMergePreviewSeq;
    const route = state.route;
    const dialog = $('#personDetailDialog');
    if (!dialog.open) return;
    const button = $('#personMergeQueue');
    const restoreQueueFocus = document.activeElement === button;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: merge.plan_id });
      if (state.route !== route) return;
      showQueued(result, 'Performer merge');
      if (requestSeq !== state.personMergePreviewSeq || !dialog.open) { loadPeople(); return; }
      closeDialog(dialog); loadPeople();
    } catch (error) {
      if (requestSeq !== state.personMergePreviewSeq || !dialog.open) return;
      const feedback = $('#personMergeFeedback');
      if (error.status === 409) {
        state.pendingPersonMerge = null;
        const confirmation = $('#personMergeUndoConfirm'); if (confirmation) confirmation.checked = false;
        button.disabled = true;
        feedback.textContent = `The merge preview is stale. ${error.message} Preview the merge again and review it before queueing.`;
        const previewButton = $('#personMergePreview');
        if (restoreQueueFocus && document.activeElement === document.body) previewButton?.focus({ preventScroll: true });
      } else {
        feedback.textContent = error.message; button.disabled = false;
        if (restoreQueueFocus && document.activeElement === document.body) button.focus({ preventScroll: true });
      }
    }
  }
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
    $('#visualDuplicateRefresh').addEventListener('click', () => loadVisualDuplicateResults());
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
    $('#peopleRefresh').addEventListener('click', () => loadPeople());
    $('#peopleQuery').addEventListener('input', () => {
      clearTimeout(state.peopleTimer); state.peopleOffset = 0;
      state.peopleTimer = setTimeout(loadPeople, 250);
    });
    $('#peoplePrev').addEventListener('click', () => { state.peopleOffset = Math.max(0, state.peopleOffset - 50); loadPeople(); });
    $('#peopleNext').addEventListener('click', () => { state.peopleOffset += 50; loadPeople(); });
    $('#quarantineRefresh').addEventListener('click', () => loadQuarantine());
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
      updateMoreFilterCount();
      if (event.target.name === 'q' || ['channel_id', 'studio', 'performer', 'tag'].includes(event.target.name)) debounceLoad();
    });
    $('#libraryFilters').addEventListener('change', event => {
      updateMoreFilterCount();
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
      $('#libraryFilters').reset(); clearLibrarySelection(); resetCatalogPage(); updateMoreFilterCount(); commitFilterUrl(); loadVideos();
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
      if (action === 'refresh-status') loadStatus();
      if (action === 'close-detail') closeDetail();
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
})();
