# Scryglass — Delivery Reliability & OTA

**Status:** proposal, 2026-07-06. Plan only — no code yet. Review before building.
**Scope:** get Scryglass reliably onto other people's Macs and keep the advice fresh.
**Design bias:** minimum that works; defer everything speculative with a named trigger.

---

## 0. The incident that triggered this

A real game (Mono White Lifegain, 2026-07-06): the advisor recommended **pass**
for 14 straight turns while the player held a castable bomb (Tigra) and lands.
Root cause was not a logic bug — the bundled advise DB was compiled **before**
the Marvel (MSH/OM1) catalog was fetched and never recompiled. **255 cards were
silently invisible** to the advisor. Nothing failed. Nothing warned. The app
looked healthy and gave confidently-wrong advice.

**Failure class: a data artifact drifted from its source with zero signal.**
This is the worst failure mode for a beta — users don't report "the card DB is
stale," they conclude the product is bad and leave.

---

## 1. Assumptions (correct these if wrong)

- Beta = a small group of individuals on macOS / Apple Silicon. Not the App Store.
- Card data changes ~every set (6–8 weeks); app/engine code changes more often
  during dev, settling over time.
- Distribution via **GitHub Releases** (chosen), repo `vbalko-claimate/scryglass`.
- You will consider paying for an Apple Developer account **if** it is the real
  blocker (it is — see Layer 2).

## 2. What I verified (not assumed)

| Fact | Evidence | Consequence |
|---|---|---|
| App is **ad-hoc signed**, `spctl: rejected` | `codesign -dv` → `flags=0x2(adhoc)`, `TeamIdentifier=not set`; `spctl -a` → `rejected` | **Will not open cleanly on any other Mac.** #1 blocker. |
| Tauri **updater already configured** | `tauri.conf.json` → `plugins.updater.endpoints` = GitHub Releases `latest.json`; minisign key at `~/.tauri/scryglass.key(.pub)` | OTA is ~80% wired, not greenfield. |
| Bundle **44 MB**, updates infrequent | `du -sh` | Separate data-pack channel **not justified yet**. |
| Build gate exists | `scripts/check_advise_db.sh` in `build_and_test.sh` (added today) | Our builds can't ship a blind advisor for known cards. |

---

## 3. The plan — cheapest first

### Layer 0 — Freshness at build time  ✅ mostly done
- **Done:** `check_advise_db.sh` fails the build if the staged advise DB is
  missing any oracle card. Verified against the incident (flagged all 255).
- **Add:** release pipeline re-runs `fetch_ub_catalog.sh` before compiling, so
  `standard_oracle_plus.json` itself can't be stale relative to the latest sets.
- **Add:** stamp a **data version** (build date + card count + oracle git sha)
  into the advise DB and surface it in the overlay / About. Staleness becomes
  visible at a glance instead of invisible.

### Layer 1 — Runtime honesty  (cheap insurance; do regardless of OTA)
When the advisor advises over a decision whose legal actions reference cards
**absent from the advise DB**:
- glass-host downgrades confidence and emits `unknown_card` telemetry.
- Overlay shows: **"⚠ N cards not recognized — advice may be incomplete."**

Why this matters even with perfect OTA: it is the **only** guard that covers
cards we never packaged — a user's off-meta brew, or a set released after their
last update. It converts *silent-wrong* into *visibly-incomplete*, which is the
difference between a user who trusts the tool and one who churns.

### Layer 2 — Signing & notarization  ⛔ THE beta blocker — solve first
Ad-hoc signing means Gatekeeper rejects the app on every other Mac. To ship to
other people you need:
- **Developer ID Application** certificate → **notarization** (`notarytool`) →
  **staple**, with hardened runtime. Tauri performs this in the bundle step
  given the cert + `APPLE_*` credentials.
- **Cost / decision:** Apple Developer Program, **$99/yr**. This gates the beta.
  Without it: users get *"Apple cannot check it for malware,"* the right-click→
  Open workaround is fragile and worsening on newer macOS — **not** recommended
  for a real beta.

> **This is the single most important decision.** OTA, data packs, and runtime
> polish are all moot if the app won't open on the target machine.

### Layer 3 — OTA updates  (mechanism already ~80% wired)
Finish and activate the existing Tauri updater:
- Ensure `bundle.createUpdaterArtifacts = true` (generates the signed update
  artifact + `.sig`).
- Call the updater plugin on launch: check → prompt (or silent) → download →
  relaunch.
- Publish each release to GitHub Releases with a `latest.json` manifest (the
  endpoint already configured).

**One channel for both code and card data:** a new set = a new release. Simple,
signed (minisign), verifiable, and rollback = pin an older release. This fully
satisfies "OTA updates and delivery" with no second mechanism to build or secure.

### Layer 4 — Data-pack OTA  ⏸ DEFERRED (explicitly not now)
Splitting card data into a separately-fetched, separately-versioned pack is real
but adds a **second** update + signing + verification path.

- **Not justified** at 44 MB / infrequent updates — Layer 3's full-bundle update
  is simpler and sufficient.
- **Revisit only when** (trigger): card-data updates clearly outpace code
  updates *and* full-bundle re-downloads become a felt cost, **or** we add
  non-macOS targets where per-platform bundles multiply the download.
- Documented here so we don't build it speculatively.

---

## 4. Recommended sequence

