fn main() {
    // Compile Swift overlay helper into native binary
    #[cfg(target_os = "macos")]
    {
        let manifest_dir =
            std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
        let arch = std::env::var("CARGO_CFG_TARGET_ARCH").unwrap();
        let target_triple = format!("{}-apple-darwin", arch);
        let output_path = manifest_dir
            .join("binaries")
            .join(format!("overlay-helper-{}", target_triple));
        let swift_source = manifest_dir.join("overlay_helper.swift");

        println!("cargo:rerun-if-changed=overlay_helper.swift");

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
    }

    tauri_build::build()
}
