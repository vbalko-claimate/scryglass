const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));

function makeEnv() {
  const calls = [];
  const els = {};
  const mkEl = (id) => {
    const state = { text: '', classes: new Set() };
    const el = new Proxy(function () {}, {
      get(_, p) {
        if (p === 'classList') return { add: (c) => state.classes.add(c), remove: (c) => state.classes.delete(c), contains: (c) => state.classes.has(c), toggle(){} };
        if (p === 'textContent') return state.text;
        if (p === 'style') return {};
        if (p === '__state') return state;
        if (p === 'children' || p === 'childNodes') return [];
        if (p === 'querySelectorAll') return () => [];
        if (p === 'closest') return () => null;
        return el;
      },
      set(_, p, v) { if (p === 'textContent') state.text = v; return true; },
      apply() { return el; },
    });
    return el;
  };
  const fbBtns = [mkEl('b1'), mkEl('b2'), mkEl('b3')];
  const document = {
    getElementById: (id) => (els[id] ||= mkEl(id)),
    querySelectorAll: (sel) => (sel === '.fb-btn' ? fbBtns : []),
    querySelector: () => mkEl('q'),
    createElement: () => mkEl('c'),
    addEventListener() {},
    body: mkEl('body'),
  };
  let wsInst = null;
  class WebSocket { constructor() { wsInst = this; setTimeout(() => this.onopen && this.onopen(), 0); } send() {} }
  const pending = {};
  const fetch = (url, opts) => {
    if (!/flag-decision|reject-advice/.test(url)) return new Promise(() => {});   // unrelated polling
    calls.push({ url, method: opts && opts.method });
    return new Promise((res) => { pending[url.split('?')[0]] = (body) => res({ json: () => Promise.resolve(body) }); });
  };
  const ctx = { document, WebSocket, fetch, calls, console, localStorage: { getItem: () => null, setItem() {} },
    window: { addEventListener() {}, innerWidth: 1000, innerHeight: 800 }, location: { host: 'x', protocol: 'http:' },
    setTimeout: (f, ms) => { ctx.timers.push([f, ms]); return 0; }, timers: [], setInterval() { return 0; }, clearTimeout() {}, clearInterval() {},
    requestAnimationFrame() {}, performance: { now: () => 0 }, Date, JSON, Math };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(script + '\n;globalThis.__api={connect, setInteractiveMode, sendFeedback, rejectOutcomeText, getSnap:()=>feedbackAdviceSnapshot, setInMatch:(v)=>{inMatch=v}};', ctx);
  return { ctx, calls, pending, fbBtns, els, getWs: () => wsInst };
}

(async () => {
  let ok = true; const check = (c, m) => { if (!c) { ok = false; console.log('FAIL', m); } else console.log('ok  ', m); };
  // 1. advice with seq -> ✗ rejects with that seq, failure shows the reason
  let e = makeEnv(); const api = e.ctx.__api;
  api.connect(); await new Promise(r => setTimeout(r, 5));
  e.getWs().onmessage({ data: JSON.stringify({ type: 'advice', seq: 7, for: { turn: 3 }, data: [{ message: 'Cast Titan', source: 'engine', priority: 'high', action_scores: [], recommended_cards: ['Titan'] }] }) });
  api.setInMatch(true);
  api.setInteractiveMode(true);
  check(api.getSnap() && api.getSnap().seq === 7, 'snapshot carries the push seq');
  api.sendFeedback('down');
  check(e.calls.map(c => c.url).join('|') === '/flag-decision?reason=bad|/reject-advice?seq=7', 'down = flag(bad) then reject with seq: ' + e.calls.map(c => c.url));
  check(e.calls.every(c => c.method === 'POST'), 'both POST');
  e.pending['/reject-advice']({ ok: false, reason: 'no_alternative' });
  await new Promise(r => setTimeout(r, 5));
  check(e.els['interactive-indicator'].__state.text === 'No other option', 'refusal is shown: ' + e.els['interactive-indicator'].__state.text);
  // 2. success re-arms the buttons
  e = makeEnv(); const a2 = e.ctx.__api; a2.connect(); await new Promise(r => setTimeout(r, 5));
  e.getWs().onmessage({ data: JSON.stringify({ type: 'advice', seq: 9, data: [{ message: 'm', source: 'engine', priority: 'high', action_scores: [], recommended_cards: ['x'] }] }) });
  a2.setInMatch(true); a2.setInteractiveMode(true); a2.sendFeedback('down');
  check(e.fbBtns.every(b => b.__state.classes.has('sent')), 'buttons marked sent while pending');
  e.pending['/reject-advice']({ ok: true, recommended: 'Y' }); await new Promise(r => setTimeout(r, 5));
  check(e.fbBtns.every(b => !b.__state.classes.has('sent')), 'buttons re-armed after a successful reject');
  // 3. older server without seq: plain vote
  e = makeEnv(); const a3 = e.ctx.__api; a3.connect(); await new Promise(r => setTimeout(r, 5));
  e.getWs().onmessage({ data: JSON.stringify({ type: 'advice', data: [{ message: 'm', source: 'engine', priority: 'high', action_scores: [], recommended_cards: ['x'] }] }) });
  a3.setInMatch(true); a3.setInteractiveMode(true); a3.sendFeedback('down');
  check(e.calls.length === 1 && e.calls[0].url === '/flag-decision?reason=bad', 'no seq -> only the vote: ' + e.calls.map(c => c.url));
  // 4. up never rejects
  e = makeEnv(); const a4 = e.ctx.__api; a4.connect(); await new Promise(r => setTimeout(r, 5));
  e.getWs().onmessage({ data: JSON.stringify({ type: 'advice', seq: 1, data: [{ message: 'm', source: 'engine', priority: 'high', action_scores: [], recommended_cards: ['x'] }] }) });
  a4.setInMatch(true); a4.setInteractiveMode(true); a4.sendFeedback('up');
  check(e.calls.length === 1 && e.calls[0].url === '/flag-decision?reason=good', 'up -> no reject: ' + e.calls.map(c => c.url));
  // 5. reason text table
  const t = a4.rejectOutcomeText;
  check([t('stale'), t('no_alternative'), t('unsupported_mode'), t('nothing_named'), t('zzz')].join('|') === 'Advice changed — look again|No other option|No alternative here|No pick to replace|Could not change', 'reason texts');
  process.exit(ok ? 0 : 1);
})();
