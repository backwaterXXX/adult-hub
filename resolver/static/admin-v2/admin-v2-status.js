(() => {
  'use strict';
  // Admin V2 header status chips, overview status cards, and the problem list.
  const AV = window.AdminV2;
  const { $, V2_API, announce, create, first, getJson, issueFeedUrl, readIssueFiltersFromUrl, safeText,
    setHidden, setText, state } = AV;
  const displayTime = (...args) => AV.displayTime(...args);
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
  function renderActiveTasksChip(payload) {
    const element = $('#activeTasks');
    if (!element) return;
    const taskState = window.AdminV2HeaderStatus.activeTasks(payload !== undefined ? payload : state.services.activeTasks);
    const label = $('span:last-child', element);
    if (element.dataset.state === taskState.state && label && label.textContent === taskState.label) return;
    element.dataset.state = taskState.state;
    element.title = taskState.state === 'ready'
      ? `${taskState.count} active ${taskState.count === 1 ? 'task' : 'tasks'} currently running or queued.`
      : taskState.state === 'checking'
        ? 'Active task status is still loading.'
        : 'Active tasks could not be verified.';
    setText(label, taskState.label);
    const taskLabel = taskState.label.endsWith('…') ? taskState.label.slice(0, -1) : taskState.label;
    element.setAttribute('aria-label', `${taskLabel}. Open Tasks.`);
  }
  function updateServices(patch) {
    state.services = { ...state.services, checking: false, ...patch };
    renderServiceChip();
    if (patch && Object.prototype.hasOwnProperty.call(patch, 'activeTasks')) {
      renderActiveTasksChip(state.services.activeTasks);
    }
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
  function capabilitiesPayload(payload) {
    if (!payload) return null;
    return payload.capabilities || payload;
  }
  function metric(label, value, caption, icon) {
    const card = create('article', 'metric-card');
    const top = create('div', 'metric-top'); top.append(create('span', '', label));
    if (icon) top.append(create('span', 'metric-icon', icon));
    card.append(top, create('strong', 'metric-value', value));
    if (caption) card.append(create('span', 'metric-caption', caption));
    return card;
  }
  function coverageLabel(covered, total) {
    if (!Number.isSafeInteger(covered) || !Number.isSafeInteger(total) || covered < 0 || total < 0) return 'No data';
    const percent = total > 0 ? Math.round((covered / total) * 100) : 0;
    return `${covered.toLocaleString('en-US')} (${percent}%)`;
  }
  function countLabel(value, fallback = 'No data') {
    return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('en-US') : fallback;
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
      metric('Videos in library', countLabel(videos), 'Catalog records', '▤'),
      metric('Active tasks', countLabel(queue), 'Queued or processing', '◷'),
      metric('Problems', countLabel(problemTotal),
        problemTotal === null ? 'The problem sources could not be read'
          : problems.incomplete ? 'Unresolved problems; some sources are unavailable'
            : 'Unresolved problems', '!'),
      metric('Artwork coverage', coverageLabel(db.artwork_total, videos), '', ''),
      metric('OSHASH coverage', coverageLabel(db.oshash_total, videos), '', ''),
      metric('pHash coverage', coverageLabel(db.phash_total, videos), '', '')
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

  Object.assign(AV, { loadHeaderStatus, loadHeaderSyncStatus, loadIssues, loadStatus, loadSystemStatus });
})();
