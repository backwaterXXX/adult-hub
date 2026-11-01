const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const diagnostics = fs.readFileSync('resolver/static/admin-v2/diagnostics.js', 'utf8');
for (const id of ['diagnosticsExport', 'diagnosticsEnable', 'diagnosticsDisable', 'diagnosticsClear', 'diagnosticsRows', 'diagnosticsDetail']) {
  assert.doesNotMatch(html, new RegExp(`id="${id}"`), `retired playback UI must not render ${id}`);
}
assert.doesNotMatch(diagnostics, /diagnostics\/playback\/(?:export|reports|enable|disable|status|summary)/,
  'the System controller must not query or export playback report data');
assert.match(html, /id="systemStatusView"[\s\S]*?id="systemProbeRows"/,
  'service probes remain in the Status tab');
console.log('PASS: playback diagnostics and exports are absent while bounded service probes remain');
