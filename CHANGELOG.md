# Changelog

All notable changes to the Scryglass app are recorded here. The advisor engine
ships from `glass-shard@main` (bundled `glass-host`); versions are the Tauri app
version used for OTA updates.

## [0.8.58] - 2026-08-22

### Fixed
- Hostile auras (Pacifism-style "can't attack/block") are no longer
  recommended onto your own creature — v0.8.57's aura targeting preferred
  your side for every aura, including the opponent's disabling ones.

## [0.8.57] - 2026-08-21

### Changed
- Aura targeting advice now prefers the synergy carrier (a creature that
  benefits from being enchanted) over the biggest body — measured against
  real players, this fixes the two worst-scoring aura recommendations.

## [0.8.56] - 2026-08-21

### Fixed
- Target advice now considers every candidate the game offers — enchantments,
  artifacts and planeswalkers were silently dropped, so spells like Sheltered
  by Ghosts' exile could get no recommendation (or only creature options).

## [0.8.55] - 2026-08-21

### Fixed
- Regenerating a deck's AI guide now saves the new rules locally — previously
  a version that had already received a guide once silently kept the old one.

## [0.8.54] - 2026-08-21

### Changed
- Deck strategy steering is now driven purely by the strategy's RULES (each
  changed recommendation names the rule that caused it). Legacy GA-era
  family-wide biases no longer shift recommendations — they were tuned for
  the old advice sorter, and measurement showed them dominating changes with
  hard-to-justify sequencing shoves.

## [0.8.53] - 2026-08-21

### Fixed
- Deck guide viewer renders the AI guide with the same safe DOM renderer as
  the cloud dashboard (no third-party markdown library, model output never
  reaches raw HTML).
- The combo banner now simulates exactly the recommended action — previously
  a card with two abilities could get the banner computed for the wrong one.

## [0.8.52] - 2026-08-21

### Added
- The overlay now shows WHICH deck strategy is steering the advice, and warns
  when the active rules were generated for an older version of your deck.
- Deck screen: a STALE chip when the deck list moved past its active rules,
  and a "Legacy strategies" section for rule files without a deck list (with
  one-click deactivation).

### Fixed
- A one-card strategy signature can no longer outrank a fully-matched
  five-card one when binding a strategy to your match.

## [0.8.51] - 2026-08-21

### Fixed
- Guide rules with conditions the engine can't evaluate (unknown fields,
  typos) now stay silent instead of firing as if the condition passed; the
  cloud generator repairs them at generation time.
- Rule color conditions are now actually checked (previously always treated
  as satisfied).
- "Import from recent matches" no longer offers decks with unreadable cards —
  importing them would have saved an incomplete list as if it were complete.

## [0.8.50] - 2026-08-20

### Changed
- Your deck's AI Guide rules now STEER the advisor's recommendation, not just
  annotate it: a fired rule can promote a close-call play (bounded — the
  engine keeps veto power over large gaps), HOLD rules genuinely favor
  holding, and when a rule changes the pick the overlay says which rule did.
- GA/LLM-tuned `global_biases` from your deck strategy are applied again
  (they were silently dropped by the Rust port).

## [0.8.49] — 2026-08-20

### Fixed

- **Older decks now appear on the Decks screen.** Decks created before the deck-management
  module existed only as files and were invisible — they are adopted automatically, versions
  included.
- **Deck sync is automatic.** Creating a deck, adding a version, activating rules or finishing
  a guide now pushes to your account by itself; the ⇅ Sync button remains for manual use. Your
  account dashboard at scryglass.win fills in without extra steps.

## [0.8.48] — 2026-08-20

### Added

- **Deck card overview.** The deck screen now lists every card of the active version — and after
  generating a guide, each card shows its ROLE (engine / payoff / removal / finisher…) from the
  analysis step, with a one-line why on hover.
- **Generation insight.** Generating a guide now shows what is happening (analyzing card roles →
  writing guide + rules → validating → repair) with elapsed time, and finishes with a summary:
  rules per layer, the real cost of the generation, and the reason if rules were omitted.
- **Real rules preview.** The rules dialog shows the actual rules grouped by layer — action,
  conditions and priority — instead of a file path.

## [0.8.47] — 2026-08-19

### Added

- **Deck management.** A new Decks screen: create decks from an MTGA export or import one you
  already played (no copy-paste — every played deck is recorded), version them with change
  diffs, and generate an **AI play guide** for your exact list — game plan, key cards, combos,
  mulligans, first turns, role assessment, meta matchups, dangerous cards, and a cheat sheet.
  Generation also produces advisor rules; you review them and press Activate — nothing goes
  live silently. Guide generation runs on your account budget (alpha allowance included).
- **The advisor now knows which deck you're playing.** Activated deck rules bind automatically
  when a match starts — no configuration.
- **Deck sync.** Your decks, versions, guides and rules mirror to your account: sign in on a
  new computer and they come back. Sync never deletes anything locally.

### Fixed

- **Transformed cards have names again.** A flipped double-faced card (e.g. Bloodbat Summoner)
  showed as "Unknown" to the advisor — 1487 back faces added to the card catalog.