1. **Layer 2 (notarization)** — decide on the Apple account; unblocks shipping at all.
2. **Layer 3 (finish OTA)** — so fixes flow to betas after the first ship.
3. **Layer 1 (runtime honesty)** + **Layer 0 version stamp** — small, ship alongside.
4. **Layer 4** — deferred until the trigger.

## 5. Decisions needed from you

1. **Apple Developer Program enrollment (Y/N)?** Gates Layer 2 → gates the beta.
   If N, we ship only to people willing to do the manual first-launch override.
2. **Update UX:** auto-update silently on launch, or prompt the user?
3. **Data cadence:** auto-rebuild + release card data on every set drop, or only
   on explicit "card data" releases?

---

## Build status (2026-07-06)

- ✅ **Layer 0** — `check_advise_db.sh` build gate wired into `build_and_test.sh`.
- ✅ **Layer 1** — runtime honesty shipped: `advice_to_overlay_items` threads
  `confidence`/`confidence_basis` and emits a "cards not recognized" item instead
  of a blank when the engine is blind; overlay renders an amber caveat under the
  advice. glass-host rebuilt + hot-swapped into the installed app.
- ⛔ **Layer 2 / Layer 3 (macOS)** — gated on the Apple enrollment decision (§5.1).
  Runbook in §6. **Deprioritized:** the first beta ships on Windows (see below).

## Windows beta (the actual first target)

The first beta ships as a **Windows** app to **one** tester. This changes the
critical path — **Apple notarization is NOT a blocker for this beta.**

**Ready (verified 2026-07-06):**
- Overlay: Windows uses a Tauri `overlay` window (transparent / always-on-top /
  no decorations) + `launch_overlay_windows` (show/hide on MTGA-foreground +
  `/match-status`, click-through, Right-Win feedback hotkey). No Swift helper.
- glass-host builds for `x86_64-pc-windows-msvc`: `default_log_path` now has a
  Windows branch (`%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\
  Player.log`) and `rusqlite` uses `bundled` SQLite. No other macOS-only paths.
- Card data / Marvel fix / build gate / Layer-1 honesty are platform-agnostic.
- CI: Windows matrix entry re-enabled in `release.yml` (+ `shell: bash` default
  so the bash `run:` steps work on the Windows runner).

**Remaining:**
1. **Run the build.** Can't cross-compile from the M4 → a GitHub Actions
   `windows-latest` runner builds it (native `x86_64-pc-windows-msvc`). Trigger:
   push a `v*` tag → CI builds macOS + Windows → draft release with an NSIS `.exe`.
   First run may need 1–2 fixes for Windows specifics (glass-server build,
   WebView2/NSIS, the `windows` crate).
2. **Signing.** Unsigned → the tester clicks SmartScreen "More info → Run anyway"
   (fine for one trusted tester). Real Authenticode (or Azure Trusted Signing) is
   a later item, not needed for a 1-person beta.
3. **Deliver.** Download the `.exe` from the draft release, send it to the tester.
   OTA (Tauri updater, Windows NSIS) works the same way once wanted; for one
   tester, re-sending a new installer is fine initially.

## 6. Notarization + OTA runbook (turnkey once enrolled)

Everything here is mechanical once you have an Apple Developer account. Nothing
below can be done without your Apple credentials, so it is staged, not applied.

### 6a. Enroll (one-time, ~$99/yr, ~24–48h to activate)
1. Apple Developer Program: <https://developer.apple.com/programs/enroll/>.
2. In Xcode / developer portal, create a **Developer ID Application** certificate;
   install it in the login keychain. Note the identity name
   (`Developer ID Application: Your Name (TEAMID)`).
3. Create an **app-specific password** for `notarytool` at appleid.apple.com.

### 6b. Notarization config (`src-tauri/tauri.conf.json` → `bundle.macOS`)
```jsonc
"macOS": {
  "signingIdentity": "Developer ID Application: Your Name (TEAMID)",
  "hardenedRuntime": true,
  "entitlements": "entitlements.plist"   // if any special entitlements are needed
}
```
Build with notarization credentials in the environment (Tauri notarizes + staples
in the bundle step):
```sh
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="<app-specific-password>"
export APPLE_TEAM_ID="TEAMID"
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
# then the normal build (tools/build_and_test.sh → cargo tauri build)
```
Verify: `spctl -a -vvv Scryglass.app` must print `accepted` (today: `rejected`).

### 6c. Activate OTA (Layer 3 — do AFTER 6b; delivering a non-notarized app is broken)
1. `bundle.createUpdaterArtifacts: true` in `tauri.conf.json` (currently unset →
   no update artifacts are produced).
2. Call the updater on launch (plugin is registered at `src-tauri/src/lib.rs:161`
   but never invoked). Minimal check-on-launch (Rust, in the setup hook):
   ```rust
   // tauri::async_runtime::spawn: check → download → install → relaunch
   if let Ok(update) = app.updater()?.check().await { /* prompt or apply */ }
   ```
3. Release flow (GitHub Releases — chosen host; endpoint already in
   `tauri.conf.json`): CI builds signed + notarized artifacts and publishes
   `latest.json` + the `.app.tar.gz` + `.sig` to the release. `release.yml`
   already compiles the advise DB (Layer 0 gate applies); add the sign/notarize
   env + `createUpdaterArtifacts`. Updater artifacts are signed with the existing
   minisign key at `~/.tauri/scryglass.key`.

### 6d. Not-yet-built small items (no cert needed — can do anytime)
- **Data-version stamp** (Layer 0 finish): bake `{built, card_count, oracle_sha}`
  into the advise DB + show it in the overlay/About, so staleness is visible.
