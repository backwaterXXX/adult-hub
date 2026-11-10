(() => {
  'use strict';
  // Admin V2 shared core: constants, route state, URL filters, API and dialog helpers, and the window.AdminV2 namespace.
  const AV = window.AdminV2 = {};
  const clearLibrarySelection = (...args) => AV.clearLibrarySelection(...args);
  const closeDetailVisual = (...args) => AV.closeDetailVisual(...args);
  const loadDetail = (...args) => AV.loadDetail(...args);
  const loadDuplicateCandidates = (...args) => AV.loadDuplicateCandidates(...args);
  const loadIssues = (...args) => AV.loadIssues(...args);
  const loadPeople = (...args) => AV.loadPeople(...args);
  const loadQuarantine = (...args) => AV.loadQuarantine(...args);
  const loadStatus = (...args) => AV.loadStatus(...args);
  const loadSystemStatus = (...args) => AV.loadSystemStatus(...args);
  const loadVideos = (...args) => AV.loadVideos(...args);
  const loadVisualDuplicateResults = (...args) => AV.loadVisualDuplicateResults(...args);
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
  const state = { route: 'start', detailId: null, libraryTab: 'videos', duplicateMatch: 'catalog', performersTab: 'review', qualityTab: null, systemTab: null, peopleDirectoryQuery: '', status: null, statusLoaded: false, statusRequestSeq: 0,
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
  const PERFORMER_TABS = ['directory', 'review', 'name-variants'];
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
      return target('performers', tab === 'matching' ? 'name-variants' : 'review');
    }
    if (route === 'performers' && tab === 'review' && params.get('area') === 'name-variants') return target('performers', 'name-variants');
    if (route === 'performers' && tab === 'review' && params.has('area')) return target('performers', 'review');
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
      return { route, primaryRoute: 'performers', detailId: null, performersTab,
        focusTarget: performersTab === 'name-variants' ? '#fuzzyTitle' : null };
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
    performersTabList: { directory: '#performerDirectoryPane', review: '#performerReviewPane', 'name-variants': '#performerNameVariantsPane' }
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
      void loadChannelFilterOptions();
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
        systemTab: state.systemTab, sourcesTab: next.sourcesTab || null
      }
    }));
    if (next.focusTarget) requestAnimationFrame(() => {
      const target = $(next.focusTarget);
      target?.focus({ preventScroll: true });
      if (next.duplicateMatch === 'visual' || next.performersTab === 'name-variants') {
        target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
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
  let channelFilterLoaded = false;
  async function loadChannelFilterOptions() {
    const select = $('#filterChannel');
    if (!select || channelFilterLoaded) return;
    try {
      const data = await window.AdminV2.getJson('/admin/api/v2/sources/list');
      const channels = [];
      if (Array.isArray(data?.sources)) {
        for (const source of data.sources) {
          if (!source || (source.provider && source.provider !== 'telegram')) continue;
          if (Array.isArray(source.channels)) {
            for (const ch of source.channels) {
              if (ch && ch.id) channels.push(ch);
            }
          }
        }
      } else if (Array.isArray(data?.items)) {
        for (const item of data.items) {
          if (item && item.id && (!item.provider || item.provider === 'telegram')) {
            channels.push(item);
          }
        }
      }
      if (!channels.length) return;
      channels.sort((a, b) => (a.studio || a.id || '').localeCompare(b.studio || b.id || '', undefined, { sensitivity: 'base' }));
      const currentValue = select.value;
      const options = [];
      const defaultOpt = document.createElement('option');
      defaultOpt.value = '';
      defaultOpt.textContent = 'All channels';
      options.push(defaultOpt);
      channels.forEach(ch => {
        const opt = document.createElement('option');
        opt.value = ch.id;
        opt.textContent = ch.studio ? `${ch.studio} (${ch.id})` : ch.id;
        options.push(opt);
      });
      select.replaceChildren(...options);
      if (currentValue) {
        if (typeof select.querySelector === 'function' && !select.querySelector(`option[value="${currentValue}"]`)) {
          const opt = document.createElement('option');
          opt.value = currentValue;
          opt.textContent = currentValue;
          select.append(opt);
        }
        select.value = currentValue;
      }
      channelFilterLoaded = true;
    } catch {
      // A failed load keeps the current options; channelFilterLoaded stays false so the next call retries.
    }
  }
  document.addEventListener('adminv2:sources-changed', () => {
    channelFilterLoaded = false;
    void loadChannelFilterOptions();
  });
  function syncFiltersFromUrl() {
    const params = routeQueryParams();
    const form = $('#libraryFilters');
    for (const name of ['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort']) {
      const field = form.elements.namedItem(name);
      if (field) {
        const val = params.get(name) || (name === 'sort' ? 'first_seen' : '');
        if (field.id === 'filterChannel' && val && typeof field.querySelector === 'function' && !field.querySelector(`option[value="${val}"]`)) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = val;
          field.append(opt);
        }
        field.value = val;
      }
    }
    state.offset = Math.max(0, Number(params.get('offset')) || 0);
    state.catalogCursor = params.get('cursor') || null;
    state.catalogCursorDirection = params.get('cursor_direction') === 'before' ? 'before' : 'after';
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
    text = text.replace(/\b[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^\s"'<>)]+/g, '[link hidden]');
    text = text.replace(/\b[A-Za-z]:\\[^\s"'<>]*/g, '[path hidden]');
    text = text.replace(/(^|[\s("'=:])\/(?:[\w.-]+\/)+[\w.-]*/g, (match, prefix) => `${prefix}[path hidden]`);
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
  // Publish core bindings for the other Admin V2 scripts.
  Object.assign(AV, { requestJson, getJson, postJson, putJson, deleteJson, openDialog, closeDialog,
    setFeedback, downloadBlob, create, el: create, setText, announce, safeId, scrub, titleCase,
    detailField, formatDuration, scheduleSync, currentRoute, taskLink, showQueued,
    loadChannelFilterOptions, $, $$, DUPLICATE_PAGE_SIZE, DUPLICATE_SCAN_BATCH_SIZE,
    DUPLICATE_SCAN_CACHE_TTL_MS, LIMIT, V2_API, appendTelegramId, applySavedView, beginOperationRequest,
    commitFilterUrl, deleteSavedView, duplicateGroupIsVisible, filterParams, first, go, issueFeedUrl,
    libraryHash, operationRequestIsCurrent, readIssueFiltersFromUrl, renderLocation, renderSavedViews,
    resetCatalogPage, responseError, routeInfo, safeText, saveCurrentView, selectDuplicateMatch, setHidden,
    state, urlFor, writeFilterUrl, writeIssueFiltersToUrl });
})();
