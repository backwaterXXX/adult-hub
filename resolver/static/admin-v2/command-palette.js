(() => {
  'use strict';

  const API = '/admin/api/v2';
  const MAX_PER_GROUP = 5;
  const $ = selector => document.querySelector(selector);
  const dialog = $('#commandPalette');
  const trigger = $('#commandPaletteTrigger');
  const closeButton = $('#commandPaletteClose');
  const input = $('#commandPaletteQuery');
  const results = $('#commandPaletteResults');
  const status = $('#commandPaletteStatus');
  const state = { opener: null, timer: null, abort: null, sequence: 0, items: [], active: -1 };

  const pages = [
    { label: 'Dashboard', subtitle: 'Service health and Needs Attention', route: '#start', words: 'home overview status health issues' },
    { label: 'Library', subtitle: 'Search videos, files, and availability', route: '#library', words: 'videos catalog collection' },
    { label: 'Performers · Directory', subtitle: 'Canonical names, aliases, and filmography', route: '#performers?tab=directory', words: 'people actors aliases' },
    { label: 'Performers · Proposals', subtitle: 'Review names detected from Telegram', route: '#performers?tab=proposals', words: 'suggestions detected telegram' },
    { label: 'Performers · Name Matching', subtitle: 'Review possible spelling variants', route: '#performers?tab=matching', words: 'fuzzy variants merge typo' },
    { label: 'Catalog Health · Issues & Audits', subtitle: 'Metadata and file consistency findings', route: '#quality?tab=issues', words: 'quality audit missing metadata nfo' },
    { label: 'Catalog Health · Enrichment', subtitle: 'Provider progress and metadata suggestions', route: '#quality?tab=enrichment', words: 'metadata enrichment stashdb theporndb proposals scan' },
    { label: 'Catalog Health · Duplicates', subtitle: 'Review duplicate suggestions', route: '#quality?tab=duplicates', words: 'organize duplicate fingerprint' },
    { label: 'Catalog Health · Quarantine', subtitle: 'Restore hidden items and sidecars', route: '#quality?tab=quarantine', words: 'hidden restore orphan' },
    { label: 'Sources', subtitle: 'Telegram channels and sync history', route: '#sources', words: 'channels telegram sync' },
    { label: 'Jobs & Automation · Tasks', subtitle: 'Queued, running, and completed work', route: '#jobs', words: 'jobs tasks queue processing' },
    { label: 'Jobs & Automation · Bulk Operations', subtitle: 'Plans and per-item outcomes', route: '#jobs?tab=bulk', words: 'bulk operation history' },
    { label: 'Jobs & Automation · Schedules', subtitle: 'Recurring checks and scheduled tasks', route: '#jobs?tab=schedules', words: 'schedule automation recurring' },
    { label: 'System', subtitle: 'Maintenance, diagnostics, backups, and audit log', route: '#system', words: 'settings maintenance diagnostics backup' },
    { label: 'Failed jobs', subtitle: 'Open Tasks filtered to failed work', route: '#jobs', params: { job_state: 'failed' }, words: 'failed error retry' },
    { label: 'Unavailable videos', subtitle: 'Open Library filtered by availability', route: '#library', params: { availability: 'unavailable' }, words: 'broken unavailable stream links' },
    { label: 'Check availability', subtitle: 'Library · select videos for a bounded check', route: '#library', target: '#checkAvailabilityBulk', words: 'availability link check stream' },
  ];

  // Actions navigate to the existing controls and invoke their existing click
  // handlers only after the server confirms write capability. Each handler
  // remains responsible for its established preview/confirmation and request.
  const actions = [
    { label: 'Clear resolver cache', subtitle: 'System · Maintenance · confirmation required', words: 'cache clear resolver reset', route: '#system', target: '#clearResolverCache', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
    { label: 'Refresh Jellyfin library', subtitle: 'System · Maintenance · confirmation required', words: 'refresh jellyfin library rescan', route: '#system', target: '#refreshJellyfinLibrary', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
    { label: 'Run full sync', subtitle: 'Sources · full backfill · confirmation required', words: 'sync full backfill telegram', route: '#system', target: '#triggerFullSync', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
    { label: 'Run metadata quality audit', subtitle: 'Catalog Health · metadata findings · confirmation required', words: 'run audit metadata quality missing fields', route: '#quality?tab=issues', target: '#qualityRunAudit', mode: 'metadata_quality', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
    { label: 'Run file consistency audit', subtitle: 'Catalog Health · file consistency findings · confirmation required', words: 'run audit file consistency nfo strm artwork', route: '#quality?tab=issues', target: '#qualityRunAudit', mode: 'file_consistency', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
    { label: 'Run full audit', subtitle: 'Catalog Health · metadata and file checks · confirmation required', words: 'run audit full complete all', route: '#quality?tab=issues', target: '#qualityRunAudit', mode: 'full', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
    { label: 'Create database backup', subtitle: 'System · Backups · confirmation required', words: 'backup database snapshot create', route: '#system?tab=backups', target: '#backupCreate', scope: 'global', permission: 'write', confirmation: 'existing handler', handler: 'existing-control' },
  ];

  const normalize = value => String(value ?? '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().trim();
  function editDistanceAtMostOne(left, right) {
    if (!left || !right || Math.abs(left.length - right.length) > 1) return false;
    let i = 0, j = 0, edits = 0;
    while (i < left.length && j < right.length) {
      if (left[i] === right[j]) { i += 1; j += 1; continue; }
      if (++edits > 1) return false;
      if (left.length > right.length) i += 1;
      else if (right.length > left.length) j += 1;
      else { i += 1; j += 1; }
    }
    return edits + (i < left.length || j < right.length ? 1 : 0) <= 1;
  }
  function score(query, values) {
    const needle = normalize(query);
    const words = values.flatMap(value => normalize(value).split(/[^\p{L}\p{N}]+/u)).filter(Boolean);
    if (!needle) return 1;
    const haystack = values.map(normalize);
    if (haystack.includes(needle)) return 0;
    if (haystack.some(value => value.startsWith(needle))) return 1;
    if (haystack.some(value => value.includes(needle)) || words.some(value => value.startsWith(needle))) return 2;
    if (needle.length >= 4 && words.some(value => editDistanceAtMostOne(needle, value))) return 3;
    const queryWords = needle.split(/\s+/).filter(Boolean);
    if (queryWords.every(part => words.some(word => word.startsWith(part) || word.includes(part)))) return 4;
    return Infinity;
  }
  function rankStatic(query, entries) {
    return entries.map(item => ({ ...item, rank: score(query, [item.label, item.subtitle, item.words]) }))
      .filter(item => Number.isFinite(item.rank))
      .sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label, 'en'))
      .slice(0, MAX_PER_GROUP);
  }
  function capabilityState() {
    const mode = $('#adminAccessMode')?.dataset.state || 'unknown';
    return { mode, writable: mode === 'writable' || mode === 'token' };
  }
  function pageResult(item) {
    return { group: 'Pages', label: item.label, subtitle: item.subtitle, route: item.route, params: item.params, words: item.words };
  }
  function savedViewResults(query) {
    try {
      const saved = JSON.parse(localStorage.getItem('adminV2SavedViews') || '[]');
      if (!Array.isArray(saved)) return [];
      return saved.slice(0, 20).flatMap(view => {
        if (!view || typeof view.name !== 'string' || !view.filters || typeof view.filters !== 'object') return [];
        const rank = score(query, [view.name, 'saved library view']);
        if (!Number.isFinite(rank)) return [];
        const params = Object.fromEntries(Object.entries(view.filters)
          .filter(([key, value]) => ['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort'].includes(key)
            && typeof value === 'string' && value.length <= 200));
        return [{ group: 'Pages', label: `Library · ${view.name}`, subtitle: 'Saved Library filter view',
          route: '#library', params, rank }];
      }).sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label, 'en')).slice(0, MAX_PER_GROUP);
    } catch { return []; }
  }
  function actionResult(item) {
    const access = capabilityState();
    const enabled = item.permission !== 'write' || access.writable;
    const reason = enabled ? '' : access.mode === 'readonly' ? 'Unavailable in READ-ONLY mode.' : 'Write access could not be verified.';
    return { ...item, group: 'Actions', scope: item.scope || 'global', handler: item.handler || 'existing-control',
      enabled, available: enabled, unavailableReason: reason, reason };
  }
  function scopedActions(query) {
    const scoped = [];
    const match = location.hash.match(/^#film\/([^?]+)/);
    if (match) {
      let id = match[1];
      try { id = decodeURIComponent(id); } catch { /* retain the visible legacy identifier */ }
      const title = $('#detailTitle')?.textContent?.trim() || id;
      scoped.push({
        label: `Check availability — ${title}`,
        subtitle: `Library · video ${id} · preview and queue confirmation required`,
        words: 'check availability stream playback link', route: location.hash,
        target: '#detailCheckAvailability', scope: `video:${id}`, permission: 'write',
        confirmation: 'existing preview dialog', handler: 'existing-control',
      });
    }

    const personDialog = $('#personDetailDialog');
    const personTitle = $('#personDetailTitle')?.textContent?.trim();
    if (personDialog?.open && personTitle && $('#personMergeCanonical')) {
      scoped.push({
        label: `Review merge for ${personTitle}`,
        subtitle: 'Performers · focus the canonical name field, then review the existing impact preview',
        words: 'merge performer alias canonical match', route: location.hash,
        target: '#personMergeCanonical', scope: `performer:${personTitle}`, permission: 'read',
        confirmation: 'manual impact preview required', handler: 'focus-existing-control',
      });
    }

    const sourceDialog = $('#sourceDetailDialog');
    const sourceContent = $('#sourceDetailContent');
    const sourceTitle = $('#sourceDetailTitle')?.textContent?.trim();
    if (sourceDialog?.open && sourceTitle && sourceContent?.dataset.sourceId) {
      const id = sourceContent.dataset.sourceId;
      const provider = sourceContent.dataset.sourceProvider || 'source';
      if ($('#sourceDetailSyncNew')) scoped.push({
        label: `Sync new — ${sourceTitle}`,
        subtitle: `${provider} · source ${id} · existing job acknowledgement`,
        words: 'sync latest source channel telegram', route: location.hash,
        target: '#sourceDetailSyncNew', scope: `channel:${provider}:${id}`, permission: 'write',
        confirmation: 'existing source sync handler', handler: 'existing-control',
      });
      if ($('#sourceDetailCheckAccess')) scoped.push({
        label: `Check access — ${sourceTitle}`,
        subtitle: `Telegram · source ${id} · checks read access without downloading media`,
        words: 'check access permissions telegram channel', route: location.hash,
        target: '#sourceDetailCheckAccess', scope: `channel:${provider}:${id}`, permission: 'write',
        confirmation: 'none', handler: 'existing-control',
      });
    }

    return scoped.filter(item => score(query, [item.label, item.subtitle, item.words]) < Infinity)
      .map(item => item.permission === 'write' ? actionResult(item) : ({ ...item, group: 'Actions', enabled: true, available: true }));
  }
  function videoResults(payload) {
    return (Array.isArray(payload?.items) ? payload.items : []).slice(0, MAX_PER_GROUP).map(video => ({
      group: 'Videos', label: video.title || video.id || 'Untitled video',
      subtitle: [video.id, video.provider, video.studio].filter(Boolean).join(' · '),
      route: `#film/${encodeURIComponent(String(video.id || ''))}`,
      words: [video.title, video.id, ...(Array.isArray(video.performers) ? video.performers : [])].join(' '),
    }));
  }
  function performerResults(payload) {
    return (Array.isArray(payload?.items) ? payload.items : []).slice(0, MAX_PER_GROUP).map(person => ({
      group: 'Performers', label: person.name || person.id,
      subtitle: `Performer · ${person.id || 'ID unavailable'}`,
      route: '#performers?tab=directory', words: [person.name, ...(person.aliases || [])].join(' '),
      params: { person: person.name || '' },
    }));
  }
  function channelResults(payload) {
    return (Array.isArray(payload?.items) ? payload.items : []).slice(0, MAX_PER_GROUP).map(channel => ({
      group: 'Channels', label: channel.label || channel.id,
      subtitle: `${channel.provider || 'Source'} · ${channel.id || 'ID unavailable'}${channel.enabled ? '' : ' · Disabled'}`,
      route: '#sources', params: { source_id: `${channel.provider || 'telegram'}:${channel.id || ''}` },
      words: [channel.label, channel.id, channel.provider].join(' '),
    }));
  }
  async function fetchEntities(query, signal) {
    const encoded = encodeURIComponent(query);
    const requests = await Promise.allSettled([
      window.AdminV2.getJson(`${API}/library/videos?q=${encoded}&sort=title&limit=${MAX_PER_GROUP}&offset=0&exact_total=false`, signal),
      window.AdminV2.getJson(`${API}/performers/catalog?q=${encoded}&limit=${MAX_PER_GROUP}`, signal),
      window.AdminV2.getJson(`${API}/sources/search?q=${encoded}&limit=${MAX_PER_GROUP}`, signal),
    ]);
    return {
      videos: requests[0].status === 'fulfilled' ? videoResults(requests[0].value) : [],
      performers: requests[1].status === 'fulfilled' ? performerResults(requests[1].value) : [],
      channels: requests[2].status === 'fulfilled' ? channelResults(requests[2].value) : [],
      failed: requests.some(result => result.status === 'rejected' && result.reason?.name !== 'AbortError'),
    };
  }
  function render(items, message = '') {
    results.replaceChildren();
    state.items = items;
    state.active = items.length ? 0 : -1;
    const groups = ['Videos', 'Performers', 'Channels', 'Pages', 'Actions'];
    for (const group of groups) {
      const members = items.filter(item => item.group === group);
      if (!members.length) continue;
      const section = document.createElement('section');
      section.className = 'command-result-group';
      section.setAttribute('role', 'group');
      section.setAttribute('aria-label', group);
      const heading = document.createElement('h3'); heading.textContent = group;
      section.append(heading);
      for (const item of members) {
        const index = state.items.indexOf(item);
        const option = document.createElement('div');
        option.className = 'command-result';
        option.id = `command-result-${index}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(index === state.active));
        if (item.enabled === false) option.setAttribute('aria-disabled', 'true');
        const copy = document.createElement('span'); copy.className = 'command-result-copy';
        const title = document.createElement('strong'); title.textContent = item.label;
        const subtitle = document.createElement('small'); subtitle.textContent = item.reason || item.subtitle || '';
        copy.append(title, subtitle);
        const icon = document.createElement('span'); icon.className = 'command-result-type'; icon.setAttribute('aria-hidden', 'true');
        icon.textContent = group === 'Actions' ? '↵' : group === 'Pages' ? '⌘' : group === 'Videos' ? '▤' : group === 'Channels' ? '◉' : '♙';
        option.append(copy, icon);
        option.addEventListener('mousemove', () => setActive(index));
        option.addEventListener('click', () => select(index));
        section.append(option);
      }
      results.append(section);
    }
    if (state.active >= 0) input.setAttribute('aria-activedescendant', `command-result-${state.active}`);
    else input.removeAttribute('aria-activedescendant');
    status.textContent = message;
    if (items.length && !message) status.textContent = `${items.length} result${items.length === 1 ? '' : 's'}.`;
    if (!items.length && !message) status.textContent = 'No results. Try a title, performer, channel, page, or command.';
  }
  function setActive(index) {
    if (!state.items.length) return;
    state.active = (index + state.items.length) % state.items.length;
    results.querySelectorAll('[role="option"]').forEach(option => {
      const active = option.id === `command-result-${state.active}`;
      option.setAttribute('aria-selected', String(active));
    });
    input.setAttribute('aria-activedescendant', `command-result-${state.active}`);
    results.querySelector(`#command-result-${state.active}`)?.scrollIntoView?.({ block: 'nearest' });
  }
  function compose(query, entities = { videos: [], performers: [], channels: [] }) {
    const ordered = [
      ...entities.videos,
      ...entities.performers,
      ...entities.channels,
      ...rankStatic(query, pages).map(pageResult),
      ...savedViewResults(query),
      ...scopedActions(query),
      ...rankStatic(query, actions).map(actionResult),
    ];
    return ordered;
  }
  async function search(query) {
    const sequence = ++state.sequence;
    state.abort?.abort();
    state.abort = null;
    const cleaned = query.trim().slice(0, 200);
    if (cleaned.length < 2) {
      render(compose(cleaned), cleaned ? 'Enter at least two characters to search the catalog.' : 'Start typing to search the catalog or run a command.');
      return;
    }
    const controller = new AbortController(); state.abort = controller;
    const immediate = compose(cleaned);
    render(immediate, 'Searching videos, performers, and channels…');
    try {
      const entities = await fetchEntities(cleaned, controller.signal);
      if (sequence !== state.sequence || controller.signal.aborted || !dialog.open) return;
      const combined = compose(cleaned, entities);
      render(combined, entities.failed ? 'Some entity results could not be loaded.' : '');
    } catch (error) {
      if (sequence !== state.sequence || controller.signal.aborted) return;
      render(immediate, 'Entity search is unavailable. Pages and commands are still available.');
    }
  }
  function scheduleSearch() {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => search(input.value), 160);
  }
  function setUrl(route, params = {}) {
    const url = new URL(location.href);
    for (const [key, value] of Object.entries(params)) {
      if (value === null || value === undefined || value === '') url.searchParams.delete(key);
      else url.searchParams.set(key, String(value));
    }
    url.hash = route;
    history.pushState({}, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }
  function navigate(item, callback) {
    closePalette(false);
    const routePart = item.route.slice(1).split(/[?&]/, 1)[0];
    const routeQuery = item.route.includes('?') ? new URLSearchParams(item.route.slice(item.route.indexOf('?') + 1)) : new URLSearchParams();
    const targetRoute = item.group === 'Videos' || routePart.startsWith('film/') ? 'library'
      : routePart === 'performers' && routeQuery.get('tab') === 'directory' ? 'organize' : routePart;
    const onNavigate = event => {
      if (event.detail?.route !== targetRoute) return;
      document.removeEventListener('adminv2:navigate', onNavigate);
      requestAnimationFrame(() => callback?.());
    };
    document.addEventListener('adminv2:navigate', onNavigate);
    setUrl(item.route.slice(1), item.params);
  }
  function closePalette(restore = true) {
    clearTimeout(state.timer); state.timer = null;
    state.sequence += 1; state.abort?.abort(); state.abort = null;
    if (dialog.open) dialog.close();
    if (restore && state.opener?.isConnected) state.opener.focus({ preventScroll: true });
  }
  function openPalette(opener = trigger) {
    if (dialog.open) { input.focus(); return; }
    state.opener = opener;
    dialog.showModal();
    input.value = '';
    search('');
    requestAnimationFrame(() => input.focus({ preventScroll: true }));
  }
  async function select(index) {
    const item = state.items[index];
    if (!item || item.enabled === false) return;
    if (item.group === 'Actions') {
      if (item.permission === 'write') {
        try {
          const capabilities = await window.AdminV2.getJson(`${API}/capabilities`);
          if (capabilities.actions_enabled !== true) {
            status.textContent = 'This action is unavailable because the server has not enabled write access.';
            return;
          }
        } catch {
          status.textContent = 'This action is unavailable because write access could not be verified.';
          return;
        }
      }
      navigate(item, () => {
        if (item.handler === 'focus-existing-control') {
          $(item.target)?.focus({ preventScroll: true });
          return;
        }
        if (item.mode && $('#qualityAuditMode')) $('#qualityAuditMode').value = item.mode;
        const target = $(item.target);
        if (target && !target.disabled) { target.focus({ preventScroll: true }); target.click(); }
      });
      return;
    }
    navigate(item, () => {
      if (item.group === 'Performers' && item.params?.person) {
        const search = $('#peopleQuery');
        if (search) search.focus({ preventScroll: true });
      }
      if (item.target) {
        const target = $(item.target);
        (target && !target.disabled ? target : $('#filterAvailability'))?.focus({ preventScroll: true });
      }
      else $('#main-content')?.focus({ preventScroll: true });
    });
  }

  trigger.addEventListener('click', () => openPalette(trigger));
  closeButton?.addEventListener('click', () => closePalette());
  input.addEventListener('input', scheduleSearch);
  input.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive(state.active + 1); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive(state.active - 1); }
    else if (event.key === 'Enter') { event.preventDefault(); if (state.active >= 0) select(state.active); }
    else if (event.key === 'Escape') { event.preventDefault(); closePalette(); }
  });
  dialog.addEventListener('cancel', event => { event.preventDefault(); closePalette(); });
  dialog.addEventListener('click', event => { if (event.target === dialog) closePalette(); });
  document.addEventListener('adminv2:open-palette', () => openPalette(trigger));
  document.addEventListener('adminv2:access-change', () => { if (dialog.open) scheduleSearch(); });
  document.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229 || event.altKey || event.shiftKey) return;
    const isMac = /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || '');
    const modifier = isMac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    if (modifier && event.key.toLocaleLowerCase() === 'k') {
      event.preventDefault();
      if (dialog.open) closePalette(); else openPalette(document.activeElement || trigger);
    }
  });
  $('#commandPaletteShortcut').textContent = /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || '') ? '⌘ K' : 'Ctrl K';

  // Test handle: the palette internals (pages/actions/score) have no other consumer, so the contract test drives them through this export.
  window.AdminV2CommandPalette = Object.freeze({ open: openPalette, close: closePalette, pages, actions, score });
})();
