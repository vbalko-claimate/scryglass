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
    // Check if server is already running (e.g. a glass-host launched out-of-band)
    println!("[sidecar] Checking if server is already running...");
    if check_health().await {
        println!("[sidecar] Server already running at {}", SIDECAR_URL);
        return Ok(());
    }

    // Dev mode: don't try the sidecar, just wait for a manually started server.
    if cfg!(debug_assertions) {
        println!("[sidecar] Dev mode — waiting for manual server start...");
        for i in 0..MAX_WAIT_SECS {
            tokio::time::sleep(Duration::from_secs(1)).await;
            if check_health().await {
                println!("[sidecar] Server ready after {}s", i + 1);
                return Ok(());
            }
        }
        return Err("Dev mode: glass-host not running. Build + start it from glass-shard: \
                    cargo run -p glass-mtga --features server --bin glass-host".into());
    }

    // Production: spawn the bundled all-Rust host (the only backend).
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
            .env("SCRY_USER_DATA", app_data),
        Err(e) => {
            eprintln!("[sidecar] glass-host sidecar command failed: {}", e);
            return false;
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
