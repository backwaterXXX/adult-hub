const fs = require('node:fs');
const assert = require('node:assert/strict');
const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert.match(template, /id="sourceHistoryPane"/, 'the supported sync history remains under Sources');
assert.match(template, /href="#sources\?tab=history"/, 'sync history has its canonical bookmark');
assert.doesNotMatch(template, /systemSyncActivityView|sync-activity\.js|assets-v2\/activity\.js/,
  'legacy activity polling modules are not loaded by the page');
console.log('PASS: Sources owns sync history and the legacy activity controller is not loaded');
