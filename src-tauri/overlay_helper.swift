#!/usr/bin/env swift
// Native overlay window for Scryglass
// Launched as subprocess, creates NSWindow + WKWebView over fullscreen MTGA

import Cocoa
import WebKit

class OverlayDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Create borderless transparent window
        window = NSWindow(
            contentRect: NSRect(x: 8, y: 800, width: 280, height: 240),
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

        if mtgaFront && !window.isVisible {
            // Find MTGA window position
            let options: CGWindowListOption = [.optionAll, .excludeDesktopElements]
            if let windowList = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] {
                var bestArea = 0
                var bestX = 0.0, bestY = 0.0
                for w in windowList {
                    let owner = w["kCGWindowOwnerName"] as? String ?? ""
                    if owner.contains("MTGA") {
                        let bounds = w["kCGWindowBounds"] as? [String: Any] ?? [:]
                        let width = bounds["Width"] as? Int ?? 0
                        let height = bounds["Height"] as? Int ?? 0
                        let area = width * height
                        if area > bestArea && width > 100 && height > 100 {
                            bestArea = area
                            bestX = Double(bounds["X"] as? Int ?? 0)
                            bestY = Double(bounds["Y"] as? Int ?? 0)
                        }
                    }
                }
                if bestArea > 0 {
                    // Convert from top-left (CG) to bottom-left (AppKit)
                    let screenHeight = NSScreen.main?.frame.height ?? 1080
                    let appkitY = screenHeight - bestY - 240
                    window.setFrameOrigin(NSPoint(x: bestX + 8, y: appkitY))
                }
            }
            window.orderFrontRegardless()
        } else if !mtgaFront && window.isVisible {
            window.orderOut(nil)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = OverlayDelegate()
app.delegate = delegate
app.run()
