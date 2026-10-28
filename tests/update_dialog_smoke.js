// Smoke test for the in-app update dialog (static/shell.js: window.scryglassUpdate).
// Run: node tests/update_dialog_smoke.js
// The dialog is sliced out of shell.js (the file is an IIFE that wires a whole page) and run
// against a minimal fake DOM and a fake window.__TAURI__.
const fs = require('fs'), vm = require('vm'), assert = require('assert');

const src = fs.readFileSync(__dirname + '/../static/shell.js', 'utf8');
const a = src.indexOf('  window.scryglassUpdate = async function () {');
const b = src.indexOf('  window.showUpdateNotice = function');
assert(a > 0 && b > a, 'could not find scryglassUpdate in shell.js');
const code = src.slice(a, b);

function makeEnv(invoke) {
  const byId = {};
  class El {
    constructor() { this.children = []; this.style = {}; this.listeners = {}; this._text = ''; this.id = ''; }
    set textContent(v) { this._text = v; }
    get textContent() { return this._text; }
    append(...k) { this.children.push(...k); k.forEach((c) => (c.parent = this)); }
    appendChild(k) { this.append(k); return k; }
    replaceChildren(...k) { this.children = []; this.append(...k); }
    addEventListener(t, f) { this.listeners[t] = f; }
    click() { return this.listeners.click && this.listeners.click(); }
    remove() { if (this.id) delete byId[this.id]; this.removed = true; }
    all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  }
  const body = new El();
  const doc = {
    createElement: () => new El(),
    getElementById: (id) => byId[id] || null,
    body: Object.assign(body, {
      appendChild(k) { El.prototype.appendChild.call(body, k); if (k.id) byId[k.id] = k; return k; },
    }),
  };
  const listeners = {};
  const win = {
    __TAURI__: {
      core: { invoke },
      event: { listen: async (name, cb) => { listeners[name] = cb; return () => { delete listeners[name]; }; } },
    },
  };
  const ctx = { window: win, document: doc };
  vm.runInNewContext(code, ctx);
  const dialog = () => byId['shell-update-dialog'];
  const buttons = () => dialog().all().filter((e) => e.listeners.click);
  const texts = () => dialog().all().map((e) => e.textContent).filter(Boolean).join(' | ');
  return { win, dialog, buttons, texts, listeners };
}

(async () => {
  // 1) up to date
  let e = makeEnv(async () => ({ status: 'uptodate' }));
  await e.win.scryglassUpdate();
  assert(/up to date/i.test(e.texts()), e.texts());
  e.buttons()[0].click();
  assert(!e.dialog(), 'Close removes the dialog');

  // 2) a match is live: no install offered
  e = makeEnv(async () => ({ status: 'match', version: '9.9.9' }));
  await e.win.scryglassUpdate();
  assert(/match is in progress/i.test(e.texts()) && /9\.9\.9/.test(e.texts()), e.texts());
  assert.deepStrictEqual(e.buttons().map((x) => x.textContent), ['Close']);

  // 3) download with progress, then an explicit restart
  const calls = [];
  e = makeEnv(async (cmd) => {
    calls.push(cmd);
    if (cmd === 'update_start') {
      e.listeners['update-progress']({ payload: { downloaded: 5 * 1048576, total: 10 * 1048576 } });
      assert(/50%/.test(e.texts()), 'progress text: ' + e.texts());
      return { status: 'ready', version: '9.9.9', notes: 'Fixed things' };
    }
    return null;
  });
  await e.win.scryglassUpdate();
  assert(/9\.9\.9 is ready/.test(e.texts()) && /Fixed things/.test(e.texts()), e.texts());
  assert.deepStrictEqual(e.buttons().map((x) => x.textContent), ['Later', 'Restart now']);
  assert(!calls.includes('update_restart'), 'nothing restarts before the user confirms');
  await e.buttons()[1].click();
  assert(calls.includes('update_restart'), 'Restart now installs and restarts');

  // 4) "Later" closes without restarting
  const calls2 = [];
  e = makeEnv(async (cmd) => { calls2.push(cmd); return { status: 'ready', version: '9.9.9' }; });
  await e.win.scryglassUpdate();
  e.buttons()[0].click();
  assert(!e.dialog() && !calls2.includes('update_restart'));

  // 5) a failing check / install is shown, not swallowed
  e = makeEnv(async () => { throw new Error('network down'); });
  await e.win.scryglassUpdate();
  assert(/Update failed/.test(e.texts()) && /network down/.test(e.texts()), e.texts());
  e = makeEnv(async (cmd) => {
    if (cmd === 'update_restart') throw new Error('A match is in progress — restart when it ends.');
    return { status: 'ready', version: '9.9.9' };
  });
  await e.win.scryglassUpdate();
  await e.buttons()[1].click();
  assert(/not installed/i.test(e.texts()) && /match is in progress/.test(e.texts()), e.texts());

  // 6) a second click while the dialog is open does not stack a second dialog
  e = makeEnv(async () => ({ status: 'uptodate' }));
  await e.win.scryglassUpdate();
  const first = e.dialog();
  await e.win.scryglassUpdate();
  assert(e.dialog() === first);

  console.log('update dialog smoke: OK');
})().catch((err) => { console.error(err); process.exit(1); });
