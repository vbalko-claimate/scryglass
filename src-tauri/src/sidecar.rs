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
