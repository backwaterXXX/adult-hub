const fs = require('node:fs');
const { adminV2Source } = require('./helpers/admin_v2_scripts.cjs');
const assert = require('node:assert/strict');
const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const router = adminV2Source();
assert.doesNotMatch(template, /linkcheck(?:Refresh|Rows|Feedback|Samples)|linkcheck\.js|Link-check report/,
  'the retired link-check report and controller are absent from the admin page');
assert.match(router, /'linkcheck'/, 'old link-check addresses remain recognized by the redirect map');
assert.doesNotMatch(template, /assets-v2\/(?:audits|activity|linkcheck|enrichment)\.js/,
  'removed reports do not load their polling controllers');
console.log('PASS: link-check reports are removed from the UI and old links use the System status redirect');
