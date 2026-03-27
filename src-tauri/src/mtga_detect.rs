use serde::Serialize;

#[derive(Serialize, Clone, Debug)]
pub struct MtgaWindow {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
    pub found: bool,
}

// ── macOS ──────────────────────────────────────────────────────────────

#[cfg(target_os = "macos")]
pub fn find_mtga_window() -> MtgaWindow {
    use std::process::Command;
    let script = r#"
import Cocoa
let options: CGWindowListOption = [.optionAll, .excludeDesktopElements]
guard let windowList = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
    print("0,0,0,0")
    exit(0)
}
var bestArea = 0
var bestX = 0, bestY = 0, bestW = 0, bestH = 0
for window in windowList {
    let owner = window["kCGWindowOwnerName"] as? String ?? ""
    if owner.contains("MTGA") {
        let bounds = window["kCGWindowBounds"] as? [String: Any] ?? [:]
        let w = bounds["Width"] as? Int ?? 0
        let h = bounds["Height"] as? Int ?? 0
        let x = bounds["X"] as? Int ?? 0
        let y = bounds["Y"] as? Int ?? 0
        let area = w * h
        if area > bestArea && w > 100 && h > 100 {
            bestArea = area
            bestX = x; bestY = y; bestW = w; bestH = h
        }
    }
}
print("\(bestX),\(bestY),\(bestW),\(bestH)")
"#;

    let output = Command::new("swift")
        .arg("-e")
        .arg(script)
        .output();

    match output {
        Ok(out) => {
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            let parts: Vec<i32> = s.split(',').filter_map(|p| p.parse().ok()).collect();
            if parts.len() == 4 && (parts[2] > 100 && parts[3] > 100) {
                MtgaWindow {
                    x: parts[0],
                    y: parts[1],
                    width: parts[2],
                    height: parts[3],
                    found: true,
                }
            } else {
                MtgaWindow { x: 0, y: 0, width: 0, height: 0, found: false }
            }
        }
        Err(_) => MtgaWindow { x: 0, y: 0, width: 0, height: 0, found: false },
    }
}

#[cfg(target_os = "macos")]
pub fn is_mtga_frontmost() -> bool {
    use std::process::Command;
    let script = r#"
import Cocoa
let front = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
print(front.contains("MTGA") ? "1" : "0")
"#;

    let output = Command::new("swift")
        .arg("-e")
        .arg(script)
        .output();

    match output {
        Ok(out) => {
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            s == "1"
        }
        Err(_) => false,
    }
}

// ── Windows ────────────────────────────────────────────────────────────

#[cfg(target_os = "windows")]
pub fn find_mtga_window() -> MtgaWindow {
    MtgaWindow { x: 0, y: 0, width: 0, height: 0, found: is_mtga_frontmost() }
}

#[cfg(target_os = "windows")]
pub fn is_mtga_frontmost() -> bool {
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowTextW};

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_invalid() {
            return false;
        }
        let mut buf = [0u16; 256];
        let len = GetWindowTextW(hwnd, &mut buf);
        if len == 0 {
            return false;
        }
        let title = String::from_utf16_lossy(&buf[..len as usize]);
        title.contains("MTGA") || title.contains("Magic: The Gathering Arena")
    }
}
