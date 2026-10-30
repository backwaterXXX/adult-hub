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
  let editingScheduleId = '';
  let closeActiveEditor = null;
  function timeNode(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return node('span', '', '—');
    const time = node('time', '', date.toLocaleString('en-US', { timeZone: 'Europe/Warsaw' }));
    time.dateTime = date.toISOString();
    return time;
  }

  async function load({ silent = false } = {}) {
    if (silent && editingScheduleId) return null;
    if (!silent && closeActiveEditor) closeActiveEditor();
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
        const cadence = item.cron_expression ? `Cron: ${item.cron_expression} Europe/Warsaw` : item.schedule_type === 'daily' ? `Daily at ${item.daily_at} Europe/Warsaw` : `Every ${item.interval_minutes} minutes`;
        const nextRun = node('p');
        nextRun.append(node('span', '', `${cadence} · Next run `), timeNode(item.next_run_at));
        copy.append(nextRun);
        const last = { queued: 'Queued', processing: 'Running', done: 'Completed', failed: 'Failed', cancelled: 'Cancelled', skipped_overlap: 'Skipped because the previous run is still active' }[item.last_result] || 'No runs yet';
        const lastRun = node('small');
        lastRun.append(node('span', '', 'Last run '), timeNode(item.last_run_at),
          node('span', '', ` · Result: ${last}${item.video_count ? ` · ${item.video_count} video IDs` : ''}`));
        copy.append(lastRun);
        if (['duplicate_reconcile', 'catalog_full_audit'].includes(item.action)) {
          copy.append(makeScheduleEditor(item, feedback));
        }
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

  function makeScheduleEditor(item, feedback) {
    const wrap = node('div', 'schedule-edit-wrap');
    const edit = node('button', 'button button-quiet schedule-edit-toggle', 'Edit schedule');
    edit.type = 'button'; edit.setAttribute('aria-expanded', 'false');
    const editor = node('div', 'schedule-editor'); editor.hidden = true;
    const heading = node('div', 'schedule-editor-heading');
    heading.append(node('strong', '', 'Schedule'), node('span', 'schedule-timezone', 'Europe/Warsaw'));
    const fields = node('div', 'schedule-editor-fields');
    const frequencyLabel = node('label', 'schedule-field', 'Frequency');
    const frequency = node('select');
    for (const [value, label] of [['15', 'Every 15 minutes'], ['60', 'Every hour'], ['daily', 'Daily'], ['weekly', 'Weekly'], ['custom', 'Custom cron']]) {
      const option = node('option', '', label); option.value = value; frequency.append(option);
    }
    frequencyLabel.append(frequency);
    const timeLabel = node('label', 'schedule-field', 'Time');
    const time = node('input'); time.type = 'time'; time.value = '03:00'; timeLabel.append(time);
    const dayLabel = node('label', 'schedule-field', 'Day');
    const day = node('select');
    ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'].forEach((label, index) => {
      const option = node('option', '', label); option.value = String(index); day.append(option);
    });
    day.value = '1'; dayLabel.append(day);
    const cronLabel = node('label', 'schedule-field schedule-field-cron', 'Cron expression');
    const cron = node('input'); cron.type = 'text'; cron.maxLength = 100; cron.placeholder = 'minute hour day month weekday';
    cronLabel.append(cron);
    fields.append(frequencyLabel, timeLabel, dayLabel, cronLabel);
    const initial = item.cron_expression || (item.schedule_type === 'daily' && /^\d\d:\d\d$/.test(item.daily_at || '')
      ? `${Number(item.daily_at.slice(3))} ${Number(item.daily_at.slice(0, 2))} * * *`
      : item.schedule_type === 'interval' && item.interval_minutes > 0 && 60 % item.interval_minutes === 0
        ? `*/${item.interval_minutes} * * * *` : '');
    cron.value = initial;
    let match;
    if (initial === '*/15 * * * *') frequency.value = '15';
    else if (initial === '0 * * * *' || initial === '*/60 * * * *') frequency.value = '60';
    else if ((match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$/.exec(initial))) {
      time.value = `${match[2].padStart(2, '0')}:${match[1].padStart(2, '0')}`;
      frequency.value = match[3] === '*' ? 'daily' : 'weekly';
      if (match[3] !== '*') day.value = match[3];
    } else frequency.value = 'custom';
    const expression = node('div', 'schedule-expression');
    const preview = node('div', 'schedule-preview', 'Choose a frequency to preview upcoming runs.');
    preview.setAttribute('role', 'status');
    const actions = node('div', 'schedule-editor-actions');
    const save = node('button', 'button button-primary', 'Save schedule'); save.type = 'button'; save.disabled = true;
    const cancel = node('button', 'button button-quiet', 'Cancel'); cancel.type = 'button';
    actions.append(save, cancel);
    editor.append(heading, fields, expression, preview, actions);
    wrap.append(edit, editor);
    let timer = null, sequence = 0, valid = '';
    function currentExpression() {
      if (frequency.value === '15') return '*/15 * * * *';
      if (frequency.value === '60') return '0 * * * *';
      if (frequency.value === 'custom') return cron.value.trim();
      if (!/^\d\d:\d\d$/.test(time.value)) return '';
      const [hour, minute] = time.value.split(':').map(Number);
      return `${minute} ${hour} * * ${frequency.value === 'weekly' ? day.value : '*'}`;
    }
    async function previewExpression(value, current) {
      try {
        const result = await window.AdminV2.getJson(`${endpoint}/cron-preview?expression=${encodeURIComponent(value)}`);
        if (current !== sequence || editor.hidden) return;
        if (!Array.isArray(result.next_runs) || !result.next_runs.length) throw new Error('No upcoming runs.');
        preview.replaceChildren(node('span', '', 'Next runs: '));
        result.next_runs.forEach((value, index) => {
          if (index) preview.append(node('span', '', ' · '));
          preview.append(timeNode(value));
        });
        valid = value; save.disabled = false;
      } catch (error) {
        if (current !== sequence || editor.hidden) return;
        preview.textContent = `Check the schedule: ${error.message}`;
        save.disabled = true;
      }
    }
    function update() {
      timeLabel.hidden = !['daily', 'weekly'].includes(frequency.value);
      dayLabel.hidden = frequency.value !== 'weekly';
      cronLabel.hidden = frequency.value !== 'custom';
      const value = currentExpression(); expression.textContent = value ? `Cron · ${value}` : 'Enter a cron expression.';
      valid = ''; save.disabled = true; sequence += 1;
      if (timer !== null) clearTimeout(timer);
      if (!value) { preview.textContent = 'Enter a valid schedule.'; return; }
      preview.textContent = 'Checking upcoming runs…';
      const current = sequence;
      timer = setTimeout(() => previewExpression(value, current), 250);
    }
    function close() {
      editor.hidden = true; edit.setAttribute('aria-expanded', 'false');
      if (editingScheduleId === item.id) editingScheduleId = '';
      if (closeActiveEditor === close) closeActiveEditor = null;
      sequence += 1; if (timer !== null) clearTimeout(timer);
    }
    edit.addEventListener('click', () => {
      if (!editor.hidden) { close(); return; }
      if (closeActiveEditor) closeActiveEditor();
      closeActiveEditor = close; editingScheduleId = item.id; editor.hidden = false; edit.setAttribute('aria-expanded', 'true'); update();
    });
    cancel.addEventListener('click', close);
    frequency.addEventListener('change', update); time.addEventListener('input', update);
    day.addEventListener('change', update); cron.addEventListener('input', update);
    save.addEventListener('click', async () => {
      const value = currentExpression();
      if (!valid || valid !== value) return;
      save.disabled = true;
      [frequency, time, day, cron, cancel, edit].forEach(control => { control.disabled = true; });
      try {
        await window.AdminV2.postJson(`${endpoint}/${encodeURIComponent(item.id)}/cron`,
          { cron_expression: value, revision: item.revision });
        close(); const refreshed = await load();
        feedback.textContent = refreshed === false ? 'Schedule saved, but the list could not be refreshed.' : 'Schedule updated.';
      } catch (error) {
        preview.textContent = `Could not save schedule. ${error.message}`; save.disabled = false;
        [frequency, time, day, cron, cancel, edit].forEach(control => { control.disabled = false; });
      }
    });
    return wrap;
  }

  $('#scheduleFilter')?.addEventListener('change', () => load());

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
