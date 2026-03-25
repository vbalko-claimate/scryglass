/// Native macOS overlay window that can appear over fullscreen apps.
/// Uses raw NSWindow + WKWebView instead of Tauri window.

use cocoa::appkit::{NSWindow, NSWindowStyleMask, NSBackingStoreType};
use cocoa::base::{id, nil, YES, NO};
use cocoa::foundation::{NSRect, NSPoint, NSSize, NSString};
use std::sync::atomic::{AtomicBool, Ordering};

static OVERLAY_VISIBLE: AtomicBool = AtomicBool::new(false);
static mut OVERLAY_WINDOW: Option<id> = None;

/// Create the native overlay window on the main thread.
/// Must be called from the main thread!
pub fn create_overlay() {
    unsafe {
        let frame = NSRect::new(NSPoint::new(8.0, 800.0), NSSize::new(280.0, 240.0));

        let window: id = NSWindow::alloc(nil).initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMask::NSBorderlessWindowMask,
            NSBackingStoreType::NSBackingStoreBuffered,
            NO,
        );

        // Transparent background
        window.setOpaque_(NO);
        let clear_color: id = msg_send![class!(NSColor), clearColor];
        window.setBackgroundColor_(clear_color);

        // Window level and behavior for fullscreen
        window.setLevel_(1000); // screenSaver level
        let behavior: u64 = (1 << 0) | (1 << 4) | (1 << 8); // canJoinAllSpaces | stationary | fullScreenAuxiliary
        let _: () = msg_send![window, setCollectionBehavior: behavior];
        let _: () = msg_send![window, setHasShadow: NO];

        // Create WKWebView to load overlay HTML
        let wk_config: id = msg_send![class!(WKWebViewConfiguration), new];

        // Enable transparent background for WKWebView
        let prefs: id = msg_send![wk_config, preferences];
        // WKWebView doesn't have setDrawsBackground directly on config,
        // but we can set it on the webview after creation

        let content_rect = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(280.0, 240.0));
        let webview: id = msg_send![class!(WKWebView), alloc];
        let webview: id = msg_send![webview, initWithFrame:content_rect configuration:wk_config];

        // Transparent webview background
        let _: () = msg_send![webview, setDrawsBackground: NO];
        // Also set the underlying layer to transparent
        let _: () = msg_send![webview, setValue:NO forKey:NSString::alloc(nil).init_str("drawsBackground")];

        // Load overlay URL
        let url_str = NSString::alloc(nil).init_str("http://localhost:8765/overlay");
        let url: id = msg_send![class!(NSURL), URLWithString: url_str];
        let request: id = msg_send![class!(NSURLRequest), requestWithURL: url];
        let _: () = msg_send![webview, loadRequest: request];

        // Set as content view
        window.setContentView_(webview);

        // Don't show yet — wait for MTGA
        OVERLAY_WINDOW = Some(window);
        OVERLAY_VISIBLE.store(false, Ordering::SeqCst);

        println!("[overlay] Native NSWindow + WKWebView created");
    }
}

/// Show overlay and position it. Call from main thread.
pub fn show_overlay(x: f64, y: f64) {
    unsafe {
        if let Some(window) = OVERLAY_WINDOW {
            // Convert from top-left (CGWindowList) to bottom-left (AppKit)
            // Get main screen height
            let screens: id = msg_send![class!(NSScreen), screens];
            let main_screen: id = msg_send![screens, objectAtIndex: 0u64];
            let screen_frame: NSRect = msg_send![main_screen, frame];
            let screen_height = screen_frame.size.height;

            let appkit_y = screen_height - y - 240.0; // 240 = overlay height
            let frame = NSRect::new(NSPoint::new(x, appkit_y), NSSize::new(280.0, 240.0));
            window.setFrame_display_(frame, YES);

            let _: () = msg_send![window, orderFrontRegardless];
            OVERLAY_VISIBLE.store(true, Ordering::SeqCst);
        }
    }
}

/// Hide overlay. Call from main thread.
pub fn hide_overlay() {
    unsafe {
        if let Some(window) = OVERLAY_WINDOW {
            let _: () = msg_send![window, orderOut: nil];
            OVERLAY_VISIBLE.store(false, Ordering::SeqCst);
        }
    }
}

pub fn is_visible() -> bool {
    OVERLAY_VISIBLE.load(Ordering::SeqCst)
}
