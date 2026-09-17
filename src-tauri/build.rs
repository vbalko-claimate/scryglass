fn main() {
    // Compile Swift overlay helper into native binary
    #[cfg(target_os = "macos")]
    {
        let manifest_dir = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
        let arch = std::env::var("CARGO_CFG_TARGET_ARCH").unwrap();
        let target_triple = format!("{}-apple-darwin", arch);
        let output_path = manifest_dir
            .join("binaries")
            .join(format!("overlay-helper-{}", target_triple));
        let swift_source = manifest_dir.join("overlay_helper.swift");

        println!("cargo:rerun-if-changed=overlay_helper.swift");

        // The checked-in helper is a native sidecar, so a build can safely
        // reuse it when the host only needs to rebuild the Rust/Tauri app.
        // This is useful on macOS installations where the CLT Swift compiler
        // and SDK were shipped at different patch levels (Swift then refuses
        // to import SwiftShims before Rust compilation even starts).
        let skip_swift = std::env::var_os("SCRYGLASS_SKIP_SWIFT_BUILD").is_some();
        if !skip_swift {
            let status = std::process::Command::new("swiftc")
                .args([
                    "-O",
                    "-o",
                    output_path.to_str().unwrap(),
                    "-framework",
                    "Cocoa",
                    "-framework",
                    "WebKit",
                    "-target",
                    &format!("{}-apple-macosx14.0", arch),
                    swift_source.to_str().unwrap(),
                ])
                .status()
                .expect("Failed to run swiftc — is Xcode installed?");

            assert!(
                status.success(),
                "swiftc failed to compile overlay_helper.swift"
            );
        } else if !output_path.exists() {
            panic!(
                "SCRYGLASS_SKIP_SWIFT_BUILD is set but {} is missing",
                output_path.display()
            );
        }
    }

    tauri_build::build()
}
