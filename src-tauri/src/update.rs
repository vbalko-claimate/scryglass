//! Update policy for the desktop shell.
//!
//! Two things live here:
//!
//!  1. **WHEN an unattended update may restart the app.** The launch check no
//!     longer merely advertises — it installs. That makes the "am I in a match?"
//!     question load-bearing rather than advisory: a wrong answer drops the
//!     player out of a live game. `UpdateWindow` therefore has FOUR states, not a
//!     bool, because "the host could not answer" is not the same as "no match".
//!
//!  2. **The "what's changed" state that has to survive the restart.** The
//!     release notes come from the signed update manifest (`update.body`, which
//!     the release CI fills from the CHANGELOG section), so they are only in
//!     memory of the process that is about to be replaced. They are written to
//!     disk BEFORE `download_and_install`, and read back by the next launch.

// Several items here are reachable only from the release-only updater paths
// (`#[cfg(not(debug_assertions))]`), so a dev build sees them as dead. The
// allow is scoped to debug so a RELEASE build still reports genuine dead code.
#![cfg_attr(debug_assertions, allow(dead_code))]

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use tauri::Manager;

/// Lives next to the sidecar's own data — see `report::app_data_dir`, which is
/// the same `{SCRY_USER_DATA or ~/MTG/mtg-data}/app_data` that `spawn_glass_host`
/// hands glass-host. Deliberately NOT a second data root.
const STATE_FILE: &str = "update_state.json";

const KEY_LAST_SEEN: &str = "last_seen_version";
const KEY_NOTES_VERSION: &str = "staged_notes_version";
const KEY_NOTES: &str = "staged_notes";

/// Bundled fallback for the release notes (`tauri.conf.json` → resources), used
/// when nothing was staged — e.g. the user installed a DMG/MSI by hand instead
/// of taking the OTA.
const CHANGELOG_RESOURCE: &str = "CHANGELOG.md";

// ────────────────────────────── update window ──────────────────────────────

/// May an *unattended* update restart the app right now?
///
/// The variants are kept distinct instead of collapsing to a bool because the
/// two "no" reasons need different follow-up, and because collapsing an
/// unreadable answer into `false` is exactly the mistake that made the previous
/// game-aware check useless (see `probe_for_auto_apply`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpdateWindow {
    /// glass-host answered, and reports no match in progress.
    Idle,
    /// glass-host reports a match in progress — restarting would drop the
    /// player mid-game.
    MatchActive,
    /// glass-host is alive but its answer could not be read (404 from an older
    /// build, a timeout, a non-bool `active`). An unreadable condition is
    /// POSSIBLY TRUE, so this is treated as "maybe in a match".
    Unreadable,
    /// glass-host never came up. Nothing is tracking a match and no advice is
    /// being given, so a restart interrupts nothing — and an update is the one
    /// action that might actually repair this state.
    HostDown,
}

impl UpdateWindow {
    /// Only `Idle` and `HostDown` are safe for an install nobody asked for.
    pub fn may_auto_apply(self) -> bool {
        matches!(self, UpdateWindow::Idle | UpdateWindow::HostDown)
    }
}

/// Pure decision, split out from the I/O so it can be tested.
///
/// `status` is the parsed `/match-status` body, or `None` when there was no
/// readable answer at all.
pub fn classify(host_healthy: bool, status: Option<&Value>) -> UpdateWindow {
    if !host_healthy {
        return UpdateWindow::HostDown;
    }
    match status
        .and_then(|v| v.get("active"))
        .and_then(Value::as_bool)
    {
        Some(true) => UpdateWindow::MatchActive,
        Some(false) => UpdateWindow::Idle,
        // Key missing or not a bool ⇒ we did not learn anything.
        None => UpdateWindow::Unreadable,
    }
}

// ───────────────────────── what's-changed decision ─────────────────────────

/// Is `candidate` a strictly newer version than `than`?
///
/// Dotted numeric compare, tolerant of a leading `v` and of a `-pre`/`+build`
/// suffix. Anything it cannot parse answers `false`: the caller uses this to
/// decide whether to interrupt the user, and "I don't know" must not do that.
pub fn is_newer(candidate: &str, than: &str) -> bool {
    let (Some(a), Some(b)) = (numeric_parts(candidate), numeric_parts(than)) else {
        return false;
    };
    for i in 0..a.len().max(b.len()) {
        let x = a.get(i).copied().unwrap_or(0);
        let y = b.get(i).copied().unwrap_or(0);
        if x != y {
            return x > y;
        }
    }
    false
}

