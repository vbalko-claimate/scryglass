use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Manager,
};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;
#[cfg(not(debug_assertions))]
use tauri_plugin_updater::UpdaterExt;
#[cfg(not(debug_assertions))]
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

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
/// Also polls Alt key for feedback mode toggle.
#[cfg(target_os = "windows")]
fn launch_overlay_windows(handle: &tauri::AppHandle) {
    let handle = handle.clone();

    // Show/hide overlay thread (2s poll)
    let h1 = handle.clone();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        if let Some(overlay) = h1.get_webview_window("overlay") {
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

            if let Some(overlay) = h1.get_webview_window("overlay") {
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

    // Alt key + cursor polling thread (50ms poll):
    //  - Right-Win toggles feedback mode (click-through off + interactive buttons).
    //  - Otherwise forward the cursor position so the overlay can peek (shrink) when
    //    the cursor is over it. GetCursorPos is a passive read → click-through stays
    //    intact; JS owns the geometry (see overlayCursor in overlay.html).
    let h2 = handle.clone();
    std::thread::spawn(move || {
        use windows::Win32::Foundation::POINT;
        use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_RWIN};
        use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;

        let mut was_alt = false;
        let mut last_pt = (i32::MIN, i32::MIN);

        loop {
            std::thread::sleep(std::time::Duration::from_millis(50));

            // Right Windows key (≈ Right Command on Mac keyboards)
            let alt_down = unsafe { GetAsyncKeyState(VK_RWIN.0 as i32) } & (1i16 << 15) != 0;

            if alt_down != was_alt {
                if let Some(overlay) = h2.get_webview_window("overlay") {
                    let _ = overlay.set_ignore_cursor_events(!alt_down);
                    let js = format!("setInteractiveMode({})", alt_down);
                    let _ = overlay.eval(&js);
                }
                was_alt = alt_down;
            }

            // Peek: forward cursor pos (skip while in feedback mode, if unmoved, or
            // while the overlay is hidden — mirrors the Swift window.isVisible guard).
            if !alt_down {
                let mut pt = POINT::default();
                if unsafe { GetCursorPos(&mut pt) }.is_ok() && (pt.x, pt.y) != last_pt {
                    last_pt = (pt.x, pt.y);
                    if let Some(overlay) = h2.get_webview_window("overlay") {
                        if overlay.is_visible().unwrap_or(false) {
                            if let (Ok(pos), Ok(scale)) =
                                (overlay.inner_position(), overlay.scale_factor())
                            {
                                let dom_x = ((pt.x - pos.x) as f64 / scale).round() as i32;
                                let dom_y = ((pt.y - pos.y) as f64 / scale).round() as i32;
                                let _ =
                                    overlay.eval(&format!("overlayCursor({}, {})", dom_x, dom_y));
                            }
                        }
                    }
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
        .plugin(tauri_plugin_dialog::init())
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

            // (glass-host advises IN-PROCESS now — no separate engine sidecar.)

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
                                <p style='color:%23555;font-size:12px'>Try: start glass-host (cargo run -p glass-mtga --features server --bin glass-host)</p>\
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
            let update_item = MenuItemBuilder::with_id("update", "Check for Updates…").build(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "Quit Scryglass").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&show_item)
                .item(&review_item)
                .separator()
                .item(&setup_item)
                .item(&update_item)
                .separator()
                .item(&quit_item)
                .build()?;

            // On launch (release only): a background check that ADVERTISES an
            // available update on the tray (retitles the item) — no dialog, no
            // auto-install, so it never interrupts a game (Sparkle-style gentle
            // reminder). The user installs on their terms from the menu.
            #[cfg(not(debug_assertions))]
            {
                let handle = app.handle().clone();
                let item = update_item.clone();
                tauri::async_runtime::spawn(async move {
                    if let Ok(updater) = handle.updater() {
                        if let Ok(Some(update)) = updater.check().await {
                            let _ = item.set_text(format!("Install Update v{}…", update.version));
                        }
                    }
                });
            }

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
                        "update" => {
                            // Manual check → game-aware, consent-gated install.
                            #[cfg(not(debug_assertions))]
                            {
                                let h = app.clone();
                                tauri::async_runtime::spawn(async move {
                                    run_update_check(h).await;
                                });
                            }
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

/// Query glass-host whether an MTGA match is active — game-aware update deferral
/// (Scryglass must never restart mid-game). Fails safe to `false` (host
/// unreachable ⇒ treat as not-in-a-match).
#[cfg(not(debug_assertions))]
async fn is_match_active() -> bool {
    match reqwest::get("http://localhost:8765/active").await {
        Ok(resp) => resp
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|v| v.get("active").and_then(serde_json::Value::as_bool))
            .unwrap_or(false),
        Err(_) => false,
    }
}

/// The tray "Check for Updates…" flow: check → up-to-date message, or an
/// available-update path that NEVER restarts mid-game and installs only on the
/// user's explicit consent (the Sparkle menu-bar pattern).
#[cfg(not(debug_assertions))]
async fn run_update_check(app: tauri::AppHandle) {
    let current = app.package_info().version.to_string();
    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            let _ = app
                .dialog()
                .message(format!("Updater unavailable: {e}"))
                .title("Scryglass")
                .blocking_show();
            return;
        }
    };
    let update = match updater.check().await {
        Ok(Some(u)) => u,
        Ok(None) => {
            let _ = app
                .dialog()
                .message(format!("You're up to date (v{current})."))
                .title("Scryglass")
                .blocking_show();
            return;
        }
        Err(e) => {
            let _ = app
                .dialog()
                .message(format!("Update check failed: {e}"))
                .title("Scryglass")
                .blocking_show();
            return;
        }
    };
    let new_v = update.version.clone();
    // Never restart mid-game — defer with a message, leave it installable later.
    if is_match_active().await {
        let _ = app
            .dialog()
            .message(format!(
                "Scryglass v{new_v} is ready. You're in a match — install it from the menu when you finish."
            ))
            .title("Update available")
            .blocking_show();
        return;
    }
    // "What's new" — show the release notes (latest.json `notes`, sourced from
    // the CHANGELOG entry by the release CI) so the user sees what changed before
    // installing, like CodexBar/Sparkle. Truncated so the dialog stays readable.
    let whats_new = update
        .body
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|n| {
            let n: String = n.chars().take(700).collect();
            format!("\n\nWhat's new:\n{n}")
        })
        .unwrap_or_default();
    let install = app
        .dialog()
        .message(format!(
            "Scryglass v{new_v} is available (you have v{current}).{whats_new}\n\nInstall now and restart?"
        ))
        .title("Update available")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Install & Restart".into(),
            "Later".into(),
        ))
        .blocking_show();
    if install {
        match update.download_and_install(|_, _| {}, || {}).await {
            Ok(()) => app.restart(),
            Err(e) => {
                let _ = app
                    .dialog()
                    .message(format!("Install failed: {e}"))
                    .title("Scryglass")
                    .blocking_show();
            }
        }
    }
}
