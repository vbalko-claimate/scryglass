use std::time::Duration;
use tauri::AppHandle;
use tauri_plugin_shell::ShellExt;

const SIDECAR_URL: &str = "http://localhost:8765";
const HEALTH_ENDPOINT: &str = "/health";
const MAX_WAIT_SECS: u64 = 30;

/// Start the Python sidecar and wait until it responds to health checks.
pub async fn start_and_wait(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    // In dev mode, assume the server is already running (started manually)
    if cfg!(debug_assertions) {
        println!("[sidecar] Dev mode — checking if server is already running...");
        if check_health().await {
            println!("[sidecar] Server already running at {}", SIDECAR_URL);
            return Ok(());
        }
        println!("[sidecar] Server not running — starting sidecar...");
    }

    // Spawn sidecar process
    let shell = app.shell();
    let (mut _rx, _child) = shell
        .sidecar("scry-server")
        .map_err(|e| format!("Failed to create sidecar command: {}", e))?
        .spawn()
        .map_err(|e| format!("Failed to spawn sidecar: {}", e))?;

    println!("[sidecar] Spawned scry-server, waiting for health...");

    // Poll health endpoint
    for i in 0..MAX_WAIT_SECS {
        tokio::time::sleep(Duration::from_secs(1)).await;
        if check_health().await {
            println!("[sidecar] Server ready after {}s", i + 1);
            return Ok(());
        }
    }

    Err(format!("Sidecar did not respond within {}s", MAX_WAIT_SECS).into())
}

async fn check_health() -> bool {
    let url = format!("{}{}", SIDECAR_URL, HEALTH_ENDPOINT);
    match reqwest::get(&url).await {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}