- **Modal "choose one that hasn't been chosen" is enforced** — the engine no longer re-picks a
  spent mode (The Vision, Gollum, Three Bowls of Porridge).

## [0.8.46] — 2026-08-19

### Added

- **You can now see when an update is ready.** A banner appears in the app window (dismissible),
  in the overlay between matches, and as a line on the minimized overlay pill. The app checks on
  every launch and every 4 hours while running. Installing stays in the tray menu; nothing
  interrupts a game.

## [0.8.45] — 2026-08-19

### Added

- **The advisor now speaks up when the game asks you to CHOOSE.** Scry, surveil and discard
  prompts, modal spells ("Choose one —"), kicker offers and combat damage splits all reach the
  advisor for the first time. Which way a pick runs comes from whose cards they are — when you
  strip the opponent's hand the advisor names their best card, when you discard your own it names
  the cheapest — and when it cannot tell, it says so instead of guessing.
- **Kicker guidance.** If the kicker is payable and doesn't price out another play, the advisor
  says pay it and quotes what the kicked effect does; if it would cost you another spell this
  turn, it names the trade-off.
- **Damage-split guidance.** When you split combat damage the advisor confirms a correct default
  with the reason ("deathtouch makes 1 lethal, trample carries the rest") or spells out a better
  split row by row.

### Fixed

- **The advisor could recommend an illegal activation.** Abilities with an "Activate only if …"
  condition (e.g. Hired Claw) were offered even when the condition was false. The condition is
  now checked — and Hired Claw's own combo (attack → 1 damage → pump becomes legal) falls out
  correctly.
- **22 activated abilities were invisible to the advisor** (Umbral Collar Zealot, the Roads
  cycle, Magda, Immersturm Predator …) — compiled under a wrong ability kind, they were never
  offered as plays. Promoted to real activations, with the cost actually checked and paid.
- **A quiet overlay no longer looks broken.** "No play recommended right now" is now large and
  readable instead of near-invisible fine print.

## [0.8.44] — 2026-08-18

### Fixed

- **A creature that had attacked could not use its own abilities.** Attacking taps a creature, and
  the advisor treated a tapped permanent as unable to activate *anything* — even abilities that
  never asked to be tapped. So the pump you were holding mana for stopped being suggested for the
  rest of the turn. 106 cards seen in real games are affected; the most common appears in 92
  matches.
- **A decision's outcome could be measured against the wrong game.** The feature added in the
  previous release tracked how the board moved two turns after a recommendation, but a
  recommendation still waiting when a match ended was finished off against the *next* match — one
  game's numbers recorded under another's. Fixed before it had time to distort much.

### Added

- **Scry, surveil, explore and "discard a card" choices are finally seen at all.** Every prompt of
  the "choose some cards" kind was invisible to Scryglass — no record, no advice, nothing in the
  history. They are now captured, which is the prerequisite for advising on them; a single game
  turns out to contain seven such moments.

## [0.8.43] — 2026-08-18

### Fixed

- **Your thumbs-up and thumbs-down were going nowhere.** The ✓ / ✗ buttons sent their verdict
  over a channel the app never listened on, so every rating anyone has ever given was silently
  discarded. They now save the same full snapshot the flag button does, which means a verdict can
  actually be looked at — and replayed — later.
- **A flagged decision never recorded WHY.** Every flag until now carried an empty reason, so we
  knew a recommendation was disliked without knowing what was wrong with it. A thumb now supplies
  it (good / bad); the ⚑ button stays "save this, I'll look later".

### Added

- **The app records how a decision turned out.** It already noted what it recommended and whether
  you took the advice; now it also measures what the board did two turns later — life and
  creatures on both sides. That is the difference between knowing advice was *followed* and
  knowing it was *good*. This existed in the old Python version and was lost in the rewrite in
  June; it has been missing for every game since.

## [0.8.42] — 2026-08-18

### Fixed

- **A counterspell was being read as a removal spell.** Aven Interrupter is a flash creature that
  counters a spell as it arrives; Scryglass understood it as a 3-mana flier that destroys any
  permanent, a card that does not exist. So it recommended casting it in your main phase, where
  the real trigger has nothing to counter and does nothing at all. Found because a tester flagged
  exactly that recommendation.
- **The Windows installer no longer contains a file pretending to be a program.** A one-line text
  file named `overlay-helper.exe` was being packed in to satisfy a build requirement; Windows
  never used it, and a stray non-program with an .exe name is the kind of thing antivirus
  software reacts to. It is gone.

### Changed

- **Telemetry now uploads every kind of event except one.** Previously five kinds were sent and
  twenty were silently dropped — including the record of which cards were played, which is what
  every "how often does this card actually turn up" measurement is built on, so those questions
  could only ever be answered for the machine sitting in front of you. The one exception is the
  bulky per-candidate scoring, which is 37% of all data and can be recomputed from the game
  record.

## [0.8.41] — 2026-08-18

### Fixed

