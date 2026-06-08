use std::time::Duration;
use tauri::AppHandle;
use tauri_plugin_shell::ShellExt;

const SIDECAR_URL: &str = "http://localhost:8765";
const HEALTH_ENDPOINT: &str = "/health";
const MAX_WAIT_SECS: u64 = 45;

/// Start the Python sidecar and wait until it responds to health checks.
/// Returns Ok(()) on success, Err(message) on failure.
pub async fn start_and_wait(app: &AppHandle) -> Result<(), String> {
    // Check if server is already running
    println!("[sidecar] Checking if server is already running...");
    if check_health().await {
        println!("[sidecar] Server already running at {}", SIDECAR_URL);
        return Ok(());
    }

    // Dev mode: don't try sidecar, just wait for manual server
    if cfg!(debug_assertions) {
        println!("[sidecar] Dev mode — waiting for manual server start...");
        for i in 0..MAX_WAIT_SECS {
            tokio::time::sleep(Duration::from_secs(1)).await;
            if check_health().await {
                println!("[sidecar] Server ready after {}s", i + 1);
                return Ok(());
            }
        }
        return Err("Dev mode: Python server not running. Start with: uv run python run.py".into());
    }

    // Production: spawn sidecar
    let shell = app.shell();
    let spawn_result = shell
        .sidecar("scry-server")
        .map_err(|e| format!("Failed to create sidecar command: {}", e))?
        .spawn();

    match spawn_result {
        Ok((mut _rx, _child)) => {
            println!("[sidecar] Spawned scry-server, waiting for health...");
        }
        Err(e) => {
            return Err(format!("Failed to spawn sidecar: {}. Is scry-server bundled?", e));
        }
    }

    // Poll health endpoint
    for i in 0..MAX_WAIT_SECS {
        tokio::time::sleep(Duration::from_secs(1)).await;
        if check_health().await {
            println!("[sidecar] Server ready after {}s", i + 1);
            return Ok(());
        }
    }

    Err(format!("Server did not respond within {}s. Check logs for errors.", MAX_WAIT_SECS))
}

pub async fn check_health() -> bool {
    let url = format!("{}{}", SIDECAR_URL, HEALTH_ENDPOINT);
    match reqwest::get(&url).await {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}

const ENGINE_URL: &str = "http://localhost:3000";

/// Start the bundled glass-engine advice sidecar (glass-server) on :3000,
/// pointing it at the bundled compiled card DB. BEST-EFFORT and
/// NON-FATAL: the Python advisor's `engine` advice source degrades to
/// nothing when this isn't reachable, so any failure here is logged, not
/// propagated. Runs in parallel with the Python sidecar at startup.
pub async fn start_glass_engine(app: &AppHandle) {
    use tauri::Manager;

    if engine_health().await {
        println!("[glass-engine] already running at {}", ENGINE_URL);
        return;
    }
    if cfg!(debug_assertions) {
        println!("[glass-engine] dev mode — not spawning bundled glass-server");
        return;
    }

    let db_path = match app.path().resolve(
        "resources/glass_advise_db.json",
        tauri::path::BaseDirectory::Resource,
    ) {
        Ok(p) => p.to_string_lossy().into_owned(),
        Err(e) => {
            eprintln!("[glass-engine] cannot resolve bundled card DB: {}", e);
            return;
        }
    };

    let shell = app.shell();
    let cmd = match shell.sidecar("glass-server") {
        Ok(c) => c.env("GLASS_DB", db_path).env("PORT", "3000"),
        Err(e) => {
            eprintln!("[glass-engine] sidecar command failed: {}", e);
            return;
        }
    };
    match cmd.spawn() {
        Ok((_rx, _child)) => println!("[glass-engine] spawned glass-server"),
        Err(e) => {
            eprintln!("[glass-engine] spawn failed: {} (is glass-server bundled?)", e);
            return;
        }
    }

    for i in 0..25 {
        tokio::time::sleep(Duration::from_secs(1)).await;
        if engine_health().await {
            println!("[glass-engine] ready after {}s", i + 1);
            return;
        }
    }
    eprintln!("[glass-engine] not healthy within 25s — engine advice disabled this session");
}

async fn engine_health() -> bool {
    match reqwest::get(format!("{}/health", ENGINE_URL)).await {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}
