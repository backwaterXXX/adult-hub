const fs = require('fs');
const assert = require('assert/strict');

const html = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const router = fs.readFileSync('resolver/static/admin-v2/admin-v2.js', 'utf8');
const activity = fs.readFileSync('resolver/static/admin-v2/activity.js', 'utf8');

assert.match(html, /id="systemActivityView"/);
assert.match(html, /Shared access does not identify an individual operator/);
assert.match(html, /request bodies and credentials are never stored/i);
assert.match(html, /Metadata changes are recorded when the worker commits them/);
assert.match(html, /queued operation plans and schedule changes are recorded with their state update/i);
assert.match(html, /assets-v2\/activity\.js/);
assert.match(router, /\$\('#systemActivityView'\)\.hidden = route !== 'system' \|\| systemTab !== 'activity'/);
assert.match(html, /data-system-tab="activity"/);
assert.match(router, /document\.addEventListener\('DOMContentLoaded', renderLocation, \{ once: true \}\)/,
  'The initial route must dispatch only after deferred feature scripts register their listeners');
assert.match(activity, /\/admin\/api\/v2\/activity/);
assert.match(activity, /endpoint \+ '\/changes\?' \+ params/);
assert.match(activity, /activity_method/);
assert.match(activity, /change_type/);
assert.match(activity, /function safeHref/);
assert.match(activity, /#film\//);
assert.match(activity, /element\.textContent = String\(text\)/);
assert.doesNotMatch(activity, /innerHTML/);
assert.match(activity, /activityPrev/);
assert.match(activity, /activityNext/);
assert.match(activity, /Operation queued/);
assert.match(activity, /Schedule created/);

console.log('PASS: Admin activity history is routed, paginated, accurately scoped, and rendered as text');