fn numeric_parts(v: &str) -> Option<Vec<u64>> {
    let core = v.trim().trim_start_matches('v');
    let core = core.split(['-', '+']).next().unwrap_or("");
    if core.is_empty() {
        return None;
    }
    core.split('.').map(|p| p.parse::<u64>().ok()).collect()
}

/// Should this launch show the "what's changed" panel?
///
/// `last_seen` is the version recorded by the previous run; `None` means there
/// is no record at all — a FRESH INSTALL, which must not be greeted with
/// release notes for software the user has never run.
pub fn should_show_whats_changed(last_seen: Option<&str>, current: &str) -> bool {
    match last_seen {
        None => false,
        Some(prev) => is_newer(current, prev),
    }
}

/// Pull the `## [VERSION]` section out of a keep-a-changelog file.
///
/// The heading is matched with its closing bracket so `0.8.6` cannot match
/// `## [0.8.64]`. Returns `None` when the version has no section or the section
/// is empty.
pub fn changelog_section(changelog: &str, version: &str) -> Option<String> {
    let heading = format!("## [{version}]");
    let mut body: Vec<&str> = Vec::new();
    let mut inside = false;
    for line in changelog.lines() {
        if line.starts_with("## ") {
            if inside {
                break;
            }
            inside = line.starts_with(&heading);
            continue;
        }
        if inside {
            body.push(line);
        }
    }
    while body.first().is_some_and(|l| l.trim().is_empty()) {
        body.remove(0);
    }
    while body.last().is_some_and(|l| l.trim().is_empty()) {
        body.pop();
    }
    if body.is_empty() {
        None
    } else {
        Some(body.join("\n"))
    }
}

// ──────────────────────────────── persistence ───────────────────────────────

fn state_path(app: &tauri::AppHandle) -> PathBuf {
    crate::report::app_data_dir(app).join(STATE_FILE)
}

