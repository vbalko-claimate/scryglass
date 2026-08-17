//! Upload a desktop failure report to glass-cloud.
//!
//! ⚠ WHY THE SHELL SENDS THIS AND NOT glass-host. The sidecar owns the normal
//! cloud upload path (`glass-mtga::cloud` → `POST /ingest`), but the failure
//! worth reporting most is "the sidecar did not start" — at which point nothing
//! in the sidecar can send anything. So this is a second, deliberately tiny
//! sender that lives in the shell and shares the sidecar's account.
//!
//! It is best-effort by construction: every step returns `None` rather than
//! propagating, and the caller does not await a result that could block
//! startup. A telemetry channel must never be able to break the app it reports
//! on.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// Same bundled default the sidecar is launched with (see `spawn_glass_host`).
/// Not a secret.
const CLOUD_URL: &str = "https://scryglass.win";

/// How much of the diagnostics log rides along. Enough to see the failure and
/// what led to it; small enough that a broken client cannot post megabytes.
const LOG_TAIL_LINES: usize = 120;
const LOG_TAIL_BYTES: usize = 16 * 1024;

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

/// The sidecar's data dir — the same `{SCRY_USER_DATA or ~/MTG/mtg-data}/app_data`
/// that `spawn_glass_host` passes it, so we read the account it already uses
/// instead of minting a second identity.
fn app_data_dir(app: &tauri::AppHandle) -> PathBuf {
    use tauri::Manager;
    let root = std::env::var("SCRY_USER_DATA").unwrap_or_else(|_| {
        app.path()
            .home_dir()
            .map(|h| {
                h.join("MTG")
                    .join("mtg-data")
                    .to_string_lossy()
                    .into_owned()
            })
            .unwrap_or_else(|_| "mtg-data".into())
    });
    PathBuf::from(root).join("app_data")
}

/// Has the user turned cloud sync off? Honoured in all three places the sidecar
/// honours it, because a failure report is still telemetry: an explicit opt-out
/// must silence this channel too.
fn sync_disabled(dir: &Path) -> bool {
    if matches!(
        std::env::var("SCRY_CLOUD_SYNC").ok().as_deref(),
        Some("0") | Some("off") | Some("false")
    ) {
        return true;
    }
    read_json(&dir.join("cloud_sync.json"))
        .and_then(|c| c.get("enabled").and_then(Value::as_bool))
        .is_some_and(|enabled| !enabled)
}

fn configured_url(dir: &Path) -> String {
    read_json(&dir.join("cloud_sync.json"))
        .and_then(|c| {
            c.get("url")
                .and_then(Value::as_str)
                .map(|s| s.trim_end_matches('/').to_string())
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| CLOUD_URL.to_string())
}

/// The bearer token the sidecar already uses, minting one only if there is none.
///
/// ⚠ When a token is minted here it is written back into the SAME state file the
/// sidecar reads, so the sidecar reuses it instead of provisioning a second
/// anonymous identity. The write preserves every other key (watermarks!) and
/// happens ONLY when `token` was absent — which, in the case this module exists
/// for, is a moment when the sidecar is not running to race with.
async fn token_for(http: &reqwest::Client, dir: &Path, url: &str) -> Option<String> {
    // An explicitly configured token wins, exactly as it does for the sidecar.
    if let Some(t) = read_json(&dir.join("cloud_sync.json"))
        .and_then(|c| c.get("token").and_then(Value::as_str).map(str::to_string))
        .filter(|s| !s.is_empty())
    {
        return Some(t);
    }
    let state_path = dir.join("cloud_sync_state.json");
    let state = read_json(&state_path);
    if let Some(t) = state
        .as_ref()
        .and_then(|s| s.get("token").and_then(Value::as_str).map(str::to_string))
        .filter(|s| !s.is_empty())
    {
        return Some(t);
    }
    let client_id = state
        .as_ref()
        .and_then(|s| s.get("client_id").and_then(Value::as_str))
        .unwrap_or("scryglass-desktop")
        .to_string();
    let resp = http
        .post(format!("{url}/auth/anon"))
        .json(&json!({ "client_id": client_id }))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let token = resp
        .json::<Value>()
        .await
        .ok()?
        .get("token")
        .and_then(Value::as_str)?
        .to_string();
    // Merge, never replace: the state file also carries upload watermarks, and
    // clobbering those would make the sidecar re-upload its whole history.
    let mut merged = state.unwrap_or_else(|| json!({}));
    if let Some(obj) = merged.as_object_mut() {
        obj.insert("token".into(), json!(token));
        obj.entry("client_id").or_insert(json!(client_id));
        let _ = std::fs::create_dir_all(dir);
        let _ = std::fs::write(&state_path, merged.to_string());
    }
    Some(token)
}

/// Post one failure report. Silent on every failure — see the module note.
///
/// `id` must be stable for one incident so a retry dedups server-side rather
/// than turning a crash loop into a hundred rows.
pub async fn send_failure(app: &tauri::AppHandle, kind: &str, message: &str) {
    let dir = app_data_dir(app);
    if sync_disabled(&dir) {
        crate::diag::log("[report] cloud sync is off — failure not uploaded");
        return;
    }
    let url = configured_url(&dir);
    let http = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
    {
        Ok(c) => c,
        Err(_) => return,
    };
    let Some(token) = token_for(&http, &dir, &url).await else {
        crate::diag::log("[report] no cloud token available — failure not uploaded");
        return;
    };

    let mut log_tail = crate::diag::tail(LOG_TAIL_LINES).join("\n");
    if log_tail.len() > LOG_TAIL_BYTES {
        log_tail = log_tail.split_off(log_tail.len() - LOG_TAIL_BYTES);
    }
    let version = app.package_info().version.to_string();
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);

    let row = json!({
        // One row per launch: stable across retries within this launch, distinct
        // across launches, so a repeated failure is still visible as repeated.
        "id": format!("{kind}#{version}#{now_ms}"),
        "kind": kind,
        "message": message,
        "app_version": version,
        "os": std::env::consts::OS,
        "log_tail": log_tail,
    });

    match http
        .post(format!("{url}/ingest"))
        .bearer_auth(token)
        .json(&json!({ "client_errors": [row] }))
        .send()
        .await
    {
        Ok(resp) => {
            let status = resp.status();
            // The server ECHOES how many client_errors it stored. A build that
            // predates the field ignores the unknown key and still answers 200,
            // so the status alone would prove nothing.
            let stored = resp
                .json::<Value>()
                .await
                .ok()
                .and_then(|v| v.get("client_errors").and_then(Value::as_u64));
            crate::diag::log(&format!(
                "[report] uploaded: status={status} stored={stored:?}"
            ));
        }
        Err(e) => crate::diag::log(&format!("[report] upload failed: {e}")),
    }
}
