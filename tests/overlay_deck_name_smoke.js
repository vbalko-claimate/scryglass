// The overlay names the deck the way the deck list does (`deck_name`), falling back to the strategy
// file's own name for built-in strategies. Run: node tests/overlay_deck_name_smoke.js static/overlay.html
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('function updateStrategyLine');
const b = html.indexOf('// Second micro-line');
const c = html.indexOf('function updateOppLine');
const d = html.indexOf('\n}\n', c) + 3;
assert(a > 0 && b > a && c > b, 'functions not found');
const els = {};
const mk = () => ({ style: {}, textContent: '', innerHTML: '' });
const ctx = { document: { getElementById: (id) => (els[id] ||= mk()) } };
vm.createContext(ctx);
vm.runInContext(html.slice(a, b) + 'function updateHeuristicsLine(){}\n' + html.slice(c, d) + ';this.s=updateStrategyLine;this.o=updateOppLine;', ctx);
const t = (n, f) => { f(); console.log('ok  ', n); };
t('deck_name wins on the strategy line', () => {
  ctx.s({ strategy_name: 'Mono White Lifegain Aggro', deck_name: 'Mono White Lifegain' });
  assert(els['strategy-line'].textContent.includes('Mono White Lifegain') && !els['strategy-line'].textContent.includes('Aggro'), els['strategy-line'].textContent);
});
t('deck_name wins in the header fallback', () => {
  ctx.o({ strategy_name: 'Mono White Lifegain Aggro', deck_name: 'Mono White Lifegain' });
  assert(els['opp-line'].innerHTML.includes('>Mono White Lifegain<'), els['opp-line'].innerHTML);
});
t('built-in strategy (no deck_name) keeps the strategy name', () => {
  ctx.s({ strategy_name: 'Boros Aggro' });
  assert(els['strategy-line'].textContent.includes('Boros Aggro'));
  ctx.o({ strategy_name: 'Boros Aggro' });
  assert(els['opp-line'].innerHTML.includes('Boros Aggro'));
});
