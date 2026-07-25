# Changelog

All notable changes to the Scryglass app are recorded here. The advisor engine
ships from `glass-shard@main` (bundled `glass-host`); versions are the Tauri app
version used for OTA updates.

## [0.8.18] — 2026-07-25

### Fixed

- **The overlay stayed minimized for the whole match.** The v0.8.17 change that
  makes it rest as a small pill between matches never handed control back when
  a match started, so it sat collapsed the entire game. It now expands on match
  start as it always did, and only shrinks when you hover it.

## [0.8.17] — 2026-07-25

### Fixed

- **The "Swing for lethal" banner no longer promises a kill your opponent can
  simply block.** It was comparing your attackers' raw power to their life
  total and ignoring their board entirely — measured against real games, half
  the lethal calls you acted on were wrong (a 26-power Chocobo called lethal
  into 17 life, chump-blocked by a 1/1 for zero). It now works out what
  actually gets through after their best block, and says "lethal if it
  connects, but they have blockers that can absorb it" when it can't promise.
  Trample is understood, so a real Mossborn Hydra kill is still called lethal.
- **Attack advice stops billing fully-blockable damage as "pressure"** — it now
  names how much their blockers can stop.
- **Two combat-advice bugs from flagged in-game decisions.** Two identically
  named attackers were collapsed into one, so a losing chump block looked
  good; and creatures tapped for mana were still offered as attackers.
- **Delney, Streetwise Lookout and friends are read correctly.** "Creatures you
  control with power 2 or less can't be blocked by creatures with power 3 or
  greater" — and the cards that name themselves instead of saying "this
  creature" — were silently ignored by block-legality.

### Changed

- **The overlay rests as the small pill between matches** and opens when you
  hover it, the reverse of its in-match behaviour (expanded, hides on hover).

## [0.8.16] — 2026-07-24

### Changed

- **New, unified app design + a first-class account control.** The whole app now
  shares one design system (the same look as the web dashboard) with a single
  persistent top bar on every page: unified nav (Advisor · Stats · Review ·
  Manage) and — top-right, always visible, no longer buried in a settings tab —
  your account. It shows whether you're signed in (your email, an *alpha* badge
  when applicable, sync status) or on an anonymous device, with **Sign in**,
  **Sync now**, and **Sign out** in one place. "Sign in" and the old "Link your
  email" are now a single sign-in flow.
- **Decluttered.** Removed the noise that had accumulated: the app now calls
  itself only *Scryglass*; the Advisor drops the Focus/Full/Tactical profile
  switcher, the "Ask AI" button, the Auto-LLM toggle, and the LLM backend selector
  (advice is engine-driven — the only LLM feature left is the post-game Match
  Summary); and Manage is slimmed to just Collection, Meta decks, and Cloud-sync
  settings (the Strategies, General Rules, Decks, Guides, and GA-Runs tabs are
  gone). Stats and Review keep all of their content, restyled.

### Added

- **Sign out.** The account menu now has a working **Sign out** — it forgets the
  account on this device (reverting to an anonymous device that keeps syncing
  anonymously). Your cloud account is untouched: it stays reachable from the web
  dashboard and by signing in again.

### Fixed

- **Sign-in no longer "disappears" after an update.** The app kept a check that
  reused whatever glass-host was already on :8765 — so an old sidecar orphaned by
  a previous update (or a second copy of the app) was adopted, and the app ran
  stale backend code (e.g. missing the account/sign-in endpoints). Production
  launches now kill any stale `glass-host`/`overlay-helper` and always run this
  build's bundled binary (dev/manual hosts opt out via `SCRY_EXTERNAL_HOST`).
- **Quit works.** The tray "Quit Scryglass" item had no handler (it did nothing —
  you had to force-quit). It now shuts the sidecars down cleanly and exits.

## [0.8.15] — 2026-07-24

### Added

- **Sign in from the app.** The Cloud Sync settings now show a **Sign in**
  button for anonymous accounts. It opens your browser to sign in (magic-link),
  and once you approve, this device is linked to your account and its play data
  is merged in — no token to copy. Uses a secure OAuth loopback + PKCE flow; the
  sign-in token never appears in any URL. (Requires the matching `glass-host`
  build.)

## [0.8.14] — 2026-07-24

### Added

- **Flag a decision (⚑).** When the overlay gives advice you think is wrong,
  hold the feedback key (Left ⌘ / Left Ctrl) and click ⚑ to save that decision —
  the exact engine input plus the advice shown — to the local database, so it can
  be reproduced and debugged later without a screenshot.
