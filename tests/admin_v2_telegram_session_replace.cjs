const assert = require('node:assert/strict');
const fs = require('node:fs');

const telegramUi = fs.readFileSync('resolver/static/admin-v2/sources-telegram.js', 'utf8');
const authRoutes = fs.readFileSync('resolver/admin_v2_telegram_auth.py', 'utf8');

assert.match(authRoutes, /session\/replace-unauthorized"/,
  'the backend exposes the replace-unauthorized route the wizard calls');
assert.match(telegramUi, /'Replace unauthorized session'/,
  'the unauthorized-session wizard offers the replace button');
assert.match(telegramUi, /\/session\/replace-unauthorized`/,
  'the button calls the account-scoped replace endpoint');
assert.match(telegramUi, /window\.confirm\(\s*`Replace the unauthorized Telegram session for account "\$\{target\}"/,
  'replacement asks for confirmation that names the account');
assert.match(telegramUi, /moved to a backup in the same folder and kept/,
  'the confirmation says the old file is kept as a backup');
assert.ok(telegramUi.indexOf('window.confirm(') < telegramUi.indexOf('/session/replace-unauthorized`'),
  'the confirmation runs before the replace request is sent');
assert.match(telegramUi, /await refreshTelegramAuthWizard\(target\);/,
  'success reuses the normal login wizard for the account');
assert.doesNotMatch(telegramUi, /back up or remove/i,
  'the panel no longer tells the operator to move the session file by hand');

console.log('admin_v2_telegram_session_replace.cjs: ok');
