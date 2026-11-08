const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { createAdminV2Helpers } = require('./helpers/admin_v2_shared_helpers.cjs');

function createDeferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let activeElement = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.textContent = '';
    this.className = ''; this.isConnected = true; this.attributes = {}; this.noValidate = false;
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children.push(...nodes); }
  appendChild(node) { this.append(node); return node; }
  hasAttribute(name) { return name in this.attributes; }
  replaceChildren(...nodes) {
    this.children.forEach(node => {
      node.parentElement = null;
      descendants(node, () => true).forEach(desc => { desc.isConnected = false; });
    });
    nodes.forEach(node => {
      node.parentElement = this;
      descendants(node, () => true).forEach(desc => { desc.isConnected = true; });
    });
    this.children = nodes;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) {
    if (selector.startsWith('#')) {
      const id = selector.slice(1);
      return descendants(this, node => node.id === id)[0] || null;
    }
    if (selector.startsWith('.')) return this.children.find(node => (node.className || '').split(/\s+/).includes(selector.slice(1))) || null;
    return null;
  }
  reportValidity() { return true; }
  select() {}
  get elements() {
    const inputs = descendants(this, node => ['input', 'select', 'textarea'].includes(node.tagName));
    const named = {};
    inputs.forEach(input => { if (input.name) named[input.name] = input; });
    return named;
  }
  closest(selector) {
    let curr = this;
    while (curr) {
      if (selector.startsWith('#') && curr.id === selector.slice(1)) return curr;
      if (selector.startsWith('.') && curr.className && curr.className.split(/\s+/).includes(selector.slice(1))) return curr;
      curr = curr.parentElement;
    }
    return null;
  }
  focus() { if (!this.disabled) activeElement = this; }
  querySelectorAll() { return []; }
}

function descendants(root, predicate) {
  return [
    ...(predicate(root) ? [root] : []),
    ...(root.children || []).flatMap(child => descendants(child, predicate)),
  ];
}

