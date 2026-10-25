// Rules from an older guide generator are marked OUTDATED in the Decks UI, and the user is told
// nothing is regenerated automatically (it costs credit). Pure helpers sliced out of the page.
// Run: node tests/decks_outdated_smoke.js static/decks.html
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('function outdatedPill');
const b = html.indexOf('// What the activated strategy gives the advisor');
assert(a > 0 && b > a, 'outdated helpers not found');
const c = html.indexOf('function heuristicsPanel');
const d = html.indexOf('async function loadDecks');
assert(c > 0 && d > c, 'heuristicsPanel not found');
const ctx = { esc: (t) => String(t) };
vm.createContext(ctx);
vm.runInContext(html.slice(a, b) + html.slice(c, d) + ';this.pill = outdatedPill; this.banner = outdatedBanner; this.panel = heuristicsPanel;', ctx);

let n = 0;
const ok = (c, m) => { assert(c, m); console.log('ok   ' + m); n++; };
ok(/OUTDATED/.test(ctx.pill(true)) && ctx.pill(false) === '', 'the pill shows only for an outdated version');
ok(/Generate/.test(ctx.pill(true)) && /credit/.test(ctx.pill(true)), 'the pill says how to fix it and that it costs credit');

const decks = [
  { deck_id: 'a', activated: true, rules_outdated: true },
  { deck_id: 'b', activated: true, rules_outdated: false },
  { deck_id: 'c', activated: false, rules_outdated: true },   // not active: advises nothing, not counted
  { deck_id: 'd', activated: true },                          // old app/server without the flag
];
ok(/1 of your decks/.test(ctx.banner(decks)), 'the banner counts only ACTIVE decks with outdated rules');
ok(/nothing is regenerated automatically/.test(ctx.banner(decks)), 'the banner says nothing is regenerated automatically');
ok(ctx.banner([{ activated: true, rules_outdated: false }]) === '' && ctx.banner([]) === '' && ctx.banner(undefined) === '', 'no banner when nothing is outdated');

const h = { rules: 30, search_preferences: 1, tutors_covered: [], tutors_uncovered: [], tutors_known: true };
ok(/older version of the guide generator/.test(ctx.panel(h, true, true)), 'the heuristics panel warns when the active rules are outdated');
ok(!/older version/.test(ctx.panel(h, true, false)), '...and does not when they are current');
console.log(n + ' checks passed');
