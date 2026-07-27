use std::time::Duration;
use tauri::AppHandle;
use tauri_plugin_shell::ShellExt;

const SIDECAR_URL: &str = "http://localhost:8765";
const HEALTH_ENDPOINT: &str = "/health";
const MAX_WAIT_SECS: u64 = 45;

/// Start the all-Rust host (`glass-host`, crate glass-mtga) and wait until it
/// responds to health checks. This is the sole backend — there is no Python
/// fallback. Returns Ok(()) on success, Err(message) on failure.
pub async fn start_and_wait(app: &AppHandle) -> Result<(), String> {
    // Dev, or an explicitly external host (SCRY_EXTERNAL_HOST): DON'T manage the
    // sidecar — reuse a manually-started glass-host (or wait for one). This is the
    // only path that reuses a running server.
    let external = std::env::var("SCRY_EXTERNAL_HOST").is_ok();
    if external || cfg!(debug_assertions) {
        println!("[sidecar] External/dev host mode — using a manually started server...");
        if check_health().await {
            println!("[sidecar] Server already running at {}", SIDECAR_URL);
            return Ok(());
        }
        for i in 0..MAX_WAIT_SECS {
            tokio::time::sleep(Duration::from_secs(1)).await;
            if check_health().await {
                println!("[sidecar] Server ready after {}s", i + 1);
                return Ok(());
            }
        }
        return Err("No glass-host on :8765. Start it: \
                    cargo run -p glass-mtga --features server --bin glass-host \
                    (or unset SCRY_EXTERNAL_HOST to use the bundled one).".into());
    }

    // Production: NEVER reuse whatever is on :8765 — an orphan from a prior
    // version's update (or a second copy) would otherwise be adopted, so the app
    // ends up running stale backend code. Kill any stale sidecars, then always run
    // THIS bundle's binary.
    kill_stale_sidecars();
    tokio::time::sleep(Duration::from_millis(600)).await; // let :8765 free up

    if !spawn_glass_host(app) {
        return Err("Failed to spawn glass-host. Is the Rust binary bundled in externalBin? \
                    (no Python fallback exists anymore)".into());
    }
    for i in 0..MAX_WAIT_SECS {
        tokio::time::sleep(Duration::from_secs(1)).await;
        if check_health().await {
            println!("[sidecar] glass-host (Rust) ready after {}s", i + 1);
            return Ok(());
        }
    }

    Err(format!("glass-host did not respond within {}s. Check logs for errors.", MAX_WAIT_SECS))
}

/// Best-effort kill of any running `glass-host` / `overlay-helper` sidecar
/// processes — an orphan that survived a prior version's update, or a second
/// copy of the app. Called on production launch (before spawning the bundled
/// binary) and on quit (clean shutdown). Never runs in dev/external mode, so a
/// manually-started host is left alone.
pub fn kill_stale_sidecars() {
    for name in ["glass-host", "overlay-helper"] {
        #[cfg(unix)]
        {
            let _ = std::process::Command::new("pkill").arg("-x").arg(name).status();
        }
        #[cfg(windows)]
        {
            let _ = std::process::Command::new("taskkill")
                .args(["/F", "/IM", &format!("{name}.exe")])
                .status();
        }
    }
}

/// Spawn the bundled glass-host (Rust) sidecar on :8765, pointing it at the
/// bundled UI/catalog/strategies (resources) and the user's persistent data
/// dir (the same `~/MTG/mtg-data` the Python host uses, override via
/// SCRY_USER_DATA — so advisor.db history is shared). Returns false on failure.
fn spawn_glass_host(app: &AppHandle) -> bool {
    use tauri::Manager;

    let res = |rel: &str| {
        app.path()
            .resolve(rel, tauri::path::BaseDirectory::Resource)
            .map(|p| p.to_string_lossy().into_owned())
    };
    let (Ok(static_dir), Ok(catalog), Ok(strategy_dir), Ok(advise_db)) = (
        res("scry/static"),
        res("scry/data/cards_cache.json"),
        res("scry/data"),
        res("resources/glass_advise_db.json"),
    ) else {
        eprintln!("[sidecar] cannot resolve bundled glass-host resources");
        return false;
    };
    // Deck catalogue → the belief engine's prior over opponent decklists. NOT in the tuple
    // above: a missing catalogue must degrade to "no opponent model" (the pre-belief
    // behavior), not refuse to start the host at all.
    //
    // ★ `.filter(|p| p.is_dir())` is load-bearing. Tauri's `PathResolver::resolve` only JOINS
    // path components — it never checks the filesystem — so `res(..).ok()` is ALWAYS `Some` and
    // the "no bundled deck catalogue" branch below was dead code. A catalogue that failed to
    // stage therefore produced zero diagnostics anywhere: the env var was set to a path that did
    // not exist and glass-host swallowed the `read_dir` error. Found by review, 2026-07-27.
    let deck_catalog_dir = res("resources/deck_catalog")
        .ok()
        .filter(|p| std::path::Path::new(p).is_dir());

    // Persistent user data — match the Python host (USER_DATA_DIR =
    // $SCRY_USER_DATA or ~/MTG/mtg-data; db + decks + collection underneath).
    let user_root = std::env::var("SCRY_USER_DATA").unwrap_or_else(|_| {
        app.path()
            .home_dir()
            .map(|h| h.join("MTG").join("mtg-data").to_string_lossy().into_owned())
            .unwrap_or_else(|_| "mtg-data".into())
    });
    let app_data = format!("{user_root}/app_data");

    let shell = app.shell();
    let cmd = match shell.sidecar("glass-host") {
        Ok(c) => c
            .env("SCRY_PORT", "8765")
            .env("SCRY_ADVISE_DB", advise_db)
            .env("SCRY_STATIC_DIR", static_dir)
            .env("SCRY_CATALOG_PATH", catalog)
            .env("SCRY_STRATEGY_DIR", strategy_dir)
            .env("SCRY_DB_PATH", format!("{app_data}/advisor.db"))
            .env("SCRY_DECKS_ROOT", format!("{user_root}/decks"))
            .env("SCRY_USER_DATA", app_data)
            // Bundled cloud URL (NOT a secret) — the host auto-provisions an
            // anonymous account on first launch so cloud sync just works. No
            // token is baked; `SCRY_CLOUD_SYNC=0` still disables it.
            .env("SCRY_CLOUD_URL", "https://scryglass.win"),
        Err(e) => {
            eprintln!("[sidecar] glass-host sidecar command failed: {}", e);
            return false;
        }
    };
    // Belief prior. Passed only when the resource resolved; glass-host falls back to its own
    // relative default and, failing that, logs "opponent model OFF" rather than guessing.
    let cmd = match deck_catalog_dir {
        Some(d) => cmd.env("GLASS_DECK_CATALOG_DIR", d),
        None => {
            eprintln!("[sidecar] no bundled deck catalogue — belief opponent model will be off");
            cmd
        }
    };
    match cmd.spawn() {
        Ok((_rx, _child)) => {
            println!("[sidecar] Spawned glass-host (Rust), waiting for health...");
            true
        }
        Err(e) => {
            eprintln!("[sidecar] glass-host spawn failed: {} (is it bundled?)", e);
            false
        }
    }
}

pub async fn check_health() -> bool {
    let url = format!("{}{}", SIDECAR_URL, HEALTH_ENDPOINT);
    match reqwest::get(&url).await {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}

// (glass-host advises in-process via the bundled glass_advise_db.json — set as
// SCRY_ADVISE_DB above. No separate glass-server engine sidecar.)
