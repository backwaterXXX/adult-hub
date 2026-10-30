(() => {
  'use strict';

  const endpoint = '/admin/api/v2/activity';
  const limit = 20;
  let offset = 0;
  let total = 0;
  let changeOffset = 0;
  let changeTotal = 0;
  let loadSeq = 0;
  let changeLoadSeq = 0;
  const $ = selector => document.querySelector(selector);
  const filters = { activity_method: '', activity_outcome: '', activity_area: '', change_type: '', object_type: '', change_q: '' };
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  function readFilters() {
    const raw = new URLSearchParams((location.hash.split('?', 2)[1] || ''));
    const allowed = {
      activity_method: ['POST', 'PUT', 'PATCH', 'DELETE'],
      activity_outcome: ['accepted', 'completed'],
      activity_area: ['library', 'performers', 'quality', 'sources', 'jobs', 'system'],
      change_type: ['metadata_update', 'source_settings_update', 'admin_operation_queued', 'schedule_created', 'schedule_enabled', 'performer_profile_update'],
      object_type: ['video', 'source_configuration', 'operation_plan', 'schedule', 'performer'],
    };
    for (const [key, values] of Object.entries(allowed)) {
      const value = raw.get(key) || '';
      filters[key] = values.includes(value) ? value : '';
    }
    const q = raw.get('change_q') || '';
    filters.change_q = q.length <= 100 ? q : '';
    if ($('#activityMethod')) $('#activityMethod').value = filters.activity_method;
    if ($('#activityOutcome')) $('#activityOutcome').value = filters.activity_outcome;
    if ($('#activityArea')) $('#activityArea').value = filters.activity_area;
    if ($('#activityChangeType')) $('#activityChangeType').value = filters.change_type;
    if ($('#activityObjectType')) $('#activityObjectType').value = filters.object_type;
    if ($('#activityChangeQuery')) $('#activityChangeQuery').value = filters.change_q;
  }
  function writeFiltersToUrl() {
    const url = new URL(location.href);
    const params = new URLSearchParams(location.hash.split('?', 2)[1] || '');
    params.set('tab', 'activity');
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value); else params.delete(key);
    }
    url.hash = 'system?' + params.toString();
    history.pushState({}, '', url);
  }
  function safeHref(item) {
    if (item.object_type === 'video' && typeof item.object_id === 'string' && item.object_id.length <= 256) {
      return '#film/' + encodeURIComponent(item.object_id);
    }
    if (item.object_type === 'source_configuration') return '#sources';
    if (item.object_type === 'schedule' && /^(?:[a-f0-9]{32}|catalog-health-full)$/.test(item.object_id || '')) {
      return '#jobs?tab=schedules&schedule_id=' + encodeURIComponent(item.object_id);
    }
    if (item.object_type === 'performer' && typeof item.object_id === 'string' && item.object_id.length <= 200) {
      return '#performers?tab=directory&person=' + encodeURIComponent(item.object_id);
    }
    return '';
  }
  function appendObjectLinks(copy, item) {
    const href = safeHref(item);
    if (!href) return;
    const label = item.object_type === 'video' ? 'Open video'
      : item.object_type === 'source_configuration' ? 'Open Sources'
      : item.object_type === 'schedule' ? 'Open schedule'
        : item.object_type === 'performer' ? 'Open performer' : 'Open task';
    const link = node('a', 'button button-quiet', label);
    link.href = href;
    copy.append(link);
  }

  function formatDate(value) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? 'Time unavailable' : parsed.toLocaleString('en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  }

  function render(items) {
    const host = $('#activityRows');
    host.replaceChildren();
    if (!items.length) {
      host.append(node('p', 'catalog-empty', 'No successful Admin v2 actions have been recorded yet.'));
      return;
    }
    for (const item of items) {
      const row = node('article', 'operation-history-row');
      const copy = node('div');
      copy.append(node('strong', '', `${item.method} ${item.route}`));
      copy.append(node('p', '', `${formatDate(item.created_at)} · ${item.outcome === 'accepted' ? 'Accepted by the queue' : 'Request completed'} · HTTP ${item.status_code}`));
      if (item.correlation_id) copy.append(node('small', '', `Request ID: ${item.correlation_id}`));
      row.append(copy);
      host.append(row);
    }
  }

  async function load(nextOffset = offset) {
    const sequence = ++loadSeq;
    offset = nextOffset;
    const feedback = $('#activityFeedback');
    feedback.textContent = 'Loading admin activity…';
    try {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      for (const [key, value] of [['method', filters.activity_method], ['outcome', filters.activity_outcome], ['area', filters.activity_area]]) {
        if (value) params.set(key, value);
      }
      const result = await window.AdminV2.getJson(endpoint + '?' + params);
      if (sequence !== loadSeq) return;
      if (!Array.isArray(result.items) || !Number.isInteger(result.total)) throw new Error('The activity response is incomplete.');
      total = result.total;
      render(result.items);
      feedback.textContent = `${total.toLocaleString('en-US')} successful Admin v2 request${total === 1 ? '' : 's'} recorded.`;
      const pages = Math.max(1, Math.ceil(total / limit));
      const current = Math.min(pages, Math.floor(offset / limit) + 1);
      $('#activityPageInfo').textContent = `Page ${current} of ${pages}`;
      $('#activityPagination').hidden = pages <= 1;
      $('#activityPrev').disabled = current <= 1;
      $('#activityNext').disabled = current >= pages;
      offset = (current - 1) * limit;
    } catch (error) {
      if (sequence !== loadSeq) return;
      feedback.textContent = `Could not load admin activity. ${error.message}`;
    }
  }

  function displayValue(value) {
    if (value === null || value === undefined) return 'Empty';
    if (typeof value === 'string') return value || 'Empty';
    try { return JSON.stringify(value); } catch { return 'Value unavailable'; }
  }

  function renderChanges(items) {
    const host = $('#activityChangeRows');
    host.replaceChildren();
    if (!items.length) {
      host.append(node('p', 'catalog-empty', 'No applied field changes have been recorded yet.'));
      return;
    }
    const labels = {
      title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Cast', tags: 'Tags',
      date_released: 'Release date', operation_type: 'Operation', requested_count: 'Selected items',
      job_count: 'Tasks created', reused: 'Existing task reused', state: 'Plan state',
      action: 'Scheduled action', schedule_type: 'Schedule type', interval_minutes: 'Interval (minutes)',
      daily_at: 'Daily time', timezone: 'Time zone', enabled: 'Enabled', revision: 'Revision',
      video_count: 'Scheduled videos', bio: 'Biography', photo: 'Artwork', aliases: 'Aliases',
    };
    for (const item of items) {
      const row = node('article', 'operation-history-row');
      const copy = node('div');
      const eventLabel = item.event_type === 'metadata_update' ? 'Metadata updated'
        : item.event_type === 'source_settings_update' ? 'Source settings updated'
        : item.event_type === 'admin_operation_queued' ? 'Operation queued'
        : item.event_type === 'schedule_created' ? 'Schedule created'
        : item.event_type === 'schedule_enabled' ? 'Schedule updated'
        : item.event_type === 'performer_profile_update' ? 'Performer profile updated' : 'Admin change';
      copy.append(node('strong', '', `${eventLabel} · ${item.object_id}`));
      appendObjectLinks(copy, item);
      if (item.job_id && item.object_type !== 'schedule') {
        const task = node('a', 'button button-quiet', 'Open task');
        task.href = '/admin/v2/?job_id=' + encodeURIComponent(item.job_id) + '#jobs';
        copy.append(task);
      }
      copy.append(node('p', '', `${formatDate(item.created_at)}${item.job_id ? ` · Task ${item.job_id}` : ''}`));
      if (item.truncated) copy.append(node('small', 'catalog-state', 'Only part of this configuration change is available in the audit record.'));
      for (const [field, values] of Object.entries(item.changes || {})) {
        const detail = node('details', 'activity-change-detail');
        detail.append(node('summary', '', labels[field] || field));
        detail.append(node('p', '', `Before: ${displayValue(values.before)}`));
        detail.append(node('p', '', `After: ${displayValue(values.after)}`));
        if (typeof values.before_overridden === 'boolean' && values.before_overridden !== values.after_overridden) {
          detail.append(node('small', '', values.after_overridden ? 'Now using a manual override.' : 'Now inheriting the latest source value.'));
        }
        copy.append(detail);
      }
      row.append(copy);
      host.append(row);
    }
  }

  async function loadChanges(nextOffset = changeOffset) {
    const sequence = ++changeLoadSeq;
    changeOffset = nextOffset;
    const feedback = $('#activityChangeFeedback');
    feedback.textContent = 'Loading applied changes…';
    try {
      const params = new URLSearchParams({ offset: String(changeOffset), limit: String(limit) });
      for (const [key, value] of [['event_type', filters.change_type], ['object_type', filters.object_type], ['q', filters.change_q]]) {
        if (value) params.set(key, value);
      }
      const result = await window.AdminV2.getJson(endpoint + '/changes?' + params);
      if (sequence !== changeLoadSeq) return;
      if (!Array.isArray(result.items) || !Number.isInteger(result.total)) throw new Error('The change history response is incomplete.');
      changeTotal = result.total;
      renderChanges(result.items);
      feedback.textContent = `${changeTotal.toLocaleString('en-US')} applied change event${changeTotal === 1 ? '' : 's'} recorded.`;
      const pages = Math.max(1, Math.ceil(changeTotal / limit));
      const current = Math.min(pages, Math.floor(changeOffset / limit) + 1);
      $('#activityChangePageInfo').textContent = `Page ${current} of ${pages}`;
      $('#activityChangePagination').hidden = pages <= 1;
      $('#activityChangePrev').disabled = current <= 1;
      $('#activityChangeNext').disabled = current >= pages;
      changeOffset = (current - 1) * limit;
    } catch (error) {
      if (sequence !== changeLoadSeq) return;
      feedback.textContent = `Could not load applied changes. ${error.message}`;
    }
  }

  $('#activityRefresh').addEventListener('click', () => load(offset));
  $('#activityPrev').addEventListener('click', () => load(Math.max(0, offset - limit)));
  $('#activityNext').addEventListener('click', () => load(offset + limit));
  $('#activityChangeRefresh').addEventListener('click', () => loadChanges(changeOffset));
  $('#activityChangePrev').addEventListener('click', () => loadChanges(Math.max(0, changeOffset - limit)));
  $('#activityChangeNext').addEventListener('click', () => loadChanges(changeOffset + limit));
  $('#activityFilters')?.addEventListener('change', () => {
    filters.activity_method = $('#activityMethod').value;
    filters.activity_outcome = $('#activityOutcome').value;
    filters.activity_area = $('#activityArea').value;
    writeFiltersToUrl(); void load(0);
  });
  $('#activityChangeFilters')?.addEventListener('submit', event => {
    event.preventDefault();
    filters.change_type = $('#activityChangeType').value;
    filters.object_type = $('#activityObjectType').value;
    filters.change_q = $('#activityChangeQuery').value.trim().slice(0, 100);
    writeFiltersToUrl(); void loadChanges(0);
  });
  $('#activityChangeFilters')?.addEventListener('reset', () => {
    queueMicrotask(() => {
      filters.change_type = ''; filters.object_type = ''; filters.change_q = '';
      writeFiltersToUrl(); void loadChanges(0);
    });
  });
  document.addEventListener('adminv2:navigate', event => {
    if (event.detail.route === 'system' && event.detail.systemTab === 'activity') {
      readFilters();
      load(0);
      loadChanges(0);
    }
  });
})();
