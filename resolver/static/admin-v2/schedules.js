(() => {
  'use strict';
  const endpoint = '/admin/api/v2/schedules';
  const $ = selector => document.querySelector(selector);
  const node = (tag, cls, text) => {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (text !== undefined) item.textContent = String(text);
    return item;
  };
  const actions = { availability_check: 'Availability check', consistency_audit: 'Consistency audit', backup_create: 'Database backup', duplicate_reconcile: 'Duplicate reconciliation', catalog_full_audit: 'Full catalog audit', metadata_quality_audit: 'Metadata quality audit' };
  const POLL_INTERVAL_MS = 30_000;
  let loadSeq = 0;
  let schedulesActive = false;
  let pollTimer = null;
  let focusedScheduleId = '';
  function timeNode(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return node('span', '', '—');
    const time = node('time', '', date.toLocaleString('en-US', { timeZone: 'Europe/Warsaw' }));
    time.dateTime = date.toISOString();
    return time;
  }

  async function load({ silent = false } = {}) {
    const sequence = ++loadSeq;
    const feedback = $('#schedulesFeedback');
    if (!silent) feedback.textContent = 'Loading schedules…';
    try {
      const result = await window.AdminV2.getJson(endpoint);
      if (sequence !== loadSeq) return null;
      const host = $('#schedulesRows'); host.replaceChildren();
      const filter = $('#scheduleFilter')?.value || 'all';
      let visibleCount = 0;
      result.items.forEach(item => {
        if (!['all', 'admin'].includes(filter) && !(filter === 'problems' && ['failed', 'skipped_overlap'].includes(item.last_result))) return;
        visibleCount += 1;
        const row = node('article', 'operation-history-row');
        const validId = typeof item.id === 'string' && /^(?:[a-f0-9]{32}|catalog-health-full)$/.test(item.id);
        if (validId) {
          row.id = 'schedule-' + item.id;
          row.tabIndex = -1;
        }
        const copy = node('div');
        copy.append(node('strong', '', `${item.name} · ${actions[item.action] || item.action}`));
        copy.append(node('small', '', 'Admin schedule'));
        const cadence = item.schedule_type === 'daily' ? `Daily at ${item.daily_at} Europe/Warsaw` : `Every ${item.interval_minutes} minutes`;
        const nextRun = node('p');
        nextRun.append(node('span', '', `${cadence} · Next run `), timeNode(item.next_run_at));
        copy.append(nextRun);
        const last = { queued: 'Queued', processing: 'Running', done: 'Completed', failed: 'Failed', cancelled: 'Cancelled', skipped_overlap: 'Skipped because the previous run is still active' }[item.last_result] || 'No runs yet';
        const lastRun = node('small');
        lastRun.append(node('span', '', 'Last run '), timeNode(item.last_run_at),
          node('span', '', ` · Result: ${last}${item.video_count ? ` · ${item.video_count} video IDs` : ''}`));
        copy.append(lastRun);
        const jobId = typeof item.last_job_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(item.last_job_id)
          ? item.last_job_id : '';
        if (jobId) {
          const task = node('a', 'button button-quiet', 'Open task');
          task.href = `/admin/v2/?job_id=${encodeURIComponent(jobId)}#jobs`;
          task.setAttribute('aria-label', `Open task for ${item.name}`);
          copy.append(task);
        }
        const toggle = node('button', 'button button-quiet', item.enabled ? 'Disable' : 'Enable');
        toggle.type = 'button'; toggle.addEventListener('click', async () => {
          toggle.disabled = true;
          try {
            await window.AdminV2.postJson(`${endpoint}/${encodeURIComponent(item.id)}/enabled`, { enabled: !item.enabled, revision: item.revision });
            await load();
          } catch (error) { feedback.textContent = `Could not update schedule. ${error.message}`; toggle.disabled = false; }
        });
        row.append(copy, toggle); host.append(row);
      });
      (result.system_items || []).forEach(item => {
        const dependent = item.group === 'dependent';
        if (filter === 'admin' || (filter === 'problems' && !item.problem) || (filter === 'system' && dependent) || (filter === 'dependent' && !dependent)) return;
        visibleCount += 1;
        const row = node('article', 'operation-history-row');
        const copy = node('div');
        const groupLabel = dependent ? 'Runs during another schedule' : 'System schedule';
        copy.append(node('strong', '', item.name), node('small', '', groupLabel),
          node('p', '', item.cadence), node('small', '', `${item.source} · ${item.details}`));
        row.append(copy); host.append(row);
      });
      if (!visibleCount) host.append(node('p', 'catalog-empty', filter === 'problems'
        ? 'No schedule problems are currently reported.' : 'No entries match this filter.'));
      const selectedId = new URLSearchParams(location.hash.split('?')[1] || '').get('schedule_id') || '';
      let selectedMessage = '';
      if (selectedId && /^(?:[a-f0-9]{32}|catalog-health-full)$/.test(selectedId)) {
        const selected = document.getElementById?.('schedule-' + selectedId);
        if (selected) {
          if (focusedScheduleId !== selectedId) {
            selected.scrollIntoView({ block: 'nearest' });
            selected.focus({ preventScroll: true });
            focusedScheduleId = selectedId;
          }
        } else selectedMessage = ' The schedule from this audit record is no longer available.';
      } else {
        focusedScheduleId = '';
      }
      feedback.textContent = `${visibleCount} recurring entr${visibleCount === 1 ? 'y' : 'ies'} shown · ${result.items.length} admin schedules · ${(result.system_items || []).length} system and dependent tasks.${selectedMessage}`;
      return true;
    } catch (error) {
      if (sequence !== loadSeq) return null;
      feedback.textContent = silent
        ? `Schedules may be out of date. Could not refresh them. ${error.message}`
        : `Could not load schedules. ${error.message}`;
      return false;
    }
  }

  function updateForm() {
    $('#scheduleDailyField').hidden = $('#scheduleType').value !== 'daily';
    $('#scheduleIntervalField').hidden = $('#scheduleType').value !== 'interval';
    $('#scheduleVideosField').hidden = $('#scheduleAction').value !== 'availability_check';
  }
  function formValues() {
    return ['scheduleName', 'scheduleAction', 'scheduleType', 'scheduleDailyAt', 'scheduleInterval', 'scheduleVideoIds']
      .map(id => $(`#${id}`).value);
  }
  async function create(event) {
    event.preventDefault();
    const form = $('#scheduleCreateForm'); const button = $('#scheduleCreate');
    const restoreFocus = document.activeElement === button || event.submitter === button;
    button.disabled = true;
    const submittedValues = formValues();
    const action = $('#scheduleAction').value, type = $('#scheduleType').value;
    const body = { name: $('#scheduleName').value.trim(), action, schedule_type: type, timezone: 'Europe/Warsaw' };
    if (type === 'daily') body.daily_at = $('#scheduleDailyAt').value;
    else body.interval_minutes = Number($('#scheduleInterval').value);
    if (action === 'availability_check') body.video_ids = $('#scheduleVideoIds').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    try {
      await window.AdminV2.postJson(endpoint, body);
      const newerDraftExists = formValues().some((value, index) => value !== submittedValues[index]);
      if (!newerDraftExists) {
        form.reset(); $('#scheduleDailyAt').value = '03:00'; $('#scheduleInterval').value = '1440';
      }
      updateForm();
      const refreshed = await load();
      $('#schedulesFeedback').textContent = refreshed !== false
        ? newerDraftExists ? 'Schedule created. Your newer draft was kept.' : 'Schedule created.'
        : `Schedule created, but the schedule list could not be refreshed.${newerDraftExists ? ' Your newer draft was kept.' : ''}`;
    } catch (error) { $('#schedulesFeedback').textContent = `Could not create schedule. ${error.message}`; }
    finally {
      button.disabled = false;
      if (restoreFocus && button.isConnected && document.activeElement === document.body) button.focus({ preventScroll: true });
    }
  }
  $('#scheduleType').addEventListener('change', updateForm);
  $('#scheduleAction').addEventListener('change', updateForm);
  $('#scheduleCreateForm').addEventListener('submit', create);
  $('#scheduleFilter')?.addEventListener('change', () => load());
  $('#schedulesRefresh').addEventListener('click', load);
  function syncSchedules(route) {
    schedulesActive = route === 'jobs' && new URLSearchParams(location.hash.split('?')[1] || '').get('tab') === 'schedules';
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
    if (schedulesActive) {
      if (!document.hidden) load();
      pollTimer = setInterval(() => {
        if (schedulesActive && !document.hidden) load({ silent: true });
      }, POLL_INTERVAL_MS);
    } else {
      loadSeq += 1;
    }
  }
  document.addEventListener('adminv2:navigate', event => syncSchedules(event.detail?.route));
  document.addEventListener('visibilitychange', () => {
    if (schedulesActive && !document.hidden) load({ silent: true });
  });
  syncSchedules(location.hash.replace(/^#\/?/, '').split(/[/?]/)[0]);
  updateForm();
})();
