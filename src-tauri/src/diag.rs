//! Diagnostics log for the desktop shell.
//!
//! WHY THIS EXISTS. Every window in `tauri.conf.json` — including `main` — loads
//! `http://localhost:8765`, which the `glass-host` sidecar serves. When that
//! sidecar does not start, the app opens a blank window and there is nowhere at
//! all to look: the shell's `println!` diagnostics go to stdout, and a Windows
//! GUI-subsystem process has no console attached to receive them. A tester on
//! 2026-08-17 reported exactly that shape ("app runs, no overlay, game not
//! detected") and neither they nor I could see a single line explaining it.
//!
//! So this writes to a FILE, in the platform's log directory, and keeps the tail
//! in memory for the failure dialog and the cloud report.
//!
//! ⚠ It must never depend on the sidecar. The failure being diagnosed is
//! precisely "the process serving :8765 is down", so anything served from :8765
//! is unavailable exactly when it is needed.

use std::collections::VecDeque;
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

/// Truncate the log once it passes this. A tester's machine must not accumulate
/// an unbounded file because of a crash loop — which is the very situation this
/// log exists to record.
const MAX_LOG_BYTES: u64 = 2 * 1024 * 1024;

/// Lines kept in memory for the failure dialog and the cloud report. The dialog
/// shows a handful; the report sends the tail.
const RING_CAPACITY: usize = 400;

struct Diag {
    path: Option<PathBuf>,
    ring: VecDeque<String>,
}

static DIAG: OnceLock<Mutex<Diag>> = OnceLock::new();

fn cell() -> &'static Mutex<Diag> {
    DIAG.get_or_init(|| {
        Mutex::new(Diag {
            path: None,
            ring: VecDeque::with_capacity(RING_CAPACITY),
        })
    })
}

fn epoch_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Point the log at the platform log directory and start a fresh session.
///
/// Uses Tauri's `app_log_dir()` rather than the sidecar's data root on purpose:
/// `spawn_glass_host` defaults that root to `~/MTG/mtg-data`, which is the
/// author's own macOS layout and need not exist at all on a tester's Windows
/// box — writing the log there would make it fail in the same conditions it is
/// meant to explain.
pub fn init(app: &tauri::AppHandle) {
    use tauri::Manager;
    let dir = match app.path().app_log_dir() {
        Ok(d) => d,
        Err(e) => {
            // No file, but keep the in-memory ring working: the dialog and the
            // cloud report still have something to show.
            log(&format!("[diag] no log dir ({e}) — memory only"));
            return;
        }
    };
    if let Err(e) = std::fs::create_dir_all(&dir) {
        log(&format!("[diag] cannot create {}: {e}", dir.display()));
        return;
    }
    let path = dir.join("scryglass.log");
    if std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0) > MAX_LOG_BYTES {
        let _ = std::fs::remove_file(&path);
    }
    if let Ok(mut g) = cell().lock() {
        g.path = Some(path.clone());
    }
    log(&format!(
        "[diag] session start — version {} — log {}",
        app.package_info().version,
        path.display()
    ));
}

/// Append one line to the log file and the in-memory tail.
///
/// Also prints, so `cargo tauri dev` on macOS behaves exactly as before.
/// Deliberately infallible: a diagnostics channel that can itself fail loudly
/// would take the app down over a full disk.
pub fn log(line: &str) {
    let stamped = format!("{} {}", epoch_millis(), line.trim_end());
    println!("{stamped}");
    let Ok(mut g) = cell().lock() else {
        return;
    };
    if g.ring.len() == RING_CAPACITY {
        g.ring.pop_front();
    }
    g.ring.push_back(stamped.clone());
    if let Some(p) = g.path.clone() {
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&p) {
            let _ = writeln!(f, "{stamped}");
        }
    }
}

/// The last `n` lines, oldest first. Used by the failure dialog and the report.
pub fn tail(n: usize) -> Vec<String> {
    let Ok(g) = cell().lock() else {
        return Vec::new();
    };
    let skip = g.ring.len().saturating_sub(n);
    g.ring.iter().skip(skip).cloned().collect()
}

/// Where the log lives, once `init` has run. `None` means memory-only.
pub fn log_path() -> Option<PathBuf> {
    cell().lock().ok().and_then(|g| g.path.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The in-memory tail must stay bounded and keep the NEWEST lines.
    ///
    /// Both halves matter for the failure report: unbounded growth would turn a
    /// sidecar crash-loop into a memory leak on a tester's machine, and keeping
    /// the OLDEST lines would report the boot sequence instead of the failure
    /// that just happened.
    ///
    /// One test, not several: `DIAG` is a process-wide `OnceLock`, so separate
    /// tests in this binary would share the ring and race.
    #[test]
    fn the_tail_is_bounded_and_keeps_the_newest_lines() {
        for i in 0..(RING_CAPACITY + 50) {
            log(&format!("line {i}"));
        }
        let all = tail(usize::MAX);
        assert_eq!(
            all.len(),
            RING_CAPACITY,
            "ring must cap at {RING_CAPACITY}, got {}",
            all.len()
        );
        assert!(
            all.last().is_some_and(|l| l.ends_with(&format!("line {}", RING_CAPACITY + 49))),
            "newest line must survive, got {:?}",
            all.last()
        );
        assert!(
            !all.iter().any(|l| l.ends_with("line 0")),
            "oldest line must have been evicted"
        );
        assert_eq!(tail(5).len(), 5, "a short tail returns exactly what was asked");
    }
}
