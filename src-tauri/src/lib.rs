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
mod overlay_config;
mod report;
mod sidecar;
mod update;

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
    let feedback_key = overlay_config::load(handle);
    let mut restart_count = 0u32;

    loop {
        println!(
            "[overlay] Starting overlay helper (attempt {})",
            restart_count + 1
        );

        let cmd = match shell.sidecar("overlay-helper") {
            Ok(c) => c.args(["--feedback-key", feedback_key.config_value()]),
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

#[cfg(target_os = "windows")]
fn push_windows_hotkey_label(overlay: &tauri::WebviewWindow, key: overlay_config::FeedbackKey) {
    if let Ok(label) = serde_json::to_string(key.windows_label()) {
        let _ = overlay.eval(&format!("setHotkeyLabel({label})"));
    }
}

/// Windows: Use Tauri overlay window — poll MTGA foreground + match status to show/hide.
/// Also polls the configured feedback key (Left Alt by default).
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
                        push_windows_hotkey_label(&overlay, overlay_config::load(&h1));
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
    //  - The configured key toggles feedback mode (click-through off + buttons).
    //    The left-hand default can be held while the mouse stays in the right hand.
    //  - Otherwise forward the cursor position so the overlay can peek (shrink) when
    //    the cursor is over it. GetCursorPos is a passive read → click-through stays
    //    intact; JS owns the geometry (see overlayCursor in overlay.html).
    let h2 = handle.clone();
    std::thread::spawn(move || {
        use windows::Win32::Foundation::POINT;
        use windows::Win32::UI::Input::KeyboardAndMouse::{
            GetAsyncKeyState, VK_LCONTROL, VK_LMENU, VK_MENU, VK_RCONTROL,
        };
        use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;

        let mut feedback_key = overlay_config::load(&h2);
        let mut last_config_refresh = std::time::Instant::now();
        let mut was_feedback = false;
        let mut was_peek_key = false;
        let mut suppress_feedback_until_release = false;
        let mut last_pt = (i32::MIN, i32::MIN);

        if let Some(overlay) = h2.get_webview_window("overlay") {
            push_windows_hotkey_label(&overlay, feedback_key);
        }

        loop {
            std::thread::sleep(std::time::Duration::from_millis(50));

            // File I/O stays off the 50ms hot path. A manual/UI config change is
            // picked up within about two seconds without restarting the app.
            if last_config_refresh.elapsed() >= std::time::Duration::from_secs(2) {
                let refreshed = overlay_config::load(&h2);
                if refreshed != feedback_key {
                    feedback_key = refreshed;
                    if let Some(overlay) = h2.get_webview_window("overlay") {
                        push_windows_hotkey_label(&overlay, feedback_key);
                    }
                }
                last_config_refresh = std::time::Instant::now();
            }

            let feedback_vk = match feedback_key {
                overlay_config::FeedbackKey::LeftAlt => VK_LMENU.0 as i32,
                overlay_config::FeedbackKey::RightCtrl => VK_RCONTROL.0 as i32,
                overlay_config::FeedbackKey::LeftCtrl => VK_LCONTROL.0 as i32,
            };
            let feedback_down = unsafe { GetAsyncKeyState(feedback_vk) } & (1i16 << 15) != 0;

            // Alt+H remains the peek shortcut even though Left Alt is now the
            // default feedback key. The chord takes precedence until Alt is
            // released, so setInteractiveMode's setPeek(false) coupling remains
            // intact and cannot immediately undo the toggle.
            let peek_key = (unsafe { GetAsyncKeyState(VK_MENU.0 as i32) } & (1i16 << 15) != 0)
                && (unsafe { GetAsyncKeyState(0x48) } & (1i16 << 15) != 0); // 0x48 = 'H'
            if suppress_feedback_until_release && !feedback_down {
                suppress_feedback_until_release = false;
            }
            if peek_key && !was_peek_key {
                suppress_feedback_until_release = true;
                if let Some(overlay) = h2.get_webview_window("overlay") {
                    let _ = overlay.set_ignore_cursor_events(true);
                    let _ = overlay.eval("setInteractiveMode(false); togglePeek()");
                }
                was_feedback = false;
            }
            was_peek_key = peek_key;

            let feedback_active = feedback_down && !suppress_feedback_until_release;
            if feedback_active != was_feedback {
                if let Some(overlay) = h2.get_webview_window("overlay") {
                    let _ = overlay.set_ignore_cursor_events(!feedback_active);
                    push_windows_hotkey_label(&overlay, feedback_key);
                    let js = format!("setInteractiveMode({feedback_active})");
                    let _ = overlay.eval(&js);
                }
                was_feedback = feedback_active;
            }

            // Peek: forward cursor pos (skip while in feedback mode, if unmoved, or
            // while the overlay is hidden — mirrors the Swift window.isVisible guard).
            if !feedback_active {
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
        // ★ FIRST IN THE CHAIN, AND IT MUST STAY FIRST.
        //
        // Two reasons, one of them specific to this app:
        //
        //  * the plugin's own docs require it (a later registration can miss the
        //    hand-off), and
        //  * ⚠ THE SIDECAR. `sidecar::start_and_wait` — called from `.setup()`
        //    below — runs `kill_stale_sidecars()` before spawning THIS bundle's
        //    glass-host, by design (it must never adopt an orphan from a prior
        //    version). A second launch that reached that code would therefore
        //    kill the RUNNING instance's backend and leave every one of its
        //    windows on a blank page.
        //
        // That cannot happen: plugin `setup` hooks run in `Builder::build()`
        // (`AppManager::initialize_plugins`, in registration order), whereas the
        // app's own `.setup()` closure runs later, from `App::run()`. The second
        // instance calls `std::process::exit(0)` inside the plugin's setup —
        // before any window is created, before `diag::init`, and before a single
        // line of sidecar code executes.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Runs in the ALREADY-RUNNING instance (its diag log is open, so
            // this is visible; the second process exits too early to log).
            diag::log("[single-instance] second launch — focusing the running window");
            if let Some(w) = app.get_webview_window("main") {
                // `show` as well as `unminimize`: closing the window only hides
                // it (see `on_window_event`), which is the state a menu-bar app
                // spends most of its life in — an unminimize alone would focus
                // a window nobody can see.
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
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

            // "What's changed": the FIRST run of a new version shows that
            // version's release notes in the app window, once. Not gated on the
            // updater (or on release builds) — a hand-installed DMG/MSI upgrade
            // has to be covered too, and the notes then come from the bundled
            // CHANGELOG instead of the update manifest.
            {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    update::deliver_whats_changed(&handle).await;
                });
            }

            // On launch AND every 4 hours after (release only): a background
            // update check.
            //
            // AT LAUNCH it now AUTO-APPLIES: check → download → install →
            // relaunch, with no dialog. The hard constraint is unchanged and is
            // enforced in `update::try_auto_apply` — it never restarts while a
            // match is live (and never on a match state it could not read). When
            // it declines, this falls straight back to the advertise-only
            // behaviour below and tries again on the next launch.
            //
            // The PERIODIC re-check stays advertise-only: the app stays open
            // across days, and silently restarting someone hours into a session
            // is a different (worse) bargain than doing it during startup. Two
            // surfaces:
            //   * the tray item retitles ("Install Update v…"), where the manual,
            //     consent-gated install still happens, unchanged;
            //   * the OVERLAY shows a banner. USER-REPORTED 2026-08-19: the
            //     tray-only advert was invisible in practice — a tester sat on
            //     an old version for days without knowing. The overlay is the
            //     one surface the player actually looks at.
            #[cfg(not(debug_assertions))]
            {
                let handle = app.handle().clone();
                let item = update_item.clone();
                tauri::async_runtime::spawn(async move {
                    // Only the first pass is the launch pass.
                    let mut at_launch = true;
                    loop {
                        if let Ok(updater) = handle.updater() {
                            if let Ok(Some(update)) = updater.check().await {
                                let v = update.version.clone();
                                if at_launch {
                                    // Returns only if it DECLINED — a successful
                                    // install restarts the process here.
                                    update::try_auto_apply(&handle, update).await;
                                }
                                let _ = item.set_text(format!("Install Update v{v}…"));
                                // Surface it in BOTH webviews: the MAIN app
                                // window is the primary notice (a real, clickable
                                // banner); the overlay shows it only out of a
                                // match and as a peek-pill line (its JS decides).
                                // The guarded call makes pages without the shell
                                // (e.g. /decks) a silent no-op instead of a JS
                                // error. Windows may not exist yet right after
                                // launch — retry for ~2 minutes, then let the
                                // 4-hour cycle try again. The version string
                                // comes from the SIGNED update manifest (semver),
                                // so interpolating it into eval is safe.
                                let js = format!(
                                    "window.showUpdateNotice&&window.showUpdateNotice('{v}')"
                                );
                                for _ in 0..24 {
                                    let mut delivered = 0;
                                    for label in ["main", "overlay"] {
                                        if let Some(w) = handle.get_webview_window(label) {
                                            if w.eval(&js).is_ok() {
                                                delivered += 1;
                                            }
                                        }
                                    }
                                    if delivered == 2 {
                                        break;
                                    }
                                    tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                                }
                            }
                        }
                        at_launch = false;
                        tokio::time::sleep(std::time::Duration::from_secs(4 * 3600)).await;
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
    //
    // ⚠ THIS USED TO BE DEAD. The old helper asked glass-host for `/active`, a
    // route that does not exist (verified: 404 against a running host), so the
    // deferral fired exactly never and this dialog could install on top of a
    // live game. `update::match_definitely_active` asks `/match-status`, which
    // is the tracker's own match-lifecycle flag. "Definitely": an unreadable
    // answer must not block a user who explicitly asked for the update — that
    // asymmetry with the unattended path is deliberate.
    if update::match_definitely_active().await {
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
        // Keep the release notes for the process that replaces this one:
        // `update.body` dies with it, and the next launch shows "what's changed"
        // whether the install came from here or from the automatic launch path.
        update::stage_notes(&app, &new_v, update.body.as_deref());
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
                // The install we staged notes for did not happen; leaving them
                // would make a later launch of THIS version show them.
                update::clear_staged_notes(&app);
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
