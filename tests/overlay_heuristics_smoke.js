// The overlay's second micro-line: what the deck's heuristics cover (strategy_info.heuristics).
// Pulls the one function out of overlay.html and runs it against a plain element object.
// Run: node tests/overlay_heuristics_smoke.js static/overlay.html
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('let heuristicsExpanded');
const b = html.indexOf('function updateOppLine');
assert(a > 0 && b > a, 'updateHeuristicsLine not found');
const el = { style: {}, textContent: '' };
const ctx = { document: { getElementById: () => el } };
vm.createContext(ctx);
vm.runInContext(html.slice(a, b) + '; this.update = updateHeuristicsLine;', ctx);
const show = (h) => { ctx.update(h === null ? null : { heuristics: h }); return { text: el.textContent, hidden: el.style.display === 'none', color: el.style.color }; };
const t = (name, f) => { f(); console.log('ok  ', name); };

t('no info -> hidden', () => assert(show(null).hidden));
t('strategy without heuristics block -> hidden', () => assert(show(undefined === 1 ? 0 : null).hidden));
t('tutor without list -> warning colour + count', () => {
  const r = show({ rules: 5, search_preferences: 0, tutors_known: true, tutors_covered: [], tutors_uncovered: ['Lively Dirge'] });
  assert(!r.hidden && /no search-target list: 1 card$/.test(r.text) && r.color === '#ffd166', JSON.stringify(r));
});
t('all tutors listed -> ok line', () => {
  const r = show({ rules: 5, search_preferences: 2, tutors_known: true, tutors_covered: ['A'], tutors_uncovered: [] });
  assert(/search targets ✓ \(2\)/.test(r.text) && r.color !== '#ffd166', JSON.stringify(r));
});
t('no tutors in the deck -> says so, no warning', () => {
  const r = show({ rules: 5, search_preferences: 0, tutors_known: true, tutors_covered: [], tutors_uncovered: [] });
  assert(/no tutors/.test(r.text) && r.color !== '#ffd166', JSON.stringify(r));
});
t('click expands to names + rule count', () => {
  ctx.update({ heuristics: { rules: 7, search_preferences: 0, tutors_known: true, tutors_covered: [], tutors_uncovered: ['Lively Dirge', 'Other'] } });
  el.onclick();
  assert(/7 rules/.test(el.textContent) && /missing: Lively Dirge, Other/.test(el.textContent), el.textContent);
  el.onclick();
  assert(!/missing/.test(el.textContent), 'second click collapses');
});
