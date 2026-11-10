'use strict';
// Admin V2 browser scripts are split into classic-script parts. These helpers read the parts in the
// order admin_v2.html loads them, so text contracts see the whole UI and sandboxes run it in sequence.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const STATIC_DIR = path.join(ROOT, 'resolver', 'static', 'admin-v2');
const TEMPLATE = path.join(ROOT, 'resolver', 'templates', 'admin_v2.html');

// Script file names that admin_v2.html loads, in document order.
function templateScripts() {
  const html = fs.readFileSync(TEMPLATE, 'utf8');
  return [...html.matchAll(/<script defer src="\/admin\/assets-v2\/([^"]+\.js)"><\/script>/g)].map(match => match[1]);
}

// Template-ordered script names whose file name starts with the given prefix.
function scriptsWithPrefix(prefix) {
  return templateScripts().filter(name => name.startsWith(prefix));
}

function readScript(name) {
  return fs.readFileSync(path.join(STATIC_DIR, name), 'utf8');
}

// Concatenated source of the Admin V2 parts (admin-v2-*.js), in template order.
function adminV2Source() {
  return scriptsWithPrefix('admin-v2-').map(readScript).join('\n');
}

// Concatenated source of the Sources parts (sources-*.js), in template order.
function sourcesSource() {
  return scriptsWithPrefix('sources-').map(readScript).join('\n');
}

// Index where the classic-script part holding `from` stops: its namespace registration, its freeze, or its
// closing IIFE line. Slices that cut one function out of a part must not run past this point.
function partEndAfter(source, from) {
  const markers = ['\n  // Publish core bindings', '\n  Object.assign(', '\n  Object.freeze(', '\n})();'];
  const found = markers.map(marker => source.indexOf(marker, from)).filter(index => index >= 0);
  return found.length ? Math.min(...found) : -1;
}

// Runs the Admin V2 parts in template order inside one sandbox context.
function runAdminV2Scripts(context) {
  for (const name of scriptsWithPrefix('admin-v2-')) vm.runInNewContext(readScript(name), context);
  return context;
}

// Runs the Sources parts in template order inside one sandbox context.
function runSourcesScripts(context) {
  for (const name of scriptsWithPrefix('sources-')) vm.runInNewContext(readScript(name), context);
  return context;
}

module.exports = { adminV2Source, sourcesSource, runAdminV2Scripts, runSourcesScripts, scriptsWithPrefix, partEndAfter };