fn read_state(path: &Path) -> Value {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

/// Merge `edit` into the state file, preserving every key it does not touch.
fn write_state(app: &tauri::AppHandle, edit: impl FnOnce(&mut serde_json::Map<String, Value>)) {
    let path = state_path(app);
    let mut state = read_state(&path);
    let Some(obj) = state.as_object_mut() else {
        return;
    };
    edit(obj);
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Err(e) = std::fs::write(&path, state.to_string()) {
        crate::diag::log(&format!("[update!] cannot write {}: {e}", path.display()));
    }
}

/// Remember the release notes for the version we are about to install, so the
/// process that replaces this one can show them. Best effort — a failure here
/// costs the "what's new" panel, never the update.
pub fn stage_notes(app: &tauri::AppHandle, version: &str, notes: Option<&str>) {
    let notes = notes.map(str::trim).filter(|s| !s.is_empty());
    write_state(app, |obj| {
        obj.insert(KEY_NOTES_VERSION.into(), json!(version));
        match notes {
            Some(n) => obj.insert(KEY_NOTES.into(), json!(n)),
            None => obj.remove(KEY_NOTES),
        };
    });
    crate::diag::log(&format!(
        "[update] staged release notes for v{version} (have_body={})",
        notes.is_some()
    ));
}

/// Forget staged notes — the install we staged them for did not happen.
pub fn clear_staged_notes(app: &tauri::AppHandle) {
    write_state(app, |obj| {
        obj.remove(KEY_NOTES_VERSION);
        obj.remove(KEY_NOTES);
    });
}

/// The release notes to show on this launch, or `None`.
///
/// Does NOT record anything: the caller records only once the panel has actually
/// been handed to a window, so a failed delivery retries on the next launch.
pub fn pending(app: &tauri::AppHandle, current: &str) -> Option<String> {
    let state = read_state(&state_path(app));
    let last_seen = state.get(KEY_LAST_SEEN).and_then(Value::as_str);
    if !should_show_whats_changed(last_seen, current) {
        return None;
    }
    // The staged body wins: it is the notes for exactly this version, signed
    // into the manifest the user actually installed.
    let staged = state
        .get(KEY_NOTES_VERSION)
        .and_then(Value::as_str)
        .filter(|v| *v == current)
        .and_then(|_| state.get(KEY_NOTES).and_then(Value::as_str))
        .map(str::to_string)
        .filter(|s| !s.trim().is_empty());

    Some(
        staged
            .or_else(|| bundled_changelog(app).and_then(|md| changelog_section(&md, current)))
            .unwrap_or_else(|| format!("Scryglass was updated to v{current}.")),
    )
}

fn bundled_changelog(app: &tauri::AppHandle) -> Option<String> {
    let path = app
        .path()
        .resolve(CHANGELOG_RESOURCE, tauri::path::BaseDirectory::Resource)
        .ok()?;
    std::fs::read_to_string(path).ok()
}

/// Record that `current` has now been seen, and drop the notes we just used.
///
/// Called on EVERY launch — including the fresh-install path that shows nothing
/// — because without a record the next update would look like a fresh install
/// too and stay silent forever.
pub fn mark_seen(app: &tauri::AppHandle, current: &str) {
    write_state(app, |obj| {
        obj.insert(KEY_LAST_SEEN.into(), json!(current));
        // ⚠ Only drop the notes we just consumed. `try_auto_apply` may already
        // have staged notes for the version it is INSTALLING, and this runs on
        // the same launch (both are spawned from `setup`) — clearing
        // unconditionally would race it and throw those away.
        if obj.get(KEY_NOTES_VERSION).and_then(Value::as_str) == Some(current) {
            obj.remove(KEY_NOTES_VERSION);
            obj.remove(KEY_NOTES);
        }
    });
}

// ─────────────────────────────── delivery (JS) ───────────────────────────────

/// Encode a Rust string as a JS string literal for `eval`.
///
/// JSON string syntax is a subset of JS string syntax, so `serde_json` does the
/// escaping. U+2028/U+2029 are stripped first: JSON passes them through
/// unescaped and they were illegal inside a JS string literal before ES2019.
fn js_string(s: &str) -> String {
    let cleaned: String = s.replace(['\u{2028}', '\u{2029}'], " ");
    serde_json::to_string(&cleaned).unwrap_or_else(|_| "\"\"".into())
}

/// Show the release notes for this build in the main window, once.
///
/// Runs on every launch (not just after an OTA) so a hand-installed DMG/MSI
/// upgrade is covered too. Records "seen" only after a window has taken the
/// call, so a delivery that lands nowhere retries next launch instead of
/// silently burning the one chance to show it.
pub async fn deliver_whats_changed(app: &tauri::AppHandle) {
    let current = app.package_info().version.to_string();
    let Some(notes) = pending(app, &current) else {
        mark_seen(app, &current);
        return;
    };
    crate::diag::log(&format!(
        "[update] first run of v{current} — showing what's changed"
    ));

    // Every window in this app loads from :8765; pushing before the page exists
    // would eval into the old/blank document.
    for _ in 0..60 {
        if crate::sidecar::check_health().await {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
    // …and health only says the SERVER is up; give the page a moment to load
    // shell.js, which is what defines `showWhatsChanged`.
    tokio::time::sleep(std::time::Duration::from_secs(3)).await;

    // ⚠ DELIVER REPEATEDLY, not once. `eval` reports that the script was
    // dispatched, never that the page had shell.js loaded — and the main window
    // re-navigates to :8765 as soon as `start_and_wait` returns, which throws
    // away anything evaluated a moment earlier. A single attempt is therefore a
    // coin flip. Re-sending is safe because the panel's dismissal is persisted
    // in localStorage (see `showWhatsChanged`), so a dismissed panel stays
    // dismissed no matter how many times this arrives.
    let js = format!(
        "window.showWhatsChanged&&window.showWhatsChanged({}, {})",
        js_string(&current),
        js_string(&notes)
    );
    let mut delivered = false;
    for _ in 0..12 {
        if let Some(w) = app.get_webview_window("main") {
            delivered |= w.eval(&js).is_ok();
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
    if delivered {
        mark_seen(app, &current);
    } else {
        crate::diag::log("[update!] could not deliver what's-changed — will retry next launch");
    }
}

// ───────────────────────────── automatic install ─────────────────────────────

/// `/match-status` is the tracker's own show/hide signal for the overlay, and
/// the only place that knows whether a game is live.
///
/// ⚠ NOT `/active`, which the previous game-aware check asked for: that route
/// does not exist (verified 404 against a running glass-host), so the check
/// failed safe to "no match" every single time.
#[cfg(not(debug_assertions))]
const MATCH_STATUS_URL: &str = "http://localhost:8765/match-status";

/// How long to wait for glass-host before concluding it is not coming up.
#[cfg(not(debug_assertions))]
const HOST_WAIT_SECS: u64 = 60;

/// One read of `/match-status`. `None` = no readable answer.
///
/// A bounded timeout, and a timeout yields `None` (→ `Unreadable`, never
/// `Idle`): `reqwest` has no default timeout, and a hung read must not be
/// mistaken for "no match".
#[cfg(not(debug_assertions))]
async fn read_match_status() -> Option<Value> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .ok()?;
    let resp = client.get(MATCH_STATUS_URL).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    resp.json::<Value>().await.ok()
}

/// Ask glass-host whether an UNATTENDED restart is safe right now.
///
/// Waits for the host, because at launch it is still starting: answering before
/// it is up would classify every launch as `HostDown` and auto-apply blindly.
///
/// The answer is only meaningful once the host has replayed the existing log —
/// a match that started before Scryglass launched must still read as active.
/// `watcher_task` does that catch-up while holding the tracker mutex and
/// `/match-status` takes the same mutex, so an ANSWER already implies the
/// catch-up finished; the extra settle is belt-and-braces against that
/// invariant changing in the host.
#[cfg(not(debug_assertions))]
pub async fn probe_for_auto_apply() -> UpdateWindow {
    let mut healthy = false;
    for _ in 0..HOST_WAIT_SECS {
        if crate::sidecar::check_health().await {
            healthy = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
    if !healthy {
        return classify(false, None);
    }
    tokio::time::sleep(std::time::Duration::from_secs(3)).await;
    classify(true, read_match_status().await.as_ref())
}

/// True when a match is definitely in progress. Used by the MANUAL tray flow.
///
/// Deliberately does NOT wait for the host the way the unattended probe does:
/// the user is sitting in front of a menu they just clicked, and a dead backend
/// must not cost them a minute of nothing happening. Two asymmetries with the
/// unattended path, both on purpose — no waiting, and an unreadable answer does
/// not block someone who explicitly asked to update.
#[cfg(not(debug_assertions))]
pub async fn match_definitely_active() -> bool {
    let healthy = crate::sidecar::check_health().await;
    classify(healthy, read_match_status().await.as_ref()) == UpdateWindow::MatchActive
}

/// The launch path: install the update without asking, unless doing so would
/// interrupt a live match.
///
/// Returns only when it DECLINED — a successful install restarts the process.
/// A decline is not an error: the caller falls back to advertising the update
/// (tray title + in-app banner), and the next launch tries again.
#[cfg(not(debug_assertions))]
pub async fn try_auto_apply(app: &tauri::AppHandle, update: tauri_plugin_updater::Update) {
    let v = update.version.clone();
    let window = probe_for_auto_apply().await;
    crate::diag::log(&format!(
        "[update] v{v} available at launch — auto-apply window: {window:?}"
    ));
    if !window.may_auto_apply() {
        crate::diag::log(
            "[update] NOT auto-applying (would interrupt a match, or the match state is \
             unreadable) — advertising instead, will retry next launch",
        );
        return;
    }

    // Before the restart, not after: `update.body` only exists in this process.
    stage_notes(app, &v, update.body.as_deref());

    // ⚠ STOP THE SIDECAR FIRST — same reason as the manual flow: Windows cannot
    // replace a binary a running process holds open, and the installer's
    // "close related apps" step never sees glass-host.exe.
    crate::diag::log("[update] stopping glass-host so the installer can replace it");
    crate::sidecar::kill_stale_sidecars();
    match update.download_and_install(|_, _| {}, || {}).await {
        Ok(()) => {
            crate::diag::log(&format!(
                "[update] installed v{v} automatically — restarting"
            ));
            app.restart()
        }
        Err(e) => {
            crate::diag::log(&format!("[update!] automatic install failed: {e}"));
            // Nothing will show those notes now, and leaving them staged would
            // make a LATER launch of this same version show them.
            clear_staged_notes(app);
            // We killed the backend for an install that did not happen; without
            // this every window in the app is a blank page.
            if let Err(e) = crate::sidecar::start_and_wait(app).await {
                crate::diag::log(&format!("[update!] could not restart glass-host: {e}"));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// The four-way classification. The load-bearing row is the third: an
    /// unreadable answer from a LIVE host must not read as "no match" — that
    /// is precisely the bug the `/active` 404 caused, and after this change the
    /// consequence is an install during a game rather than a missed banner.
    #[test]
    fn classify_separates_no_match_from_no_answer() {
        assert_eq!(
            classify(true, Some(&json!({"active": true}))),
            UpdateWindow::MatchActive
        );
        assert_eq!(
            classify(true, Some(&json!({"active": false}))),
            UpdateWindow::Idle
        );
        assert_eq!(classify(true, None), UpdateWindow::Unreadable);
        assert_eq!(
            classify(true, Some(&json!({}))),
            UpdateWindow::Unreadable,
            "a body without `active` says nothing about a match"
        );
        assert_eq!(
            classify(true, Some(&json!({"active": "yes"}))),
            UpdateWindow::Unreadable,
            "a non-bool `active` must not be coerced"
        );
        assert_eq!(classify(false, None), UpdateWindow::HostDown);
        assert_eq!(
            classify(false, Some(&json!({"active": true}))),
            UpdateWindow::HostDown,
            "health decides first — a stale body cannot resurrect the host"
        );
    }

    #[test]
    fn only_idle_and_host_down_may_auto_apply() {
        assert!(UpdateWindow::Idle.may_auto_apply());
        assert!(UpdateWindow::HostDown.may_auto_apply());
        assert!(!UpdateWindow::MatchActive.may_auto_apply());
        assert!(
            !UpdateWindow::Unreadable.may_auto_apply(),
            "an unreadable match state must block an unattended restart"
        );
    }

    #[test]
    fn version_compare_is_numeric_not_lexical() {
        assert!(is_newer("0.8.10", "0.8.9"), "10 > 9 despite '1' < '9'");
        assert!(!is_newer("0.8.9", "0.8.10"));
        assert!(!is_newer("0.8.64", "0.8.64"), "equal is not newer");
        assert!(is_newer("0.9.0", "0.8.99"));
        assert!(is_newer("1.0", "0.9.9"), "missing components read as 0");
        assert!(!is_newer("0.8", "0.8.0"), "0.8 == 0.8.0");
        assert!(is_newer("v0.8.65", "0.8.64"), "a leading v is tolerated");
        assert!(
            is_newer("0.8.65-beta.1", "0.8.64"),
            "pre-release suffix ignored"
        );
        assert!(
            !is_newer("nightly", "0.8.64"),
            "an unparseable version must not claim to be newer"
        );
        assert!(
            !is_newer("0.8.65", ""),
            "an unparseable baseline answers no"
        );
    }

    /// Fresh install ⇒ silent. Same version ⇒ silent (this is also what makes a
    /// dismissal stick across launches). Only a genuine upgrade shows.
    #[test]
    fn whats_changed_shows_only_on_an_upgrade() {
        assert!(!should_show_whats_changed(None, "0.8.64"));
        assert!(!should_show_whats_changed(Some("0.8.64"), "0.8.64"));
        assert!(should_show_whats_changed(Some("0.8.63"), "0.8.64"));
        assert!(
            !should_show_whats_changed(Some("0.8.65"), "0.8.64"),
            "a rollback must not show the older build's notes"
        );
    }

    const SAMPLE: &str = "\
# Changelog

Preamble that belongs to nothing.

## [0.8.64] - 2026-08-24

### Fixed
- Removal that shrinks a creature is recognized.

## [0.8.6] - 2026-01-01

### Added
- Ancient history.
";

    #[test]
    fn changelog_section_is_exact_and_bounded() {
        let s = changelog_section(SAMPLE, "0.8.64").expect("0.8.64 has a section");
        assert!(
            s.starts_with("### Fixed"),
            "leading blank lines trimmed: {s:?}"
        );
        assert!(
            s.ends_with("recognized."),
            "trailing blank lines trimmed: {s:?}"
        );
        assert!(
            !s.contains("Ancient history"),
            "the section must stop at the next `## ` heading: {s:?}"
        );
        assert!(
            !s.contains("Preamble"),
            "text above the heading is not part of the section: {s:?}"
        );
        // The prefix trap: `0.8.6` must not be satisfied by `## [0.8.64]`.
        let older = changelog_section(SAMPLE, "0.8.6").expect("0.8.6 has its own section");
        assert!(
            older.contains("Ancient history") && !older.contains("Removal"),
            "0.8.6 matched the 0.8.64 heading: {older:?}"
        );
        assert!(changelog_section(SAMPLE, "9.9.9").is_none());
        assert!(
            changelog_section("## [1.0.0] - x\n\n## [0.9.0] - y\n- a\n", "1.0.0").is_none(),
            "an empty section is None, not Some(\"\")"
        );
    }

    /// The notes are interpolated into an `eval`d JS string literal, so the
    /// escaping is a correctness boundary, not cosmetics.
    #[test]
    fn js_string_escapes_quotes_newlines_and_backslashes() {
        assert_eq!(js_string("a\"b"), "\"a\\\"b\"");
        assert_eq!(js_string("a\nb"), "\"a\\nb\"");
        assert_eq!(js_string("a\\b"), "\"a\\\\b\"");
        assert_eq!(
            js_string("a\u{2028}b"),
            "\"a b\"",
            "U+2028 is stripped: JSON leaves it raw and it breaks a JS literal"
        );
    }
}
