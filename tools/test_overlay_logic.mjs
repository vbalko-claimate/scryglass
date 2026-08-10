// Unit tests for the overlay's PURE decision logic, run straight out of overlay.html.
//
//   node tools/test_overlay_logic.mjs
//
// There is no JS test harness in this repo and adding one for two functions would be
// out of proportion. Instead the functions are extracted from the page by name and
// evaluated, so the test runs against the SHIPPED source — no copy to drift.
//
// What is worth testing here is exactly what a screenshot cannot show: whether advice
// is classified as live or stale, and what the two seat rows say about priority. The
// visuals (colours, the pulse) are checked by looking at the overlay.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '..', 'static', 'overlay.html'), 'utf8');

function extract(name) {
    const start = html.indexOf(`function ${name}(`);
    if (start < 0) throw new Error(`function ${name} not found in overlay.html`);
    // Brace-match from the first { after the signature.
    let i = html.indexOf('{', start), depth = 0;
    for (let j = i; j < html.length; j++) {
        if (html[j] === '{') depth++;
        else if (html[j] === '}' && --depth === 0) {
            return html.slice(start, j + 1);
        }
    }
    throw new Error(`unbalanced braces in ${name}`);
}

const { adviceStatus, seatRows } = new Function(
    `${extract('adviceStatus')}\n${extract('seatRows')}\nreturn { adviceStatus, seatRows };`
)();

