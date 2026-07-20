# Changelog

All notable changes to the Scryglass app are recorded here. The advisor engine
ships from `glass-shard@main` (bundled `glass-host`); versions are the Tauri app
version used for OTA updates.

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
