(() => {
  'use strict';
  // Sources Telegram accounts and the Telegram sign-in wizard.
  const SRC = window.AdminV2Sources;
  const { $, TELEGRAM_ACCOUNTS_URL, TELEGRAM_AUTH_CANCEL_URL, TELEGRAM_AUTH_CODE_URL,
    TELEGRAM_AUTH_PASSWORD_URL, TELEGRAM_AUTH_RESEND_URL, TELEGRAM_AUTH_START_URL,
    TELEGRAM_AUTH_STATUS_URL, V2_API, create, isValidTelegramAccountId, scrub, setNotice, state } = SRC;
  const loadSources = (...args) => SRC.loadSources(...args);
  let wizardWriteToken = '';

  function extractErrorMessage(err, fallback = '') {
    const visited = new Set();
    function resolve(value) {
      if (typeof value === 'string') {
        const trimmed = value.trim();
        return (trimmed && trimmed !== '[object Object]') ? trimmed : '';
      }
      if (!value || typeof value !== 'object' || visited.has(value)) {
        return '';
      }
      visited.add(value);
      if (Array.isArray(value)) {
        for (const item of value) {
          const nested = resolve(item);
          if (nested) return nested;
        }
        return '';
      }
      if (value.error !== undefined && value.error !== null) {
        if (typeof value.error === 'string') {
          const trimmed = value.error.trim();
          if (trimmed && trimmed !== '[object Object]') return trimmed;
        } else if (typeof value.error === 'object') {
          const nested = resolve(value.error);
          if (nested) return nested;
        }
      }
      if (typeof value.message === 'string') {
        const trimmed = value.message.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.detail === 'string') {
        const trimmed = value.detail.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      } else if (typeof value.detail === 'object' && value.detail !== null) {
        const nested = resolve(value.detail);
        if (nested) return nested;
      }
      if (typeof value.msg === 'string') {
        const trimmed = value.msg.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.description === 'string') {
        const trimmed = value.description.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.reason === 'string') {
        const trimmed = value.reason.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.title === 'string') {
        const trimmed = value.title.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.data === 'object' && value.data !== null) {
        const nested = resolve(value.data);
        if (nested) return nested;
      }
      if (typeof value.cause === 'object' && value.cause !== null) {
        const nested = resolve(value.cause);
        if (nested) return nested;
      }
      return '';
    }
    const extracted = resolve(err);
    const resolvedFallback = (typeof fallback === 'string' && fallback.trim() !== '[object Object]')
      ? fallback.trim()
      : (resolve(fallback) || '');
    const resultMsg = extracted || resolvedFallback;
    return (resultMsg && resultMsg !== '[object Object]') ? resultMsg : '';
  }

  async function requestTelegramAuth(url, payload, method = 'POST') {
    const capabilities = await window.AdminV2.getJson(`${V2_API}/capabilities`);
    if (capabilities.read_only_reason === 'invalid_admin_token') {
      throw new Error('Admin write token configuration is invalid.');
    }
    if (capabilities.read_only_reason === 'admin_disabled') {
      throw new Error('Admin actions are disabled.');
    }
    const headers = new Headers({ 'Accept': 'application/json', 'Content-Type': 'application/json' });
    if (capabilities.token_required === true) {
      if (!wizardWriteToken) {
        wizardWriteToken = (prompt('Enter the admin write token:') || '').trim();
      }
      if (!wizardWriteToken) throw new Error('An admin write token is required.');
      headers.set('Authorization', `Bearer ${wizardWriteToken}`);
    }
    const bodyPayload = { ...(payload || {}) };
    if (!bodyPayload.account_id && state.activeAuthAccountId) {
      bodyPayload.account_id = state.activeAuthAccountId;
    }
    const response = await fetch(url, {
      method,
      headers,
      body: method === 'GET' ? undefined : JSON.stringify(bodyPayload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 403 && capabilities.token_required === true) {
        wizardWriteToken = '';
      }
      const floodWait = result.flood_wait_seconds || (result.error && typeof result.error === 'object' ? result.error.flood_wait_seconds : undefined);
      const fallback = floodWait
        ? `Please wait ${floodWait}s before requesting a code.`
        : `Request failed with status ${response.status}`;
      const err = new Error(extractErrorMessage(result, fallback));
      err.status = response.status;
      err.data = result;
      err.flood_wait_seconds = floodWait;
      err.code = result.code || (result.error && typeof result.error === 'object' ? result.error.code : undefined);
      throw err;
    }
    return result;
  }

  function postTelegramAuth(url, payload) {
    return requestTelegramAuth(url, payload, 'POST');
  }

  async function renderTelegramAccountCard() {
    const card = $('#telegramAccountCard');
    if (!card) return;
    const intro = $('#telegramAccountIntro');
    const container = $('#telegramAccountsContainer');

    let accountsPayload = null;
    try {
      accountsPayload = await window.AdminV2.getJson(TELEGRAM_ACCOUNTS_URL);
    } catch {
      // Offline fallback
    }
    const accounts = Array.isArray(accountsPayload?.accounts) ? accountsPayload.accounts : [];

    if (intro) {
      intro.textContent = accounts.length
        ? `Configured Telegram user accounts: ${accounts.length}. Multi-account routing balances stream and history requests automatically.`
        : 'Connect Telegram user accounts to enable channel synchronization and streaming.';
    }

    if (!container) return;

    if (!accounts.length) {
      const emptyNote = create('p', 'detail-note', 'No Telegram accounts configured yet. Click "Add Telegram account" to authenticate your first account.');
      container.replaceChildren(emptyNote);
      return;
    }

    const listWrap = create('div', 'telegram-accounts-wrap');
    for (const acc of accounts) {
      const accCard = create('div', 'telegram-account-card');
      const header = create('div', 'telegram-account-card-header');
      const titleWrap = create('div');
      const title = create('h3', '', acc.label || acc.account_id);
      if (acc.label && acc.label !== acc.account_id) {
        title.appendChild(create('span', 'detail-kicker', ` (${acc.account_id})`));
      }
      titleWrap.appendChild(title);

      const badge = create('span', 'telegram-badge');
      if (acc.status === 'cooling_down') {
        badge.className += ' telegram-badge-warning';
        badge.textContent = `Cooldown (${acc.cooldown_seconds}s)`;
      } else if (acc.status === 'authorized') {
        badge.className += ' telegram-badge-success';
        badge.textContent = 'Connected';
      } else if (acc.status === 'disabled') {
        badge.className += ' telegram-badge-muted';
        badge.textContent = 'Disabled';
      } else if (acc.status === 'unauthorized_session_present') {
        badge.className += ' telegram-badge-warning';
        badge.textContent = 'Session unverified';
      } else if (acc.status === 'in_progress') {
        badge.className += ' telegram-badge-warning';
        badge.textContent = 'In progress';
      } else {
        badge.className += ' telegram-badge-muted';
        badge.textContent = 'Not connected';
      }
      header.append(titleWrap, badge);

      const body = create('div', 'telegram-account-card-body');
      if (acc.phone_mask) {
        body.appendChild(create('span', 'telegram-stat', `Phone: ${acc.phone_mask}`));
      }
      const channelSummary = acc.channel_summary || {};
      const accessibleCount = channelSummary.accessible ?? (acc.channels || []).filter(c => c.access_status === 'accessible').length;
      const inaccessibleCount = channelSummary.inaccessible ?? (acc.channels || []).filter(c => c.access_status === 'no_access' || c.access_status === 'inaccessible').length;
      let channelText = `Verified Channels: ${accessibleCount}`;
      if (inaccessibleCount > 0) {
        channelText += ` (${inaccessibleCount} inaccessible)`;
      }
      body.appendChild(create('span', 'telegram-stat', channelText));

      const metrics = acc.metrics || {};
      const activeOps = metrics.active_ops || 0;
      body.appendChild(create('span', 'telegram-stat', `Active: ${activeOps} ${activeOps === 1 ? 'stream' : 'streams'}`));

      const bytesTransferred = metrics.bytes_transferred || 0;
      const formatBytes = window.AdminV2?.formatBytes || ((b) => `${(b / (1024 * 1024)).toFixed(1)} MB`);
      body.appendChild(create('span', 'telegram-stat', `Transferred: ${formatBytes(bytesTransferred)}`));

      if (typeof metrics.throughput_mb_s === 'number' && metrics.throughput_mb_s > 0) {
        body.appendChild(create('span', 'telegram-stat', `Throughput: ${metrics.throughput_mb_s} MB/s`));
      }
      if (typeof metrics.buffer_init_latency_ms === 'number' && metrics.buffer_init_latency_ms > 0) {
        body.appendChild(create('span', 'telegram-stat', `TTFB: ${Math.round(metrics.buffer_init_latency_ms)} ms`));
      }

      const errorsCount = metrics.errors_count || 0;
      const floodCount = metrics.flood_wait_count || 0;
      if (errorsCount > 0 || floodCount > 0) {
        const errText = floodCount > 0
          ? `Errors: ${errorsCount} (${floodCount} FloodWait)`
          : `Errors: ${errorsCount}`;
        const errSpan = create('span', 'telegram-stat telegram-stat-warning', errText);
        body.appendChild(errSpan);
      }

      const actions = create('div', 'telegram-account-card-actions');

      if (!acc.authorized) {
        const loginBtn = create('button', 'button button-primary', acc.has_session_file ? 'Verify session' : 'Log in');
        loginBtn.type = 'button';
        loginBtn.addEventListener('click', () => {
          if (!isValidTelegramAccountId(acc.account_id)) {
            setNotice('error', 'Invalid account ID.');
            return;
          }
          return openTelegramAuthWizard(loginBtn, acc.account_id);
        });
        actions.appendChild(loginBtn);
      } else {
        const testBtn = create('button', 'button button-quiet', 'Test access');
        testBtn.type = 'button';
        testBtn.addEventListener('click', async () => {
          if (!isValidTelegramAccountId(acc.account_id)) {
            setNotice('error', 'Invalid account ID.');
            return;
          }
          testBtn.disabled = true;
          testBtn.textContent = 'Testing…';
          try {
            const res = await postTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(acc.account_id)}/test`, {});
            if (res.ok && res.authorized) {
              setNotice('success', `Account ${acc.label || acc.account_id} verified. Accessible channels: ${Object.values(res.channels || {}).filter(s => s === 'accessible').length}`);
            } else {
              setNotice('warning', `Account check returned status: ${res.status}`);
            }
            await renderTelegramAccountCard();
          } catch (err) {
            setNotice('error', `Test failed: ${err.message}`);
          } finally {
            testBtn.disabled = false;
            testBtn.textContent = 'Test access';
          }
        });
        actions.appendChild(testBtn);
      }

      const renameBtn = create('button', 'button button-quiet', 'Rename');
      renameBtn.type = 'button';
      renameBtn.addEventListener('click', async () => {
        if (!isValidTelegramAccountId(acc.account_id)) {
          setNotice('error', 'Invalid account ID.');
          return;
        }
        const newLabel = prompt('Enter new label for this account:', acc.label || acc.account_id);
        if (newLabel !== null && newLabel.trim()) {
          try {
            await requestTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(acc.account_id)}`, { label: newLabel.trim() }, 'PATCH');
            setNotice('success', 'Account renamed.');
            await renderTelegramAccountCard();
          } catch (err) {
            setNotice('error', `Rename failed: ${err.message}`);
          }
        }
      });
      actions.appendChild(renameBtn);

      const toggleBtn = create('button', 'button button-quiet', acc.enabled ? 'Disable' : 'Enable');
      toggleBtn.type = 'button';
      toggleBtn.addEventListener('click', async () => {
        if (!isValidTelegramAccountId(acc.account_id)) {
          setNotice('error', 'Invalid account ID.');
          return;
        }
        try {
          await requestTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(acc.account_id)}`, { enabled: !acc.enabled }, 'PATCH');
          setNotice('success', `Account ${acc.enabled ? 'disabled' : 'enabled'}.`);
          await renderTelegramAccountCard();
        } catch (err) {
          setNotice('error', `Toggle failed: ${err.message}`);
        }
      });
      actions.appendChild(toggleBtn);

      const deleteBtn = create('button', 'button button-quiet', 'Remove');
      deleteBtn.type = 'button';
      deleteBtn.addEventListener('click', () => {
        if (!isValidTelegramAccountId(acc.account_id)) {
          setNotice('error', 'Invalid account ID.');
          return;
        }
        openTelegramAccountRemovalDialog(acc, deleteBtn);
      });
      actions.appendChild(deleteBtn);

      accCard.append(header, body, actions);
      listWrap.appendChild(accCard);
    }
    container.replaceChildren(listWrap);
  }

  let authCountdownTimer = null;

  async function openTelegramAddAccountWizard(opener) {
    const dialog = $('#telegramAuthDialog');
    if (!dialog) return;
    state.activeWizardMode = 'add_account';
    state.addAccountDraft = null;
    const sequence = ++state.wizardSeq;
    const title = $('#telegramAuthTitle');
    if (title) title.textContent = 'Add Telegram Account';
    setWizardBanner('', '');
    const body = $('#telegramAuthBody');
    if (body) {
      body.replaceChildren(create('p', 'detail-note', 'Preparing account setup…'));
    }
    window.AdminV2.openDialog(dialog, opener || document.activeElement);

    let defaultId = 'worker_2';
    try {
      const list = await window.AdminV2.getJson(TELEGRAM_ACCOUNTS_URL);
      if (!list?.accounts?.length) {
        defaultId = 'default';
      } else {
        defaultId = `account_${list.accounts.length + 1}`;
      }
    } catch {
      // Keep the fallback identifier when the account list cannot be read.
    }

    if (sequence !== state.wizardSeq || state.activeWizardMode !== 'add_account') return;
    renderWizardAddAccountStep(defaultId, opener);
  }

  function renderWizardAddAccountStep(defaultId, opener) {
    const body = $('#telegramAuthBody');
    if (!body) return;
    setWizardBanner('', '');

    const wrap = create('div', 'telegram-auth-view');
    const note = create('p', 'detail-note', 'Configure a new Telegram account identifier and display label before starting login authorization.');
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgAddAccountForm';
    form.noValidate = true;

    const inlineError = create('p', 'detail-error');
    inlineError.id = 'tgAddAccountError';
    inlineError.setAttribute('role', 'alert');
    inlineError.hidden = true;

    const idLabel = create('label');
    idLabel.setAttribute('for', 'tgAccountIdInput');
    idLabel.textContent = 'Account ID';
    const idInput = create('input');
    idInput.id = 'tgAccountIdInput';
    idInput.name = 'account_id';
    idInput.type = 'text';
    idInput.placeholder = 'e.g. worker_2';
    idInput.setAttribute('aria-required', 'true');
    idInput.autocomplete = 'off';
    idInput.value = state.addAccountDraft?.account_id ?? (defaultId || '');
    idLabel.appendChild(idInput);

    const labelLabel = create('label');
    labelLabel.setAttribute('for', 'tgAccountLabelInput');
    labelLabel.textContent = 'Label';
    const labelInput = create('input');
    labelInput.id = 'tgAccountLabelInput';
    labelInput.name = 'label';
    labelInput.type = 'text';
    labelInput.placeholder = 'e.g. Worker 2';
    labelInput.autocomplete = 'off';
    labelInput.value = state.addAccountDraft?.label ?? (defaultId || '');
    labelLabel.appendChild(labelInput);

    const clearError = () => {
      if (!inlineError.hidden) {
        inlineError.hidden = true;
        inlineError.textContent = '';
      }
      idInput.removeAttribute('aria-invalid');
      idInput.removeAttribute('aria-describedby');
    };

    const setInputError = (errorMsg) => {
      inlineError.textContent = errorMsg;
      inlineError.hidden = false;
      idInput.setAttribute('aria-invalid', 'true');
      idInput.setAttribute('aria-describedby', 'tgAddAccountError');
      idInput.focus({ preventScroll: true });
    };

    idInput.addEventListener('input', () => {
      clearError();
      state.addAccountDraft = state.addAccountDraft || {};
      state.addAccountDraft.account_id = idInput.value;
    });

    labelInput.addEventListener('input', () => {
      clearError();
      state.addAccountDraft = state.addAccountDraft || {};
      state.addAccountDraft.label = labelInput.value;
    });

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.id = 'tgAddAccountCancel';
    cancelBtn.addEventListener('click', () => closeTelegramAuthWizard());

    const continueBtn = create('button', 'button button-primary', state.addAccountPending ? 'Continuing…' : 'Continue');
    continueBtn.type = 'submit';
    continueBtn.id = 'tgAddAccountContinue';
    if (state.addAccountPending) {
      continueBtn.setAttribute('aria-disabled', 'true');
    }

    actions.append(cancelBtn, continueBtn);
    form.append(inlineError, idLabel, labelLabel, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    idInput.focus({ preventScroll: true });
    if (typeof idInput.select === 'function') {
      idInput.select();
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (state.addAccountPending) return;

      const rawId = idInput.value;
      const normalizedId = rawId ? rawId.trim() : '';

      if (!normalizedId) {
        setInputError('Account ID is required.');
        return;
      }

      if (!isValidTelegramAccountId(normalizedId)) {
        setInputError('Account ID must contain only alphanumeric characters, underscores, and hyphens (1-64 characters).');
        return;
      }

      const rawLabel = labelInput.value;
      const normalizedLabel = (rawLabel && rawLabel.trim()) ? rawLabel.trim() : normalizedId;

      clearError();
      setWizardBanner('', '');

      state.addAccountPending = true;
      const requestToken = Symbol('addAccount');
      state.addAccountToken = requestToken;
      continueBtn.setAttribute('aria-disabled', 'true');
      continueBtn.textContent = 'Continuing…';

      const sequence = ++state.wizardSeq;
      const dialog = $('#telegramAuthDialog');

      try {
        await postTelegramAuth(TELEGRAM_ACCOUNTS_URL, { account_id: normalizedId, label: normalizedLabel });
      } catch (err) {
        if (sequence !== state.wizardSeq || state.activeWizardMode !== 'add_account' || !dialog?.open) {
          setNotice('error', `Could not create account "${normalizedId}": ${err.message}`);
          renderTelegramAccountCard();
          return;
        }
        setInputError(`Could not create account: ${err.message}`);
        continueBtn.removeAttribute('aria-disabled');
        continueBtn.textContent = 'Continue';
        return;
      } finally {
        if (state.addAccountToken === requestToken) {
          state.addAccountPending = false;
          state.addAccountToken = null;
          const currentContinueBtn = $('#tgAddAccountContinue');
          if (currentContinueBtn && currentContinueBtn.getAttribute('aria-disabled') === 'true') {
            currentContinueBtn.removeAttribute('aria-disabled');
            currentContinueBtn.textContent = 'Continue';
          }
        }
      }

      state.addAccountDraft = null;

      if (sequence !== state.wizardSeq || state.activeWizardMode !== 'add_account' || !dialog?.open) {
        setNotice('info', `Account "${normalizedId}" was created. Click "Log in" to authenticate.`);
        renderTelegramAccountCard();
        return;
      }

      try {
        await openTelegramAuthWizard(opener, normalizedId);
      } catch (err) {
        setWizardBanner('error', `Account created, but could not start login wizard: ${err.message}`);
      }
    });
  }

  async function openTelegramAuthWizard(opener, accountId = 'default') {
    const rawTarget = typeof accountId === 'string' ? accountId.trim() : '';
    const validTarget = isValidTelegramAccountId(rawTarget) ? rawTarget : 'default';
    state.activeAuthAccountId = validTarget;
    state.activeWizardMode = 'auth';
    state.addAccountDraft = null;
    const dialog = $('#telegramAuthDialog');
    if (!dialog) return;
    const title = $('#telegramAuthTitle');
    if (title) title.textContent = `Telegram Login: ${state.activeAuthAccountId}`;
    setWizardBanner('', '');
    const sequence = ++state.wizardSeq;
    window.AdminV2.openDialog(dialog, opener || document.activeElement);
    await refreshTelegramAuthWizard(state.activeAuthAccountId, sequence);
  }

  function closeTelegramAuthWizard() {
    const dialog = $('#telegramAuthDialog');
    if (!dialog) return;
    if (authCountdownTimer) {
      clearInterval(authCountdownTimer);
      authCountdownTimer = null;
    }
    state.activeWizardMode = null;
    state.addAccountDraft = null;
    state.wizardSeq = (state.wizardSeq || 0) + 1;
    window.AdminV2.closeDialog(dialog);
    renderTelegramAccountCard();
  }

  function setWizardBanner(type, message) {
    const banner = $('#telegramAuthBanner');
    if (!banner) return;
    const text = extractErrorMessage(message, typeof message === 'string' ? message : '');
    if (!text || text === '[object Object]') {
      banner.hidden = true;
      banner.textContent = '';
      return;
    }
    banner.className = `state-banner state-${type}`;
    banner.textContent = text;
    banner.hidden = false;
  }

  function setAccountActionBanner(type, message) {
    const banner = $('#telegramAccountActionBanner');
    if (!banner) return;
    const text = extractErrorMessage(message, typeof message === 'string' ? message : '');
    if (!text || text === '[object Object]') {
      banner.hidden = true;
      banner.textContent = '';
      return;
    }
    banner.className = `state-banner state-${type}`;
    banner.textContent = text;
    banner.hidden = false;
  }

  function openTelegramAccountRemovalDialog(acc, opener) {
    if (!isValidTelegramAccountId(acc?.account_id)) {
      setNotice('error', 'Invalid account ID.');
      return;
    }
    const dialog = $('#telegramAccountActionDialog');
    if (!dialog) return;

    const actionToken = Symbol('accountRemoval');
    state.accountActionToken = actionToken;
    state.activeAccountActionId = acc.account_id;

    const title = $('#telegramAccountActionTitle');
    if (title) title.textContent = 'Remove Telegram Account';
    setAccountActionBanner('', '');

    const body = $('#telegramAccountActionBody');
    if (!body) return;

    const wrap = create('div', 'telegram-account-action-view');
    const labelText = acc.label && acc.label !== acc.account_id
      ? `account “${acc.label}” (${acc.account_id})`
      : `account “${acc.account_id}”`;
    const note = create('p', 'detail-note',
      `Are you sure you want to remove ${labelText}? Its local session file will be removed. Channels and media files will NOT be deleted.`
    );

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.id = 'telegramAccountActionCancel';
    cancelBtn.addEventListener('click', () => closeTelegramAccountActionDialog());

    const removeBtn = create('button', 'button button-primary', 'Remove');
    removeBtn.type = 'button';
    removeBtn.id = 'telegramAccountActionConfirm';

    removeBtn.addEventListener('click', async () => {
      if (removeBtn.disabled) return;
      removeBtn.disabled = true;
      removeBtn.textContent = 'Removing…';
      cancelBtn.disabled = true;
      setAccountActionBanner('', '');

      const requestToken = actionToken;
      const targetId = acc.account_id;
      const targetLabel = acc.label || acc.account_id;

      try {
        await requestTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(targetId)}?confirm=true`, {}, 'DELETE');
        const isCurrentDialog = (state.accountActionToken === requestToken && state.activeAccountActionId === targetId && dialog?.open);
        if (isCurrentDialog) {
          closeTelegramAccountActionDialog(false);
        }
        setNotice('success', `Account ${targetLabel} removed.`);
        await renderTelegramAccountCard();
        const extantAccountsControl = $('#telegramLoginButton') || $('#telegramAccountsRefreshBtn') || $('#telegramAccountHeading');
        if (extantAccountsControl) {
          const active = document.activeElement;
          if (isCurrentDialog || !active || !active.isConnected || active === document.body) {
            const tag = (extantAccountsControl.tagName || '').toUpperCase();
            if (!['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'].includes(tag) && extantAccountsControl.getAttribute('tabindex') === null) {
              extantAccountsControl.setAttribute('tabindex', '-1');
            }
            if (typeof extantAccountsControl.focus === 'function') {
              extantAccountsControl.focus({ preventScroll: true });
            }
          }
        }
      } catch (err) {
        const isCurrentDialog = (state.accountActionToken === requestToken && state.activeAccountActionId === targetId && dialog?.open);
        if (isCurrentDialog) {
          removeBtn.disabled = false;
          removeBtn.textContent = 'Remove';
          cancelBtn.disabled = false;
          setAccountActionBanner('error', extractErrorMessage(err, `Removal failed: ${err.message}`));
        } else {
          setNotice('error', extractErrorMessage(err, `Could not remove account "${targetLabel}": ${err.message}`));
          await renderTelegramAccountCard();
        }
      }
    });

    actions.append(cancelBtn, removeBtn);
    wrap.append(note, actions);
    body.replaceChildren(wrap);

    window.AdminV2.openDialog(dialog, opener || document.activeElement);
    cancelBtn.focus({ preventScroll: true });
  }

  function closeTelegramAccountActionDialog(restoreFocus = true) {
    const dialog = $('#telegramAccountActionDialog');
    if (!dialog) return;
    setAccountActionBanner('', '');
    const body = $('#telegramAccountActionBody');
    if (body) body.replaceChildren();
    state.activeAccountActionId = null;
    state.accountActionToken = null;
    window.AdminV2.closeDialog(dialog, restoreFocus);
  }

  async function refreshTelegramAuthWizard(accountId, sequence) {
    const rawTarget = typeof accountId === 'string' ? accountId.trim() : '';
    const target = isValidTelegramAccountId(rawTarget) ? rawTarget : (isValidTelegramAccountId(state.activeAuthAccountId) ? state.activeAuthAccountId : 'default');
    state.activeAuthAccountId = target;
    setWizardBanner('', '');
    const body = $('#telegramAuthBody');
    if (!body) return;

    const seq = sequence !== undefined ? sequence : ++state.wizardSeq;

    body.replaceChildren(create('p', 'detail-note', `Checking Telegram authorization status for ${target}…`));

    let statusPayload;
    try {
      statusPayload = await window.AdminV2.getJson(`${TELEGRAM_AUTH_STATUS_URL}?account_id=${encodeURIComponent(target)}&check_live=false`);
    } catch (err) {
      if (seq !== state.wizardSeq || state.activeWizardMode !== 'auth') return;
      body.replaceChildren(create('p', 'detail-error', `Could not check Telegram status: ${scrub(err.message)}`));
      return;
    }

    if (seq !== state.wizardSeq || state.activeWizardMode !== 'auth') return;

    state.telegramAuth = statusPayload;

    if (statusPayload.status === 'authorized') {
      renderWizardAuthorized(body);
    } else if (statusPayload.status === 'unauthorized_session_present') {
      renderWizardUnauthorizedPresent(body, statusPayload.message);
    } else if (statusPayload.status === 'session_file_present') {
      renderWizardSessionPresent(body, statusPayload.message);
    } else if (statusPayload.status === 'unconfigured') {
      renderWizardUnconfigured(body, statusPayload.message);
    } else if (statusPayload.status === 'in_progress') {
      if (statusPayload.login_state === 'password_needed') {
        renderWizardPasswordStep(body);
      } else {
        renderWizardCodeStep(body, statusPayload.phone_mask);
      }
    } else {
      renderWizardPhoneStep(body);
    }
  }

  function renderWizardAuthorized(body) {
    const wrap = create('div', 'telegram-auth-view');
    const msg = create('p', 'detail-note', '✓ Your Telegram account is already authorized and active. To protect existing data, re-authentication is not permitted.');
    const actions = create('div', 'operation-dialog-actions');
    const closeBtn = create('button', 'button button-primary', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeTelegramAuthWizard);
    actions.appendChild(closeBtn);
    wrap.append(msg, actions);
    body.replaceChildren(wrap);
  }

  function renderWizardUnauthorizedPresent(body, message) {
    const wrap = create('div', 'telegram-auth-view');
    const err = create('p', 'detail-error', message || 'Existing session file is present but unauthorized. Adult Hub will not overwrite it automatically. Please back up or remove the invalid file manually.');
    const actions = create('div', 'operation-dialog-actions');
    const closeBtn = create('button', 'button button-primary', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeTelegramAuthWizard);
    actions.appendChild(closeBtn);
    wrap.append(err, actions);
    body.replaceChildren(wrap);
  }

  function renderWizardSessionPresent(body, message) {
    const wrap = create('div', 'telegram-auth-view');
    const note = create('p', 'detail-note', message || 'An existing Telegram session file is present on the server. You can verify its authorization status without replacing or re-authenticating.');
    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeTelegramAuthWizard);

    const verifyBtn = create('button', 'button button-primary', 'Verify session');
    verifyBtn.type = 'button';
    verifyBtn.addEventListener('click', async () => {
      const expectedSeq = state.wizardSeq;
      const target = isValidTelegramAccountId(state.activeAuthAccountId) ? state.activeAuthAccountId : 'default';
      verifyBtn.disabled = true;
      verifyBtn.textContent = 'Verifying…';
      try {
        const liveStatus = await window.AdminV2.getJson(`${TELEGRAM_AUTH_STATUS_URL}?account_id=${encodeURIComponent(target)}&check_live=true`);
        if (expectedSeq !== state.wizardSeq || state.activeWizardMode !== 'auth' || state.activeAuthAccountId !== target) {
          return;
        }
        state.telegramAuth = liveStatus;
        renderTelegramAccountCard();
        if (liveStatus.status === 'authorized') {
          renderWizardAuthorized(body);
        } else if (liveStatus.status === 'unauthorized_session_present') {
          renderWizardUnauthorizedPresent(body, liveStatus.message);
        } else {
          setWizardBanner('warning', liveStatus.message || 'Session verification inconclusive.');
          verifyBtn.disabled = false;
          verifyBtn.textContent = 'Verify session';
        }
      } catch (err) {
        if (expectedSeq !== state.wizardSeq || state.activeWizardMode !== 'auth' || state.activeAuthAccountId !== target) {
          return;
        }
        setWizardBanner('error', err.message || 'Failed to verify session.');
        verifyBtn.disabled = false;
        verifyBtn.textContent = 'Verify session';
      }
    });

    actions.append(cancelBtn, verifyBtn);
    wrap.append(note, actions);
    body.replaceChildren(wrap);
  }

  function renderWizardUnconfigured(body, message) {
    const wrap = create('div', 'telegram-auth-view');
    const err = create('p', 'detail-error', message || 'TELEGRAM_API_ID and TELEGRAM_API_HASH are not configured in the server environment.');
    const actions = create('div', 'operation-dialog-actions');
    const closeBtn = create('button', 'button button-primary', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeTelegramAuthWizard);
    actions.appendChild(closeBtn);
    wrap.append(err, actions);
    body.replaceChildren(wrap);
  }

  function createStepIndicator(activeStep, show2fa) {
    const nav = create('div', 'telegram-auth-steps');
    const step1 = create('div', `telegram-auth-step ${activeStep === 1 ? 'active' : activeStep > 1 ? 'done' : ''}`, '1. Phone number');
    const step2 = create('div', `telegram-auth-step ${activeStep === 2 ? 'active' : activeStep > 2 ? 'done' : ''}`, '2. Verification code');
    nav.append(step1, step2);
    if (show2fa || activeStep === 3) {
      const step3 = create('div', `telegram-auth-step ${activeStep === 3 ? 'active' : activeStep > 3 ? 'done' : ''}`, '3. 2FA Password');
      nav.appendChild(step3);
    }
    return nav;
  }

  function renderWizardPhoneStep(body) {
    const wrap = create('div', 'telegram-auth-view');
    wrap.appendChild(createStepIndicator(1, false));

    const note = create('p', 'detail-note', 'Enter your Telegram phone number with international country code (e.g. +48123456789). A verification code will be sent to your Telegram app or SMS.');
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgPhoneForm';

    const label = create('label');
    label.setAttribute('for', 'tgPhoneInput');
    label.textContent = 'Phone number';
    const input = create('input');
    input.id = 'tgPhoneInput';
    input.type = 'tel';
    input.placeholder = '+48123456789';
    input.required = true;
    input.autocomplete = 'tel';
    label.appendChild(input);

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeTelegramAuthWizard);

    const submitBtn = create('button', 'button button-primary', 'Send verification code');
    submitBtn.type = 'submit';

    actions.append(cancelBtn, submitBtn);
    form.append(label, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    input.focus({ preventScroll: true });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const phoneVal = input.value.trim();
      if (!phoneVal.startsWith('+') || phoneVal.length < 8) {
        setWizardBanner('error', 'Phone number must be in international format (e.g. +48123456789).');
        return;
      }
      setWizardBanner('', '');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Sending code…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_START_URL, { phone: phoneVal });
        if (result.ok) {
          if (result.state === 'authorized') {
            renderWizardSuccess(body);
          } else {
            renderWizardCodeStep(body, result.phone_mask || phoneVal);
          }
        } else {
          setWizardBanner('error', extractErrorMessage(result, 'Failed to send verification code.'));
          submitBtn.disabled = false;
          submitBtn.textContent = 'Send verification code';
        }
      } catch (err) {
        if (err.flood_wait_seconds) {
          setWizardBanner('warning', extractErrorMessage(err, `Please wait ${err.flood_wait_seconds}s before requesting a code.`));
        } else {
          setWizardBanner('error', extractErrorMessage(err, 'Could not send verification code.'));
        }
        submitBtn.disabled = false;
        submitBtn.textContent = 'Send verification code';
      }
    });
  }

  function renderWizardCodeStep(body, phoneMask) {
    const wrap = create('div', 'telegram-auth-view');
    wrap.appendChild(createStepIndicator(2, false));

    const note = create('p', 'detail-note', `A verification code has been sent to ${phoneMask || 'your phone'}. Please enter the code below.`);
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgCodeForm';

    const label = create('label');
    label.setAttribute('for', 'tgCodeInput');
    label.textContent = 'Verification code';
    const input = create('input');
    input.id = 'tgCodeInput';
    input.type = 'text';
    input.placeholder = '12345';
    input.required = true;
    input.autocomplete = 'one-time-code';
    input.inputMode = 'numeric';
    label.appendChild(input);

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', async () => {
      try {
        await postTelegramAuth(TELEGRAM_AUTH_CANCEL_URL, {});
      } catch {
        // A failed cancel is not shown; the wizard returns to the phone step either way.
      }
      renderWizardPhoneStep(body);
    });

    const resendBtn = create('button', 'button button-quiet', 'Resend code');
    resendBtn.type = 'button';
    resendBtn.addEventListener('click', async () => {
      resendBtn.disabled = true;
      resendBtn.textContent = 'Resending…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_RESEND_URL, {});
        const floodWait = result.flood_wait_seconds || (result.error && typeof result.error === 'object' ? result.error.flood_wait_seconds : undefined);
        if (result.ok) {
          setWizardBanner('info', result.message || 'Verification code resent.');
          resendBtn.textContent = 'Code resent';
          setTimeout(() => { resendBtn.disabled = false; resendBtn.textContent = 'Resend code'; }, 10000);
        } else if (floodWait) {
          setWizardBanner('warning', extractErrorMessage(result, `Please wait ${floodWait}s before requesting a code.`));
          startResendCountdown(resendBtn, floodWait);
        } else {
          setWizardBanner('error', extractErrorMessage(result, 'Could not resend code.'));
          resendBtn.disabled = false;
          resendBtn.textContent = 'Resend code';
        }
      } catch (err) {
        if (err.flood_wait_seconds) {
          setWizardBanner('warning', extractErrorMessage(err, `Please wait ${err.flood_wait_seconds}s before requesting a code.`));
          startResendCountdown(resendBtn, err.flood_wait_seconds);
        } else {
          setWizardBanner('error', extractErrorMessage(err, 'Could not resend code.'));
          resendBtn.disabled = false;
          resendBtn.textContent = 'Resend code';
        }
      }
    });

    const submitBtn = create('button', 'button button-primary', 'Verify code');
    submitBtn.type = 'submit';

    actions.append(cancelBtn, resendBtn, submitBtn);
    form.append(label, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    input.focus({ preventScroll: true });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const codeVal = input.value.trim();
      if (!codeVal) return;
      setWizardBanner('', '');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Verifying…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_CODE_URL, { code: codeVal });
        if (result.ok) {
          if (result.state === 'password_needed') {
            renderWizardPasswordStep(body);
          } else if (result.state === 'authorized') {
            renderWizardSuccess(body, result.sync_notified);
          } else {
            setWizardBanner('error', extractErrorMessage(result, 'Verification code failed.'));
            submitBtn.disabled = false;
            submitBtn.textContent = 'Verify code';
          }
        } else {
          setWizardBanner('error', extractErrorMessage(result, 'Verification code failed.'));
          submitBtn.disabled = false;
          submitBtn.textContent = 'Verify code';
        }
      } catch (err) {
        setWizardBanner('error', extractErrorMessage(err, 'Verification failed.'));
        submitBtn.disabled = false;
        submitBtn.textContent = 'Verify code';
      }
    });
  }

  function startResendCountdown(button, seconds) {
    if (authCountdownTimer) clearInterval(authCountdownTimer);
    let remaining = Number(seconds) || 0;
    button.disabled = true;
    button.textContent = `Wait ${remaining}s`;
    authCountdownTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(authCountdownTimer);
        authCountdownTimer = null;
        button.disabled = false;
        button.textContent = 'Resend code';
      } else {
        button.disabled = true;
        button.textContent = `Wait ${remaining}s`;
      }
    }, 1000);
  }

  function renderWizardPasswordStep(body) {
    const wrap = create('div', 'telegram-auth-view');
    wrap.appendChild(createStepIndicator(3, true));

    const note = create('p', 'detail-note', 'Two-Factor Authentication (2FA) is enabled on this account. Please enter your 2FA password to complete login.');
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgPasswordForm';

    const label = create('label');
    label.setAttribute('for', 'tgPasswordInput');
    label.textContent = '2FA Password';
    const input = create('input');
    input.id = 'tgPasswordInput';
    input.type = 'password';
    input.required = true;
    input.autocomplete = 'current-password';
    label.appendChild(input);

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', async () => {
      try {
        await postTelegramAuth(TELEGRAM_AUTH_CANCEL_URL, {});
      } catch {
        // A failed cancel is not shown; the wizard returns to the phone step either way.
      }
      renderWizardPhoneStep(body);
    });

    const submitBtn = create('button', 'button button-primary', 'Sign in');
    submitBtn.type = 'submit';

    actions.append(cancelBtn, submitBtn);
    form.append(label, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    input.focus({ preventScroll: true });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const pwdVal = input.value;
      if (!pwdVal) return;
      setWizardBanner('', '');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Signing in…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_PASSWORD_URL, { password: pwdVal });
        if (result.ok && result.state === 'authorized') {
          renderWizardSuccess(body, result.sync_notified);
        } else {
          setWizardBanner('error', extractErrorMessage(result, '2FA authentication failed.'));
          submitBtn.disabled = false;
          submitBtn.textContent = 'Sign in';
        }
      } catch (err) {
        setWizardBanner('error', extractErrorMessage(err, '2FA authentication failed.'));
        submitBtn.disabled = false;
        submitBtn.textContent = 'Sign in';
      }
    });
  }

  function renderWizardSuccess(body, syncNotified) {
    if (authCountdownTimer) {
      clearInterval(authCountdownTimer);
      authCountdownTimer = null;
    }
    const wrap = create('div', 'telegram-auth-view');
    const msg = syncNotified
      ? '✓ Telegram authentication successful! Your session has been promoted and background sync has been notified.'
      : '✓ Telegram authentication successful! Your session has been promoted and is ready for channel synchronization.';
    const banner = create('div', 'state-banner state-success', msg);
    const actions = create('div', 'operation-dialog-actions');
    const doneBtn = create('button', 'button button-primary', 'Done');
    doneBtn.type = 'button';
    doneBtn.addEventListener('click', () => {
      closeTelegramAuthWizard();
      loadSources();
    });
    actions.appendChild(doneBtn);
    wrap.append(banner, actions);
    body.replaceChildren(wrap);
  }

  Object.assign(SRC, { closeTelegramAccountActionDialog, closeTelegramAuthWizard,
    openTelegramAddAccountWizard, renderTelegramAccountCard });
})();
