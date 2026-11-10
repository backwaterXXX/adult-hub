const assert = require('node:assert/strict');
const fs = require('node:fs');
const { adminV2Source } = require('./helpers/admin_v2_scripts.cjs');
const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const router = adminV2Source();
assert.doesNotMatch(template, /qualityRows|auditRows|auditFindings|qualityTrend|qualityHistory|auditRefresh|assets-v2\/audits\.js/,
  'manual audit reports and their polling controller are absent');
assert.match(router, /route === 'quality'[^\n]*return retired\(\)/,
  'old manual audit links open System status without activating a retired controller');
assert.match(template, /id="systemStatusView"/, 'the redirect destination is available');
console.log('PASS: manual audit reports and findings views are retired without changing backend automation');
