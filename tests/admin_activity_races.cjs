const assert = require('node:assert/strict');
const fs = require('node:fs');
const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert.doesNotMatch(template, /systemActivityView|activityRows|activityChangeRows|assets-v2\/activity\.js/,
  'administrative history cannot start list or change-history requests from the page');
console.log('PASS: retired administrative history has no browser controller or in-flight requests');
