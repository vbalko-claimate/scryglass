#!/usr/bin/env node
// Headless test for the overlay's PEEK state machine.
//
//   node tools/overlay_peek_test.mjs static/overlay.html
//
// The overlay is a fullscreen click-through window driven by a native
// cursor-forward callback, so it can't be exercised in a normal browser tab and
// nothing here was covered by a test. That gap shipped a real regression in
// v0.8.17: the out-of-match "rest as the pill" change derived its transition
// from `inMatch`, which SIX other places write — the WS handlers flip it the
// instant a match starts, so by the time the /match-status poll ran the
// transition was already invisible and the overlay stayed a pill for the entire
// match. Run this against that tag and it fails on "match start expands".
//
// Stubs the DOM / fetch / WebSocket, evals the page script, then drives the real
// match lifecycle and asserts the panel is expanded or collapsed at each step.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

function makeEl(id) {
  const classes = new Set();
  return {
    id, style: {}, textContent: '', dataset: {}, offsetLeft: 8, offsetTop: 8,
    offsetWidth: 340, offsetHeight: 120,
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      contains: c => classes.has(c),
      toggle: (c, on) => (on === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c)) : (on ? classes.add(c) : classes.delete(c))),
    },
    _classes: classes,
    addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
    appendChild() {}, remove() {}, insertBefore() {}, cloneNode() { return makeEl(id); },
  };
}
const els = new Map();
const getEl = id => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); };

let matchActive = false;
let wsInstance = null;
const sandbox = {
  console,
  document: {
    getElementById: getEl, createElement: makeEl, addEventListener() {},
    querySelector: () => null, querySelectorAll: () => [], body: makeEl('body'),
    documentElement: makeEl('html'),
  },
  window: { addEventListener() {}, location: { hostname: 'localhost', hash: '' } },
  location: { hostname: 'localhost', hash: '' },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: () => 0,
  fetch: async (url) => ({
    ok: true,
    json: async () => (url.includes('/match-status') ? { active: matchActive } : {}),
  }),
  WebSocket: class { constructor() { wsInstance = this; } send() {} close() {} },
  navigator: { userAgent: 'Mac' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  JSON, Math, Date, Promise, Object, Array, String, Number, Boolean, Error,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(script, sandbox);

const peeked = () => getEl('overlay')._classes.has('peek');
const tick = () => vm.runInContext('tick()', sandbox);
const wsSend = (msg) => wsInstance && wsInstance.onmessage && wsInstance.onmessage({ data: JSON.stringify(msg) });

let failures = 0;
const check = (label, got, want) => {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: peeked=${got} (want ${want})`);
};

const run = async () => {
  // 1. Boot OUT of a match → rests as the pill.
  matchActive = false;
  await tick();
  check('boot out of match rests as pill', peeked(), true);

  // 2. Match starts. The WS handler flips the in-match flag FIRST (this is what
  //    really happens live) and only then does the poll see it.
  vm.runInContext('connect()', sandbox);
  wsSend({ type: 'match_start' });
  matchActive = true;
  await tick();
  check('match start expands the panel', peeked(), false);

  // 3. Hovering in a match shrinks it, leaving does not strand it.
  vm.runInContext('overlayCursor(100, 50)', sandbox);
  check('in-match hover shrinks', peeked(), true);
  vm.runInContext('overlayCursor(9999, 9999)', sandbox);
  check('in-match unhover restores', peeked(), false);

  // 4. Match ends → back to the pill.
  wsSend({ type: 'match_end', data: {} });
  matchActive = false;
  await tick();
  check('match end rests as pill', peeked(), true);

  // 5. Out of match the polarity is REVERSED: hover opens.
  vm.runInContext('overlayCursor(100, 50)', sandbox);
  check('out-of-match hover opens', peeked(), false);
  vm.runInContext('overlayCursor(9999, 9999)', sandbox);
  check('out-of-match unhover re-pills', peeked(), true);

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nall good');
  process.exit(failures ? 1 : 0);
};
run();
