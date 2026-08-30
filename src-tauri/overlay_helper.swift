// overlay_helper — compiled native overlay for Scryglass
// Launched as subprocess, creates NSWindow + WKWebView over fullscreen MTGA

import Cocoa
import WebKit

enum FeedbackKey: String {
    case leftAlt = "left_alt"
    case rightCtrl = "right_ctrl"
    case leftCtrl = "left_ctrl"

    static func fromLaunchArguments() -> FeedbackKey {
        let args = CommandLine.arguments
        guard let flag = args.firstIndex(of: "--feedback-key"), flag + 1 < args.count else {
            return .leftAlt
        }
        return FeedbackKey(rawValue: args[flag + 1]) ?? .leftAlt
    }

    var keyCode: UInt16 {
        switch self {
        case .leftAlt: return 58       // Left Option
        case .rightCtrl: return 62     // Right Control (experimental)
        case .leftCtrl: return 55      // Legacy binding: Left Command on macOS
        }
    }

    var label: String {
        switch self {
        case .leftAlt: return "Left Option"
        case .rightCtrl: return "Right Ctrl"
        case .leftCtrl: return "Left Command"
        }
    }
}

/// Find the NSScreen that MTGA's main window is on (multi-monitor aware).
/// CoreGraphics window bounds use a top-left origin; convert the window center
/// to AppKit global coords (bottom-left origin) to locate the containing screen.
func mtgaScreen() -> NSScreen? {
    let options: CGWindowListOption = [.optionAll, .excludeDesktopElements]
    guard let windowList = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else { return nil }
    let primaryHeight = CGDisplayBounds(CGMainDisplayID()).height
    for win in windowList {
        let owner = win["kCGWindowOwnerName"] as? String ?? ""
        guard owner.contains("MTGA") else { continue }
        guard let bounds = win["kCGWindowBounds"] as? [String: Any],
              let x = (bounds["X"] as? NSNumber)?.doubleValue,
              let y = (bounds["Y"] as? NSNumber)?.doubleValue,
              let w = (bounds["Width"] as? NSNumber)?.doubleValue,
              let h = (bounds["Height"] as? NSNumber)?.doubleValue,
              w > 100, h > 100 else { continue }
        // window center in CoreGraphics coords → AppKit global coords
        let cgCenter = NSPoint(x: x + w / 2.0, y: y + h / 2.0)
        let akPoint = NSPoint(x: cgCenter.x, y: primaryHeight - cgCenter.y)
        if let screen = NSScreen.screens.first(where: { $0.frame.contains(akPoint) }) {
            return screen
        }
    }
    return nil
}

// Custom window — canBecomeKey toggles for feedback mode
class OverlayWindow: NSWindow {
    var interactiveMode = false
    override var canBecomeKey: Bool { interactiveMode }
    override var canBecomeMain: Bool { false }
}

class OverlayDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var timer: Timer?
    let feedbackKey = FeedbackKey.fromLaunchArguments()

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Create borderless transparent window
        // Full screen transparent window — overlay elements position themselves via CSS
        let screenFrame = mtgaScreen()?.frame ?? NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        window = OverlayWindow(
            contentRect: screenFrame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.isOpaque = false
        window.backgroundColor = NSColor.clear
        window.level = NSWindow.Level(rawValue: 1000) // above fullscreen
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        window.hasShadow = false
        window.ignoresMouseEvents = true  // click-through — clicks go to MTGA

        // Keyboard repositioning: Option + arrow keys move the overlay panel
        // Sends position offset to WKWebView via JavaScript
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, event.modifierFlags.contains(.option) else { return }
            // Option+H remains the peek shortcut. H makes this a chord, so when
            // Left Option is also the feedback key the chord takes precedence
            // until Option is released.
            if event.keyCode == 4 {
                DispatchQueue.main.async {
                    if self.feedbackKey == .leftAlt || self.isInteractive {
                        self.suppressFeedbackUntilRelease = true
                        self.exitFeedbackMode()
                    }
                    self.webView.evaluateJavaScript("togglePeek()", completionHandler: nil)
                }
                return
            }
            let step: CGFloat = event.modifierFlags.contains(.shift) ? 50 : 10
            var dx: CGFloat = 0, dy: CGFloat = 0
            switch event.keyCode {
            case 123: dx = -step  // left
            case 124: dx = step   // right
            case 126: dy = -step  // up
            case 125: dy = step   // down
            default: return
            }
            // Send to WKWebView
            let js = "moveOverlay(\(dx), \(dy))"
            self.webView.evaluateJavaScript(js, completionHandler: nil)
        }

        // Mouse-move monitor — peek (shrink) the overlay while the cursor is over
        // it and spring it back when the cursor leaves, so it stops covering the
        // board on demand. A GLOBAL monitor is a passive observer (it does not
        // consume the event), so click-through to MTGA stays intact. JS owns the
        // geometry; we just forward the throttled cursor position in CSS px.
        NSEvent.addGlobalMonitorForEvents(matching: .mouseMoved) { [weak self] _ in
            guard let self = self, self.window.isVisible, !self.isInteractive else { return }
            let m = NSEvent.mouseLocation          // global coords, bottom-left origin
            let f = self.window.frame
            let domX = Int(m.x - f.minX)
            let domY = Int(f.maxY - m.y)           // → top-left origin (CSS px)
            let send = { [weak self] in
                self?.webView.evaluateJavaScript("overlayCursor(\(domX), \(domY))", completionHandler: nil)
            }
            self.pendingPeek?.cancel()
            let now = Date()
            if now.timeIntervalSince(self.lastPeekSend) >= 0.05 {
                self.lastPeekSend = now
                send()
            } else {
                // Trailing flush: deliver the cursor's final resting position, which
                // the leading throttle would otherwise drop — so a fast sweep off the
                // panel can't leave it stuck shrunk.
                let work = DispatchWorkItem { [weak self] in
                    self?.lastPeekSend = Date()
                    send()
                }
                self.pendingPeek = work
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.06, execute: work)
            }
        }

        // Create WKWebView with transparent background
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self

        // Load overlay HTML
        if let url = URL(string: "http://localhost:8765/overlay") {
            webView.load(URLRequest(url: url))
        }

        window.contentView = webView

        // Poll for MTGA every 2 seconds
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.syncWithMTGA()
        }

        // Monitor parent process — exit if parent dies (prevents zombie overlay)
        let parentPid = getppid()
        Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in
            if getppid() != parentPid || kill(parentPid, 0) != 0 {
                print("overlay: parent died, exiting")
                NSApplication.shared.terminate(nil)
            }
        }

        // Also exit if server stops responding
        Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.checkServerAlive()
        }

        // Configured modifier monitor — Left Option by default, so MTGA's
        // hold-Left-Control Full Control shortcut remains untouched.
        NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            guard let self = self else { return }
            guard event.keyCode == self.feedbackKey.keyCode else { return }
            // modifierFlags combines the left/right variants. Query the exact
            // key so holding the opposite Ctrl/Option/Command cannot mask this
            // key's release and leave feedback mode stuck on.
            let keyPressed = CGEventSource.keyState(
                .combinedSessionState,
                key: CGKeyCode(self.feedbackKey.keyCode)
            )
            DispatchQueue.main.async {
                if self.suppressFeedbackUntilRelease && !keyPressed {
                    self.suppressFeedbackUntilRelease = false
                }
                if keyPressed && !self.suppressFeedbackUntilRelease && !self.isInteractive && self.window.isVisible {
                    self.enterFeedbackMode()
                } else if !keyPressed && self.isInteractive {
                    self.exitFeedbackMode()
                }
            }
        }

        // Local mouse monitor — exit feedback mode shortly after a click/drag ends.
        // Keyed on mouse-UP (not down) so a drag-to-move (down → move → up) isn't
        // torn down mid-drag by the auto-exit flipping the window click-through.
        NSEvent.addLocalMonitorForEvents(matching: .leftMouseUp) { [weak self] event in
            guard let self = self, self.isInteractive else { return event }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                self?.exitFeedbackMode()
            }
            return event
        }

        print("overlay:ready")
        fflush(stdout)
    }

    var isInteractive = false
    var feedbackTimeout: DispatchWorkItem?
    var lastPeekSend = Date.distantPast
    var pendingPeek: DispatchWorkItem?
    var suppressFeedbackUntilRelease = false

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pushHotkeyLabel()
    }

    func pushHotkeyLabel() {
        webView.evaluateJavaScript("setHotkeyLabel('\(feedbackKey.label)')", completionHandler: nil)
    }

    func enterFeedbackMode() {
        isInteractive = true
        (window as! OverlayWindow).interactiveMode = true
        window.ignoresMouseEvents = false
        window.makeKeyAndOrderFront(nil)
        webView.evaluateJavaScript("setHotkeyLabel('\(feedbackKey.label)'); setInteractiveMode(true)", completionHandler: nil)

        // Safety timeout — 5s max
        feedbackTimeout?.cancel()
        feedbackTimeout = DispatchWorkItem { [weak self] in
            self?.exitFeedbackMode()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0, execute: feedbackTimeout!)
    }

    func exitFeedbackMode() {
        guard isInteractive else { return }
        isInteractive = false
        feedbackTimeout?.cancel()
        (window as! OverlayWindow).interactiveMode = false
        window.ignoresMouseEvents = true
        window.resignKey()
        webView.evaluateJavaScript("setHotkeyLabel('\(feedbackKey.label)'); setInteractiveMode(false)", completionHandler: nil)

        // Re-focus MTGA
        if let mtga = NSWorkspace.shared.runningApplications.first(where: { ($0.localizedName ?? "").contains("MTGA") }) {
            mtga.activate()
        }
    }

    func syncWithMTGA() {
        let frontApp = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
        let mtgaFront = frontApp.contains("MTGA")

        // Keep the overlay on MTGA's display (multi-monitor): reposition when
        // the detected MTGA screen differs from the window's current frame.
        if let s = mtgaScreen(), window.frame != s.frame {
            window.setFrame(s.frame, display: true)
        }

        // Show whenever MTGA is frontmost: in a match the advisor renders;
        // between matches the overlay shows the session record (the web view
        // decides which, based on match state). Hide only when MTGA isn't front.
        if mtgaFront && !window.isVisible {
            window.orderFrontRegardless()
        } else if !mtgaFront && window.isVisible {
            window.orderOut(nil)
        }
    }

    var serverFailCount = 0

    func checkServerAlive() {
        guard let url = URL(string: "http://localhost:8765/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { data, _, _ in
            ok = data != nil
            sem.signal()
        }.resume()
        sem.wait()
        if ok {
            serverFailCount = 0
        } else {
            serverFailCount += 1
            if serverFailCount >= 3 {
                print("overlay: server unreachable 3x, exiting")
                DispatchQueue.main.async {
                    NSApplication.shared.terminate(nil)
                }
            }
        }
    }

    func checkMatchActive() -> Bool {
        guard let url = URL(string: "http://localhost:8765/match-status") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        var active = false
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data = data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let isActive = json["active"] as? Bool {
                active = isActive
            }
            sem.signal()
        }.resume()
        sem.wait()
        return active
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = OverlayDelegate()
app.delegate = delegate
app.run()