- **The overlay appeared on the wrong screen.** It followed whichever monitor the Scryglass
  window happened to be on, because the Windows code never actually looked up where MTGA was —
  it reported "found" and nothing else. It now reads MTGA's real window rectangle and sits on
  top of it, re-checking every time it appears, so moving MTGA to another monitor mid-session
  works too.
- **Updating was blocked by Scryglass's own background process.** The installer closes the app,
  but `glass-host.exe` is a separate process it never knew about, so it stayed running and
  Windows refused to replace it. The updater now stops it first — and starts it again if the
  install fails, instead of leaving the app running with nothing behind it.

### Added

- **Failures are now reported to the cloud** (and only when cloud sync is on — turning sync off
  silences this too). When the backend cannot start, the app sends what it knows along with the
  tail of its own log, so a problem on a tester's machine can be looked at without asking them
  to find and paste a file.

## [0.8.40] — 2026-08-17

### Fixed

- **The advisor never saw a single game on any computer not set to US English.** MTGA writes its
  log using the machine's own date format, and Scryglass only recognised the American one
  (`8/17/2026 10:23:47 PM`). On a Czech or German Windows the very same line reads
  `17.08.2026 23:57:36`, and the advisor read *nothing at all* from a log that was present,
  readable and full of the game it was looking for — so it never noticed a match had started and
  the overlay never appeared. Every part of the app reported itself healthy, because every part
  of it was.

### Added

- **The diagnostics log now says whether the engine reads anything from MTGA's log.** The
  previous build narrowed a "no overlay" report to one line — MTGA in the foreground, but no
  match ever detected — and then ran out of things to say: it proved the log file was readable
  and proved no match was active, with nothing in between. It now records the first time it
  successfully parses the live log, and every change to the match-active flag, so "MTGA isn't
  writing match data" and "we read it but never register the match" are finally
  distinguishable.

## [0.8.39] — 2026-08-17

### Added

- **The app now says why it failed.** A Windows tester reported "it starts, but there's no
  overlay and it never sees the game", and nothing anywhere could explain it: every window in
  the app loads from the local backend, so when that backend does not start there is no UI left
  to report from, and all the diagnostics went to a console that a Windows app does not have.
  Scryglass now writes a log file (menu bar → **Open Diagnostics Log**, always available), shows
  a dialog naming the problem and the log when the backend fails to start, and records whether
  it can read MTGA's `Player.log` at all — an unreadable log used to look exactly like a quiet
  one. The overlay also records which of its two conditions is unmet, so "no overlay" now says
  whether MTGA wasn't in the foreground or no match was detected.

### Fixed

- **Eight cards gained 2 life they do not have.** The compiler read the phrase "you gained life
  this turn" — a CONDITION, or a reference to an amount — as an instruction to gain life, and
  invented it. Tragedy Feaster gained 2 life every time it was cast. On Will, Scion of Peace and
  Gumdrop Poisoner the invented life gain was the *only* thing that compiled, replacing a cost
  reduction and a targeted -4/-4 respectively.
- **Twenty-eight cards untapped themselves for free.** "Untapped" is an adjective, and the
  compiler matched it as the verb — so every card that costs "tap two untapped creatures you
  control" also granted an untap it never had. Tangle Tumbler, Archangel of Tithes, Spelunking
  and Caparocti Sunborn among them.
- **Five cards scried when they should not.** A rule written for Warden of the Inner Sky fired on
  any tap-cost card mentioning a +1/+1 counter and gave it a fixed body. Baylen, the Haymaker now
  gets its three counters instead of one and its trample; Dust Animus keeps the counters it enters
  with instead of an ability that could be activated.
- **Twelve "Infusion" cards were read wrong.** The keyword is a label, but it hid each card's
  real text from the compiler. Old-Growth Educator's two +1/+1 counters were applied as it was
  cast rather than when it entered the battlefield.

## [0.8.38] — 2026-08-14

### Fixed

- **Sixteen cards that remove a permanent were wrong, and seven of them did nothing at all.**
  The "exile it until this leaves the battlefield" effect was fully built and used by 27 cards,
  but only two kinds of card could reach it, so the same sentence printed on a creature or a
  plain enchantment compiled to nothing: Aang's Iceberg, Werefox Bodyguard, All-Fates Stalker,
  Earth Kingdom Jailer, Mardu Siegebreaker, Turncoat Kunoichi and Celebrate the Mountain-king
  were all inert.
- **Five more exiled the permanent FOREVER.** Driftgloom Coyote, Henchbots, Perilous Snare,
  White Auracite and Dusk Rose Reliquary all give the card back when they leave; the advisor
  believed the removal was permanent, so it over-valued them and mis-planned around an opponent
  who was in fact getting their creature back.
- **Liminal Hold and Prayer of Binding lost their exile entirely** and counted only as "gain 2
  life" — the whole point of both cards was missing.
- **Assimilation Aegis and Glass Casket could be aimed at things they cannot touch.** Both say
  "target creature"; the advisor had been offered any nonland permanent.

## [0.8.37] — 2026-08-14

### Fixed

