#!/usr/bin/env swift
// Native overlay window for Scryglass
// Launched as subprocess, creates NSWindow + WKWebView over fullscreen MTGA

import Cocoa
import WebKit

// Custom window that never becomes key/main — won't steal focus from MTGA
class OverlayWindow: NSWindow {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

class OverlayDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Create borderless transparent window
        // Full screen transparent window — overlay elements position themselves via CSS
        let screenFrame = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
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

        // Create WKWebView with transparent background
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")

        // Load overlay HTML
        if let url = URL(string: "http://localhost:8765/overlay") {
            webView.load(URLRequest(url: url))
        }

        window.contentView = webView

        // Poll for MTGA every 2 seconds
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.syncWithMTGA()
        }

        print("overlay:ready")
        fflush(stdout)
    }

    func syncWithMTGA() {
        let frontApp = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
        let mtgaFront = frontApp.contains("MTGA")

        // Check if match is active via server
        let matchActive = checkMatchActive()

        if mtgaFront && matchActive && !window.isVisible {
            window.orderFrontRegardless()
        } else if (!mtgaFront || !matchActive) && window.isVisible {
            window.orderOut(nil)
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