(async () => {
  const ids = [
    'sourcesView', 'sourcesFilters', 'sourcesClearFilters', 'sourcesRefresh', 'sourcesAddChannel',
    'sourcesStatusFilter', 'sourcesRows', 'sourcesPagination', 'sourceDetailDialog', 'sourcesNotice',
    'sourcesFeedback', 'sourceDetailContent', 'sourceChannelsPane', 'sourceHistoryPane',
    'sourcesHistoryFeedback', 'sourcesHistoryRows', 'sourcesHistoryPagination', 'sourcesState',
    'sourcesTableWrap', 'sourcesQuery', 'liveMessage', 'crumbCurrent',
    'operationalEyebrow', 'operationalTitle', 'operationalIntro',
    'telegramLoginButton', 'telegramAccountsRefreshBtn', 'telegramAuthClose',
    'telegramAuthDialog', 'telegramAuthTitle', 'telegramAuthBanner', 'telegramAuthBody',
    'telegramAccountActionDialog', 'telegramAccountActionClose', 'telegramAccountActionTitle',
    'telegramAccountActionBanner', 'telegramAccountActionBody',
    'telegramAccountIntro', 'telegramAccountsContainer', 'telegramAccountBanner', 'telegramAccountCard'
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'sourcesFilters' ? 'form' : (id === 'telegramAuthDialog' || id === 'telegramAccountActionDialog') ? 'dialog' : 'div')]));
  const document = {
    get activeElement() { return activeElement; },
    querySelector(selector) {
      if (selector.startsWith('#')) {
        const id = selector.slice(1);
        if (elements[id]) return elements[id];
        for (const root of Object.values(elements)) {
          const match = descendants(root, node => node.id === id)[0];
          if (match) return match;
        }
        return null;
      }
      return null;
    },
    querySelectorAll: () => [],
    createElement: tag => new Element(tag),
    addEventListener() {},
  };

  const postCalls = [];
  let existingAccounts = [{ account_id: 'default', label: 'Default Account', authorized: false }];
  let authStatusResponse = { status: 'unconfigured', message: 'Ready for auth' };

  const dialogOpeners = new WeakMap();
  const window = {
    addEventListener() {},
    prompt() {
      throw new Error('Native prompt() must not be called when adding Telegram account');
    },
    confirm() {
      throw new Error('Native confirm() must not be called');
    },
    AdminV2: {
      ...createAdminV2Helpers(document),
      async getJson(path) {
        if (path.endsWith('/capabilities')) {
          return { token_required: false };
        }
        if (path.endsWith('/sources/list')) {
          return { available: true, managed_revision: 0, applied_revision: 0, sources: [] };
        }
        if (path.endsWith('/sources/settings')) {
          return { revision: 0, overrides: {} };
        }
        if (path.includes('/sync-history')) {
          return { runs: [] };
        }
        if (path.endsWith('/telegram/accounts')) {
          return { accounts: existingAccounts };
        }
        if (path.includes('/telegram/auth/status')) {
          return authStatusResponse;
        }
        throw new Error(`Unexpected GET ${path}`);
      },
      openDialog(dialog, opener = document.activeElement) {
        if (!dialog) return;
        dialog.open = true;
        if (opener && typeof opener.focus === 'function') dialogOpeners.set(dialog, opener);
      },
      closeDialog(dialog, restoreFocus = true) {
        if (!dialog) return;
        dialog.open = false;
        const previous = dialogOpeners.get(dialog);
        dialogOpeners.delete(dialog);
        if (restoreFocus && previous && typeof previous.focus === 'function') {
          previous.focus();
        }
      },
    },
  };

  const location = { hash: '#sources', search: '', href: 'http://localhost/admin/v2/#sources' };
  const history = {
    replaceState() {},
    pushState() {},
  };

  let postFetchHandler = null;
  const customFetch = async (url, options = {}) => {
    const payload = options.body ? JSON.parse(options.body) : null;
    postCalls.push({ path: url, payload, method: options.method });

    if (postFetchHandler) {
      const resp = await postFetchHandler(url, options, payload);
      if (resp) return resp;
    }

    return {
      ok: true,
      status: 200,
      async json() { return { ok: true, account_id: payload?.account_id }; }
    };
  };

  const context = {
    window, document, location, history, URL, URLSearchParams, AbortController,
    Headers, fetch: customFetch,
    setTimeout, clearTimeout, setInterval, clearInterval, setImmediate, Intl, Date, Map, Set, Array, Object, Number, String, Boolean, Math, Symbol,
  };

  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));

  // 1. Verify clicking telegramLoginButton opens telegramAuthDialog with Add Account screen without calling prompt()
  const loginBtn = elements.telegramLoginButton;
  assert.ok(loginBtn.listeners.click, 'telegramLoginButton has click listener');
  await loginBtn.listeners.click({ currentTarget: loginBtn });

  assert.equal(elements.telegramAuthDialog.open, true, 'dialog is opened');
  assert.equal(elements.telegramAuthTitle.textContent, 'Add Telegram Account', 'dialog title reflects Add Telegram Account');

  const idInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const labelInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountLabelInput')[0];
  const continueBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountContinue')[0];
  const cancelBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountCancel')[0];
  const addForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  const errorEl = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountError')[0];

  assert.ok(idInput, 'Account ID input exists');
  assert.ok(labelInput, 'Label input exists');
  assert.ok(continueBtn, 'Continue button exists');
  assert.ok(cancelBtn, 'Cancel button exists');
  assert.ok(addForm, 'Add Account form exists');
  assert.ok(errorEl, 'Inline error element exists');
  assert.equal(errorEl.hidden, true, 'Inline error initially hidden');
  assert.equal(idInput.value, 'account_2', 'Default account ID is prefilled based on existing accounts');
  assert.equal(idInput.getAttribute('aria-invalid'), null, 'aria-invalid not initially set');
  assert.equal(idInput.getAttribute('aria-describedby'), null, 'aria-describedby not initially set');

  // 2. Validate empty and whitespace submission: form has noValidate to avoid browser bubble; inline error displayed
  assert.equal(addForm.noValidate, true, 'Form has noValidate=true to prevent native browser bubble');
  assert.equal(idInput.getAttribute('aria-required'), 'true', 'Account ID input has aria-required="true" for accessibility');

  idInput.value = '';
  let prevented = false;
  await addForm.listeners.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'Form submit prevented default on empty ID');
  assert.equal(postCalls.length, 0, 'No account POST occurred on empty validation failure');
  assert.equal(errorEl.hidden, false, 'Inline error is shown for empty ID');
  assert.match(errorEl.textContent, /Account ID is required/i, 'Error text indicates Account ID is required');
  assert.equal(idInput.getAttribute('aria-invalid'), 'true', 'idInput has aria-invalid="true" on error');
  assert.equal(idInput.getAttribute('aria-describedby'), 'tgAddAccountError', 'idInput has aria-describedby pointing to inline error');
  assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner stays hidden');

  // Verify clearing error when typing
  idInput.value = 'valid_name';
  idInput.listeners.input();
  assert.equal(errorEl.hidden, true, 'Inline error is hidden when user types');
  assert.equal(idInput.getAttribute('aria-invalid'), null, 'aria-invalid is cleared when user types');
  assert.equal(idInput.getAttribute('aria-describedby'), null, 'aria-describedby is cleared when user types');

  idInput.value = '   ';
  prevented = false;
  await addForm.listeners.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'Form submit prevented default on whitespace ID');
  assert.equal(postCalls.length, 0, 'No account POST occurred on whitespace validation failure');
  assert.equal(errorEl.hidden, false, 'Inline error is shown for whitespace ID');
  assert.match(errorEl.textContent, /Account ID is required/i, 'Error text indicates Account ID is required');
  assert.equal(idInput.getAttribute('aria-invalid'), 'true', 'idInput has aria-invalid="true" on error');
  assert.equal(idInput.getAttribute('aria-describedby'), 'tgAddAccountError', 'idInput has aria-describedby on error');
  assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner stays hidden');

  // 3. Validate safe character and length contract before endpoint path (Finding 7)
  for (const invalidId of ['bad/id', 'bad\\id', '../evil', 'bad id', 'worker;drop', 'a'.repeat(65)]) {
    idInput.value = invalidId;
    await addForm.listeners.submit({ preventDefault() {} });
    assert.equal(postCalls.length, 0, `No POST sent for invalid ID ${invalidId}`);
    assert.equal(errorEl.hidden, false, `Inline error shown for invalid ID ${invalidId}`);
    assert.match(errorEl.textContent, /must contain only alphanumeric characters/i, `Validation message matches safe contract for ${invalidId}`);
    assert.equal(idInput.getAttribute('aria-invalid'), 'true', `idInput has aria-invalid for ${invalidId}`);
    assert.equal(idInput.getAttribute('aria-describedby'), 'tgAddAccountError');
    assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner stays hidden');
  }

  // 4. Cancel before submit closes dialog and creates no account
  cancelBtn.listeners.click({});
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog is closed after Cancel');
  assert.equal(postCalls.length, 0, 'No account created on Cancel');

  // Verify Escape / dialog cancel event closes dialog and clears draft
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  const escIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  escIdInput.value = 'draft_to_discard';
  escIdInput.listeners.input();
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog is closed on Escape/cancel');
  assert.equal(postCalls.length, 0, 'No account created on Escape');

  // Re-open and verify draft was cleared (shows prefilled account_2 instead of discarded draft)
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  const reopenedIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  assert.equal(reopenedIdInput.value, 'account_2', 'Draft was cleared after close');

  // 5. Verify API rejection simulation through postTelegramAuth path (HTTP 409 Conflict)
  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_exists') {
      return {
        ok: false,
        status: 409,
        async json() { return { ok: false, code: 'account_exists', error: "Account 'worker_exists' already exists." }; }
      };
    }
    return null;
  };

  const retryForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  const retryContinueBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountContinue')[0];
  const retryErrorEl = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountError')[0];

  reopenedIdInput.value = 'worker_exists';
  await retryForm.listeners.submit({ preventDefault() {} });

  assert.equal(retryErrorEl.hidden, false, 'Inline error is shown when backend rejects creation');
  assert.match(retryErrorEl.textContent, /Account 'worker_exists' already exists/i, 'Error message matches backend 409 response');
  assert.equal(reopenedIdInput.getAttribute('aria-invalid'), 'true', 'aria-invalid set on backend rejection');
  assert.equal(reopenedIdInput.getAttribute('aria-describedby'), 'tgAddAccountError', 'aria-describedby set on backend rejection');
  assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner remains hidden on inline error');
  assert.equal(retryContinueBtn.disabled, false, 'Continue button is NOT disabled so keyboard focus remains stable');
  assert.equal(retryContinueBtn.getAttribute('aria-disabled'), null, 'Continue button aria-disabled cleared for retry');

  // 6. Test closing while POST is pending & double-submit guard (Findings 1, 2, 3)
  reopenedIdInput.value = 'worker_pending';
  const pendingStart = createDeferred();
  const pendingGate = createDeferred();

  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_pending') {
      pendingStart.resolve();
      await pendingGate.promise;
      existingAccounts.push({ account_id: 'worker_pending', label: 'worker_pending', authorized: false });
      return {
        ok: true,
        status: 201,
        async json() { return { ok: true, account: { account_id: 'worker_pending', label: 'worker_pending' } }; }
      };
    }
    return null;
  };

  const postCountBeforePending = postCalls.length;
  const submitPromise = retryForm.listeners.submit({ preventDefault() {} });
  await pendingStart.promise;
  assert.equal(postCalls.length, postCountBeforePending + 1, 'First submit initiated POST');

  // Verify accessibility during submit: aria-disabled is set, but button is NOT disabled to keep focus stable
  assert.equal(retryContinueBtn.getAttribute('aria-disabled'), 'true', 'Continue button has aria-disabled="true" during submit');
  assert.equal(retryContinueBtn.disabled, false, 'Continue button is NOT disabled during submit to keep keyboard focus stable');

  // Double submit while pending must be blocked
  await retryForm.listeners.submit({ preventDefault() {} });
  assert.equal(postCalls.length, postCountBeforePending + 1, 'Double-submit while pending was blocked');

  // User closes / dismisses the dialog while POST is in-flight
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed while POST is pending');

  // Resolve the in-flight POST
  pendingGate.resolve();
  await submitPromise;

  // Must NOT reopen into the login wizard when dismissed
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog must not reopen into login wizard after user dismissed it');
  // Must show non-modal notice
  assert.equal(elements.sourcesNotice.hidden, false, 'Non-modal notice is displayed after background completion');
  const noticeChildren = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
  assert.match(noticeChildren, /worker_pending/i, 'Non-modal notice mentions created account');

  // 7. Closed-dialog failure test branch (Finding 4)
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  assert.equal(elements.telegramAuthDialog.open, true, 'Dialog opened for closed-failure test');

  const failIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const failForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  failIdInput.value = 'worker_fail';

  const failStart = createDeferred();
  const failGate = createDeferred();

  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_fail') {
      failStart.resolve();
      await failGate.promise;
      return {
        ok: false,
        status: 500,
        async json() { return { ok: false, error: 'Database timeout on account creation' }; }
      };
    }
    return null;
  };

  const failSubmitPromise = failForm.listeners.submit({ preventDefault() {} });
  await failStart.promise;

  // Close dialog while failure POST is in flight
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed while failing POST was pending');

  // Let failing POST complete
  failGate.resolve();
  await failSubmitPromise;

  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog remains closed after background failure');
  assert.equal(elements.sourcesNotice.hidden, false, 'Notice is shown for background failure');
  assert.match(elements.sourcesNotice.className, /state-error/, 'Notice has error state for background failure');
  const failNoticeText = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
  assert.match(failNoticeText, /worker_fail/i, 'Notice mentions failed account ID');
  assert.match(failNoticeText, /Database timeout on account creation/i, 'Notice mentions backend error message');

  // 8. Close / Reopen / Second submit regression test (Finding 2)
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  assert.equal(elements.telegramAuthDialog.open, true, 'Dialog opened for close/reopen test');

  const reopen1IdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const reopen1Form = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  reopen1IdInput.value = 'worker_reopen_1';

  const reopen1Start = createDeferred();
  const reopen1Gate = createDeferred();

  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_reopen_1') {
      reopen1Start.resolve();
      await reopen1Gate.promise;
      existingAccounts.push({ account_id: 'worker_reopen_1', label: 'worker_reopen_1', authorized: false });
      return {
        ok: true,
        status: 201,
        async json() { return { ok: true, account: { account_id: 'worker_reopen_1', label: 'worker_reopen_1' } }; }
      };
    }
    if (payload?.account_id === 'worker_reopen_2') {
      existingAccounts.push({ account_id: 'worker_reopen_2', label: 'worker_reopen_2', authorized: false });
      return {
        ok: true,
        status: 201,
        async json() { return { ok: true, account: { account_id: 'worker_reopen_2', label: 'worker_reopen_2' } }; }
      };
    }
    return null;
  };

  const reopen1SubmitPromise = reopen1Form.listeners.submit({ preventDefault() {} });
  await reopen1Start.promise;

  // Close dialog while POST 1 is in-flight
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed while POST 1 is pending');

  // Reopen dialog while POST 1 is STILL in-flight
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  assert.equal(elements.telegramAuthDialog.open, true, 'Dialog reopened while POST 1 is pending');

  const reopen2ContinueBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountContinue')[0];
  const reopen2Form = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  const reopen2IdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];

  assert.equal(reopen2ContinueBtn.getAttribute('aria-disabled'), 'true', 'Reopened continue button reflects pending request');

  // Attempt second submit while first is still pending: must be blocked
  const postCountBeforeSecondSubmit = postCalls.length;
  reopen2IdInput.value = 'worker_reopen_2';
  await reopen2Form.listeners.submit({ preventDefault() {} });
  assert.equal(postCalls.length, postCountBeforeSecondSubmit, 'Second submit while first POST is pending was blocked');

  // Now resolve first POST
  reopen1Gate.resolve();
  await reopen1SubmitPromise;

  // Notice shown for worker_reopen_1
  assert.equal(elements.sourcesNotice.hidden, false, 'Notice shown for background success of first account');
  const reopen1NoticeText = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
  assert.match(reopen1NoticeText, /worker_reopen_1/i, 'Notice mentions worker_reopen_1');

  // Continue button in reopened dialog should now be re-enabled
  assert.equal(reopen2ContinueBtn.getAttribute('aria-disabled'), null, 'Continue button re-enabled after first POST completed');

  // Now submit second account successfully
  const postCountBeforeSecondSend = postCalls.length;
  await reopen2Form.listeners.submit({ preventDefault() {} });
  assert.equal(postCalls.length, postCountBeforeSecondSend + 1, 'Second account creation POST was sent');
  assert.equal(postCalls[postCountBeforeSecondSend].payload.account_id, 'worker_reopen_2');

  // Close wizard
  elements.telegramAuthClose.listeners.click();
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');

  // 9. Test empty-label fallback: whitespace label falls back to account_id
  postFetchHandler = null;
  await loginBtn.listeners.click({ currentTarget: loginBtn });

  const fallbackIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const fallbackLabelInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountLabelInput')[0];
  const fallbackForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];

  fallbackIdInput.value = 'worker_fallback';
  fallbackLabelInput.value = '   '; // whitespace-only label

  const postCountBeforeFallback = postCalls.length;
  await fallbackForm.listeners.submit({ preventDefault() {} });

  assert.equal(postCalls.length, postCountBeforeFallback + 1, 'Account creation POST sent');
  assert.equal(postCalls[postCountBeforeFallback].path, '/admin/api/v2/telegram/accounts');
  assert.deepEqual(postCalls[postCountBeforeFallback].payload, { account_id: 'worker_fallback', label: 'worker_fallback' }, 'Empty label fell back to normalizedId');
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: worker_fallback', 'Transitions to login wizard for new account');

  // Close the wizard
  elements.telegramAuthClose.listeners.click();
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');

  // 10. Verify-session async race regression test: switching accounts while verification is pending (Finding 1)
  existingAccounts.push(
    { account_id: 'acc_verify_a', label: 'Verify A', authorized: false, has_session_file: true },
    { account_id: 'acc_verify_b', label: 'Verify B', authorized: false, has_session_file: true }
  );

  const verifyAStart = createDeferred();
  const verifyAGate = createDeferred();

  const originalGetJson = window.AdminV2.getJson;
  window.AdminV2.getJson = async function (path) {
    if (path.includes('account_id=acc_verify_a') && path.includes('check_live=false')) {
      return { status: 'session_file_present', message: 'Session file present for acc_verify_a' };
    }
    if (path.includes('account_id=acc_verify_a') && path.includes('check_live=true')) {
      verifyAStart.resolve();
      await verifyAGate.promise;
      return { status: 'authorized', message: 'acc_verify_a verified successfully' };
    }
    if (path.includes('account_id=acc_verify_b') && path.includes('check_live=false')) {
      return { status: 'session_file_present', message: 'Session file present for acc_verify_b' };
    }
    return originalGetJson.call(this, path);
  };

  // Refresh account cards
  await elements.telegramAccountsRefreshBtn.listeners.click();

  const cards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
  const cardA = cards.find(c => descendants(c, n => (n.textContent || '').includes('acc_verify_a')).length > 0);
  const cardB = cards.find(c => descendants(c, n => (n.textContent || '').includes('acc_verify_b')).length > 0);
  assert.ok(cardA, 'cardA found');
  assert.ok(cardB, 'cardB found');

  const verifyLoginBtnA = descendants(cardA, n => n.tagName === 'button' && n.textContent === 'Verify session')[0];
  const verifyLoginBtnB = descendants(cardB, n => n.tagName === 'button' && n.textContent === 'Verify session')[0];
  assert.ok(verifyLoginBtnA, 'verifyLoginBtnA found');
  assert.ok(verifyLoginBtnB, 'verifyLoginBtnB found');

  // Open wizard for acc_verify_a
  await verifyLoginBtnA.listeners.click();
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_verify_a');

  // Click "Verify session" inside wizard
  const verifyBtnInsideA = descendants(elements.telegramAuthBody, n => n.tagName === 'button' && n.textContent === 'Verify session')[0];
  assert.ok(verifyBtnInsideA, 'Verify session button found inside wizard');

  const verifyPromiseA = verifyBtnInsideA.listeners.click();
  await verifyAStart.promise;
  assert.equal(verifyBtnInsideA.textContent, 'Verifying…', 'Verify button shows Verifying… state');

  // While acc_verify_a live verification is pending, switch accounts to acc_verify_b
  await verifyLoginBtnB.listeners.click();
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_verify_b', 'Wizard switched to acc_verify_b');

  // Now resolve the live check for acc_verify_a
  verifyAGate.resolve();
  await verifyPromiseA;

  // Stale acc_verify_a response must be discarded: dialog must remain on acc_verify_b!
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_verify_b', 'Title remains acc_verify_b; stale verification discarded');
  const currentWizardText = descendants(elements.telegramAuthBody, () => true).map(n => n.textContent).join(' ');
  assert.doesNotMatch(currentWizardText, /already authorized and active/i, 'Authorized view of acc_verify_a was not rendered');
  assert.match(currentWizardText, /Session file present for acc_verify_b/i, 'Wizard body still displays acc_verify_b session view');

  // Close wizard
  elements.telegramAuthClose.listeners.click();
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');

  // 11. Test overlapping Login opens and sequence guard
  existingAccounts.push(
    { account_id: 'account_slow', label: 'Account Slow', authorized: false },
    { account_id: 'account_fast', label: 'Account Fast', authorized: false }
  );

  const slowStatusStart = createDeferred();
  const slowStatusGate = createDeferred();

  window.AdminV2.getJson = async function (path) {
    if (path.includes('account_id=account_slow')) {
      slowStatusStart.resolve();
      await slowStatusGate.promise;
      return { status: 'in_progress', login_state: 'code_needed', phone_mask: '+1234567890' };
    }
    if (path.includes('account_id=account_fast')) {
      return { status: 'unconfigured', message: 'Fast unconfigured account' };
    }
    return originalGetJson.call(this, path);
  };

  // Refresh account cards to include account_slow and account_fast
  await elements.telegramAccountsRefreshBtn.listeners.click();

  const refreshedCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
  const slowCard = refreshedCards.find(card => descendants(card, n => (n.textContent || '').includes('account_slow')).length > 0);
  const fastCard = refreshedCards.find(card => descendants(card, n => (n.textContent || '').includes('account_fast')).length > 0);
  assert.ok(slowCard, 'slowCard found');
  assert.ok(fastCard, 'fastCard found');

  const slowLoginBtn = descendants(slowCard, n => n.tagName === 'button' && n.textContent === 'Log in')[0];
  const fastLoginBtn = descendants(fastCard, n => n.tagName === 'button' && n.textContent === 'Log in')[0];
  assert.ok(slowLoginBtn, 'slowLoginBtn found');
  assert.ok(fastLoginBtn, 'fastLoginBtn found');

  // Trigger login for account_slow (starts slow request)
  const slowLoginPromise = slowLoginBtn.listeners.click();
  await slowStatusStart.promise;

  // Immediately trigger login for account_fast (supersedes account_slow)
  await fastLoginBtn.listeners.click();
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: account_fast', 'Title updated to account_fast');

  // Now let the slow status check resolve
  slowStatusGate.resolve();
  await slowLoginPromise;

  // The stale account_slow response must be discarded; dialog remains showing account_fast
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: account_fast', 'Stale response discarded by sequence guard');
  const wizardText = descendants(elements.telegramAuthBody, () => true).map(n => n.textContent).join(' ');
  assert.doesNotMatch(wizardText, /\+1234567890/, 'Stale code step from account_slow was not rendered');

  window.AdminV2.getJson = originalGetJson;

  // 12. Regression: Resend code with object-shaped error payload displays human-readable message, not [object Object]
  existingAccounts.push({ account_id: 'acc_resend_err', label: 'Resend Error Account', authorized: false });
  try {
    window.AdminV2.getJson = async function (path) {
      if (path.includes('account_id=acc_resend_err')) {
        return { status: 'in_progress', login_state: 'code_needed', phone_mask: '+1234567890', expires_in_seconds: 300 };
      }
      return originalGetJson.call(this, path);
    };

    await elements.telegramAccountsRefreshBtn.listeners.click();
    const resendErrCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const resendErrCard = resendErrCards.find(card => descendants(card, n => (n.textContent || '').includes('acc_resend_err')).length > 0);
    assert.ok(resendErrCard, 'resendErrCard found');
    const resendErrLoginBtn = descendants(resendErrCard, n => n.tagName === 'button' && n.textContent === 'Log in')[0];
    assert.ok(resendErrLoginBtn, 'resendErrLoginBtn found');

    await resendErrLoginBtn.listeners.click();
    assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_resend_err');

    const resendBtn = descendants(elements.telegramAuthBody, n => n.tagName === 'button' && n.textContent === 'Resend code')[0];
    assert.ok(resendBtn, 'Resend code button found in code step');

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/resend')) {
        return {
          ok: false,
          status: 400,
          async json() {
            return {
              ok: false,
              error: {
                code: 'request_failed',
                message: 'Maximum verification code resends exceeded. Please restart login.',
                retryable: false,
                correlation_id: 'corr_resend_err_1',
              },
            };
          },
        };
      }
      return null;
    };

    await resendBtn.listeners.click();

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on resend failure');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.match(elements.telegramAuthBanner.textContent, /Maximum verification code resends exceeded/i, 'Banner displays normalized message from error object');
    assert.equal(resendBtn.disabled, false, 'Resend button re-enabled on error');
    assert.equal(resendBtn.textContent, 'Resend code');

    // Deterministic case: resend returns HTTP 200 with {ok:false, error:{message:"Maximum verification code resends exceeded"}},
    // exercising the branch handling a resolved failure object.
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/resend')) {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              ok: false,
              error: {
                message: 'Maximum verification code resends exceeded',
              },
            };
          },
        };
      }
      return null;
    };

    await resendBtn.listeners.click();

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on resolved resend failure');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.equal(elements.telegramAuthBanner.textContent, 'Maximum verification code resends exceeded', 'Banner displays human-readable string from resolved failure object');
    assert.equal(resendBtn.disabled, false, 'Resend button re-enabled on error');
    assert.equal(resendBtn.textContent, 'Resend code');

    // Case C: Resend returns HTTP 420 with nested FloodWait metadata in error.flood_wait_seconds
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/resend')) {
        return {
          ok: false,
          status: 420,
          async json() {
            return {
              ok: false,
              error: {
                code: 'flood_wait',
                message: 'A wait of 45 seconds is required before sending another code.',
                flood_wait_seconds: 45,
              },
            };
          },
        };
      }
      return null;
    };

    await resendBtn.listeners.click();

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner shown on nested flood wait');
    assert.match(elements.telegramAuthBanner.className, /state-warning/, 'Banner shows warning for flood wait');
    assert.match(elements.telegramAuthBanner.textContent, /45 seconds is required/, 'Banner displays message from nested error');
    assert.equal(resendBtn.disabled, true, 'Resend button disabled during countdown');
    assert.equal(resendBtn.textContent, 'Wait 45s', 'Resend button displays initial countdown seconds');

    // Case D: Resend returns HTTP 200 with resolved failure and nested flood_wait_seconds without message (fallback message)
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/resend')) {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              ok: false,
              error: {
                flood_wait_seconds: 30,
              },
            };
          },
        };
      }
      return null;
    };

    await resendBtn.listeners.click();

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner shown on resolved nested flood wait');
    assert.match(elements.telegramAuthBanner.className, /state-warning/, 'Banner shows warning for flood wait');
    assert.match(elements.telegramAuthBanner.textContent, /Please wait 30s before requesting a code\./, 'Banner displays fallback message with flood wait seconds');
    assert.equal(resendBtn.disabled, true, 'Resend button disabled during countdown');
    assert.equal(resendBtn.textContent, 'Wait 30s', 'Resend button displays initial countdown seconds');

    elements.telegramAuthClose.listeners.click();
    assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');
  } finally {
    postFetchHandler = null;
    window.AdminV2.getJson = originalGetJson;
  }

  // 13. Regression: Code submission with object-shaped error payload displays human-readable message, not [object Object]
  existingAccounts.push({ account_id: 'acc_code_err', label: 'Code Error Account', authorized: false });
  try {
    window.AdminV2.getJson = async function (path) {
      if (path.includes('account_id=acc_code_err')) {
        return { status: 'in_progress', login_state: 'code_needed', phone_mask: '+1234567890', expires_in_seconds: 300 };
      }
      return originalGetJson.call(this, path);
    };

    await elements.telegramAccountsRefreshBtn.listeners.click();
    const codeErrCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const codeErrCard = codeErrCards.find(card => descendants(card, n => (n.textContent || '').includes('acc_code_err')).length > 0);
    assert.ok(codeErrCard, 'codeErrCard found');
    const codeErrLoginBtn = descendants(codeErrCard, n => n.tagName === 'button' && n.textContent === 'Log in')[0];
    assert.ok(codeErrLoginBtn, 'codeErrLoginBtn found');

    await codeErrLoginBtn.listeners.click();
    assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_code_err');

    const codeForm = descendants(elements.telegramAuthBody, n => n.id === 'tgCodeForm')[0];
    const codeInput = descendants(elements.telegramAuthBody, n => n.id === 'tgCodeInput')[0];
    const verifySubmitBtn = descendants(elements.telegramAuthBody, n => n.tagName === 'button' && n.textContent === 'Verify code')[0];
    assert.ok(codeForm, 'tgCodeForm found');
    assert.ok(codeInput, 'tgCodeInput found');
    assert.ok(verifySubmitBtn, 'verifySubmitBtn found');

    // Case A: HTTP 400 with object-shaped error payload { ok: false, error: { code: 'invalid_code', message: '...' } }
    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: false,
          status: 400,
          async json() {
            return {
              ok: false,
              error: {
                code: 'invalid_code',
                message: 'Invalid verification code. Please check the code and try again.',
                retryable: false,
                correlation_id: 'corr_code_err_1',
              },
            };
          },
        };
      }
      return null;
    };

    codeInput.value = '99999';
    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on code rejection');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.match(elements.telegramAuthBanner.textContent, /Invalid verification code\. Please check the code and try again\./, 'Banner displays human-readable error from error object');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    // Case B: HTTP 200 with resolved failure object { ok: false, error: { message: 'Verification code expired' } }
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              ok: false,
              error: {
                message: 'Verification code has expired. Please request a new code.',
              },
            };
          },
        };
      }
      return null;
    };

    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on resolved code failure');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.equal(elements.telegramAuthBanner.textContent, 'Verification code has expired. Please request a new code.', 'Banner displays human-readable string from resolved failure object');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    // Case C: HTTP 400 with detail-shaped error payload { ok: false, error: { detail: 'Code expired or incorrect' } }
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: false,
          status: 400,
          async json() {
            return {
              ok: false,
              error: {
                detail: 'Code expired or incorrect',
              },
            };
          },
        };
      }
      return null;
    };

    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on detail-shaped code error');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.equal(elements.telegramAuthBanner.textContent, 'Code expired or incorrect', 'Banner displays human-readable detail from error object');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    // Case D: HTTP 200 with ok:true and unknown code-verification state with message
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              ok: true,
              state: 'unknown_hardware_token_required',
              message: 'Hardware security key verification is not supported in this client.',
            };
          },
        };
      }
      return null;
    };

    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on unknown verification state');
    assert.match(elements.telegramAuthBanner.className, /state-error/, 'Banner shows error on unknown state');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.equal(elements.telegramAuthBanner.textContent, 'Hardware security key verification is not supported in this client.', 'Banner displays human-readable error from unknown state');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    // Case E: HTTP 200 with ok:true and unknown state without message (fallback message)
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              ok: true,
              state: 'unrecognized_state',
            };
          },
        };
      }
      return null;
    };

    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on unknown verification state fallback');
    assert.match(elements.telegramAuthBanner.className, /state-error/, 'Banner shows error on unknown state fallback');
    assert.equal(elements.telegramAuthBanner.textContent, 'Verification code failed.', 'Banner displays fallback error message');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    // Case F: HTTP 422 with FastAPI-style detail array [{ loc, msg, type }]
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: false,
          status: 422,
          async json() {
            return {
              detail: [
                {
                  loc: ['body', 'code'],
                  msg: "String should match pattern '^[0-9]+$'",
                  type: 'string_pattern_mismatch',
                },
              ],
            };
          },
        };
      }
      return null;
    };

    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on FastAPI-style detail array');
    assert.match(elements.telegramAuthBanner.className, /state-error/, 'Banner shows error on 422');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.equal(elements.telegramAuthBanner.textContent, "String should match pattern '^[0-9]+$'", 'Banner displays extracted msg from FastAPI detail array');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    // Case G: HTTP 400 with nested error.detail array
    elements.telegramAuthBanner.hidden = true;
    elements.telegramAuthBanner.textContent = '';

    postFetchHandler = async (url) => {
      if (url.includes('/telegram/auth/code')) {
        return {
          ok: false,
          status: 400,
          async json() {
            return {
              error: {
                detail: [
                  {
                    msg: 'Verification code must be numeric',
                  },
                ],
              },
            };
          },
        };
      }
      return null;
    };

    await codeForm.listeners.submit({ preventDefault() {} });

    assert.equal(elements.telegramAuthBanner.hidden, false, 'Auth banner is shown on nested error.detail array');
    assert.doesNotMatch(elements.telegramAuthBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.equal(elements.telegramAuthBanner.textContent, 'Verification code must be numeric', 'Banner displays extracted msg from nested detail array');
    assert.equal(verifySubmitBtn.disabled, false, 'Verify button re-enabled on error');
    assert.equal(verifySubmitBtn.textContent, 'Verify code');

    elements.telegramAuthClose.listeners.click();
    assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');
  } finally {
    postFetchHandler = null;
    window.AdminV2.getJson = originalGetJson;
  }

  // 14. UI Contract: Telegram account removal uses styled in-app modal instead of browser-native confirm()
  existingAccounts.push({ account_id: 'acc_styled_remove', label: 'Styled Removal Account', authorized: true });
  try {
    await elements.telegramAccountsRefreshBtn.listeners.click();
    const styledCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const targetCard = styledCards.find(card => descendants(card, n => (n.textContent || '').includes('acc_styled_remove')).length > 0);
    assert.ok(targetCard, 'targetCard for acc_styled_remove found');

    const removeBtn = descendants(targetCard, n => n.tagName === 'button' && n.textContent === 'Remove')[0];
    assert.ok(removeBtn, 'Remove button found on account card');

    removeBtn.focus();
    assert.equal(document.activeElement, removeBtn, 'Remove button is activeElement before opening');

    // 14.1 Open styled removal modal: verify confirm() is NOT called and dialog is opened
    await removeBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Styled confirmation dialog is opened');
    assert.match(elements.telegramAccountActionTitle.textContent, /Remove Telegram Account/i, 'Dialog title reflects account removal');

    const actionText = descendants(elements.telegramAccountActionBody, () => true).map(n => n.textContent).join(' ');
    assert.match(actionText, /Styled Removal Account/i, 'Dialog body includes account label');
    assert.match(actionText, /acc_styled_remove/i, 'Dialog body includes account ID');
    assert.match(actionText, /local session file will be removed/i, 'Dialog body warns session file will be removed');
    assert.match(actionText, /Channels and media files will NOT be deleted/i, 'Dialog body reassures media/channels are preserved');

    const cancelActionBtn = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionCancel')[0];
    const confirmActionBtn = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionConfirm')[0];
    assert.ok(cancelActionBtn, 'Cancel action button exists');
    assert.ok(confirmActionBtn, 'Remove action button exists');
    assert.equal(document.activeElement, cancelActionBtn, 'Cancel button received initial focus for safe keyboard defaults');

    // 14.2 Cancel action: closes dialog without sending DELETE, restores focus to opener
    const deleteCallsCountBeforeCancel = postCalls.filter(c => c.method === 'DELETE').length;
    cancelActionBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed on Cancel click');
    assert.equal(document.activeElement, removeBtn, 'Focus restored to opener after Cancel');
    assert.equal(postCalls.filter(c => c.method === 'DELETE').length, deleteCallsCountBeforeCancel, 'No DELETE request was sent on Cancel');

    // 14.3 Escape / dialog cancel event: closes dialog and restores focus
    await removeBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog opened for Escape test');
    elements.telegramAccountActionDialog.listeners.cancel({ preventDefault() {} });
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed on Escape/cancel event');
    assert.equal(document.activeElement, removeBtn, 'Focus restored to opener after Escape');
    assert.equal(postCalls.filter(c => c.method === 'DELETE').length, deleteCallsCountBeforeCancel, 'No DELETE request on Escape');

    // 14.4 Close button click: closes dialog and restores focus
    await removeBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog opened for close button test');
    elements.telegramAccountActionClose.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed via close button');
    assert.equal(document.activeElement, removeBtn, 'Focus restored to opener after close button');
    assert.equal(postCalls.filter(c => c.method === 'DELETE').length, deleteCallsCountBeforeCancel, 'No DELETE request on close button');

    // 14.5 Error path: server failure displays safe message in banner, keeps dialog open, re-enables buttons
    await removeBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog opened for error test');

    const errConfirmBtn = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionConfirm')[0];
    const errCancelBtn = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionCancel')[0];

    postFetchHandler = async (url, options) => {
      if (options.method === 'DELETE' && url.includes('acc_styled_remove')) {
        return {
          ok: false,
          status: 500,
          async json() {
            return {
              ok: false,
              error: {
                message: 'Failed to unlink session file: disk read-only',
              },
            };
          },
        };
      }
      return null;
    };

    await errConfirmBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog remains open on deletion failure');
    assert.equal(elements.telegramAccountActionBanner.hidden, false, 'Account action banner shown on error');
    assert.doesNotMatch(elements.telegramAccountActionBanner.textContent, /\[object Object\]/, 'Banner must not display [object Object]');
    assert.match(elements.telegramAccountActionBanner.textContent, /Failed to unlink session file/i, 'Banner displays error message');
    assert.equal(errConfirmBtn.disabled, false, 'Remove button re-enabled after failure');
    assert.equal(errConfirmBtn.textContent, 'Remove');
    assert.equal(errCancelBtn.disabled, false, 'Cancel button re-enabled after failure');

    // 14.6 Successful deletion: sends DELETE to exact endpoint with confirm=true, closes dialog, shows notice
    postFetchHandler = async (url, options) => {
      if (options.method === 'DELETE' && url.includes('acc_styled_remove')) {
        existingAccounts = existingAccounts.filter(a => a.account_id !== 'acc_styled_remove');
        return {
          ok: true,
          status: 200,
          async json() {
            return { ok: true, account_id: 'acc_styled_remove' };
          },
        };
      }
      return null;
    };

    const deleteCallsCountBeforeSuccess = postCalls.filter(c => c.method === 'DELETE').length;
    await errConfirmBtn.listeners.click();

    const deleteCalls = postCalls.filter(c => c.method === 'DELETE');
    assert.equal(deleteCalls.length, deleteCallsCountBeforeSuccess + 1, 'DELETE request was sent');
    const lastDelete = deleteCalls[deleteCalls.length - 1];
    assert.equal(lastDelete.path, '/admin/api/v2/telegram/accounts/acc_styled_remove?confirm=true', 'Delete endpoint matches expected contract with confirm=true');

    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed on successful removal');
    assert.equal(elements.sourcesNotice.hidden, false, 'Sources notice shown for removed account');
    const noticeText = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
    assert.match(noticeText, /Styled Removal Account.*removed/i, 'Notice confirms account removal');

    const remainingCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const removedCard = remainingCards.find(card => descendants(card, n => (n.textContent || '').includes('acc_styled_remove')).length > 0);
    assert.equal(removedCard, undefined, 'acc_styled_remove is no longer rendered in account list');

    // Finding 2: After successful removal and card refresh, focus moves to extant Accounts control, not the removed button
    assert.equal(document.activeElement, elements.telegramLoginButton, 'Focus moved to extant Accounts control (telegramLoginButton) after successful removal');
    assert.notEqual(document.activeElement, removeBtn, 'Focus was not restored to the removed account button');
    assert.equal(removeBtn.isConnected, false, 'The removed button is no longer connected');

    // 14.7 Finding 1: In-flight dialog dismissal (via Escape) surfaces server failure in global notice after closure
    existingAccounts.push({ account_id: 'acc_race_fail', label: 'Race Fail Account', authorized: true });
    await elements.telegramAccountsRefreshBtn.listeners.click();

    const raceFailCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const raceFailCard = raceFailCards.find(c => descendants(c, n => (n.textContent || '').includes('acc_race_fail')).length > 0);
    assert.ok(raceFailCard, 'raceFailCard found');
    const raceFailRemoveBtn = descendants(raceFailCard, n => n.tagName === 'button' && n.textContent === 'Remove')[0];
    assert.ok(raceFailRemoveBtn, 'Remove button found on raceFailCard');

    await raceFailRemoveBtn.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog open for acc_race_fail');

    const raceFailConfirmBtn = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionConfirm')[0];
    assert.ok(raceFailConfirmBtn, 'Confirm button found');

    const raceFailStart = createDeferred();
    const raceFailGate = createDeferred();

    postFetchHandler = async (url, options) => {
      if (options.method === 'DELETE' && url.includes('acc_race_fail')) {
        raceFailStart.resolve();
        await raceFailGate.promise;
        return {
          ok: false,
          status: 500,
          async json() {
            return {
              ok: false,
              error: {
                message: 'Internal server error while removing acc_race_fail',
              },
            };
          },
        };
      }
      return null;
    };

    elements.sourcesNotice.hidden = true;
    elements.sourcesNotice.textContent = '';

    const raceFailPromise = raceFailConfirmBtn.listeners.click();
    await raceFailStart.promise;

    // Dismiss dialog while DELETE is in flight
    elements.telegramAccountActionDialog.listeners.cancel({ preventDefault() {} });
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed on Escape while DELETE is in flight');

    // Resolve DELETE with error
    raceFailGate.resolve();
    await raceFailPromise;

    // Dialog remains closed
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog remains closed after late rejection');
    // Error surfaced in global notice
    assert.equal(elements.sourcesNotice.hidden, false, 'Global notice is shown for late failure after closure');
    assert.match(elements.sourcesNotice.className, /state-error/, 'Global notice has error state');
    const raceFailNoticeText = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
    assert.match(raceFailNoticeText, /Race Fail Account|acc_race_fail/i, 'Global notice mentions account');
    assert.match(raceFailNoticeText, /Internal server error while removing acc_race_fail/i, 'Global notice contains error message');

    // 14.8 Finding 1: Late success of dismissed removal does NOT close a newer dialog for another account
    existingAccounts.push(
      { account_id: 'acc_race_a', label: 'Race Account A', authorized: true },
      { account_id: 'acc_race_b', label: 'Race Account B', authorized: true }
    );
    await elements.telegramAccountsRefreshBtn.listeners.click();

    const multiCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const cardRaceA = multiCards.find(c => descendants(c, n => (n.textContent || '').includes('acc_race_a')).length > 0);
    const cardRaceB = multiCards.find(c => descendants(c, n => (n.textContent || '').includes('acc_race_b')).length > 0);
    assert.ok(cardRaceA, 'cardRaceA found');
    assert.ok(cardRaceB, 'cardRaceB found');

    const removeBtnA = descendants(cardRaceA, n => n.tagName === 'button' && n.textContent === 'Remove')[0];
    const removeBtnB = descendants(cardRaceB, n => n.tagName === 'button' && n.textContent === 'Remove')[0];
    assert.ok(removeBtnA, 'removeBtnA found');
    assert.ok(removeBtnB, 'removeBtnB found');

    // Open removal dialog for acc_race_a
    await removeBtnA.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog open for acc_race_a');

    const confirmBtnA = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionConfirm')[0];
    assert.ok(confirmBtnA, 'Confirm button found for acc_race_a');

    const raceAStart = createDeferred();
    const raceAGate = createDeferred();

    postFetchHandler = async (url, options) => {
      if (options.method === 'DELETE' && url.includes('acc_race_a')) {
        raceAStart.resolve();
        await raceAGate.promise;
        existingAccounts = existingAccounts.filter(a => a.account_id !== 'acc_race_a');
        return {
          ok: true,
          status: 200,
          async json() {
            return { ok: true, account_id: 'acc_race_a' };
          },
        };
      }
      return null;
    };

    const deletePromiseA = confirmBtnA.listeners.click();
    await raceAStart.promise;

    // Dismiss dialog while DELETE for acc_race_a is in flight
    elements.telegramAccountActionClose.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed via X while DELETE A is in flight');

    // User opens removal dialog for acc_race_b
    await removeBtnB.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog reopened for acc_race_b');
    const actionTextB = descendants(elements.telegramAccountActionBody, () => true).map(n => n.textContent).join(' ');
    assert.match(actionTextB, /acc_race_b/i, 'Dialog body reflects acc_race_b');

    // Now resolve late DELETE for acc_race_a
    raceAGate.resolve();
    await deletePromiseA;

    // acc_race_b dialog MUST REMAIN OPEN! Late success of acc_race_a must not close newer dialog
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog for acc_race_b remains open; late success does not close newer dialog');
    const actionTextBAfter = descendants(elements.telegramAccountActionBody, () => true).map(n => n.textContent).join(' ');
    assert.match(actionTextBAfter, /acc_race_b/i, 'Dialog body still reflects acc_race_b');

    // Global notice announces acc_race_a removal
    assert.equal(elements.sourcesNotice.hidden, false, 'Global notice shown for background completion of acc_race_a');
    const noticeTextA = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
    assert.match(noticeTextA, /Race Account A.*removed/i, 'Notice announces acc_race_a removal');

    // Cancel dialog for acc_race_b to close clean
    const cancelBtnB = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionCancel')[0];
    cancelBtnB.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed via Cancel');

    // 14.9 Finding 1: Late failure of dismissed removal does NOT update banner of newer dialog for another account
    existingAccounts.push(
      { account_id: 'acc_race_c', label: 'Race Account C', authorized: true },
      { account_id: 'acc_race_d', label: 'Race Account D', authorized: true }
    );
    await elements.telegramAccountsRefreshBtn.listeners.click();

    const multiCards2 = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
    const cardRaceC = multiCards2.find(c => descendants(c, n => (n.textContent || '').includes('acc_race_c')).length > 0);
    const cardRaceD = multiCards2.find(c => descendants(c, n => (n.textContent || '').includes('acc_race_d')).length > 0);
    assert.ok(cardRaceC, 'cardRaceC found');
    assert.ok(cardRaceD, 'cardRaceD found');

    const removeBtnC = descendants(cardRaceC, n => n.tagName === 'button' && n.textContent === 'Remove')[0];
    const removeBtnD = descendants(cardRaceD, n => n.tagName === 'button' && n.textContent === 'Remove')[0];

    // Open dialog for acc_race_c
    await removeBtnC.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog open for acc_race_c');

    const confirmBtnC = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionConfirm')[0];
    const raceCStart = createDeferred();
    const raceCGate = createDeferred();

    postFetchHandler = async (url, options) => {
      if (options.method === 'DELETE' && url.includes('acc_race_c')) {
        raceCStart.resolve();
        await raceCGate.promise;
        return {
          ok: false,
          status: 500,
          async json() {
            return {
              ok: false,
              error: {
                message: 'Disk I/O error during acc_race_c removal',
              },
            };
          },
        };
      }
      return null;
    };

    const deletePromiseC = confirmBtnC.listeners.click();
    await raceCStart.promise;

    // Dismiss dialog while DELETE C is in flight
    elements.telegramAccountActionClose.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed via X while DELETE C in flight');

    // Open removal dialog for acc_race_d
    await removeBtnD.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog opened for acc_race_d');
    assert.equal(elements.telegramAccountActionBanner.hidden, true, 'Banner in dialog D is initially hidden');
    assert.equal(elements.telegramAccountActionBanner.textContent, '', 'Banner in dialog D is initially empty');

    // Let DELETE C fail
    raceCGate.resolve();
    await deletePromiseC;

    // Dialog for acc_race_d remains open
    assert.equal(elements.telegramAccountActionDialog.open, true, 'Dialog for acc_race_d remains open');
    // Stale error must NOT pollute acc_race_d banner
    assert.equal(elements.telegramAccountActionBanner.hidden, true, 'Dialog D banner must remain hidden; stale error discarded');
    assert.equal(elements.telegramAccountActionBanner.textContent, '', 'Dialog D banner must remain empty; stale error discarded');

    // Failure of acc_race_c surfaced in global notice
    assert.equal(elements.sourcesNotice.hidden, false, 'Global notice is shown for late failure of acc_race_c');
    const noticeTextC = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
    assert.match(noticeTextC, /acc_race_c/i, 'Global notice mentions failed account acc_race_c');
    assert.match(noticeTextC, /Disk I\/O error during acc_race_c removal/i, 'Global notice contains error message');

    // Cancel dialog for acc_race_d
    const cancelBtnD = descendants(elements.telegramAccountActionBody, n => n.id === 'telegramAccountActionCancel')[0];
    cancelBtnD.listeners.click();
    assert.equal(elements.telegramAccountActionDialog.open, false, 'Dialog closed via Cancel');
  } finally {
    postFetchHandler = null;
    window.AdminV2.getJson = originalGetJson;
  }

  process.stdout.write('PASS: Telegram Add Account dialog screen replaces prompt, validates inline, and creates account only on Continue\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