let failures = 0;
const eq = (got, want, what) => {
    const g = JSON.stringify(got), w = JSON.stringify(want);
    if (g !== w) { console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`); failures++; }
    else console.log(`ok   ${what}`);
};

// ── adviceStatus ────────────────────────────────────────────────────────────
eq(adviceStatus({ turn: 6, phase: 'Main 1' }, 6, 'Main 1'), 'live',
   'same turn and phase is live');
eq(adviceStatus({ turn: 6, phase: 'Main 1' }, 6, 'Main 2'), 'stale',
   'the phase moved on -> stale');
eq(adviceStatus({ turn: 6, phase: 'Main 1' }, 7, 'Main 1'), 'stale',
   'a new turn with the same phase name -> stale');
// A push from a build that predates the `for` field must not grey out every
// recommendation; unknown is rendered as live.
eq(adviceStatus(null, 6, 'Main 1'), 'unknown', 'no context -> unknown, not stale');
// Advice logged before the live phase is known (first push of a match).
eq(adviceStatus({ turn: 1, phase: 'Main 1' }, 1, ''), 'live',
   'unknown live phase does not falsely mark stale');

// ── seatRows ────────────────────────────────────────────────────────────────
// My turn, I hold priority: the ordinary case.
eq(seatRows({ active_player: 1, priority_player: 1 }, 1).map(r => [r.who, r.active, r.priority, r.state]),
   [['YOU', true, true, 'your move'], ['OPP', false, false, 'waiting']],
   'my turn, my priority');
// ★ My turn, the OPPONENT holds priority — the trick window, and the thing the
// overlay used to be blind to.
eq(seatRows({ active_player: 1, priority_player: 2 }, 1).map(r => [r.who, r.active, r.priority, r.state]),
   [['YOU', true, false, 'active, opp responding'], ['OPP', false, true, 'responding']],
   'my turn, opponent has priority');
// Opponent's turn, I hold priority: my own response window.
eq(seatRows({ active_player: 2, priority_player: 1 }, 1).map(r => [r.who, r.active, r.priority, r.state]),
   [['YOU', false, true, 'responding'], ['OPP', true, false, 'active, opp responding']],
   'opp turn, my priority');
// Opponent's turn and priority: "their move", never "your move" on the OPP row.
eq(seatRows({ active_player: 2, priority_player: 2 }, 1).map(r => [r.who, r.state]),
   [['YOU', 'waiting'], ['OPP', 'their move']],
   'opp turn and priority reads as their move');
// Seat unknown (before the first state carrying my_seat_id).
eq(seatRows({ active_player: 1, priority_player: 1 }, null).map(r => [r.who, r.state]),
   [['YOU', '—'], ['OPP', '—']],
   'unknown seat degrades to placeholders');

// ── DOM WIRING ───────────────────────────────────────────────────────────────
// The pure functions above can be right while nothing appears on screen, because
// the render functions look elements up BY ID and a typo fails silently
// (`getElementById` returns null, the `?.` swallows it). No browser here — the
// memory rule is not to open one — so this is a stub DOM that records what the
// renderers actually set. It catches the id typos and the class toggles; colours
// and the pulse timing still need a human looking at the overlay.
function stubDom(ids) {
    const els = {};
    for (const id of ids) {
        els[id] = {
            id, innerHTML: '', textContent: '', style: {},
            _c: new Set(),
            classList: {
                add: (...c) => c.forEach(x => els[id]._c.add(x)),
                remove: (...c) => c.forEach(x => els[id]._c.delete(x)),
                toggle: (c, on) => (on ? els[id]._c.add(c) : els[id]._c.delete(c)),
                contains: c => els[id]._c.has(c),
            },
            get offsetWidth() { return 1; },
        };
    }
    return els;
}

const RENDER_IDS = ['seat-lines', 'advice-for', 'key-play', 'advice-idle', 'compliance-flash'];
const els = stubDom(RENDER_IDS);
globalThis.document = { getElementById: id => els[id] ?? null };

const src = `${extract('adviceStatus')}\n${extract('seatRows')}\n`
    + `${extract('renderSeatLines')}\n${extract('renderAdviceFor')}\n${extract('flashCompliance')}\n`
    + 'return { renderSeatLines, renderAdviceFor, flashCompliance,'
    + ' set: (t,p,s,c) => { liveTurn=t; livePhase=p; mySeatId=s; adviceForCtx=c; } };';
const R = new Function(
    'let liveTurn=null, livePhase="", mySeatId=null, adviceForCtx=null, complianceTimer=null;\n' + src
)();

// Seat rows reach the DOM at all, and priority is marked on the right row.
R.set(6, 'Main 1', 1, null);
R.renderSeatLines({ number: 6, active_player: 1, priority_player: 2 });
eq(els['seat-lines'].innerHTML.includes('YOU'), true, 'DOM: seat rows are rendered');
const youRow = els['seat-lines'].innerHTML.split('</div>')[0];
eq(youRow.includes('has-priority'), false, 'DOM: YOU row is not marked when opp holds priority');
eq(els['seat-lines'].innerHTML.split('</div>')[1].includes('has-priority'), true,
   'DOM: OPP row carries has-priority');

// With no advice on screen the "advice for" row stays hidden.
els['key-play'].style.display = 'none';
R.set(6, 'Main 1', 1, { turn: 6, phase: 'Main 1', is_my_turn: true });
R.renderAdviceFor();
eq(els['advice-for'].classList.contains('visible'), false,
   'DOM: no advice showing -> the context row is hidden');

// Advice showing and current: visible, live, card NOT dimmed.
els['key-play'].style.display = '';
R.renderAdviceFor();
eq([els['advice-for'].classList.contains('visible'),
    els['advice-for'].classList.contains('live'),
    els['key-play'].classList.contains('stale')],
   [true, true, false], 'DOM: live advice is labelled live and not dimmed');
eq(els['advice-for'].innerHTML.includes('T6 · Main 1'), true, 'DOM: the row names the decision');

// The game moves on: same push, new phase -> stale, and the card dims.
R.set(6, 'Main 2', 1, { turn: 6, phase: 'Main 1', is_my_turn: true });
R.renderAdviceFor();
eq([els['advice-for'].classList.contains('stale'), els['key-play'].classList.contains('stale')],
   [true, true], 'DOM: superseded advice is marked stale and dimmed');
eq(els['advice-for'].innerHTML.includes('superseded'), true, 'DOM: stale row says so');

// ── The compliance pulse ─────────────────────────────────────────────────────
// v1 put it on `.key-play` as an inset shadow at the left edge — under the 3px
// `.key-play-indicator`, so invisible — and cleared it at the top of
// `updateAdvice`, while the next decision arrives inside the animation window 57%
// of the time (median 0.0s). It was never seen once in real play. Both properties
// are pinned here.
globalThis.setTimeout = () => 0;      // the fade-out timer is not under test
globalThis.clearTimeout = () => {};
R.flashCompliance(true);
eq([els['compliance-flash'].classList.contains('followed'),
    els['compliance-flash'].classList.contains('ignored')],
   [true, false], 'DOM: followed pulse lands on its own element');
eq([els['key-play'].classList.contains('followed'), els['key-play'].classList.contains('ignored')],
   [false, false], 'DOM: the pulse is NOT on the advice card (its left edge is covered)');
R.flashCompliance(false);
eq([els['compliance-flash'].classList.contains('followed'),
    els['compliance-flash'].classList.contains('ignored')],
   [false, true], 'DOM: ignored replaces followed, never both');

// ★ THE REGRESSION THAT MADE IT INVISIBLE: a new advice push must not cancel it.
// `updateAdvice` is too entangled with the DOM to call here, so this asserts the
// property at the source — no advice-path function may touch the flash element.
const advicePath = extract('updateAdvice') + extract('clearAdvicePanels');
eq(/compliance-flash/.test(advicePath), false,
   'SOURCE: no advice-path function clears the compliance flash');

console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
