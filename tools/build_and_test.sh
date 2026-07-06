#!/bin/bash
# Build the all-Rust product (glass-host + glass-server from glass-shard),
# compile the card DB, optionally smoke-test, then build+install the Tauri app.
# Usage: bash tools/build_and_test.sh [--install]
#   --install  Also rebuild Tauri and install to /Applications
#
# The Python host is parked under legacy-python/ and is no longer built or
# shipped. This script is the Rust-only replacement for the old PyInstaller flow.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
SCRY_ROOT="$(pwd)"

GLASS_SHARD="${GLASS_SHARD_DIR:-$(cd .. 2>/dev/null && pwd)/glass-shard}"
TARGET="${TARGET:-aarch64-apple-darwin}"

if [ ! -d "$GLASS_SHARD" ]; then
    echo "FAIL: glass-shard not found at $GLASS_SHARD"
    echo "  set GLASS_SHARD_DIR or clone it as a sibling of scryglass."
    exit 1
fi

echo "=== Step 1: Build Rust sidecars (from $GLASS_SHARD) ==="
GS_LOG=/tmp/scryglass-glass-shard-build.log
(
    cd "$GLASS_SHARD"
    cargo build -p glass-mtga --features server --release --bin glass-host
    cargo build -p glass-server --release
) >"$GS_LOG" 2>&1
tail -3 "$GS_LOG"
mkdir -p src-tauri/binaries
cp "$GLASS_SHARD/target/release/glass-host"  "src-tauri/binaries/glass-host-${TARGET}"
cp "$GLASS_SHARD/target/release/glass-server" "src-tauri/binaries/glass-server-${TARGET}"
chmod +x src-tauri/binaries/glass-host-${TARGET} src-tauri/binaries/glass-server-${TARGET}
echo "  glass-host + glass-server built + staged (target: $TARGET)"

echo ""
echo "=== Step 2: Compile card DB (glass_advise_db.json) ==="
DB_SRC="$GLASS_SHARD/data/cards/standard_oracle_plus.json"
[ -f "$DB_SRC" ] || DB_SRC="$GLASS_SHARD/data/cards/standard_oracle.json"
mkdir -p src-tauri/resources
(cd "$GLASS_SHARD" && cargo run -q -p glass-cli --release -- \
    compile-cards --input "$DB_SRC" --output "$SCRY_ROOT/src-tauri/resources/glass_advise_db.json") \
    && echo "  card DB compiled + staged"

# Guard: fail the build if the staged advise DB is blind to any card in the
# oracle (the 2026-07-06 Marvel-blindness incident: 255 cards silently dropped,
# advisor recommended "pass" for 14 turns). Non-negotiable gate before bundling.
echo "  verifying advise DB coverage ..."
"$GLASS_SHARD/scripts/check_advise_db.sh" "$SCRY_ROOT/src-tauri/resources/glass_advise_db.json" \
    || { echo "BUILD ABORTED: advise DB failed coverage guard"; exit 1; }

echo ""
echo "=== Step 2a: Build recognition meta_decks (discriminative TF-IDF + variant merge) ==="
# Combine ALL Standard decklist sources (arena tier-list + goldfish metagame +
# any future sources), then the builder merges near-duplicate variants
# (Jaccard ≥ 0.45) into broader buckets. More sources = more coverage.
DECK_DIR="$GLASS_SHARD/data/meta/decklists"
COMBINED=$(mktemp -t scry_decklists.XXXXXX)
cat "$DECK_DIR"/standard_*.txt > "$COMBINED" 2>/dev/null
if [ -s "$COMBINED" ]; then
    mkdir -p data/meta
    (cd "$GLASS_SHARD" && cargo run -q -p glass-cli --release -- build-meta-decks \
        --decklists "$COMBINED" --catalog "$DB_SRC" --merge 0.45 \
        --out "$SCRY_ROOT/data/meta/meta_decks.json") \
        && echo "  meta_decks built + staged from $(ls "$DECK_DIR"/standard_*.txt 2>/dev/null | wc -l | tr -d ' ') source file(s)"
else
    echo "  (no decklists found — keeping existing meta_decks.json)"
fi
rm -f "$COMBINED"

echo ""
echo "=== Step 2b: overlay-helper ==="
if [ -f "$GLASS_SHARD/scripts/build-overlay-helper.sh" ]; then
    bash "$GLASS_SHARD/scripts/build-overlay-helper.sh" \
        && echo "  overlay-helper built" \
        || echo "  overlay-helper build skipped (non-fatal)"
elif [ ! -f "src-tauri/binaries/overlay-helper-${TARGET}" ]; then
    echo "  overlay-helper: no builder, leaving any existing binary in place"
fi

if [[ "${1:-}" == "--install" ]]; then
    echo ""
    echo "=== Step 3: Tauri build ==="
    TAURI_LOG=/tmp/scryglass-tauri-build.log
    (
        cd src-tauri
        cargo tauri build --bundles app >"$TAURI_LOG" 2>&1
    )
    tail -3 "$TAURI_LOG"

    echo ""
    echo "=== Step 4: Install ==="
    pkill -x Scryglass 2>/dev/null || true
    pkill -x scryglass 2>/dev/null || true  # the macOS binary is lowercase
    pkill -x overlay-helper 2>/dev/null || true
    pkill -f "/Applications/Scryglass.app/Contents/MacOS/glass-host" 2>/dev/null || true
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
    echo "=== Step 5: Launch + verify ==="
    open /Applications/Scryglass.app
    EXPECTED_MANAGE_HASH=$(shasum -a 256 static/manage.html | awk '{print $1}')
    ACTUAL_MANAGE_HASH=""
    for i in $(seq 1 60); do  # cold start loads two Rust sidecars' card DBs
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
