// viewGuide must render the guide's markdown, not the JSON envelope the endpoint wraps it in.
// Run: node tests/decks_view_guide_smoke.js static/decks.html
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('async function viewGuide');
const b = html.indexOf('// ── Card overview');
assert(a > 0 && b > a, 'viewGuide not found');
const rendered = [];
const body = { replaceChildren: (n) => rendered.push(n) };
const ctx = {
  encodeURIComponent,
  toast: () => {},
  renderMarkdown: (md) => md,
  document: { getElementById: () => ({ ...body, classList: { add() {} } }) },
  fetch: async () => {
    const env = { content: '# Play Guide\n\nhello', deck_id: 'd', file: 'f.md' };
    return { ok: true, json: async () => env, text: async () => JSON.stringify(env) };
  },
};
vm.createContext(ctx);
vm.runInContext(html.slice(a, b) + ';this.v = viewGuide;', ctx);
(async () => {
  await ctx.v('d', 'guides/f.md');
  assert.strictEqual(rendered.length, 1);
  assert.strictEqual(rendered[0], '# Play Guide\n\nhello', 'got: ' + JSON.stringify(rendered[0]));
  console.log('ok   renders the markdown content, not the JSON envelope');
})().catch((e) => { console.error(e.message); process.exit(1); });
