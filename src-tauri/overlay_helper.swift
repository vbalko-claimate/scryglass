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

        // Monitor for Option+drag to reposition overlay
        NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            guard let self = self else { return }
            // Option key toggles mouse interactivity (for dragging)
            if event.modifierFlags.contains(.option) {
                self.window.ignoresMouseEvents = false
            } else {
                self.window.ignoresMouseEvents = true
            }
        }

        // Allow dragging when mouse events are enabled
        window.isMovableByWindowBackground = true

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

        // Restore saved position
        let savedX = UserDefaults.standard.double(forKey: "overlay_x")
        let savedY = UserDefaults.standard.double(forKey: "overlay_y")
        if savedX > 0 || savedY > 0 {
            window.setFrameOrigin(NSPoint(x: savedX, y: savedY))
        }

        // Save position when window moves
        NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification, object: window, queue: nil
        ) { [weak self] _ in
            guard let origin = self?.window.frame.origin else { return }
            UserDefaults.standard.set(origin.x, forKey: "overlay_x")
            UserDefaults.standard.set(origin.y, forKey: "overlay_y")
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
            // Only set position if user hasn't manually positioned it
            let hasCustomPosition = UserDefaults.standard.double(forKey: "overlay_x") > 0
                || UserDefaults.standard.double(forKey: "overlay_y") > 0
            if !hasCustomPosition {
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
                        let screenHeight = NSScreen.main?.frame.height ?? 1080
                        let appkitY = screenHeight - bestY - 240
                        window.setFrameOrigin(NSPoint(x: bestX + 8, y: appkitY))
                    }
                }
            }
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
