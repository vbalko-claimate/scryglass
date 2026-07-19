# Changelog

All notable changes to the Scryglass app are recorded here. The advisor engine
ships from `glass-shard@main` (bundled `glass-host`); versions are the Tauri app
version used for OTA updates.

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
