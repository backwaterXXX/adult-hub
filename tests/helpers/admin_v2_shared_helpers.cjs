'use strict';
// Fake window.AdminV2 helper surface for the browser contract tests.
//
// The shared helpers are extracted from the real admin-v2.js module so the sandboxes
// exercise the same implementations the panel ships, instead of a second copy of them.
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', '..', 'resolver', 'static', 'admin-v2', 'admin-v2.js'), 'utf8');
const start = source.indexOf('  // Shared Admin V2 helpers.');
const end = source.indexOf('  window.AdminV2 = Object.freeze({');
if (start < 0 || end <= start) throw new Error('admin-v2.js no longer exposes the shared helper block');

function createAdminV2Helpers(document, timers = {}) {
  const schedule = timers.setTimeout || setTimeout;
  const cancel = timers.clearTimeout || clearTimeout;
  const build = new Function('document', '$', 'setTimeout', 'clearTimeout', `${source.slice(start, end)}
    return { create, el: create, setText, announce, safeId, scrub, titleCase, detailField, formatDuration, scheduleSync };`);
  return build(document, (selector, root = document) => root.querySelector(selector), schedule, cancel);
}

module.exports = { createAdminV2Helpers };
