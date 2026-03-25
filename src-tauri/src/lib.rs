use tauri::{
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, LogicalPosition,
};

mod mtga_detect;
mod sidecar;

#[tauri::command]
fn toggle_overlay(app: tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("overlay") {
        if win.is_visible().unwrap_or(false) {
            let _ = win.hide();
        } else {
            let _ = win.show();
            let _ = win.set_focus();
        }
    }
}

#[tauri::command]
fn find_mtga() -> mtga_detect::MtgaWindow {
    mtga_detect::find_mtga_window()
}

/// Reposition overlay to the MTGA window's screen
fn position_overlay_on_mtga(app: &tauri::AppHandle) {
    let mtga = mtga_detect::find_mtga_window();
    if !mtga.found {
        return;
    }
    if let Some(win) = app.get_webview_window("overlay") {
        // Position overlay at top-left of MTGA window with small offset
        let x = mtga.x as f64 + 8.0;
        let y = mtga.y as f64 + 8.0;
        let _ = win.set_position(LogicalPosition::new(x, y));
        let _ = win.show();
        println!("[overlay] Positioned on MTGA at ({}, {})", x, y);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
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

            // Periodically check for MTGA and position overlay
            let handle2 = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                // Wait for sidecar to start
                tokio::time::sleep(std::time::Duration::from_secs(10)).await;

                loop {
                    position_overlay_on_mtga(&handle2);
                    // Re-check every 10 seconds (MTGA might move or start)
                    tokio::time::sleep(std::time::Duration::from_secs(10)).await;
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
