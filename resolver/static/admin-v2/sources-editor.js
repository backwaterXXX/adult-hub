(() => {
  'use strict';
  // Sources source editor, channel management, and source dialogs.
  const SRC = window.AdminV2Sources;
  const { $, SOURCE_CREATE_URL, SOURCE_PREVIEW_URL, SOURCE_REMOVE_URL, SOURCE_SAVE_URL, announce, create,
    helpers, itemKey, safeId, scrub, setNotice, state } = SRC;
  const loadSources = (...args) => SRC.loadSources(...args);
  const renderSourceDetail = (...args) => SRC.renderSourceDetail(...args);
  function hasOwnPath(object, path) {
    let current = object;
    for (const key of path) {
      if (!current || typeof current !== 'object' || !Object.prototype.hasOwnProperty.call(current, key)) return false;
      current = current[key];
    }
    return true;
  }
  function setPath(object, path, value) {
    let current = object;
    for (const key of path.slice(0, -1)) current = current[key] ||= {};
    current[path.at(-1)] = value;
  }
  function deletePath(object, path) {
    let current = object;
    const parents = [];
    for (const key of path.slice(0, -1)) {
      if (!current || typeof current !== 'object' || !Object.prototype.hasOwnProperty.call(current, key)) return;
      parents.push([current, key]);
      current = current[key];
    }
    if (!current || typeof current !== 'object') return;
    delete current[path.at(-1)];
    for (const [parent, key] of parents.reverse()) {
      if (Object.keys(parent[key] || {}).length) break;
      delete parent[key];
    }
  }
  function addEditorField(form, labelText, path, value, type = 'text', options = []) {
    const row = create('div', 'source-editor-field');
    const inputId = `sourceEditor_${form.querySelectorAll('.source-editor-input').length}`;
    let input;
    if (type === 'textarea') {
      input = document.createElement('textarea');
      input.rows = 3;
      input.value = Array.isArray(value) ? value.join('\n') : '';
    } else if (type === 'select') {
      input = document.createElement('select');
      options.forEach(([optionValue, optionLabel]) => {
        const option = create('option', '', optionLabel);
        option.value = optionValue;
        input.append(option);
      });
      input.value = safeId(value);
    } else {
      input = document.createElement('input');
      input.type = type;
      if (type === 'checkbox') input.checked = value === true;
      else input.value = value === null || value === undefined ? '' : String(value);
      if (type === 'number') { input.min = path.at(-1) === 'min_duration_seconds' ? '0' : '1'; input.step = '1'; }
    }
    input.className = 'source-editor-input';
    input.id = inputId;
    input.dataset.overlayPath = JSON.stringify(path);
    input.disabled = !hasOwnPath(state.overrides, path);
    const fieldLabel = create('label', 'source-editor-label', labelText);
    fieldLabel.htmlFor = inputId;
    row.append(fieldLabel, input);
    const inherit = document.createElement('input');
    inherit.type = 'checkbox';
    inherit.className = 'source-editor-reset';
    inherit.dataset.resetPath = JSON.stringify(path);
    inherit.checked = !hasOwnPath(state.overrides, path);
    const inheritLabel = create('label', 'source-editor-inherit');
    inheritLabel.append(inherit, create('span', '', 'Use base setting'));
    inherit.addEventListener('change', () => { input.disabled = inherit.checked; invalidateSourcePreview(); });
    row.append(inheritLabel);
    input.addEventListener('input', invalidateSourcePreview);
    input.addEventListener('change', invalidateSourcePreview);
    form.append(row);
  }
  function invalidateSourcePreview() {
    state.editorCandidate = null;
    const save = $('#sourceEditorSave');
    if (save) save.disabled = true;
    const preview = $('#sourceEditorPreview');
    if (preview) preview.replaceChildren(create('p', 'detail-muted', 'Review the updated settings before saving.'));
  }
  function sourceEditorCandidate(form) {
    const candidate = JSON.parse(JSON.stringify(state.overrides || {}));
    const resetFields = [...form.querySelectorAll('.source-editor-reset')];
    for (const input of form.querySelectorAll('.source-editor-input')) {
      const path = JSON.parse(input.dataset.overlayPath);
      const inherit = resetFields.find(field => field.dataset.resetPath === input.dataset.overlayPath);
      if (inherit && inherit.checked) { deletePath(candidate, path); continue; }
      let value;
      if (input.type === 'checkbox') value = input.checked;
      else if (input.type === 'number') value = Number(input.value);
      else if (input.tagName === 'TEXTAREA') value = input.value.split(/[\n,]/).map(tag => tag.trim()).filter(Boolean);
      else value = input.value.trim();
      setPath(candidate, path, value);
    }
    return candidate;
  }
  function renderSourceEditor(item) {
    const host = $('#sourceDetailContent');
    const sourcePath = [item.provider];
    const channelPath = [...sourcePath, 'channels', item.id];
    const channel = item.effective || {};
    const defaults = item.sourceDefaults || {};
    host.replaceChildren();
    host.append(create('p', 'detail-kicker', 'EDIT SOURCE SETTINGS'));
    host.append(create('h2', 'detail-title', item.studio));
    host.append(create('p', 'detail-note', 'Changes are stored as overrides. Clearing an override restores the read-only base value.'));
    const form = document.createElement('form');
    form.id = 'sourceEditorForm';
    form.className = 'source-editor-form';
    const sourceHeading = create('h3', 'detail-section-title', 'Source');
    form.append(sourceHeading);
    addEditorField(form, 'Source enabled', [...sourcePath, 'enabled'], item.sourceEnabled, 'checkbox');
    form.append(create('h3', 'detail-section-title', 'Source defaults'));
    addEditorField(form, 'Default stream mode', [...sourcePath, 'defaults', 'stream_mode'], defaults.streamMode, 'select', [['proxy', 'Proxy'], ['redirect', 'Redirect']]);
    addEditorField(form, 'Default max pages', [...sourcePath, 'defaults', 'max_pages'], defaults.maxPages, 'number');
    addEditorField(form, 'Default minimum duration (seconds)', [...sourcePath, 'defaults', 'min_duration_seconds'], defaults.minDurationSeconds, 'number');
    addEditorField(form, 'Default history batch size', [...sourcePath, 'defaults', 'history_batch_size'], defaults.historyBatchSize, 'number');
    addEditorField(form, 'Default tags whitelist (one per line)', [...sourcePath, 'defaults', 'tags_whitelist'], defaults.tagsWhitelist, 'textarea');
    addEditorField(form, 'Default tags blacklist (one per line)', [...sourcePath, 'defaults', 'tags_blacklist'], defaults.tagsBlacklist, 'textarea');
    form.append(create('h3', 'detail-section-title', 'Channel settings'));
    addEditorField(form, 'Channel enabled', [...channelPath, 'enabled'], item.channelEnabled, 'checkbox');
    addEditorField(form, 'Channel name (studio label)', [...channelPath, 'studio'], item.studio, 'text');
    addEditorField(form, 'Stream mode', [...channelPath, 'stream_mode'], channel.streamMode, 'select', [['proxy', 'Proxy'], ['redirect', 'Redirect']]);
    addEditorField(form, 'Max pages', [...channelPath, 'max_pages'], channel.maxPages, 'number');
    addEditorField(form, 'Minimum duration (seconds)', [...channelPath, 'min_duration_seconds'], channel.minDurationSeconds, 'number');
    addEditorField(form, 'History batch size', [...channelPath, 'history_batch_size'], channel.historyBatchSize, 'number');
    addEditorField(form, 'Tags whitelist (one per line)', [...channelPath, 'tags_whitelist'], channel.tagsWhitelist, 'textarea');
    addEditorField(form, 'Tags blacklist (one per line)', [...channelPath, 'tags_blacklist'], channel.tagsBlacklist, 'textarea');
    const preview = create('div', 'source-editor-preview');
    preview.id = 'sourceEditorPreview';
    preview.setAttribute('aria-live', 'polite');
    preview.append(create('p', 'detail-muted', 'Preview changes before saving.'));
    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Back to details');
    back.type = 'button';
    back.dataset.sourceEditorBack = 'true';
    const previewButton = create('button', 'button button-quiet', 'Preview changes');
    previewButton.type = 'button';
    previewButton.dataset.sourceEditorPreview = 'true';
    const save = create('button', 'button button-primary', 'Save settings');
    save.type = 'button';
    save.id = 'sourceEditorSave';
    save.disabled = true;
    actions.append(back, previewButton, save);
    form.append(preview, actions);
    form.addEventListener('submit', event => event.preventDefault());
    host.append(form);
    const panel = panelFor($('#sourceDetailDialog'));
    if (panel) panel.scrollTop = 0;
    previewButton.focus({ preventScroll: true });
  }
  function openAddChannel(opener) {
    if (!state.settingsAvailable || state.configRevision === null) return;
    state.detailItem = null;
    const host = $('#sourceDetailContent');
    host.replaceChildren(
      create('p', 'detail-kicker', 'TELEGRAM SOURCE'),
      create('h2', 'detail-title', 'Add a channel'),
      create('p', 'detail-note', 'Add the Telegram channel ID and a studio label. The ID is immutable after creation; verify it before saving.')
    );
    const form = document.createElement('form');
    form.id = 'sourceCreateForm';
    form.className = 'source-editor-form';
    const id = document.createElement('input'); id.name = 'id'; id.required = true; id.maxLength = 256; id.autocomplete = 'off';
    const studio = document.createElement('input'); studio.name = 'studio'; studio.required = true; studio.maxLength = 160; studio.autocomplete = 'off';
    for (const [labelText, input] of [['Telegram channel ID', id], ['Studio label', studio]]) {
      const label = document.createElement('label'); label.className = 'source-editor-field'; input.className = 'source-editor-input';
      label.append(create('span', '', labelText), input); form.append(label);
    }
    const preview = create('div', 'source-editor-preview'); preview.id = 'sourceCreatePreview'; preview.setAttribute('aria-live', 'polite');
    preview.append(create('p', 'detail-muted', 'Add the channel immediately, or preview changes before saving.'));
    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Cancel'); back.type = 'button'; back.dataset.sourceCreateCancel = 'true';
    const previewButton = create('button', 'button button-quiet', 'Preview'); previewButton.type = 'button'; previewButton.dataset.sourceCreatePreview = 'true';
    const save = create('button', 'button button-primary', 'Add channel'); save.type = 'button'; save.id = 'sourceCreateSave';
    actions.append(back, previewButton, save); form.append(preview, actions); form.addEventListener('submit', event => event.preventDefault()); host.append(form);
    form.addEventListener('input', () => { state.editorCandidate = null; preview.replaceChildren(create('p', 'detail-muted', 'Add the channel immediately, or preview changes before saving.')); });
    const dialog = $('#sourceDetailDialog'); window.AdminV2.openDialog(dialog, opener || document.activeElement);
    const panel = panelFor(dialog); if (panel) { panel.focus({ preventScroll: true }); panel.scrollTop = 0; }
    id.focus({ preventScroll: true });
  }
  async function previewNewChannel(button) {
    const form = $('#sourceCreateForm'); if (!form || !form.reportValidity()) return;
    const id = form.elements.id ? form.elements.id.value.trim() : '';
    const studio = form.elements.studio ? form.elements.studio.value.trim() : '';
    state.editorCandidate = null;
    const preview = $('#sourceCreatePreview');
    if (state.items.some(item => item.provider === 'telegram' && item.id === id)) {
      preview.replaceChildren(create('p', 'detail-error', 'This Telegram channel ID is already configured. Use its settings instead.'));
      return;
    }
    const candidate = JSON.parse(JSON.stringify(state.overrides || {}));
    candidate.telegram ||= {}; candidate.telegram.channels ||= {};
    const previous = candidate.telegram.channels[id] || {};
    candidate.telegram.channels[id] = { ...previous, enabled: true, studio };
    if (Array.isArray(candidate.telegram.removed_channels)) {
      candidate.telegram.removed_channels = candidate.telegram.removed_channels.filter(channelId => channelId !== id);
      if (!candidate.telegram.removed_channels.length) delete candidate.telegram.removed_channels;
    }
    button.disabled = true;
    preview.replaceChildren(create('p', 'detail-muted', 'Validating the complete configuration…'));
    try {
      const result = await window.AdminV2.postJson(SOURCE_PREVIEW_URL, { expected_revision: state.configRevision, overrides: candidate });
      preview.replaceChildren(create('p', 'detail-muted', result.changes.map(change => `${change.path.join(' / ')}: ${change.after ?? 'None'}`).join(' · ') || 'Configuration is valid.'));
      state.editorCandidate = { id, studio, revision: state.configRevision };
    } catch (error) {
      state.editorCandidate = null;
      preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not validate this channel.'));
    } finally { button.disabled = false; }
  }
  async function saveNewChannel(button) {
    const form = $('#sourceCreateForm'); if (!form || !form.reportValidity()) return;
    const id = form.elements.id ? form.elements.id.value.trim() : '';
    const studio = form.elements.studio ? form.elements.studio.value.trim() : '';
    if (!id || !studio) return;
    const preview = $('#sourceCreatePreview');
    if (state.items.some(item => item.provider === 'telegram' && item.id === id)) {
      if (preview) preview.replaceChildren(create('p', 'detail-error', 'This Telegram channel ID is already configured. Use its settings instead.'));
      return;
    }
    const revision = (state.editorCandidate && state.editorCandidate.id === id && state.editorCandidate.studio === studio)
      ? state.editorCandidate.revision
      : state.configRevision;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(SOURCE_CREATE_URL, { expected_revision: revision, channel: { id, studio } });
      state.editorCandidate = null; state.configRevision = result.revision; closeSourceDetail();
      setNotice('success', result.queued ? result.message : result.audit_pending
        ? 'Telegram channel added. Its audit record is queued until the database is available.'
        : 'Telegram channel added. The next sync will apply it.'); announce(result.queued ? result.message : 'Telegram channel added.'); await loadSources();
      if (typeof CustomEvent === 'function') document.dispatchEvent(new CustomEvent('adminv2:sources-changed'));
      if (state.settingsAvailable && !state.auditPending && state.historyAvailable
          && state.configRevision === result.revision && !result.queued) {
        setNotice('success', 'Telegram channel added. The next sync will apply it.');
      }
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) state.editorCandidate = null;
      button.disabled = staleRevision;
      if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not add this channel. Reload and try again.'));
    } finally {
      if (!button.disabled || !button.closest('dialog')?.open) {
        button.disabled = false;
      }
    }
  }
  function renderChannelNameEditor(item) {
    const host = $('#sourceDetailContent');
    if (!host) return;
    host.replaceChildren(
      create('p', 'detail-kicker', 'TELEGRAM CHANNEL'),
      create('h2', 'detail-title', 'Edit channel name'),
      create('p', 'detail-note', `Update the display name (studio label) for channel ${item.id}.`)
    );
    const form = document.createElement('form');
    form.id = 'sourceEditNameForm';
    form.className = 'source-editor-form';
    const nameInput = document.createElement('input');
    nameInput.name = 'studio';
    nameInput.required = true;
    nameInput.maxLength = 160;
    nameInput.autocomplete = 'off';
    nameInput.className = 'source-editor-input';
    nameInput.value = item.studio || '';
    const label = document.createElement('label');
    label.className = 'source-editor-field';
    label.append(create('span', '', 'Channel name'), nameInput);
    form.append(label);

    const preview = create('div', 'source-editor-preview');
    preview.id = 'sourceEditNamePreview';
    preview.setAttribute('aria-live', 'polite');

    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Cancel');
    back.type = 'button';
    back.dataset.sourceEditNameCancel = 'true';
    const save = create('button', 'button button-primary', 'Save channel name');
    save.type = 'button';
    save.id = 'sourceEditNameSave';
    actions.append(back, save);
    form.append(preview, actions);
    form.addEventListener('submit', event => event.preventDefault());
    host.append(form);
    const panel = panelFor($('#sourceDetailDialog'));
    if (panel) panel.scrollTop = 0;
    nameInput.focus({ preventScroll: true });
    nameInput.select();
  }
  async function saveChannelName(button) {
    const item = state.detailItem;
    if (!item || !state.settingsAvailable) return;
    const form = $('#sourceEditNameForm');
    if (!form || !form.reportValidity()) return;
    const newStudio = form.elements.studio ? form.elements.studio.value.trim() : '';
    if (!newStudio) return;
    if (newStudio === item.studio) {
      renderSourceDetail(item);
      return;
    }
    const preview = $('#sourceEditNamePreview');
    button.disabled = true;
    try {
      const candidate = JSON.parse(JSON.stringify(state.overrides || {}));
      candidate.telegram ||= {};
      candidate.telegram.channels ||= {};
      const previous = candidate.telegram.channels[item.id] || {};
      candidate.telegram.channels[item.id] = { ...previous, studio: newStudio };
      const removedList = Array.isArray(candidate.telegram.removed_channels) ? candidate.telegram.removed_channels : [];
      const removedSet = new Set(removedList.map(String));
      for (const ch of Object.keys(candidate.telegram.channels)) {
        const clean = ch.replace(/^-100/, '').replace(/^-/, '');
        if (removedSet.has(ch) || removedSet.has(clean)) {
          delete candidate.telegram.channels[ch];
        }
      }

      const result = await window.AdminV2.postJson(SOURCE_SAVE_URL, {
        expected_revision: state.configRevision,
        overrides: candidate
      });
      state.overrides = candidate;
      state.configRevision = result.revision;
      setNotice('success', result.queued ? result.message : result.audit_pending
        ? 'Channel name updated. Its audit record is queued until the database is available.'
        : 'Channel name updated. The next sync will apply it.');
      announce(result.queued ? result.message : 'Channel name updated.');
      await loadSources();
      if (typeof CustomEvent === 'function') document.dispatchEvent(new CustomEvent('adminv2:sources-changed'));
      const updatedItem = state.items.find(candidateItem => itemKey(candidateItem) === itemKey(item));
      if (updatedItem) {
        state.detailItem = updatedItem;
        renderSourceDetail(updatedItem);
      } else {
        closeSourceDetail(false);
      }
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) await loadSources();
      button.disabled = staleRevision;
      if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not update channel name. Reload and try again.'));
    } finally {
      if (!button.disabled || !button.closest('dialog')?.open) {
        button.disabled = false;
      }
    }
  }
  async function removeSourceChannel(button) {
    const item = state.detailItem;
    if (!item || item.provider !== 'telegram' || !state.settingsAvailable || !button) return;
    const prompt = `Permanently remove “${item.studio}” from Sources? This deletes its channel-only catalog videos, managed video files and channel-specific sync history. Shared catalog items remain if another active source still provides them. This cannot be undone.`;
    if (!window.confirm(prompt)) return;
    button.disabled = true;
    button.dataset.busy = 'true';
    try {
      const result = await window.AdminV2.postJson(SOURCE_REMOVE_URL, {
        expected_revision: state.configRevision,
        channel_id: item.id,
      });
      state.configRevision = result.revision;
      closeSourceDetail(false);
      await loadSources();
      if (typeof CustomEvent === 'function') document.dispatchEvent(new CustomEvent('adminv2:sources-changed'));
      helpers().showQueued(result, 'Channel deletion', $('#sourcesNotice'));
      announce('Channel removed from future syncs. Video and sync-history deletion is queued.');
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) await loadSources();
      button.disabled = staleRevision;
      delete button.dataset.busy;
      setNotice('error', scrub(error.message) || 'Could not remove this channel. Reload and try again.');
      announce('Could not remove this channel.');
    }
  }
  async function previewSourceEditor(button) {
    const form = $('#sourceEditorForm');
    if (!form || !form.reportValidity()) return;
    const candidate = sourceEditorCandidate(form);
    button.disabled = true;
    const preview = $('#sourceEditorPreview');
    preview.replaceChildren(create('p', 'detail-muted', 'Checking the complete source configuration…'));
    try {
      const result = await window.AdminV2.postJson(SOURCE_PREVIEW_URL, {
        expected_revision: state.configRevision,
        overrides: candidate
      });
      preview.replaceChildren();
      if (!result.changes.length) preview.append(create('p', 'detail-muted', result.has_override_changes
        ? 'Effective values are unchanged; this will change which values inherit the base configuration.'
        : 'No effective settings will change.'));
      else {
        const list = create('ul', 'source-editor-diff');
        result.changes.forEach(change => {
          const before = change.before === null || change.before === undefined ? 'None' : JSON.stringify(change.before);
          const after = change.after === null || change.after === undefined ? 'None' : JSON.stringify(change.after);
          list.append(create('li', '', `${change.path.join(' / ')}: ${before} → ${after}`));
        });
        preview.append(list);
      }
      state.editorCandidate = result.has_override_changes ? candidate : null;
      const save = $('#sourceEditorSave');
      if (save) save.disabled = !state.editorCandidate;
    } catch (error) {
      state.editorCandidate = null;
      preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not preview settings.'));
    } finally { button.disabled = false; }
  }
  async function saveSourceEditor(button) {
    if (!state.editorCandidate) return;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(SOURCE_SAVE_URL, {
        expected_revision: state.configRevision,
        overrides: state.editorCandidate
      });
      state.overrides = state.editorCandidate;
      state.configRevision = result.revision;
      state.editorCandidate = null;
      closeSourceDetail(false);
      setNotice('success', result.queued ? result.message : result.audit_pending
        ? 'Source settings saved. Their audit record is queued until the database is available.'
        : 'Source settings saved. The next sync will apply them.');
      announce(result.queued ? result.message : result.audit_pending ? 'Source settings saved; audit record queued.' : 'Source settings saved.');
      await loadSources();
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) state.editorCandidate = null;
      const save = $('#sourceEditorSave');
      if (save) save.disabled = staleRevision;
      const preview = $('#sourceEditorPreview');
      if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not save settings. Reload and try again.'));
    }
  }
  function openSourceDetail(item, opener) {
    const dialog = $('#sourceDetailDialog');
    if (!dialog || !item) return;
    state.detailItem = item;
    renderSourceDetail(item);
    window.AdminV2.openDialog(dialog, opener || document.activeElement);
    const panel = panelFor(dialog);
    if (panel) panel.focus({ preventScroll: true });
    announce(`Channel details for ${item.studio}.`);
  }
  function closeSourceDetail(restoreFocus = true) {
    const dialog = $('#sourceDetailDialog');
    if (!dialog) return;
    window.AdminV2.closeDialog(dialog, restoreFocus);
    state.detailItem = null;
  }
  function panelFor(dialog) {
    return dialog.querySelector('.source-detail-panel');
  }

  Object.assign(SRC, { closeSourceDetail, openAddChannel, openSourceDetail, previewNewChannel,
    previewSourceEditor, removeSourceChannel, renderChannelNameEditor, renderSourceEditor, saveChannelName,
    saveNewChannel, saveSourceEditor });
})();