- **Earthbender Ascension was treated as a free pump on every land drop.** The card puts a quest
  counter on itself when a land enters, and only once it has four does it grow a creature. The
  advisor skipped the counting and behaved as though every land you played put a +1/+1 counter on
  a creature — so it valued the enchantment, and boards built around it, higher than the card can
  actually deliver. It now counts to four like the card does. (The trample the fourth trigger
  grants is still not modelled.)

### Changed

- **Uploads are compressed, and several times smaller.** Telemetry went up as raw JSON until now;
  it is repetitive text, which compresses well, so the same data costs a fraction of the
  bandwidth. Nothing about what is sent has changed.
- **Finished games now upload as a full replay.** The corpus that measurement runs on used to be
  rebuildable only from the raw game log on one machine, and the game keeps only the last two of
  those — so every rotation destroyed matches nothing could reconstruct. The app now builds the
  record itself when a match ends.

## [0.8.36] — 2026-08-14

### Fixed

- **Fugitive Droid threw itself away for nothing.** Its ability — sacrifice it to counter a spell
  aimed at your own artifact or creature — was read as the sacrifice alone, so the advisor treated
  it as a play with no upside and never suggested it, nor counted it as interaction you were
  holding. It counters now, and only against spells actually pointed at your board, which is what
  the card says. Fear of Impostors' enter-the-battlefield counter had the same gap and also works.

## [0.8.35] — 2026-08-14

### Fixed

- **202 of The Hobbit's cards were nameless to the advisor.** Two card files ship with the app —
  one the engine reasons with, one that turns the game's internal card ids into names — and only
  the first had been updated for the new set. When an opponent played a Hobbit card the board
  showed an unnamed placeholder, and the opponent model refused to work for that decision
  entirely, without any error. On a real game log this took the model from 100% coverage down to
  97%, and it would have got steadily worse as more people picked up the new set. All 349 missing
  cards are in now.

## [0.8.34] — 2026-08-13

### Added

- **Opponent recognition nearly doubled its archetype list, 19 → 37.** The metagame pipeline had
  been running on one source because the second needed a browser session; it now has both, so the
  ladder archetypes that only show up in play — not in curated deck sites — are recognised too.
  Coverage is the thing that limits recognition, so this is the lever that matters.
- **Every one of the 37 has a measured interaction profile** read off a real decklist, so the
  advisor knows whether that deck holds counterspells and removal rather than assuming it holds
  none.

## [0.8.33] — 2026-08-13

### Fixed

- **An ability that costs you a creature now actually costs you one.** The Soul Stone, The
  Terminus of Return, Tithing Blade and Jade Seedstones all charge "exile a creature you
  control" as part of an activation. The advisor knew the effect and not the price, so it would
  offer the play with no creatures on board and treat it as free — a line that cannot legally
  be made.
- **Three cards drew two cards where they draw one.** Delivery Moogle, Fang-Druid Summoner and
  Guidelight Pathmaker each fetch a single card; the compiler counted the closing "if you search
  your library this way, shuffle" as a second search.

## [0.8.32] — 2026-08-13

### Fixed

- **83 double-faced cards were colourless to the advisor.** Scryfall reports those cards' colours
  per face, and the card compiler was reading a field that isn't there — so every transforming
  card in the format read as colourless. That broke colour-matters effects in both directions at
  once: they could not be hit by "target permanent that's one or more colors", and they counted as
  colourless spells. Found by an adversarial review of the Ugin work, and live since well before it.
- **The engine could cast X spells for free with a large X.** When a card is cast "without paying
  its mana cost", the rules fix X at 0; the engine was still asking for a value. It was quietly
  playing better than the rules allow — in its own simulations and in the advice built on them.
- **Ugin, Eye of the Storms is fully modelled.** Only his first exile trigger was; the advisor
  saw nothing when a colorless spell was cast and nothing at all from his ultimate. All of it
  works now — the repeatable "whenever you cast a colorless spell" exile, with the colour check
  that keeps it from firing on everything, and the −11, which exiles every colorless nonland
  card in the library and lets you cast them for free that turn. He was the top opponent-side
  card the advisor could not read correctly.

## [0.8.31] — 2026-08-13

### Fixed

- **The advisor thought 21 cards drew you a card when they don't.** A shortcut in the card
  compiler treated any "search your library" as "draw a card" — a fair approximation when the
  searched card ends up in your hand, but a fabrication everywhere else. Ugin's ultimate exiles
  the cards it finds, Wood Elves puts a Forest onto the battlefield, Vile Entomber puts a card
  in the graveyard: in each case the advisor was planning with a card that never existed. The
  approximation now applies only when the card actually reaches a hand. Four land-fetching
  creatures additionally got their real effect instead of the draw, and Claim Jumper stopped
  drawing twice.

## [0.8.30] — 2026-08-13

### Fixed

- **The Infinity Stones did nothing.** The Mind Stone, The Soul Stone and The Terminus of
  Return are the three most-seen cards in the advisor's fidelity queue that it could not
  model — all three on opponents' boards. Their "Harness" ability compiled to nothing, so
  the advisor thought six mana bought your opponent no threat at all, and their ∞ ability
  was compiled in a form that could never fire. Both halves work now: the advisor knows a
  harnessed Stone will blink a permanent at end step or reanimate a creature at upkeep,
  and it knows an unharnessed one does not.

