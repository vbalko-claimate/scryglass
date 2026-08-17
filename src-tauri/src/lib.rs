use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Manager,
};
#[cfg(not(debug_assertions))]
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
#[cfg(not(debug_assertions))]
use tauri_plugin_updater::UpdaterExt;

mod diag;
mod mtga_detect;
mod report;
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
        println!(
            "[overlay] Starting overlay helper (attempt {})",
            restart_count + 1
        );

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
                            println!(
                                "[overlay] Exited: code={:?} signal={:?}",
                                payload.code, payload.signal
                            );
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

/// Put the overlay over the WHOLE monitor it currently sits on.
///
/// `tauri.conf.json` pins the overlay to 1920x1080 at (0,0). On any other
/// resolution that is simply the wrong rectangle — on a larger screen it covers
/// the top-left corner, and the advice card, which CSS positions relative to
/// that surface, lands somewhere the player is not looking. A tester on
/// 2026-08-17 detected matches correctly and still saw no overlay.
///
/// ⚠ COMPILED ON EVERY PLATFORM ON PURPOSE. Only the Windows path calls it
/// (macOS drives its own Swift overlay), but `#[cfg(target_os = "windows")]`
/// code is NOT type-checked on the machine this is developed on, and the
/// cross-check of that target fails locally in a C dependency. Keeping this
/// cross-platform is what lets `cargo check` prove it before it ships.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn fit_overlay_to_monitor(overlay: &tauri::WebviewWindow) {
    // Prefer MTGA'S OWN rectangle. `current_monitor()` reports the monitor the
    // OVERLAY is on, which before it has ever moved is wherever the app window
    // is — so fitting to it put the overlay on the wrong screen for a
    // dual-monitor tester while cheerfully logging a correct-looking size.
    let mtga = mtga_detect::find_mtga_window();
    if mtga.found && mtga.width > 0 && mtga.height > 0 {
        let r1 = overlay.set_position(tauri::PhysicalPosition::new(mtga.x, mtga.y));
        let r2 = overlay.set_size(tauri::PhysicalSize::new(
            mtga.width as u32,
            mtga.height as u32,
        ));
        diag::log(&format!(
            "[overlay] fitted to the MTGA window {}x{} at ({},{}) (set_position={:?} set_size={:?})",
            mtga.width,
            mtga.height,
            mtga.x,
            mtga.y,
            r1.is_ok(),
            r2.is_ok()
        ));
        return;
    }
    diag::log(&format!(
        "[overlay] no MTGA geometry (found={}) — falling back to this window's monitor",
        mtga.found
    ));
    let monitor = match overlay.current_monitor() {
        Ok(Some(m)) => Some(m),
        _ => overlay.primary_monitor().ok().flatten(),
    };
    let Some(monitor) = monitor else {
        diag::log("[overlay!] no monitor reported — leaving the configured geometry");
        return;
    };
    let size = *monitor.size();
    let pos = *monitor.position();
    let r1 = overlay.set_position(tauri::PhysicalPosition::new(pos.x, pos.y));
    let r2 = overlay.set_size(tauri::PhysicalSize::new(size.width, size.height));
    diag::log(&format!(
        "[overlay] fitted to monitor {}x{} at ({},{}) scale={:.2} (set_position={:?} set_size={:?})",
        size.width,
        size.height,
        pos.x,
        pos.y,
        monitor.scale_factor(),
        r1.is_ok(),
        r2.is_ok()
    ));
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
        // The overlay needs BOTH conditions true, and when it fails to appear
        // nothing said which one was false — that ambiguity is most of the "the
        // overlay doesn't start" report from 2026-08-17. Log the pair on CHANGE
        // only; this loop runs every 2s for the life of the app.
        let mut last_inputs: Option<(bool, bool)> = None;

        loop {
            std::thread::sleep(std::time::Duration::from_secs(2));

            let mtga_front = mtga_detect::is_mtga_frontmost();
            let match_active = rt.block_on(async { check_match_active().await });

            if last_inputs != Some((mtga_front, match_active)) {
                last_inputs = Some((mtga_front, match_active));
                crate::diag::log(&format!(
                    "[overlay] mtga_foreground={mtga_front} match_active={match_active} \
                     (both must be true to show)"
                ));
            }

            let should_show = mtga_front && match_active;

            match h1.get_webview_window("overlay") {
                Some(overlay) => {
                    if should_show && !was_visible {
                        // Re-fit on every show, not once at startup: the player
                        // may have moved MTGA to another monitor or changed
                        // resolution since the last match.
                        fit_overlay_to_monitor(&overlay);
                        let _ = overlay.show();
                        was_visible = true;
                        crate::diag::log(&format!(
                            "[overlay] showing — visible={:?} position={:?} size={:?}",
                            overlay.is_visible(),
                            overlay.outer_position(),
                            overlay.outer_size()
                        ));
                    } else if !should_show && was_visible {
                        crate::diag::log("[overlay] hiding");
                        let _ = overlay.hide();
                        was_visible = false;
                    }
                }
                None => {
                    if should_show {
                        crate::diag::log("[overlay!] no 'overlay' window exists — cannot show it");
                    }
                }
            }
        }
    });

    // Key + cursor polling thread (50ms poll):
    //  - Left Ctrl toggles feedback mode (click-through off + interactive buttons).
    //    Left-hand key so it can be held while the mouse stays in the right hand.
    //  - Otherwise forward the cursor position so the overlay can peek (shrink) when
    //    the cursor is over it. GetCursorPos is a passive read → click-through stays
    //    intact; JS owns the geometry (see overlayCursor in overlay.html).
    let h2 = handle.clone();
    std::thread::spawn(move || {
        use windows::Win32::Foundation::POINT;
        use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LCONTROL, VK_MENU};
        use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;

        let mut was_ctrl = false;
        let mut was_peek_key = false;
        let mut last_pt = (i32::MIN, i32::MIN);

        loop {
            std::thread::sleep(std::time::Duration::from_millis(50));

            // Left Ctrl (≈ Left Command on Mac keyboards) toggles feedback mode.
            let ctrl_down = unsafe { GetAsyncKeyState(VK_LCONTROL.0 as i32) } & (1i16 << 15) != 0;

            if ctrl_down != was_ctrl {
                if let Some(overlay) = h2.get_webview_window("overlay") {
                    let _ = overlay.set_ignore_cursor_events(!ctrl_down);
                    let js = format!("setInteractiveMode({})", ctrl_down);
                    let _ = overlay.eval(&js);
                }
                was_ctrl = ctrl_down;
            }

            // Alt+H toggles the peek (shrink-to-pill) state (mirrors macOS Option+H).
            let peek_key = (unsafe { GetAsyncKeyState(VK_MENU.0 as i32) } & (1i16 << 15) != 0)
                && (unsafe { GetAsyncKeyState(0x48) } & (1i16 << 15) != 0); // 0x48 = 'H'
            if peek_key && !was_peek_key {
                if let Some(overlay) = h2.get_webview_window("overlay") {
                    let _ = overlay.eval("togglePeek()");
                }
            }
            was_peek_key = peek_key;

            // Peek: forward cursor pos (skip while in feedback mode, if unmoved, or
            // while the overlay is hidden — mirrors the Swift window.isVisible guard).
            if !ctrl_down {
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
                json.get("active")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false)
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
            // FIRST, before anything can fail: open the diagnostics log. Every
            // window in this app loads from the sidecar's :8765, so if the
            // sidecar dies there is no in-app surface left to report from.
            diag::init(app.handle());

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
                        diag::log(&format!("[error] Sidecar failed: {e}"));
                        // A NATIVE dialog, in addition to the error page below.
                        // The page is a `data:` URL, which Tauri may refuse to
                        // navigate to — and if it does, the tester is left with a
                        // blank window and no explanation at all, which is the
                        // report that started this. The dialog cannot be blocked
                        // by webview policy and names the log file.
                        //
                        // Non-blocking `show`: this runs inside the async runtime,
                        // where `blocking_show` is not allowed.
                        {
                            use tauri_plugin_dialog::DialogExt;
                            let where_to_look = match diag::log_path() {
                                Some(p) => format!("Log: {}", p.display()),
                                None => "No log file could be opened.".to_string(),
                            };
                            let detail = diag::tail(12).join("\n");
                            handle
                                .dialog()
                                .message(format!("{e}\n\n{where_to_look}\n\n{detail}"))
                                .title("Scryglass — the backend did not start")
                                .show(|_| {});
                        }
                        // Best-effort, and deliberately AFTER the dialog: the
                        // tester is told first, the analysis copy second. An
                        // upload that hangs must not delay what they see.
                        report::send_failure(&handle, "sidecar_failed", &e).await;
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
            // Always available, NOT only after a failure: when the backend is
            // down every other item here opens a localhost URL that cannot load.
            let diag_item = MenuItemBuilder::with_id("diagnostics", "Open Diagnostics Log").build(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "Quit Scryglass").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&show_item)
                .item(&review_item)
                .separator()
                .item(&setup_item)
                .item(&update_item)
                .item(&diag_item)
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
                        "diagnostics" => {
                            // Open the FILE, not a localhost page: this item has to
                            // work in exactly the state where the sidecar is dead.
                            match diag::log_path() {
                                Some(p) => {
                                    let _ = open::that(&p);
                                }
                                None => {
                                    use tauri_plugin_dialog::DialogExt;
                                    app.dialog()
                                        .message(diag::tail(40).join("\n"))
                                        .title("Scryglass diagnostics (memory only)")
                                        .show(|_| {});
                                }
                            }
                        }
                        "quit" => {
                            // Clean shutdown: the "Quit" item had NO handler, so it
                            // did nothing (menu-bar app never quit). Kill the
                            // sidecars first so they don't orphan + get reused by
                            // the next launch, then exit.
                            sidecar::kill_stale_sidecars();
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
        // ⚠ STOP THE SIDECAR FIRST. Windows cannot replace a binary that a
        // running process holds open, and the installer's own "close related
        // apps" step only knows about Scryglass.exe — `glass-host.exe` is a
        // separate process it never sees. A tester hit exactly this: every
        // other app closed, the sidecar stayed, and the update was blocked.
        crate::diag::log("[update] stopping glass-host so the installer can replace it");
        sidecar::kill_stale_sidecars();
        match update.download_and_install(|_, _| {}, || {}).await {
            Ok(()) => app.restart(),
            Err(e) => {
                crate::diag::log(&format!("[update!] install failed: {e}"));
                let _ = app
                    .dialog()
                    .message(format!("Install failed: {e}"))
                    .title("Scryglass")
                    .blocking_show();
                // We killed the backend to let the installer run and the
                // install did not happen, so nothing else will bring it back:
                // without this the app keeps running with a dead sidecar and
                // every window it owns is a blank page.
                if let Err(e) = sidecar::start_and_wait(&app).await {
                    crate::diag::log(&format!("[update!] could not restart glass-host: {e}"));
                }
            }
        }
    }
}