- **Hover-shrink (peek).** Move the cursor over the overlay panel and it collapses
  to a small legible pill (◈ Scryglass) so it stops covering the board (e.g. the
  opponent's name), then springs back when the cursor leaves. Also toggleable with
  Option/Alt+H. Works over fullscreen MTGA without breaking click-through.
- **Drag to move the overlay.** In feedback mode you can drag the panel with the
  mouse to reposition it (the position persists), in addition to Option/Alt+arrows.
- **Account status in Settings → Cloud Sync.** The tab now shows whether you're
  signed in (as your email) or on an anonymous device account. (First step of a
  larger cloud sign-in/linking UX overhaul.)

### Changed

- **Feedback key moved to the left hand.** Feedback mode is now Left ⌘ (macOS) /
  Left Ctrl (Windows) instead of a right-hand key, so it can be held while the
  mouse stays in the right hand. The in-panel hint shows the platform's key.
- **Cloud requests now use `scryglass.win`** (was `scryglass.app.claimate.tech`;
  same backend) — one canonical domain for the app, web dashboard, and docs.

### Removed

- **Over-the-board card indicators dropped.** The left-side recommendation
  strip and the opponent-threat badges shown over the table were removed — the
  overlay can't reconstruct the order of cards in hand or on the battlefield, so
  those per-card indicators couldn't be placed reliably and were misleading.

## [0.8.13] — 2026-07-23

### Fixed — smarter burn/removal targeting + more "leaves" triggers

- **Burn and removal are no longer aimed at creatures they can't kill.** The
  advisor's lethality check was inactive in the live app, so it would recommend a
  2-damage Burst Lightning at a 3-toughness creature, or a kicked 4 at a 5/5 —
  labeling the un-killable body "the most dangerous creature." The check is now
  active: when a creature it CAN kill is available it targets that one, and on a
  body it can't kill the advice is honest ("won't kill it — only chip"). (Modal
  and variable-damage spells stay un-gated to avoid mis-narrowing.)
- **"Put its counters on target" leave-triggers now move the real count.** Hei
  Bai, Broodguard Elite, and Selfless Police Captain transfer the actual number of
  counters they had when they left, instead of dropping it or moving just one.

## [0.8.12] — 2026-07-23

### Fixed — the bundled card database now includes the v0.8.11 compiler fixes

- v0.8.11 shipped the updated engine but the **committed card database was stale**
  (CI has no Scryfall oracle, so it reuses the committed DB). This release
  regenerates `glass_advise_db.json` from `glass-shard@main`, so the advisor
  actually receives the compiler-side work from the last releases:
  **"leaves the battlefield" triggers** (Ninja Teen, Super Shredder, and the
  self-leave cards) and **planeswalker loyalty / emblem-ultimate abilities** (20
  planeswalkers). No engine change from 0.8.11 — this is the card data catching up.

## [0.8.11] — 2026-07-23

### Fixed — more cards' abilities now actually work