## [0.8.29] — 2026-08-13

### Added

- **The Hobbit.** All 188 cards of the new set are in the advisor's database, so it no longer reads
  a Hobbit card as a blank. Two of the set's mechanics are modelled properly rather than skipped:
  **recruit** (ten cards that previously compiled to nothing at all) and **Storied** — the
  "enduring story" latch, which stays on for the rest of the game once it turns on, plus eight of
  its nine payoff cards (Bombur's untap, Balin's burn, Thorin's ward at his own cost, Kíli's free
  equip, Bifur's extra trigger). Dáin's attack tax is deliberately NOT modelled — it needs a cost
  step the engine does not have yet, and it is recorded as a known gap instead of silently ignored.

### Fixed

- **Opponent recognition was running on a two-month-old metagame.** The archetype list the advisor
  identifies opponents against was built on 20 June and could not even name 13 of the 19 decks
  currently being played. Measured top-1 recognition on today's decks: **26% before, 98% after**.
  Two separate causes, both fixed: the meta pipeline was built against a card pool that omitted
  608 Standard-legal cards (whole decks were being dropped over cards the app does have), and the
  archetype list was being filtered down to decks the simulator can play — an irrelevant
  restriction for merely *naming* an opponent.
- **Every archetype now has a measured interaction profile** (whether that deck holds counterspells
  and removal), 19 of 19, each read off a real decklist. Archetypes without one previously fell
  back to an all-zero profile, which reads as "runs no interaction" when the truth is "not measured".
- **Match records now carry MTGA's event id**, so Brawl and limited games can be told apart from
  Standard instead of being mixed into the same statistics.

## [0.8.28] — 2026-08-10

### Fixed

- **The follow / don't-follow effect was invisible.** Shipped in 0.8.27 and never seen once in real
  play, for two independent reasons. It was drawn as a 2px glow along the left edge of the advice
  card — which is exactly where the 3px priority stripe sits, so it was painted underneath an
  opaque bar. And it was cleared whenever new advice arrived, which the telemetry says happens
  within the effect's own 1.3-second window **57% of the time, median 0.0 seconds**. It now lives
  on its own layer over the whole panel, with its own timer, and nothing in the advice path can
  cancel it: a soft green wash when you follow the recommendation, amber when you play something
  else.

## [0.8.27] — 2026-08-10

### Added

- **The advice panel tells you which decision it belongs to.** A line under the recommendation
  reads `advice for: T6 · Main 1 · your turn`, and once the game moves past that decision it
  changes to `— superseded` and the recommendation itself dims. It stays readable — the play may
  still be right — but it stops looking current. Before this, advice for a phase you had already
  left was indistinguishable from advice for the board in front of you.
- **Whose turn it is AND who holds priority.** Two rows under the phase:

      T6 · Main 1
      YOU  ● active, opp responding
      OPP  ● responding

  The green dot marks who holds priority. These are different things: on your own turn, the
  opponent holding priority is the moment a combat trick or a removal spell arrives. The app has
  been receiving both values all along and showing neither.
- **A note instead of an empty panel.** When there is genuinely nothing to recommend the advisor
  used to go blank, which looks the same as a broken advisor. It now says "No play recommended
  right now." on your turn, or "Watching the opponent's turn…" on theirs.
- **A brief pulse when you make a play the advisor scored.** Green if you followed the
  recommendation, amber if you played something else. One 1.6-second glow on the card, then gone —
  it is feedback on the play you just made, not advice about the next one. The app has been
  recording this in the background for a long time without ever showing it to you.

## [0.8.26] — 2026-08-10

### Changed

- **The deep search no longer overwrites the instant recommendation.** For every main-phase
  decision the app shows a fast pick immediately, then re-runs the same decision with the
  Monte-Carlo search and, until now, replaced the on-screen advice whenever the search
  disagreed. Those replacements were measured and they were **worse than the pick they
  replaced**: 92 of them were rendered as blinded A/B positions and judged by two independent
  raters who could not see which pilot proposed what, and the search won 34% of the decisive
  comparisons — 0.338 with a 95% confidence interval of [0.238, 0.456] after controlling for a
  bias the judges have toward developing the board. Every interval excluded parity. So the
  search now stays quiet when it disagrees.
- **It still tells you when it AGREES.** When the search lands on the same play the instant
  advice named, that still arrives and the key-play badge still reads "✓ verified". You lose
  the "↻ refined" replacements and keep the confirmation.
- **One exception, deliberately kept:** an override into a reactive "trick" instant is still
  published. The instant heuristic scores that whole card class below "pass" by design, so
  there the search is not correcting noise but working around a hard limit.

### Fixed

- **The "✓ verified" badge could appear on advice the search had CHANGED, not confirmed.** When
  the instant advice recommended nothing and the search named a play, the badge compared against
  an empty string and read as a confirmation — telling you the search had verified a
  recommendation that never existed. It now reads "↻ refined".
- **A low-confidence warning or a synergy hint could silently replace a real recommendation.**
  When the search's own answer was "pass" but the engine also had a card-recognition warning or a
  board synergy to report, that converted into a message with no recommended card and overwrote
  the play on your screen. It is now suppressed like any other override.

### Internal

- Releases now pin the exact engine commit (`.glass-shard-sha`) and fail the build if the
  checkout does not match it. Previously each platform checked out the engine's default branch
  independently, so a macOS and a Windows build of the same release could contain different
  engines and nothing recorded which one shipped.

## [0.8.25] — 2026-08-08

### Fixed

- **The advisor was reconstructing every combat decision as if it were your precombat
  main phase.** MTGA reports which step you are in — declare attackers, declare
  blockers, end step — and the host collapsed all of them to "main 1" one line before
  use. Nine and a half percent of your recorded decisions are declare-blockers ones,
  and each was reasoned about in the wrong step: what is castable at sorcery speed,
  whether a window is proactive or reactive, and how the deep search values holding a
  trick all key on that. The recorded conformance fixture had the contradiction in
  plain sight — a decision labelled "declare attackers" pinned as phase "main 1".
- **The declare-blockers step now exists in the engine at all.** Combat ran entirely
  inside the declare-attackers step, so the priority window after attackers are
  declared and the one after blockers are declared were indistinguishable. Blocks are
  the moment a combat trick becomes worth casting, and nothing could tell the two
  apart.

### Changed

- ⚠ **Combat advice may differ from 0.8.24**, and it has not run live before. Corrected
  after release: the first version of this note said the reconstructed position "no
  longer offers sorcery-speed plays during combat". That mechanism does not happen —
  measured over every combat decision since 2026-07-27, the advisor produced 139
  attackers, 33 blockers and 20 target recommendations there and **zero** main-phase
  ones, so there was nothing of the kind to stop offering. What actually changes is the
  reasoning inside the attack, block and target modes: they build their position from
  the same reconstruction, which now reports the true step, so the instant-speed
  picture, whether the opponent can act, and the deep search's rollouts all start from
  the right place. If something looks wrong in a combat window, that is where to look,
  and 0.8.24 is one tag away.

### Notes

- No card-compiler changes, so the bundled advice database is unchanged from 0.8.24.
- ⚠ **Relaunch the app after updating.** An OTA swaps the bundle; the running advisor
  keeps answering until you restart it.

## [0.8.24] — 2026-08-07

### Fixed

- **Thirty-four instants and sorceries did nothing at all when cast.** The compiler
  filed their rules text under the wrong kind of ability, and the code that resolves a
  spell never looked there — so the card went to the graveyard and the game state was
  untouched. Among them: Spectacular Pileup (its entire board wipe), Blasphemous
  Edict, Exsanguinate, No Witnesses, Trial of Agony, Dream Harvest. A further **ten
  lost only part of their text**, which is worse to spot because the spell half worked
  and the card looked fine — Rowan's Grim Search silently skipped its draw-and-lose-
  life, Great Train Heist its untap. This affected the advisor as much as the
  simulation: a line built around casting one of these was built around a spell that
  does nothing.
- **"You gain that much life plus 1 instead" now happens.** Leyline of Hope and Angel
  of Vitality had no runtime for that clause at all, so in a lifegain deck every
  single life gain was one short, all game. Leyline of Hope is in six of your
  decklists and forty of your recorded matches.

### Notes

- The advice database is recompiled, which is what actually carries the spell fix into
  the installed app.
- ⚠ **Relaunch the app after updating.** An update swaps the bundle, but the advice
  engine keeps running the previous build until the app is restarted — on 2026-08-06 a
  whole evening of games was played by a nine-day-old engine that way.

## [0.8.23] — 2026-08-06

### Fixed

- **An Aura or Equipment stopped counting as soon as the advisor looked past this
  turn.** The bonus reached the advisor only as a correction applied to the current
  board, and that correction expired at the first simulated turn boundary — so the
  number you saw was right, but every line the search explored beyond this turn
  read the creature at its printed size, and lost the granted keyword with it. The
  attachment itself is now part of the position. The card that surfaced it is
  Sheltered by Ghosts, on the board in 58 of your recorded matches — more than any
  other card in this class.
- **Animated lands and crewed Vehicles were not creatures to the advisor.** A
  Restless-cycle land that had been activated, or a Vehicle you had crewed, kept
  its real power and toughness but was never counted as an attacker or a blocker —
  on either side of the table. Some permanent that can become a creature was on the
  board in 14.9% of your recorded decisions, and every confirmed case of one
  actually attacking was the opponent's, so the loss landed mostly on blocking
  advice.
- **Your creatures were being treated as permanently bigger than they are.** A
  static ability that only grants its bonus under a condition — "as long as you
  have at least 7 life more than your starting life total, creatures you control
  get +2/+2" — had its condition ignored, so the bonus applied at every life
  total. The advisor then valued attacks and blocks on a board bigger than the one
  on the table. Seven cards were in that shape; the one that surfaced it is in your
  own deck (Leyline of Hope, present in 37 of your last 198 games).
- **Cards playable only for their cheaper alternative cost are now offered.** The
  advisor reported a card as castable only if you could pay its FULL cost, so an
  Adventure half, a warp cost, a disguise, a bargain or a prepare cost that you
  COULD afford never appeared. Roughly 240 cards in the current set have such a
  cost; Adventure is the sharpest case, because a 1-mana instant half is a real
  trick while the creature side is unaffordable.
- **An opponent's mana rock no longer counts as one mana.** A rock that taps for
  three read as one, under-stating the opponent's available mana — the direction
  that can hide a warning about a trick they can actually pay for.
- **"Up to one target" spells are no longer treated as needing a target.** 152
  abilities that let you choose ZERO targets were judged unplayable on an empty
  board, so the advisor never offered them and the simulation never expected the
  opponent to cast them.
- **High Noon's spell cap now exists in the simulation.** "Each player can't cast
  more than one spell each turn" compiled to nothing, so every line the search
  explored let both players cast freely under it. ⚠ Partial: the simulation
  respects the cap, but the advisor's own legality check still cannot count the
  spells you have cast this turn, so it will not stop you from being offered a
  second one. Rare — the card was on the board in 4 recorded matches.

### Diagnostics

- **Each match now records which build played it.** On 2026-08-06 a whole evening
  of games turned out to have been played by the v0.8.21 engine — nine days after
  v0.8.22 was installed. An update swaps the app bundle, but the advice engine only
  restarts when you RELAUNCH the app, and nothing recorded which one was live.
  Recovering it afterwards was possible only for the four decisions that happened to
  be flagged. ⚠ The behaviour itself is unchanged: **relaunch the app after an
  update, or you keep the previous engine.**
- **Games now record what MTGA actually allowed at each decision.** The tooling
  that grades the advisor's own advice was checking it against the client's card
  MENU, which includes greyed-out entries and spells you cannot yet pay for —
  measurably too permissive, so a clean grade meant less than it looked. The real
  per-moment list is now recorded alongside it. Nothing you see changes; games
  played from this build on can be graded honestly, and games before it cannot.

## [0.8.22] — 2026-07-28

**The advisor now has an opponent model.** Until now it simulated your opponent as
holding *nothing* — every line it recommended was scored against an empty hand. It
now infers a posterior over their likely decklist from what they have publicly
played and seeds a coherent hidden hand and library into the search.

- **Opponent hand/library are inferred, not blank.** A prior over the current
  Standard archetypes is updated by subtracting the cards you have actually seen,
  then a consistent world is sampled for the search to reason about. Available on
  ~90% of decisions; the stricter band used for stated probabilities is 53%.
- **The belief prior now ships with the app** — it was computed but never bundled,
  so the feature was inert in the installed build.
- **Unknown deck slots are represented.** When an opponent demonstrably owns more
  cards than any known list explains (a 61-card deck, an off-meta brew), the
  extra slots are modelled as unidentified cards instead of the whole hypothesis
  being thrown away. That took world availability from 82% to 90%, and from 55% to
  100% against the one real oversized deck in the corpus.

### Fixed

- **A card in the simulation could invent a board wipe that does not exist.** The
  placeholder used for an unidentified card carried a sentinel mana value of 255.
  Anything reading a mana value read 255: a card that deals damage equal to the
  greatest mana value discarded dealt **255 damage to every creature**, and
  card-selection heuristics preferred the placeholder over every real card. It is
  now a zero-cost, non-permanent placeholder, so it cannot be put onto the
  battlefield, cannot be sacrificed, and cannot inflate any damage calculation.
- The advise database is recompiled so the shipped build actually contains that
  fix (4852 cards, none lost).
- The probability that an opponent holds a specific card disagreed with the hands
  the simulation actually dealt, for unidentified cards. The two now agree.
- The belief delivery check was not a gate: it reported success without running
  the script it claimed to run.

## [0.8.21] — 2026-07-27

### Fixed

- **"Remove their big creature with …" no longer names a card that cannot remove
  anything.** Reported from a game where the advice was to answer a 9/4 with
  Llanowar Elves. The rule behind it asks for a card whose ROLE is removal, and
  the engine had quietly stopped enforcing that half of the condition — so any
  castable card in hand qualified. Measured across your own logged games: the
  advice fired 176 times and 66 of the 161 resolvable ones named something that
  is not removal (Healer's Hawk, Ajani's Pridemate, Defend the Rider). It now
  uses the engine's own classifier, so "removal" means a spell that actually
  destroys, exiles or burns a creature.
- **The overlay stops showing advice from a turn that has passed.** When there is
  nothing to recommend, the advisor deliberately says nothing — and the overlay
  was leaving the previous recommendation on screen instead of clearing it. That
  is why a main-phase suggestion could still be sitting there during the end
  step, where the play it suggests is not even legal, and why a synergy note
  could outlive the turn that earned it. A quiet turn is now a blank panel.

## [0.8.20] — 2026-07-27

### Fixed

- **The advisor no longer tells you to cast a spell in a colour you don't have.**
  Reported from two games: it recommended a white one-drop on a board of one
  Hushwood Verge and no Plains. A Verge only makes its second colour while you
  control the land it asks for ("Activate only if you control a Forest or a
  Plains"), and the mana model never read that condition — so it credited both
  colours from an empty board. This is the whole Verge cycle, ten lands in
  Standard, plus Mox Jasper's "you control a Dragon": any deck on them could be
  told to cast a card it cannot pay for. The three flagged decisions now
  recommend green cards the board actually pays for.
- **"Castable" no longer includes spells with nothing to target.** A Treasure
  covering the cost short-circuited the check that a spell has a legal target,
  so a pump with no creature on board was listed as castable.
- **Opponent hand size is read again.** Cards in an opponent's hand are hidden,
  and the counter walked hidden objects — so it reported an empty hand in
  essentially every game. It now counts the zone.
- **A token no longer counts as evidence of what the opponent's deck is.**
  Archetype identification treated created tokens as cards it had seen played,
  so a board of Treasures or Clues pushed the deck read toward whatever list
  happens to contain those names.
- **An archetype with no measured interaction data no longer reports zero.** The
  fallback wrote a literal "runs 0 tricks" for archetypes nobody had measured,
  which reads as "they have nothing" instead of "we don't know".

### Notes

- The transformed-permanent reconstruction shipped in 0.8.19's notes was
  reverted: it made the lethal banner over-count. Back-face support is coming as
  its own change rather than a partial one.

## [0.8.19] — 2026-07-25

### Added

- **Combat advice finally accounts for instant-speed play — on both sides.**
  Attack and block advice was computed from the board alone: it could not see
  what you were holding or how much mana your opponent had up, and recommended
  every swing as if neither player could act. Your side is now read as fact (a
  castable trick, a flash creature, an uncracked fetch land you should hold);
  their side as what they could physically do with the mana they have open.
  The opponent warning only appears through turn 6, where an open opponent is
  measurably a 9× blowout risk in your own games — later it is noise, so it
  stays quiet rather than crying wolf.
- **The "they likely run board wipes, hold some back" advice appears.** The
  opponent-archetype layer had been computing this all along and then throwing
  it away — nothing on the display side ever read the field it wrote to.
  Capped at two notes, because a wall of caveats teaches you to stop reading.

### Fixed

- **Three more ways the lethal banner could lie.** Two 2/1 first strikers were
  tested one at a time against a 6/3 trampler, so a board that stops the swing
  cold read as harmless; damage already marked on an attacker was ignored, so a
  wounded trampler looked harder to stop than it is. The third was the opposite
  error — capping the search at 8 attackers threw away *real* lethals on the
  most common alpha-strike board there is, nine tokens into one blocker.
- **Lethal is no longer promised when a planeswalker or battle can soak the
  attack.** Part of that damage may not be going at the face, so the swing that
  "wins this turn" doesn't. The advice now describes the damage without
  promising the kill.
- **Blocking advice tells two identically named attackers apart.** A base 2/2
  and a pumped 5/5 sharing a name were matched by name against the board, so
  block advice could reason about a threat less than half the real one — or drop
  attackers entirely and suppress a lethal warning. Each attacker is now bound
  to its actual game object.
- **"Whenever a creature dies" fires on removal and board wipes.** Destroy
  effects moved the creature to the graveyard without it ever counting as a
  death, so every aristocrat payoff — Blood Artist, Mayhem Devil, Meathook
  Massacre — silently skipped the most common way creatures leave the
  battlefield.
- **The Vision and Three Bowls of Porridge do what they print.** A modal card
  whose text reads "choose one that hasn't been chosen this turn —" had every
  mode left dangling and unusable, so The Vision was modelled as a plain 2/5
  with no double strike and no indestructible to grant — which is how an 11/1
  came to attack into it and die.
- **A removal spell offering two different −N/−N sizes no longer gets aimed at
  the small creature.** The advisor narrowed to the weakest mode and picked the
  1/1 it could kill over the 4/4 the spell was cast to answer.

## [0.8.18] — 2026-07-25

### Fixed

- **The overlay stayed minimized for the whole match.** The v0.8.17 change that
  makes it rest as a small pill between matches never handed control back when
  a match started, so it sat collapsed the entire game. It now expands on match
  start as it always did, and only shrinks when you hover it.
- **The advisor reads +1/+1 counters again.** It only ever saw a creature's
  resulting power/toughness, never the counters themselves, so anything that
  reads counters scored as if there were none — Bristly Bill's "double the
  +1/+1 counters on each creature you control" was literally never worth
  playing to it. It now scales with the board and gets recommended when it
  should.
- **Creatures that aren't printed as creatures count again** — animated lands,
  crewed Vehicles and tokens were skipped when picking blocks and targets, so
  the advisor could tell you to take damage you could have blocked for free.

### Changed

- **Your games reach the cloud as soon as they end**, instead of waiting out the
  periodic sync — and decisions you flag in-game are uploaded too, so a flagged
  problem can be reconstructed and analysed later instead of living only on this
  machine.

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
