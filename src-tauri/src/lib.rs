use tauri::{
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

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
fn show_overlay(app: tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("overlay") {
        let _ = win.show();
    }
}

#[tauri::command]
fn hide_overlay(app: tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("overlay") {
        let _ = win.hide();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            toggle_overlay,
            show_overlay,
            hide_overlay,
        ])
        .setup(|app| {
            // Start Python sidecar
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = sidecar::start_and_wait(&handle).await {
                    eprintln!("Sidecar error: {}", e);
                }
            });

            // Build tray icon with menu
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

            // Show overlay after a short delay (wait for sidecar)
            let handle2 = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(15)).await;
                if let Some(win) = handle2.get_webview_window("overlay") {
                    let _ = win.show();
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