- **"Leaves the battlefield" triggers now fire.** Abilities that trigger when a
  permanent leaves play — e.g. Aurelia's Vindicator, City Pigeon, Greed's Gambit,
  Ninja Teen ("whenever a creature you control leaves, each opponent loses 1
  life"), Super Shredder — were being ignored by the engine, so the advisor
  under-valued these cards. They now fire on death, sacrifice, destruction, exile,
  and bounce. (A few complex bodies — e.g. Momo's modal choice, "return the exiled
  card" — are recognized but not yet fully modeled; they no longer misfire.)
- **Planeswalker loyalty abilities are understood.** 20 planeswalkers whose
  loyalty abilities (+N / −N / 0, and emblem ultimates) were treated as inert are
  now compiled correctly, so the advisor reasons about activating them.

## [0.8.10] — 2026-07-23

### Fixed — the advisor no longer recommends spells you can't actually pay for

- **Hybrid mana costs are now understood correctly.** A hybrid pip like `{R/W}`
  (pay red OR white) used to be treated as generic — payable by *any* mana — so the
  advisor would recommend casting e.g. Mechanized Ninja Cavalry `{1}{R/W}` with only
  blue mana untapped, then tag it "ENGINE ✓VERIFIED". The engine now models the two
  colors a hybrid pip actually accepts, so uncastable hybrid spells are no longer
  offered. 134 cards had their cost corrected in the bundled card database.
- **Alternative mana sources (convoke, Treasure, improvise) are now planned as one
  transaction.** A single mana planner drives both "is this castable?" and the
  payment itself, so a cast is never recommended and then found unpayable mid-way —
  and it prefers your mana pool / tapping over destructively cracking a Treasure it
  doesn't need.

## [0.8.9] — 2026-07-22

### Fixed — engine fidelity: several cards now simulate faithfully

The bundled engine (`glass-shard@main`) got a batch of compile-fidelity fixes, so
the advisor now reasons about these cards correctly instead of on a wrong model:

- **No more phantom card draws.** A combat-damage trigger the engine couldn't model
  (Drake Hatcher's incubation counters, Fynn the Fangbearer's poison, Tinybones,
  Dragon Mage, …) used to be silently compiled as "draw a card" — inventing card
  advantage that skewed the advice. It's now honestly unmodeled instead of faked.
- **"At the beginning of …, if <condition>, …" abilities now check the condition.**
  Triggers gated by an intervening "if" (CR 603.4) used to fire unconditionally —
  e.g. Emet-Selch transforming every upkeep from turn 1, Leonin Vanguard / the
  tapped-creature end-step cycle (Flight-Deck Coordinator, Frontline War-Rager, …),
  Resplendent Angel, and "if you gained life this turn" / "if you control a creature
  with power N" cards. They now only happen when the condition actually holds, at
  both trigger time and resolution.
- **"A creature with power N" no longer counts an uncrewed Vehicle** (it isn't a
  creature until crewed), fixing over-eager Ferocious-style triggers.

## [0.8.7] — 2026-07-20

### Fixed — smarter advice on legendary duplicates

- **No more "cast a second copy of a legend you already have out".** The advisor
  no longer suggests casting a duplicate legendary creature that would just die to
  the legend rule — unless it actually gains value from entering (an enter/dies/
  leaves trigger, or an enters-as-a-copy legend like Superior Spider-Man).

## [0.8.6] — 2026-07-20

### Fixed — sharper advice on Auras & conditional removal

- **No more "cast this Aura" with nothing to enchant.** An "enchant creature you
  control" Aura (e.g. Sheltered by Ghosts) is no longer suggested — or considered
  castable — when you control no creature (it would just hit the graveyard).
- **Conditional removal respects its restriction.** "Exile target … with mana
  value 2 or less" (Seam Rip) now keeps that limit: it's no longer recommended
  into a board whose only permanent is too expensive for it to hit, and the engine
  won't let it exile an over-cost permanent.

## [0.8.5] — 2026-07-20

### Added — link your email (web dashboard sign-in)

- **Link an email to your account.** Manage → Cloud Sync → *Link your email*:
  enter an email, confirm the 6-digit code we send you, and your account is
  reachable from the web dashboard at **scryglass.win/app** — the same stats and
  match history, from any browser. The confirmation code is emailed by the cloud;
  your per-user token never leaves the app (the host proxies the request).

### Fixed

- **About panel card count.** The `/health` engine card-count stamp read a field
  that no longer existed, which prevented the host from building with the server
  feature; it now reports the loaded catalog size correctly.

## [0.8.4] — 2026-07-19

### Added — cloud training corpus + version stamp

- **Your games now feed the training corpus.** The app uploads the rich
  per-decision telemetry from your games (the board state, the recommendation,
  and the outcome at each decision) to the cloud — the raw signal that will drive
  future advisor improvements. This is strictly **read-only + non-destructive**:
  your irreplaceable local `advisor.db` is only ever read, never modified, and the
  upload watermark advances only after a confirmed successful upload.
- **Version stamp.** The About panel now shows the running engine build (git
  commit) and card-catalog size, so it's clear which build you're on.

## [0.8.3] — 2026-07-19

### Fixed — advisor recommendation quality (the "much worse than yesterday" regression)

- **Beneficial effects were aimed at the opponent.** When choosing a target for a
  buff — a `+1/+1` counter (e.g. Grand Entryway // Elegant Rotunda), a positive
  pump, or a keyword grant — the advisor used its removal-oriented target picker
  and recommended the opponent's biggest creature, i.e. it told you to *buff the
  enemy*. The target chooser now recognises effect **polarity**: a beneficial
  creature-target effect is aimed at your OWN creatures. Modal cards that can also
  harm a creature (destroy/exile/burn/-1/-1/tap, e.g. Valorous Stance) keep the
  removal targeting so they can still be aimed at the opponent; an effect that by
  design targets an opponent's creature (e.g. a "+1/+1 counter on target creature
  an opponent controls") is left alone.
- **Restricted removal was recommended with no legal target.** A removal spell
  limited by mana value or power (e.g. Seam Rip — "exile target nonland permanent
  an opponent controls with mana value 2 or less") was suggested even when the
  opponent's only permanent didn't satisfy the restriction (a mana-value-4
  Leyline of Hope), so it would have exiled nothing. The dead-removal gate now
  evaluates the numeric filter (mana value / power / toughness) against the
  opponent's board.

### Notes

- These are deterministic correctness fixes in the shared advisor heuristic; they
  also improve the AI pilot and the MCTS refinement, which consume the same core.
- No change to the account/cloud-sync behaviour shipped in 0.8.2.

## [0.8.2] — 2026-07-19

- Cloud accounts (client half): the app auto-provisions an anonymous per-user
  account on first launch and syncs under it — no token entry, no baked secret
  (`SCRY_CLOUD_SYNC=0` disables). Server = `glass-shard@main` Glass Cloud.

## [0.8.1] — 2026-07-19

- OTA self-update UX: a menu-bar "Check for Updates…" item plus a gentle
  update notification, game-aware (never restarts mid-match), via
  `tauri-plugin-updater` (minisign-signed `latest.json`).
