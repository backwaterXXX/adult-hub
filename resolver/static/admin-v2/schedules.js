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
  const taskNames = {
    availability_check: 'Availability check', consistency_audit: 'Consistency audit',
    metadata_quality_audit: 'Metadata quality audit', catalog_full_audit: 'Full catalog audit',
    backup_create: 'Database backup', duplicate_reconcile: 'Duplicate reconciliation'
  };
  const resultNames = {
    queued: 'Queued', processing: 'Running', done: 'Completed', failed: 'Failed',
    cancelled: 'Cancelled', skipped_overlap: 'Previous run still active',
    inherited: 'Inherited cadence', unknown: 'Unknown', fresh: 'Fresh', stale: 'Stale'
  };
  const POLL_INTERVAL_MS = 30_000;
  let loadSeq = 0;
  let schedulesActive = false;
  let pollTimer = null;
  let focusedScheduleId = '';
  let editingScheduleId = '';
  let closeActiveEditor = null;
  let createPreviewTimer = null;
  let createPreviewSeq = 0;

  function timeNode(value, timezone = 'Europe/Warsaw') {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return node('span', 'schedule-unknown', '—');
    const time = node('time', '', date.toLocaleString('en-GB', { timeZone: timezone }));
    time.dateTime = date.toISOString();
    return time;
  }

  function cadence(item) {
    if (item.cron_expression) return item.cron_expression;
    if (item.needs_conversion || item.schedule_type === 'interval' || item.schedule_type === 'daily') return 'Requires cron conversion';
    return '—';
  }

  function initialCron(item) {
    if (item.cron_expression) return item.cron_expression;
    if (item.schedule_type === 'daily' && /^([01]\d|2[0-3]):[0-5]\d$/.test(item.daily_at || '')) {
      const [hour, minute] = item.daily_at.split(':').map(Number);
      return `${minute} ${hour} * * *`;
    }
    if (item.schedule_type === 'interval' && Number.isInteger(item.interval_minutes)) {
      const minutes = item.interval_minutes;
      if (minutes <= 60 && 60 % minutes === 0) return minutes === 60 ? '0 * * * *' : `*/${minutes} * * * *`;
      if (minutes === 1440) return '0 0 * * *';
      if (minutes > 60 && minutes % 60 === 0 && 24 % (minutes / 60) === 0) {
        const hours = minutes / 60;
        return hours === 1 ? '0 * * * *' : `0 */${hours} * * *`;
      }
    }
    return '';
  }

  function statusName(item) {
    if (item.enabled === false) return 'Disabled';
    if (item.status === 'running') return 'Running';
    return resultNames[item.last_result] || resultNames[item.status] || 'No runs yet';
  }

  function statusClass(item) {
    if (item.enabled === false) return 'is-disabled';
    const value = item.status === 'running' ? item.status : (item.last_result || item.status);
    if (value === 'failed' || item.problem) return 'is-failed';
    if (value === 'processing' || value === 'queued' || value === 'running') return 'is-running';
    if (value === 'done' || value === 'fresh') return 'is-ok';
    return 'is-unknown';
  }

  function makeScheduleEditor(item, feedback, editorRow, editButton) {
    const cell = node('td'); cell.colSpan = 6;
    const editor = node('div', 'schedule-editor');
    const heading = node('div', 'schedule-editor-heading');
    heading.append(node('strong', '', 'Edit cron schedule'), node('span', 'schedule-timezone', item.timezone || 'Europe/Warsaw'));
    const fields = node('div', 'schedule-editor-fields');
    const frequencyLabel = node('label', 'schedule-field', 'Preset');
    const frequency = node('select');
    for (const [value, label] of [
      ['15', 'Every 15 minutes'], ['30', 'Every 30 minutes'], ['hourly', 'Every hour'],
      ['daily', 'Daily'], ['weekly', 'Weekly'], ['custom', 'Custom cron']
    ]) {
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
    const cron = node('input'); cron.type = 'text'; cron.maxLength = 100;
    cron.placeholder = 'minute hour day month weekday'; cron.spellcheck = false;
    cronLabel.append(cron);
    fields.append(frequencyLabel, timeLabel, dayLabel, cronLabel);

    const expression = node('code', 'schedule-expression');
    const preview = node('div', 'schedule-preview', 'Choose a frequency to preview upcoming runs.');
    preview.setAttribute('role', 'status');
    const actions = node('div', 'schedule-editor-actions');
    const save = node('button', 'button button-primary', 'Save'); save.type = 'button'; save.disabled = true;
    const cancel = node('button', 'button button-quiet', 'Cancel'); cancel.type = 'button';
    actions.append(save, cancel);
    editor.append(heading, fields, expression, preview, actions);
    cell.append(editor); editorRow.append(cell);

    const initial = initialCron(item);
    cron.value = initial;
    let match;
    if (initial === '*/15 * * * *') frequency.value = '15';
    else if (initial === '*/30 * * * *') frequency.value = '30';
    else if (initial === '0 * * * *') frequency.value = 'hourly';
    else if ((match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$/.exec(initial))) {
      time.value = `${match[2].padStart(2, '0')}:${match[1].padStart(2, '0')}`;
      frequency.value = match[3] === '*' ? 'daily' : 'weekly';
      if (match[3] !== '*') day.value = match[3];
    } else frequency.value = 'custom';

    let timer = null, sequence = 0, valid = '';
    const controls = [frequency, time, day, cron, cancel, editButton];
    function currentExpression() {
      if (frequency.value === '15') return '*/15 * * * *';
      if (frequency.value === '30') return '*/30 * * * *';
      if (frequency.value === 'hourly') return '0 * * * *';
      if (frequency.value === 'custom') return cron.value.trim();
      if (!/^\d\d:\d\d$/.test(time.value)) return '';
      const [hour, minute] = time.value.split(':').map(Number);
      return `${minute} ${hour} * * ${frequency.value === 'weekly' ? day.value : '*'}`;
    }
    async function previewExpression(value, current) {
      try {
        const result = await window.AdminV2.getJson(`${endpoint}/cron-preview?expression=${encodeURIComponent(value)}`);
        if (current !== sequence || editorRow.hidden) return;
        if (!Array.isArray(result.next_runs) || result.next_runs.length !== 3) throw new Error('Upcoming runs are unavailable.');
        preview.replaceChildren(node('span', '', 'Next runs: '));
        result.next_runs.forEach((run, index) => {
          if (index) preview.append(node('span', '', ' · '));
          preview.append(timeNode(run));
        });
        valid = value; save.disabled = false;
      } catch (error) {
        if (current !== sequence || editorRow.hidden) return;
        preview.textContent = `Check the cron expression. ${error.message}`;
        save.disabled = true;
      }
    }
    function update() {
      timeLabel.hidden = !['daily', 'weekly'].includes(frequency.value);
      dayLabel.hidden = frequency.value !== 'weekly';
      cronLabel.hidden = frequency.value !== 'custom';
      const value = currentExpression();
      expression.textContent = value || 'Enter a cron expression.';
      valid = ''; save.disabled = true; sequence += 1;
      if (timer !== null) clearTimeout(timer);
      if (!value) { preview.textContent = 'This older interval needs a cron expression before it can be converted.'; return; }
      preview.textContent = 'Checking upcoming runs…';
      const current = sequence;
      timer = setTimeout(() => previewExpression(value, current), 250);
    }
    function close() {
      editorRow.hidden = true; editButton.setAttribute('aria-expanded', 'false');
      editButton.textContent = 'Edit';
      if (editingScheduleId === item.id) editingScheduleId = '';
      if (closeActiveEditor === close) closeActiveEditor = null;
      sequence += 1; if (timer !== null) clearTimeout(timer);
    }
    editButton.addEventListener('click', () => {
      if (!editorRow.hidden) { close(); return; }
      if (closeActiveEditor) closeActiveEditor();
      closeActiveEditor = close; editingScheduleId = item.id;
      editorRow.hidden = false; editButton.setAttribute('aria-expanded', 'true'); editButton.textContent = 'Close'; update();
    });
    cancel.addEventListener('click', close);
    frequency.addEventListener('change', update); time.addEventListener('input', update);
    day.addEventListener('change', update); cron.addEventListener('input', update);
    save.addEventListener('click', async () => {
      const value = currentExpression();
      if (!valid || valid !== value) return;
      save.disabled = true;
      controls.forEach(control => { control.disabled = true; });
      try {
        if (item.group === 'system') {
          await window.AdminV2.postJson(`${endpoint}/system/${encodeURIComponent(item.id)}`,
            { cron_expression: value, enabled: item.enabled !== false, revision: item.revision });
        } else {
          await window.AdminV2.postJson(`${endpoint}/${encodeURIComponent(item.id)}/cron`,
            { cron_expression: value, revision: item.revision });
        }
        close();
        const refreshed = await load();
        feedback.textContent = refreshed === false ? 'Schedule saved, but the list could not be refreshed.' : 'Schedule updated.';
      } catch (error) {
        preview.textContent = `Could not save the schedule. ${error.message}`;
        save.disabled = false; controls.forEach(control => { control.disabled = false; });
      }
    });
    return { editorRow, editButton };
  }

  function makeMainRow(item, group, feedback) {
    const row = node('tr', 'schedule-row');
    row.dataset.scheduleId = item.id;
    if (group === 'admin' && typeof item.id === 'string' && /^(?:[a-f0-9]{32}|catalog-health-full)$/.test(item.id)) {
      row.id = 'schedule-' + item.id; row.tabIndex = -1;
    }
    const taskCell = node('td', 'schedule-task-cell');
    taskCell.dataset.label = 'Task';
    const taskName = node('strong', 'schedule-task-name', item.name);
    const label = group === 'system' ? 'System task' : (taskNames[item.action] || item.action || 'Admin task');
    taskCell.append(taskName, node('small', 'schedule-task-kind', label));
    if (item.needs_conversion) taskCell.append(node('span', 'schedule-conversion', 'Conversion needed'));
    if (item.video_count) taskCell.append(node('small', 'schedule-task-kind', `${item.video_count} video IDs`));
    if (item.last_job_id && /^[A-Za-z0-9_-]{1,128}$/.test(item.last_job_id)) {
      const link = node('a', 'schedule-task-link', 'Open latest task');
      link.href = `/admin/v2/?job_id=${encodeURIComponent(item.last_job_id)}#jobs`;
      link.setAttribute('aria-label', `Open task for ${item.name}`);
      taskCell.append(link);
    }
    const cronCell = node('td', 'schedule-cron-cell');
    cronCell.dataset.label = 'Cron';
    const scheduleZone = item.timezone || 'Europe/Warsaw';
    if (item.cron_expression) cronCell.append(node('code', '', item.cron_expression), node('small', '', scheduleZone));
    else cronCell.append(node('span', 'schedule-unknown', 'Requires conversion'));
    const statusCell = node('td');
    statusCell.dataset.label = 'Status';
    statusCell.append(node('span', `schedule-status ${statusClass(item)}`, statusName(item)));
    if (item.details) statusCell.append(node('small', 'schedule-task-kind', item.details));
    const nextCell = node('td', 'schedule-time-cell'); nextCell.append(timeNode(item.next_run_at, scheduleZone));
    nextCell.dataset.label = 'Next run';
    const lastCell = node('td', 'schedule-time-cell');
    lastCell.dataset.label = 'Last run';
    lastCell.append(timeNode(item.last_run_at, scheduleZone));
    if (item.last_result && item.last_result !== 'processing') lastCell.append(node('small', 'schedule-task-kind', resultNames[item.last_result] || item.last_result));
    const actionCell = node('td', 'schedule-actions-cell');
    actionCell.dataset.label = 'Actions';
    const editButton = node('button', 'button button-quiet schedule-edit-toggle', 'Edit');
    editButton.type = 'button'; editButton.setAttribute('aria-expanded', 'false');
    const toggle = node('button', 'button button-quiet schedule-enable-toggle', item.enabled === false ? 'Enable' : 'Disable');
    toggle.type = 'button';
    toggle.addEventListener('click', async () => {
      toggle.disabled = true;
      try {
        if (group === 'system') {
          await window.AdminV2.postJson(`${endpoint}/system/${encodeURIComponent(item.id)}`,
            { cron_expression: item.cron_expression || '', enabled: item.enabled === false, revision: item.revision });
        } else {
          await window.AdminV2.postJson(`${endpoint}/${encodeURIComponent(item.id)}/enabled`,
            { enabled: item.enabled === false, revision: item.revision });
        }
        await load();
      } catch (error) { feedback.textContent = `Could not update the schedule. ${error.message}`; toggle.disabled = false; }
    });
    const run = node('button', 'button button-quiet schedule-run', 'Run');
    run.type = 'button';
    run.addEventListener('click', async () => {
      run.disabled = true;
      try {
        const url = group === 'system' ? `${endpoint}/system/${encodeURIComponent(item.id)}/run` : `${endpoint}/${encodeURIComponent(item.id)}/run`;
        const result = await window.AdminV2.postJson(url, {});
        await load();
        feedback.textContent = 'Run queued.';
        if (result.job_id) {
          const link = node('a', '', ' Open task');
          link.href = `/admin/v2/?job_id=${encodeURIComponent(result.job_id)}#jobs`;
          feedback.append(link);
        }
      } catch (error) { feedback.textContent = `Could not run the schedule. ${error.message}`; run.disabled = false; }
    });
    actionCell.append(editButton, toggle, run);
    row.append(taskCell, cronCell, statusCell, nextCell, lastCell, actionCell);
    const editorRow = node('tr', 'schedule-editor-row'); editorRow.hidden = true;
    const editor = makeScheduleEditor({ ...item, group }, feedback, editorRow, editButton);
    return { row, editorRow, editor };
  }

  function makeTable(items, feedback, dependentItems, parents) {
    const wrap = node('div', 'schedule-table-wrap');
    const table = node('table', 'schedule-table');
    const head = node('thead');
    const header = node('tr');
    ['Task', 'Cron', 'Status', 'Next run', 'Last run', 'Actions'].forEach(label => header.append(node('th', '', label)));
    head.append(header);
    const body = node('tbody');
    for (const item of items) {
      const row = makeMainRow(item, item.group, feedback);
      body.append(row.row, row.editorRow);
      const children = dependentItems.filter(child => child.parent_id === item.id);
      if (children.length) {
        const childRow = node('tr', 'schedule-dependent-row');
        const cell = node('td'); cell.colSpan = 6;
        cell.append(makeDependentList(children, parents)); childRow.append(cell); body.append(childRow);
      }
    }
    table.append(head, body); wrap.append(table);
    return wrap;
  }

  function makeDependentList(items, parents) {
    const section = node('section', 'schedule-dependent');
    section.setAttribute('aria-label', 'Dependent tasks');
    const grid = node('div', 'schedule-dependent-list');
    for (const item of items) {
      const parent = parents.get(item.parent_id) || {};
      const card = node('article', 'schedule-dependent-card');
      const parentNeedsAttention = Boolean(parent.problem) || parent.status === 'stale' || parent.last_result === 'failed';
      const statusText = parent.enabled === false ? 'Parent disabled'
        : parent.status === 'running' ? 'Parent running'
        : parentNeedsAttention ? 'Parent needs attention'
        : item.status === 'inherited' ? 'Inherited cadence' : (item.status || 'Unknown');
      const statusClassName = parentNeedsAttention ? 'is-failed'
        : parent.status === 'running' ? 'is-running'
        : parent.status === 'fresh' || parent.last_result === 'done' ? 'is-ok' : 'is-unknown';
      const cadenceText = parent.enabled === false ? 'Parent schedule is disabled'
        : parent.cron_expression
        ? `Inherits ${parent.cron_expression} ${parent.timezone || 'Europe/Warsaw'}`
        : parent.needs_conversion ? 'Inherits parent cadence · conversion needed' : 'Inherits parent cadence';
      const status = node('span', `schedule-status ${statusClassName}`, statusText);
      const next = node('div', 'schedule-dependent-next');
      next.append(node('span', '', 'Next run'), timeNode(parent.next_run_at, parent.timezone || 'Europe/Warsaw'));
      card.append(node('strong', '', item.name), node('small', '', `After ${item.source || 'parent task'}`),
        status, node('p', '', cadenceText), next, node('small', 'schedule-dependent-detail', item.details || ''));
      grid.append(card);
    }
    section.append(grid);
    return section;
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
      const admin = Array.isArray(result.items) ? result.items.map(item => ({ ...item, group: 'admin' })) : [];
      const systemAll = Array.isArray(result.system_items) ? result.system_items : [];
      const system = systemAll.filter(item => item.group === 'system');
      const dependent = systemAll.filter(item => item.group === 'dependent');
      const parents = new Map([...admin, ...system].map(item => [item.id, item]));
      const problems = item => item.problem || ['failed', 'skipped_overlap'].includes(item.last_result)
        || (item.group === 'dependent' && Boolean(parents.get(item.parent_id)?.problem));
      let mainItems = [...admin, ...system];
      let dependentItems = dependent;
      if (filter === 'admin') { mainItems = admin; dependentItems = []; }
      else if (filter === 'system') { mainItems = system; }
      else if (filter === 'dependent') { mainItems = system.filter(item => dependent.some(child => child.parent_id === item.id)); }
      else if (filter === 'problems') {
        mainItems = mainItems.filter(problems);
        dependentItems = dependentItems.filter(problems);
        mainItems = [...mainItems, ...system.filter(item => dependentItems.some(child => child.parent_id === item.id) && !mainItems.includes(item))];
      }
      let visibleCount = mainItems.length + dependentItems.length;
      if (mainItems.length) host.append(makeTable(mainItems, feedback, dependentItems, parents));
      if (!visibleCount) host.append(node('p', 'schedule-empty', filter === 'problems'
        ? 'No schedule problems are currently reported.' : 'No schedules match this filter.'));
      const selectedId = new URLSearchParams(location.hash.split('?')[1] || '').get('schedule_id') || '';
      let selectedMessage = '';
      if (selectedId && /^(?:[a-f0-9]{32}|catalog-health-full)$/.test(selectedId)) {
        const selected = document.getElementById?.('schedule-' + selectedId);
        if (selected) {
          if (focusedScheduleId !== selectedId) {
            selected.scrollIntoView({ block: 'nearest' }); selected.focus({ preventScroll: true }); focusedScheduleId = selectedId;
          }
        } else selectedMessage = ' The schedule from this audit record is no longer available.';
      } else focusedScheduleId = '';
      feedback.textContent = `${visibleCount} schedule entr${visibleCount === 1 ? 'y' : 'ies'} shown · ${admin.length} admin · ${system.length} independent · ${dependent.length} dependent.${selectedMessage}`;
      return true;
    } catch (error) {
      if (sequence !== loadSeq) return null;
      feedback.textContent = silent
        ? `Schedules may be out of date. Could not refresh them. ${error.message}`
        : `Could not load schedules. ${error.message}`;
      return false;
    }
  }

  async function previewCreateCron() {
    const field = $('#scheduleCron'); const button = $('#scheduleCreate'); const preview = $('#scheduleCreatePreview');
    if (!field || !button || !preview) return;
    const expression = field.value.trim();
    const sequence = ++createPreviewSeq;
    button.disabled = true;
    if (createPreviewTimer !== null) clearTimeout(createPreviewTimer);
    if (!expression) { preview.textContent = 'Enter a cron expression.'; return; }
    preview.textContent = 'Checking upcoming runs…';
    createPreviewTimer = setTimeout(async () => {
      try {
        const result = await window.AdminV2.getJson(`${endpoint}/cron-preview?expression=${encodeURIComponent(expression)}`);
        if (sequence !== createPreviewSeq) return;
        if (!Array.isArray(result.next_runs) || result.next_runs.length !== 3) throw new Error('Upcoming runs are unavailable.');
        preview.replaceChildren(node('span', '', 'Next runs: '));
        result.next_runs.forEach((run, index) => {
          if (index) preview.append(node('span', '', ' · '));
          preview.append(timeNode(run));
        });
        button.disabled = false;
      } catch (error) {
        if (sequence !== createPreviewSeq) return;
        preview.textContent = `Check the cron expression. ${error.message}`;
        button.disabled = true;
      }
    }, 250);
  }

  $('#scheduleFilter')?.addEventListener('change', () => load());
  $('#scheduleAction')?.addEventListener('change', () => {
    $('#scheduleVideosField').hidden = $('#scheduleAction').value !== 'availability_check';
  });
  $('#scheduleCron')?.addEventListener('input', previewCreateCron);
  $('#scheduleCreateForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = $('#scheduleCreateForm'); const button = $('#scheduleCreate');
    if (button.disabled) return;
    const restoreFocus = document.activeElement === button || event.submitter === button;
    const draft = [$('#scheduleName').value, $('#scheduleAction').value, $('#scheduleCron').value, $('#scheduleVideoIds').value];
    const body = {
      name: $('#scheduleName').value.trim(), action: $('#scheduleAction').value,
      schedule_type: 'cron', cron_expression: $('#scheduleCron').value.trim(), timezone: 'Europe/Warsaw'
    };
    if (body.action === 'availability_check') {
      body.video_ids = $('#scheduleVideoIds').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    }
    button.disabled = true;
    try {
      await window.AdminV2.postJson(endpoint, body);
      const newerDraftExists = [$('#scheduleName').value, $('#scheduleAction').value, $('#scheduleCron').value, $('#scheduleVideoIds').value]
        .some((value, index) => value !== draft[index]);
      if (!newerDraftExists) {
        form.reset(); $('#scheduleCron').value = '0 3 * * *';
        $('#scheduleVideosField').hidden = true; $('#scheduleCreatePanel').open = false;
        await previewCreateCron();
      }
      const refreshed = await load();
      $('#schedulesFeedback').textContent = refreshed === false
        ? 'Schedule created, but the list could not be refreshed.'
        : newerDraftExists ? 'Schedule created. Your newer draft was kept.' : 'Schedule created.';
    } catch (error) {
      $('#schedulesFeedback').textContent = `Could not create schedule. ${error.message}`;
      button.disabled = false;
    } finally {
      if (restoreFocus && button.isConnected && document.activeElement === document.body) button.focus({ preventScroll: true });
    }
  });
  $('#schedulesRefresh')?.addEventListener('click', load);

  function syncSchedules(route) {
    schedulesActive = route === 'schedules';
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
    if (schedulesActive) {
      if (!document.hidden) load();
      pollTimer = setInterval(() => {
        if (schedulesActive && !document.hidden) load({ silent: true });
      }, POLL_INTERVAL_MS);
    } else loadSeq += 1;
  }
  document.addEventListener('adminv2:navigate', event => syncSchedules(event.detail?.route));
  document.addEventListener('visibilitychange', () => {
    if (schedulesActive && !document.hidden) load({ silent: true });
  });
  syncSchedules(location.hash.replace(/^#\/?/, '').split(/[/?]/)[0]);
  $('#scheduleVideosField').hidden = $('#scheduleAction').value !== 'availability_check';
  previewCreateCron();
})();
