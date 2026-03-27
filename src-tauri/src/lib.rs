use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Manager,
};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

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

/// macOS: Launch native Swift overlay sidecar with auto-restart.
#[cfg(target_os = "macos")]
fn launch_overlay_macos(handle: &tauri::AppHandle) {
    let shell = handle.shell();
    let mut restart_count = 0u32;

    loop {
        println!("[overlay] Starting overlay helper (attempt {})", restart_count + 1);

        let cmd = match shell.sidecar("overlay-helper") {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[overlay] Cannot create overlay sidecar: {}", e);
                return;
            }
        };

        match cmd.spawn() {
            Ok((mut rx, _child)) => {
                while let Some(event) = rx.blocking_recv() {
                    match event {
                        CommandEvent::Stdout(line) => {
                            println!("[overlay] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("[overlay] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(payload) => {
                            println!("[overlay] Exited: code={:?} signal={:?}",
                                payload.code, payload.signal);
                            break;
                        }
                        _ => {}
                    }
                }
            }
            Err(e) => eprintln!("[overlay] Failed to spawn: {}", e),
        }

        restart_count += 1;
        if restart_count >= 5 {
            eprintln!("[overlay] Too many restarts ({}), giving up", restart_count);
            break;
        }
        let wait = std::time::Duration::from_secs(5);
        println!("[overlay] Restarting in {:?}...", wait);
        std::thread::sleep(wait);
    }
}

/// Windows: Use Tauri overlay window — poll MTGA foreground + match status to show/hide.
#[cfg(target_os = "windows")]
fn launch_overlay_windows(handle: &tauri::AppHandle) {
    let handle = handle.clone();

    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        // Configure overlay window: click-through
        if let Some(overlay) = handle.get_webview_window("overlay") {
            let _ = overlay.set_ignore_cursor_events(true);
        }

        let mut was_visible = false;

        loop {
            std::thread::sleep(std::time::Duration::from_secs(2));

            let mtga_front = mtga_detect::is_mtga_frontmost();
            let match_active = rt.block_on(async {
                check_match_active().await
            });

            let should_show = mtga_front && match_active;

            if let Some(overlay) = handle.get_webview_window("overlay") {
                if should_show && !was_visible {
                    println!("[overlay] MTGA in foreground + match active → showing overlay");
                    let _ = overlay.show();
                    was_visible = true;
                } else if !should_show && was_visible {
                    println!("[overlay] Hiding overlay");
                    let _ = overlay.hide();
                    was_visible = false;
                }
            }
        }
    });
}

#[cfg(target_os = "windows")]
async fn check_match_active() -> bool {
    let resp = reqwest::get("http://localhost:8765/match-status").await;
    match resp {
        Ok(r) => {
            if let Ok(json) = r.json::<serde_json::Value>().await {
                json.get("active").and_then(|v| v.as_bool()).unwrap_or(false)
            } else {
                false
            }
        }
        Err(_) => false,
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Keep running in menu bar when window is closed
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .invoke_handler(tauri::generate_handler![
            toggle_overlay,
            find_mtga,
        ])
        .setup(|app| {
            // macOS: accessory app — no dock icon, lives in menu bar
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // Start Python sidecar, then show main window
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                match sidecar::start_and_wait(&handle).await {
                    Ok(()) => {
                        if let Some(win) = handle.get_webview_window("main") {
                            let _ = win.navigate("http://localhost:8765".parse().unwrap());
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    }
                    Err(e) => {
                        eprintln!("[error] Sidecar failed: {}", e);
                        // Show error page inline
                        if let Some(win) = handle.get_webview_window("main") {
                            let error_html = format!(
                                "data:text/html,<html><body style='background:%231a1a2e;color:%23e0e0e0;\
                                font-family:system-ui;display:flex;justify-content:center;align-items:center;\
                                min-height:100vh;flex-direction:column'>\
                                <h2 style='color:%23ef5350'>Scryglass failed to start</h2>\
                                <p style='color:%23888;max-width:400px;text-align:center;margin:12px'>{}</p>\
                                <p style='color:%23555;font-size:12px'>Try: uv run python run.py</p>\
                                </body></html>",
                                e.replace("'", "\\'")
                            );
                            let _ = win.navigate(error_html.parse().unwrap());
                            let _ = win.show();
                        }
                    }
                }
            });

            // Launch overlay after server is healthy (platform-specific)
            let handle2 = app.handle().clone();
            std::thread::spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                let ready = rt.block_on(async {
                    for i in 0..45u32 {
                        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                        if sidecar::check_health().await {
                            println!("[overlay] Server healthy after {}s, launching overlay", i + 1);
                            return true;
                        }
                    }
                    false
                });
                if ready {
                    #[cfg(target_os = "macos")]
                    launch_overlay_macos(&handle2);

                    #[cfg(target_os = "windows")]
                    launch_overlay_windows(&handle2);
                } else {
                    eprintln!("[overlay] Server never became healthy, skipping overlay");
                }
            });

            // Build menu bar tray icon with dropdown
            let show_item = MenuItemBuilder::with_id("show", "Show Advisor").build(app)?;
            let review_item = MenuItemBuilder::with_id("review", "Post-Game Review").build(app)?;
            let setup_item = MenuItemBuilder::with_id("setup", "Setup").build(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "Quit Scryglass").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&show_item)
                .item(&review_item)
                .separator()
                .item(&setup_item)
                .separator()
                .item(&quit_item)
                .build()?;

            let tray_icon_path = app.path()
                .resolve("icons/tray-icon.png", tauri::path::BaseDirectory::Resource)
                .expect("tray icon not found in resources");
            let tray_icon = tauri::image::Image::from_path(&tray_icon_path)
                .expect("failed to decode tray icon PNG");

            let _tray = TrayIconBuilder::new()
                .tooltip("Scryglass")
                .icon(tray_icon)
                .icon_as_template(true)
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| {
                    match event.id().as_ref() {
                        "show" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "review" => {
                            let _ = open::that("http://localhost:8765/review");
                        }
                        "setup" => {
                            let _ = open::that("http://localhost:8765/setup");
                        }
                        "quit" => {
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
