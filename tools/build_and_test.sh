#!/bin/bash
# Build PyInstaller binary + run smoke test.
# Usage: bash tools/build_and_test.sh [--install]
#   --install  Also rebuild Tauri and install to /Applications
set -e

cd "$(git rev-parse --show-toplevel)"

echo "=== Step 1: PyInstaller build ==="
rm -rf build/ dist/
uv run pyinstaller scry-server.spec --noconfirm 2>&1 | tail -3

echo ""
echo "=== Step 2: Smoke test ==="
bash tools/smoke_test_binary.sh dist/scry-server
if [ $? -ne 0 ]; then
    echo "SMOKE TEST FAILED — not proceeding with install"
    exit 1
fi

if [[ "$1" == "--install" ]]; then
    echo ""
    echo "=== Step 3: Tauri build ==="
    cp dist/scry-server src-tauri/binaries/scry-server-aarch64-apple-darwin
    cd src-tauri && cargo tauri build 2>&1 | tail -3
    cd ..

    echo ""
    echo "=== Step 4: Install ==="
    pkill -x scryglass 2>/dev/null || true
    sleep 2
    rm -rf /Applications/Scryglass.app
    cp -R src-tauri/target/release/bundle/macos/Scryglass.app /Applications/
    xattr -dr com.apple.quarantine /Applications/Scryglass.app 2>/dev/null
    echo "Installed to /Applications/Scryglass.app"

    echo ""
    echo "=== Step 5: Launch ==="
    open /Applications/Scryglass.app
    for i in $(seq 1 25); do
        if curl -sf http://localhost:8765/health >/dev/null 2>&1; then
            echo "App running: $(curl -s http://localhost:8765/health)"
            break
        fi
        sleep 1
    done
fi

echo ""
echo "Done."
