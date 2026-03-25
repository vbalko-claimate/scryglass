use tauri::{
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};
use std::process::Command;

mod mtga_detect;
mod sidecar;

#[tauri::command]
fn toggle_overlay() {
    // Toggle is handled by the overlay process itself
    // For now, this is a placeholder
}

#[tauri::command]
fn find_mtga() -> mtga_detect::MtgaWindow {
    mtga_detect::find_mtga_window()
}

/// Launch the native overlay helper with auto-restart on crash.
fn launch_overlay_helper() {
    let overlay_script = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("overlay_helper.swift");

    if !overlay_script.exists() {
        eprintln!("[overlay] overlay_helper.swift not found at {:?}", overlay_script);
        return;
    }

    std::thread::spawn(move || {
        let mut restart_count = 0;
        loop {
            println!("[overlay] Starting overlay helper (attempt {})", restart_count + 1);
            let status = Command::new("swift")
                .arg(&overlay_script)
                .status();
            match status {
                Ok(s) => println!("[overlay] Helper exited: {}", s),
                Err(e) => eprintln!("[overlay] Failed to launch: {}", e),
            }
            restart_count += 1;
            if restart_count >= 5 {
                eprintln!("[overlay] Too many restarts ({}), giving up", restart_count);
                break;
            }
            // Wait before restart (exponential backoff)
            let wait = std::time::Duration::from_secs(2u64.pow(restart_count.min(4)));
            println!("[overlay] Restarting in {:?}...", wait);
            std::thread::sleep(wait);
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            toggle_overlay,
            find_mtga,
        ])
        .setup(|app| {
            // Start Python sidecar
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = sidecar::start_and_wait(&handle).await {
                    eprintln!("Sidecar error: {}", e);
                }
            });

            // Launch native overlay helper after sidecar is ready
            let handle2 = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                // Wait for server to be ready
                tokio::time::sleep(std::time::Duration::from_secs(12)).await;
                let _ = handle2.run_on_main_thread(|| {
                    launch_overlay_helper();
                });
            });

            // Build tray icon
            let _tray = TrayIconBuilder::new()
                .tooltip("Scryglass — MTGA Advisor")
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
