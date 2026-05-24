#!/bin/bash
# Build PyInstaller binary + run smoke test.
# Usage: bash tools/build_and_test.sh [--install]
#   --install  Also rebuild Tauri and install to /Applications
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== Step 1: PyInstaller build ==="
rm -rf build/ dist/
PYI_LOG=/tmp/scryglass-pyinstaller.log
uv run pyinstaller scry-server.spec --noconfirm >"$PYI_LOG" 2>&1
tail -3 "$PYI_LOG"

echo ""
echo "=== Step 2: Smoke test ==="
bash tools/smoke_test_binary.sh dist/scry-server || {
    echo "SMOKE TEST FAILED — not proceeding with install"
    exit 1
}

if [[ "${1:-}" == "--install" ]]; then
    echo ""
    echo "=== Step 3: Tauri build ==="
    cp dist/scry-server src-tauri/binaries/scry-server-aarch64-apple-darwin
    TAURI_LOG=/tmp/scryglass-tauri-build.log
    (
        cd src-tauri
        cargo tauri build --bundles app >"$TAURI_LOG" 2>&1
    )
    tail -3 "$TAURI_LOG"

    echo ""
    echo "=== Step 4: Install ==="
    pkill -x Scryglass 2>/dev/null || true
    pkill -x scry-serv 2>/dev/null || true
    pkill -f "/Applications/Scryglass.app/Contents/MacOS/scry-server" 2>/dev/null || true
    for i in $(seq 1 20); do
        if ! lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    rm -rf /Applications/Scryglass.app
    cp -R src-tauri/target/release/bundle/macos/Scryglass.app /Applications/
    xattr -dr com.apple.quarantine /Applications/Scryglass.app 2>/dev/null
    echo "Installed to /Applications/Scryglass.app"

    echo ""
    echo "=== Step 5: Launch ==="
    open /Applications/Scryglass.app
    EXPECTED_MANAGE_HASH=$(shasum -a 256 static/manage.html | awk '{print $1}')
    ACTUAL_MANAGE_HASH=""
    for i in $(seq 1 25); do
        if curl -sf http://localhost:8765/health >/dev/null 2>&1; then
            ACTUAL_MANAGE_HASH=$(curl -sf http://localhost:8765/manage 2>/dev/null | shasum -a 256 | awk '{print $1}')
            if [ "$ACTUAL_MANAGE_HASH" = "$EXPECTED_MANAGE_HASH" ]; then
                echo "App running: $(curl -s http://localhost:8765/health)"
                echo "Manage UI hash verified: $ACTUAL_MANAGE_HASH"
                break
            fi
        fi
        sleep 1
    done
    if [ "$ACTUAL_MANAGE_HASH" != "$EXPECTED_MANAGE_HASH" ]; then
        echo "FAIL: Installed app is serving stale manage.html"
        echo "  expected=$EXPECTED_MANAGE_HASH"
        echo "  actual=$ACTUAL_MANAGE_HASH"
        exit 1
    fi
fi

echo ""
echo "Done."
