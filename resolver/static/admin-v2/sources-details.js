(() => {
  'use strict';
  // Sources detail rendering: effective settings, provider defaults, and sync history.
  const SRC = window.AdminV2Sources;
  const { $, create, displayTime, helpers, historyFor, itemKey, lastSuccessfulSync, safeId, scrub, state,
    statusFor } = SRC;
  const loadHistoryPage = (...args) => SRC.loadHistoryPage(...args);
  const renderAccessOutcome = (...args) => SRC.renderAccessOutcome(...args);
  const detailField = (label, value) => helpers().detailField(label, value);
  function isCount(value) {
    return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  }
  function validNumber(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }
  function numberText(value) {
    const numeric = validNumber(value);
    return numeric === null ? 'Unavailable' : numeric.toLocaleString('en-US');
  }
  function durationText(value) {
    const numeric = validNumber(value);
    if (numeric === null || numeric < 0) return 'Unavailable';
    const total = Math.round(numeric);
    if (total === 0) return '0 s';
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    const parts = [];
    if (hours) parts.push(`${hours} h`);
    if (minutes) parts.push(`${minutes} min`);
    if (seconds) parts.push(`${seconds} s`);
    return parts.join(' ');
  }
  function streamModeText(value) {
    const mode = safeId(value).toLowerCase();
    if (mode === 'proxy') return 'Proxy';
    if (mode === 'redirect') return 'Redirect';
    return 'Unknown';
  }
  const SETTING_KEYS = ['stream_mode', 'max_pages', 'min_duration_seconds', 'history_batch_size'];
  function hasTagArrays(source) {
    return Array.isArray(source.tags_whitelist) && Array.isArray(source.tags_blacklist);
  }
  function readEffectiveSettings(channel, provider) {
    if (provider !== 'telegram' || !channel || typeof channel !== 'object') return null;
    if (!SETTING_KEYS.every(key => Object.prototype.hasOwnProperty.call(channel, key))) return null;
    if (!hasTagArrays(channel)) return null;
    return {
      streamMode: channel.stream_mode,
      maxPages: channel.max_pages,
      minDurationSeconds: channel.min_duration_seconds,
      historyBatchSize: channel.history_batch_size,
      tagsWhitelist: channel.tags_whitelist,
      tagsBlacklist: channel.tags_blacklist
    };
  }
  function readProviderDefaults(group, provider) {
    const defaults = group && typeof group.defaults === 'object' ? group.defaults : null;
    if (provider !== 'telegram' || !defaults) return null;
    if (!SETTING_KEYS.every(key => Object.prototype.hasOwnProperty.call(defaults, key))) return null;
    if (!hasTagArrays(defaults)) return null;
    return {
      streamMode: defaults.stream_mode,
      maxPages: defaults.max_pages,
      minDurationSeconds: defaults.min_duration_seconds,
      historyBatchSize: defaults.history_batch_size,
      tagsWhitelist: defaults.tags_whitelist,
      tagsBlacklist: defaults.tags_blacklist
    };
  }
  function tagsNode(value) {
    if (!Array.isArray(value)) return create('span', 'detail-muted', 'Unavailable');
    const cleaned = value
      .filter(tag => typeof tag === 'string' && tag.trim() !== '')
      .map(tag => scrub(tag))
      .filter(tag => tag !== '');
    if (!cleaned.length) return create('span', 'detail-muted', 'None');
    const limit = 20;
    const wrap = create('div', 'detail-tags');
    cleaned.slice(0, limit).forEach(tag => wrap.append(create('span', 'tag-chip', tag)));
    const extra = cleaned.length - limit;
    if (extra > 0) wrap.append(create('span', 'tag-chip tag-chip-more', `+${extra} more`));
    return wrap;
  }
  function detailTagField(label, value) {
    const wrapper = create('div', 'detail-field detail-field-wide');
    wrapper.append(create('dt', '', label));
    const body = create('dd');
    body.append(tagsNode(value));
    wrapper.append(body);
    return wrapper;
  }
  function appendSettingFields(fields, settings) {
    fields.append(detailField('Stream mode', streamModeText(settings.streamMode)));
    fields.append(detailField('Max pages', numberText(settings.maxPages)));
    fields.append(detailField('Minimum duration', durationText(settings.minDurationSeconds)));
    fields.append(detailField('History batch size', numberText(settings.historyBatchSize)));
    fields.append(detailTagField('Tags whitelist', settings.tagsWhitelist));
    fields.append(detailTagField('Tags blacklist', settings.tagsBlacklist));
    return fields;
  }
  function effectiveSettingsSection(item) {
    const section = create('section', 'detail-section');
    section.append(create('h3', '', item.isProviderConfig ? 'Provider configuration' : 'Effective sync settings'));
    if (item.isProviderConfig) {
      const fields = create('dl', 'detail-fields');
      fields.append(detailField('Address configured', item.providerConfig.address_configured === true ? 'Yes' : 'No'));
      fields.append(detailField('Stream mode', streamModeText(item.providerConfig.stream_mode)));
      fields.append(detailField('Max pages', numberText(item.providerConfig.max_pages)));
      fields.append(detailTagField('Sections', item.providerConfig.sections));
      section.append(fields);
      return section;
    }
    if (!item.effective) {
      section.append(create('p', 'detail-description', 'Effective sync settings are not available for this provider.'));
      return section;
    }
    section.append(create('p', 'detail-note', 'Values applied to this channel after source defaults are inherited.'));
    section.append(appendSettingFields(create('dl', 'detail-fields'), item.effective));
    return section;
  }
  function providerDefaultsSection(item) {
    if (!item.sourceDefaults) return null;
    const section = create('section', 'detail-section');
    section.append(create('h3', '', 'Provider defaults'));
    section.append(create('p', 'detail-note', 'Source defaults used when a channel does not override a setting.'));
    section.append(appendSettingFields(create('dl', 'detail-fields'), item.sourceDefaults));
    return section;
  }
  function historyStatus(success) {
    if (success === true) return ['Succeeded', 'pill-good'];
    if (success === false) return ['Failed', 'pill-danger'];
    return ['Unknown', 'pill-warning'];
  }
  function countText(entry) {
    const parts = [];
    if (isCount(entry.videos)) parts.push(`${Number(entry.videos).toLocaleString('en-US')} videos`);
    if (isCount(entry.added)) parts.push(`${Number(entry.added).toLocaleString('en-US')} added`);
    if (isCount(entry.removed)) parts.push(`${Number(entry.removed).toLocaleString('en-US')} removed`);
    return parts.join(' · ');
  }
  function historyItem(entry) {
    const item = create('li', 'source-history-item');
    const head = create('div', 'source-history-head');
    head.append(create('span', 'source-history-time', displayTime(entry.when)));
    const [label, cls] = historyStatus(entry.success);
    head.append(create('span', `status-pill ${cls}`, label));
    item.append(head);
    item.append(create('span', 'source-history-counts', countText(entry) || 'No counts recorded'));
    if (entry.success === false) {
      const error = scrub(entry.error);
      const detail = scrub(entry.detail);
      const explanation = [error, detail].filter(Boolean).join(' — ');
      if (explanation) item.append(create('span', 'source-history-failure', `Failure: ${explanation}`));
    }
    return item;
  }
  function renderSyncRuns(payload) {
    const feedback = $('#sourcesHistoryFeedback');
    const host = $('#sourcesHistoryRows');
    const pagination = $('#sourcesHistoryPagination');
    if (pagination) { pagination.replaceChildren(); pagination.hidden = true; }
    host.replaceChildren();
    if (!payload || payload.available === false || payload.error) {
      feedback.textContent = 'Sync history is unavailable. Recent run details are unknown.';
      return;
    }
    const runs = Array.isArray(payload.runs) ? payload.runs : [];
    if (!runs.length) {
      feedback.textContent = 'No sync runs have been recorded yet.';
      return;
    }
    const total = Number.isInteger(payload.total) && payload.total >= runs.length ? payload.total : runs.length;
    const offset = Number(payload.offset) || 0;
    feedback.textContent = `Showing ${offset + 1}–${offset + runs.length} of ${total} sync runs.`;
    if (pagination && total > 10) {
      pagination.hidden = false;
      const prev = create('button', 'button button-quiet', '← Previous'); prev.type = 'button'; prev.disabled = offset === 0;
      const next = create('button', 'button button-quiet', 'Next →'); next.type = 'button'; next.disabled = offset + runs.length >= total;
      prev.addEventListener('click', () => { state.historyPage--; loadHistoryPage(); });
      next.addEventListener('click', () => { state.historyPage++; loadHistoryPage(); });
      pagination.append(prev, create('span', '', `Page ${state.historyPage + 1} of ${Math.ceil(total / 10)}`), next);
    }
    for (const run of runs) {
      const row = create('article', 'operation-history-row source-sync-run');
      const copy = create('div', 'source-sync-run-copy');
      const started = run.started_at;
      const outcome = run.interrupted === true ? 'Interrupted' : run.success === true ? 'Succeeded' : run.success === false ? 'Partial / failed' : 'Unknown';
      const pill = run.success === true ? 'pill-good' : run.success === false ? 'pill-danger' : 'pill-warning';
      copy.append(create('strong', '', `${outcome} · ${displayTime(started)}`));
      const sourceFilter = scrub(run.source_filter);
      const mode = ['scheduled', 'new', 'backfill'].includes(run.sync_mode) ? run.sync_mode : null;
      const channels = Array.isArray(run.sources) ? run.sources : [];
      const added = channels.reduce((sum, source) => sum + (isCount(source.added) ? Number(source.added) : 0), 0);
      const removed = channels.reduce((sum, source) => sum + (isCount(source.removed) ? Number(source.removed) : 0), 0);
      const failed = channels.filter(source => source.success === false && !source.history_deferred);
      const deferred = channels.filter(source => source.history_deferred === true);
      const facts = [
        `${channels.length} source${channels.length === 1 ? '' : 's'}`,
        `${added.toLocaleString('en-US')} added · ${removed.toLocaleString('en-US')} removed`,
        isCount(run.duration_seconds) ? `${Number(run.duration_seconds).toFixed(1)} s` : null,
      ].filter(Boolean);
      copy.append(create('p', 'source-sync-run-facts', facts.join(' · ')));
      const technical = create('details', 'source-run-details');
      const technicalCopy = create('div');
      technical.append(create('summary', '', 'Channels and HTTP retries'), technicalCopy);
      technicalCopy.append(create('p', 'source-sync-run-details', [mode ? `${mode} mode` : '', sourceFilter ? `Filter: ${sourceFilter}` : '', Number.isInteger(run.managed_config_revision) ? `Config revision ${run.managed_config_revision}` : ''].filter(Boolean).join(' · ')));
      for (const channel of channels) technicalCopy.append(historyItem({ ...channel, when: run.started_at }));
      if (failed.length) {
        const failureText = failed.map(source => {
          const label = scrub(source.label) || scrub(source.source) || 'Source';
          const error = scrub(source.error);
          const detail = scrub(source.detail);
          return `${label}${error ? ` · ${error}` : ''}${detail ? ` — ${detail}` : ''}`;
        }).join('; ');
        technicalCopy.append(create('p', 'source-sync-run-failures', `Failed sources: ${failureText}`));
      }
      if (deferred.length) {
        const labels = deferred.map(source => scrub(source.label) || scrub(source.source) || 'Source').join(', ');
        technicalCopy.append(create('p', 'source-sync-run-details', `History deferred; will resume: ${labels}. Cleanup skipped for these sources.`));
      }
      const stages = run.stages && typeof run.stages === 'object'
        ? Object.entries(run.stages).filter(([, value]) => isCount(value && value.calls) || isCount(value && value.total_seconds))
          .sort((left, right) => Number(right[1].total_seconds || 0) - Number(left[1].total_seconds || 0)).slice(0, 3)
        : [];
      if (stages.length) {
        technicalCopy.append(create('p', 'source-sync-run-details', `Slowest stages: ${stages.map(([name, value]) =>
          `${name.replaceAll('_', ' ')} ${Number(value.total_seconds || 0).toFixed(1)} s (${Number(value.calls || 0)} calls)`
        ).join(' · ')}`));
      }
      const http = run.http && typeof run.http === 'object' ? run.http : {};
      const httpFacts = [
        isCount(http.failed_attempts) ? `${Number(http.failed_attempts)} failed attempts` : null,
        isCount(http.retries) ? `${Number(http.retries)} retries` : null,
        isCount(http.rate_limited) ? `${Number(http.rate_limited)} rate limits` : null,
      ].filter(Boolean);
      if (httpFacts.length) technicalCopy.append(create('p', 'source-sync-run-details', `HTTP: ${httpFacts.join(' · ')}`));
      const jellyfinPoll = run.jellyfin_refresh_poll && typeof run.jellyfin_refresh_poll === 'object'
        ? run.jellyfin_refresh_poll : {};
      const pollFacts = [
        isCount(jellyfinPoll.calls) ? `${Number(jellyfinPoll.calls)} polls` : null,
        isCount(jellyfinPoll.timeouts) ? `${Number(jellyfinPoll.timeouts)} timeouts` : null,
        isCount(jellyfinPoll.total_seconds) ? `${Number(jellyfinPoll.total_seconds).toFixed(1)} s total` : null,
        isCount(jellyfinPoll.max_seconds) ? `${Number(jellyfinPoll.max_seconds).toFixed(1)} s max` : null,
      ].filter(Boolean);
      technicalCopy.append(create('p', 'source-sync-run-details', pollFacts.length
        ? `Jellyfin refresh polling: ${pollFacts.join(' · ')}`
        : 'Jellyfin refresh polling: not recorded for this run'));
      copy.append(technical);
      row.append(copy, create('span', `status-pill ${pill}`, outcome));
      host.append(row);
    }
  }
  function renderSourceDetail(item) {
    const host = $('#sourceDetailContent');
    if (!host) return;
    host.replaceChildren();
    host.dataset.sourceId = item.id;
    host.dataset.sourceProvider = item.provider;
    const [statusLabel, statusClass] = statusFor(item);
    host.append(create('p', 'detail-kicker', 'CHANNEL DETAILS'));
    const title = create('h2', 'detail-title', item.studio);
    title.id = 'sourceDetailTitle';
    host.append(title);
    host.append(create('span', `status-pill detail-state ${statusClass}`, statusLabel));
    const contextualActions = create('div', 'source-actions source-detail-actions');
    if (item.enabled && !item.isProviderConfig) {
      const syncNew = create('button', 'button button-primary source-sync-button', 'Sync new');
      syncNew.type = 'button'; syncNew.id = 'sourceDetailSyncNew';
      syncNew.dataset.syncSource = item.id; syncNew.dataset.syncMode = 'new';
      const backfill = create('button', 'button button-quiet source-sync-button', 'Backfill');
      backfill.type = 'button'; backfill.id = 'sourceDetailBackfill';
      backfill.dataset.syncSource = item.id; backfill.dataset.syncMode = 'backfill';
      contextualActions.append(syncNew, backfill);
    }
    if (item.provider === 'telegram' && !item.isProviderConfig) {
      const check = create('button', 'button button-quiet source-access-button', 'Check access');
      check.type = 'button'; check.id = 'sourceDetailCheckAccess'; check.dataset.checkSource = item.id;
      const outcome = create('span', 'source-access-outcome'); outcome.dataset.accessOutcome = item.id;
      const previousCheck = state.accessChecks.get(itemKey(item));
      if (previousCheck) renderAccessOutcome(outcome, previousCheck); else outcome.hidden = true;
      contextualActions.append(check, outcome);
    }
    if (contextualActions.childElementCount) host.append(contextualActions);
    if (item.provider === 'telegram' && state.settingsAvailable) {
      const editName = create('button', 'button button-quiet source-name-edit-open', 'Edit channel name');
      editName.type = 'button';
      editName.dataset.sourceEditName = itemKey(item);
      editName.disabled = state.auditPending;
      if (state.auditPending) editName.title = 'A pending source audit must finish before source changes.';
      host.append(editName);

      const edit = create('button', 'button button-primary source-editor-open', 'Edit settings');
      edit.type = 'button';
      edit.dataset.sourceEdit = itemKey(item);
      host.append(edit);
      const remove = create('button', 'button button-quiet source-remove-button', 'Remove channel');
      remove.type = 'button';
      remove.dataset.sourceRemove = item.id;
      remove.disabled = state.auditPending;
      if (state.auditPending) remove.title = 'A pending source audit must finish before source changes.';
      host.append(remove);
    }

    const channelSection = create('section', 'detail-section');
    channelSection.append(create('h3', '', item.isProviderConfig ? 'Provider' : 'Channel'));
    const fields = create('dl', 'detail-fields');
    fields.append(detailField(item.isProviderConfig ? 'Provider key' : 'Channel ID', scrub(item.id) || '—'));
    if (!item.isProviderConfig) fields.append(detailField('Channel name', scrub(item.studio) || '—'));
    fields.append(detailField('Provider', item.providerLabel));
    if (!item.isProviderConfig) fields.append(detailField('Channel enabled', item.channelEnabled ? 'Yes' : 'No'));
    fields.append(detailField(item.isProviderConfig ? 'Provider enabled' : 'Source enabled', item.sourceEnabled ? 'Yes' : 'No'));
    fields.append(detailField('Overall status', statusLabel));
    if (!item.isProviderConfig) fields.append(detailField('Last successful sync', lastSuccessfulSync(item)));
    channelSection.append(fields);
    host.append(channelSection);

    host.append(effectiveSettingsSection(item));
    const defaultsSection = providerDefaultsSection(item);
    if (defaultsSection) host.append(defaultsSection);

    const historySection = create('section', 'detail-section');
    historySection.append(create('h3', '', 'Recent sync history'));
    if (!state.historyAvailable) {
      historySection.append(create('p', 'detail-description', 'Sync history is unavailable, so recent results are unknown.'));
    } else {
      const rows = historyFor(item).slice(0, 5);
      if (!rows.length) {
        historySection.append(create('p', 'detail-description', 'No recent syncs were found for this channel in the cached history.'));
      } else {
        historySection.append(create('p', 'detail-description', 'Showing the latest entries found in the 50 most recent available sync runs.'));
        const list = create('ul', 'source-history-list');
        rows.forEach(entry => list.append(historyItem(entry)));
        historySection.append(list);
      }
    }
    host.append(historySection);
  }
  Object.assign(SRC, { readEffectiveSettings, readProviderDefaults, renderSourceDetail, renderSyncRuns });
})();
