Design the desktop-app UI for **Scryglass** — a live Magic: The Gathering Arena
advisor that runs locally and shows advice over the game. I already have a web
design system (used by the site) and want the app brought onto it, decluttered.
Produce the pages as artifacts I can copy.

## Hard technical constraints
- Output **one shared `style.css`** (the design system + shared components) **and
  one HTML file per page** that links it (`<link rel="stylesheet" href="/static/style.css">`).
  This is a multi-page app served locally at `http://localhost:8765` by a small
  backend; pages navigate to each other by URL.
- **No external requests** — no CDN, no Google Fonts, no remote images. The app
  often runs offline mid-game. Use a system font stack
  (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`) and inline SVG or
  emoji for icons. (I'll drop in the Scryglass logo PNG.)
- All CSS in `style.css`; per-page JS in a `<script>` at the end of each page.
  No framework/build.
- Responsive-ish + accessible, but this is a desktop window (min ~900px wide) —
  optimize for that, not phones.
- **Preserve the flows/endpoints I list** (you may restructure markup). Keep DOM
  element ids stable where I note them so the existing real-time JS can attach.

## Design system (match the site)
Dark, modern, "arcane glass / competitive tool". Tokens: bg `#0f1117`, card
`#171a23`, card-hover `#1e2230`, border `#262a36`, text `#e6e8ee`, muted
`#8b90a0`, accent indigo `#5b6ccf` (hover `#6c7bd9`), success `#58c98b`, error
`#d9737a`, warning `#e0a060`. Rounded glass cards, generous spacing, crisp type.
The name is **Scryglass** everywhere (the app currently calls itself three things
— "MTGA Play Advisor", "MTGA Advisor", "Scryglass"; use only **Scryglass**).

## THE APP SHELL — a persistent top bar on EVERY page (highest priority)
A single consistent header across all pages:
- **Left:** Scryglass logo + wordmark.
- **Center/right nav:** `Advisor` (`/`) · `Stats` (`/stats`) · `Review`
  (`/review`) · `Manage` (`/manage`). Identical on every page; highlight the
  current one. (This replaces today's inconsistent per-page nav.)
- **Right: a first-class account/identity control** — this is important. Show the
  signed-in user and status **up here, always visible — never buried in a
  settings tab.** States:
  - **Anonymous:** a "Sign in" button + a subtle "syncing anonymously" hint.
  - **Signed in:** the account email + a small avatar/dot; click → a dropdown
    with account status (email, "alpha" badge if applicable, sync state) and
    actions **Sync now** and **Sign out**.
  - Consolidate what used to be two separate things ("Sign in" and "Link your
    email") into **one** sign-in flow reachable only from here.
  - Data: `GET /api/manage/cloud-me` → `{status:"ok|disabled|error", body:{email,
    is_anon, alpha, created_at}}`. Sign in: `POST /api/manage/cloud-signin`
    (opens the browser for OAuth; returns `{status, report:{email, merged}}`;
    it blocks up to ~3 min — show a "complete sign-in in your browser…" spinner).
    Sync now: `POST /api/manage/cloud-sync`. Sign out: `POST /auth/logout` is
    server-side; for the app just re-check `cloud-me` (a host endpoint may be
    added). Design the dropdown; I'll wire exact calls.

## Pages

### `/` — Advisor (the main window; LIVE, real-time)
The live play surface. It's driven by a WebSocket (`ws://<host>/ws`) that pushes
`state_update, advice, threat_assessment, match_start, decision_point,
strategy_info, llm_status`. **This page is real-time — design the layout,
components, and their states; I will preserve the WebSocket wiring.** Include:
- **Connection status** ("Waiting for match…" / connected / in-game).
- **Turn info** + a deck-strategy banner (appears mid-match).
- **Vital bar** — your life/mana vs opponent life/mana, compact + prominent.
- **Board view** — opponent zone (name, life, battlefield), your zone
  (battlefield, life, mana), and your hand ("live castable plays"). Cards are
  rendered live from state.
- **Decision HUD** — the headline advice for the current decision (this is
  **heuristics-only** now; drop any "LLM supplements" copy).
- **"Do Now"** — the immediate plays list.
- **"Context"** — threat model / matchup read.
- **Opponent Radar** — opponent threat summary + list.
- **Footer** — library / graveyard / stack counts + game-state id + version
  (click version → About).
- **About** dialog (App / Engine / Cards / Strategy / Rules versions).
- **Debug panel** — collapsible; strategy internals + rules-by-layer (keep it,
  but tuck it behind a small "Debug" toggle so it's out of the way).
- **Match Summary** button — post-game LLM summary (opens a summary view/modal).
- **CUT (do not include):** the Focus/Full/Tactical profile switcher, the "Ask AI"
  button, the "Auto-LLM" toggle, and the LLM "Backend" selector. Advice is engine-
  driven; the only LLM feature left is the post-game Match Summary.

### In-game overlay — `overlay.html` (LIVE, click-through HUD; RESTYLE ONLY)
A small translucent HUD shown ON TOP of the Arena window. **Behavior must not
change** (it's click-through, repositionable, and collapses to a "peek" pill) —
so **only restyle to the design system; keep the structure, ids, positioning, and
JS.** Elements (all live): peek pill, opponent deck + confidence, a loud **Lethal**
banner, a loud **Combo** banner, a quiet **Synergy** hint, the **Key-play
spotlight** (the main advice: CAST/PLAY LAND/ACTIVATE/ATTACK/BLOCK/HOLD/TARGET +
card + reason + ENGINE/verified badges), an advice-confidence line, a threat line,
a "Do Now" mini-list, a phase line, a control-hint line, feedback buttons (✓ good
/ ✗ bad / ⚑ flag), and a match-end debrief (W/L + "Open review"). Just give it the
new palette + clean typography; **CUT** only the between-match session-record line.
Provide this as a restyle of the existing markup (I'll hand you the current file).

### `/stats` — Stats (dashboards; local match DB)
Full redesign, keep all sections (you liked them): overview cards (matches / wins
/ losses / win rate / avg turns / streak), recent-trend chart, deck performance,
matchups, color matchups, mulligan stats, my-card performance, mana-curve
efficiency, advice compliance, weakness alerts, opponent cards, and a match
history table with a per-match **Summary** (LLM) + **turn-by-turn timeline**, and
a life graph. Endpoints: `/api/stats/*` (overview, trend, decks, matchups,
color-matchups, mulligan, my-cards, mana-curve, compliance, weaknesses, opp-cards,
life/{id}), `/api/history`, `/api/match-summary/{id}`, `/api/match-timeline/{id}`.
Design as a clean analytics dashboard (cards + tables + a couple of simple
canvas/inline-SVG charts). Escape any DB-backed strings before injecting.

### `/review` — Post-game Review (local DB)
Keep: match list (last 10), match header (W/L, decks, time), a summary bar
(turns / key moments / advice count), a Key-Moments/All filter, and a per-turn
timeline of advice items (priority color, phase, message). Endpoints:
`/api/review/matches`, `/api/review/latest`, `/api/review/{id}`. (This and the
Stats "turn-by-turn" overlap — design Review as the canonical post-game replay;
keep it clean.)

### `/manage` — Manage (slimmed down)
Only these sections survive (as a simple settings page — NOT the account stuff,
which now lives in the header):
- **Collection** — collection stats (unique / copies / per-rarity / wildcards /
  snapshot date) + a **Refresh from MTGA memory** action (note it needs a
  one-time setup). `/api/manage/collection-stats`, `/api/manage/refresh-collection`
  (+ `-status`).
- **Meta decks** — the meta-deck list + a **Sync from MTGGoldfish** action.
  `/api/manage/meta-decks` (GET/PUT), `/api/manage/sync-meta`.
- **Cloud sync settings** — the *config* only (enabled / interval; URL/token are
  advanced), + "Sync now". `/api/manage/cloud-config` (GET/PUT),
  `/api/manage/cloud-sync`. (The account identity + Sign in live in the header,
  not here.)
- **CUT from Manage:** the Strategies tab, General Rules editor, the Decks tab,
  the Guides tab, and the GA Runs tab.

### `/setup` and `/loading` (keep, restyle)
- **Setup** — first-run: 4 readiness checks (Advisor engine `/health`, Card DB
  count, MTGA log `/match-status`, Strategy rules count) + a Start button.
- **Loading** — splash: spinner + staged status, health-polls `/health`, with an
  error state (Retry / Open Setup / Copy log). Restyle to match.

## Out of scope for this pass (leave alone)
The **Decks page** (`/decks`) and **deck/rules management** (create, versions,
deploy, optimize, build-variants, rule generation) — undecided; don't design it.

## Output format
Deliver: (1) `style.css` — the shared design system + components (top bar, account
menu, nav, cards, tables, stat tiles, pills, buttons, inputs, modal). (2) One HTML
artifact per page: `index.html` (Advisor), `stats.html`, `review.html`,
`manage.html`, `setup.html`, `loading.html`, and a restyled `overlay.html`.
Prioritize the **shell (top bar + account control)** and the **Advisor** page —
they set the tone. Keep the live pages' structure faithful so I can reattach the
real-time JS.
