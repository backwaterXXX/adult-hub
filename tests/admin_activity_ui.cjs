const assert = require('node:assert/strict');
const fs = require('node:fs');
const { adminV2Source } = require('./helpers/admin_v2_scripts.cjs');

const html = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const router = adminV2Source();
assert.doesNotMatch(html, /systemActivityView|activityRows|activityChangeRows|activity\.js/,
  'administrative request and change history are removed from the UI');
assert.doesNotMatch(router, /\/api\/v2\/activity|activityChange|activityRows/,
  'the router does not query the retired history screens');
assert.match(router, /route === 'quality'[\s\S]*?return retired\(\)/,
  'Catalog Health report links are redirected into System status');
assert.match(router, /'admin-history'/,
  'legacy administrative history hashes keep a route to System status');
assert.match(html, /id="legacyScreenNotice"/,
  'retired links can show a one-time notice');
console.log('PASS: retired administrative history has no UI or polling and its old route redirects');
