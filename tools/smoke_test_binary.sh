#!/bin/bash
# Smoke test for PyInstaller binary — run after `pyinstaller scry-server.spec`
# Verifies the binary starts, serves health endpoint, and static files exist.

BINARY="${1:-dist/scry-server}"
PORT=18765  # Use non-default port to avoid conflicts

if [ ! -f "$BINARY" ]; then
    echo "FAIL: Binary not found: $BINARY"
    exit 1
fi

echo "Testing $BINARY on port $PORT..."

# Start binary on alternate port
SCRY_PORT=$PORT "$BINARY" &>/dev/null &
PID=$!
cleanup() { kill $PID 2>/dev/null; wait $PID 2>/dev/null || true; }
trap cleanup EXIT

# Wait for startup (max 30s)
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Test 1: Health endpoint
HEALTH=$(curl -sf "http://localhost:$PORT/health" 2>/dev/null)
if [ -z "$HEALTH" ]; then
    echo "FAIL: Health endpoint not responding"
    exit 1
fi
echo "  OK: health endpoint"

# Test 2: App version present
echo "$HEALTH" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'app_version' in d, 'missing app_version'" || {
    echo "FAIL: app_version missing from health"
    exit 1
}
VERSION=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.load(sys.stdin)['app_version'])")
echo "  OK: app_version=$VERSION"

# Test 3: Static files served (the bug that caused 500 errors)
for file in app.js style.css; do
    STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:$PORT/static/$file" 2>/dev/null)
    if [ "$STATUS" != "200" ]; then
        echo "FAIL: /static/$file returned $STATUS (expected 200)"
        exit 1
    fi
done
echo "  OK: static files served"

# Test 4: HTML pages
for page in / /overlay /stats /review /manage /decks; do
    STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:$PORT$page" 2>/dev/null)
    if [ "$STATUS" != "200" ]; then
        echo "FAIL: $page returned $STATUS (expected 200)"
        exit 1
    fi
done
echo "  OK: all HTML pages"

# Test 4b: Manage page content matches the checked-in source file.
SOURCE_MANAGE_HASH=$(shasum -a 256 static/manage.html | awk '{print $1}')
SERVED_MANAGE_HASH=$(curl -sf "http://localhost:$PORT/manage" 2>/dev/null | shasum -a 256 | awk '{print $1}')
if [ -z "$SERVED_MANAGE_HASH" ] || [ "$SOURCE_MANAGE_HASH" != "$SERVED_MANAGE_HASH" ]; then
    echo "FAIL: /manage content hash does not match static/manage.html"
    echo "  source=$SOURCE_MANAGE_HASH"
    echo "  served=$SERVED_MANAGE_HASH"
    exit 1
fi
echo "  OK: manage.html matches source"

# Test 5: Card count > 0
CARDS=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.load(sys.stdin).get('card_count',0))")
if [ "$CARDS" -lt 1000 ]; then
    echo "FAIL: card_count=$CARDS (expected 1000+)"
    exit 1
fi
echo "  OK: card_count=$CARDS"

echo "PASS: All smoke tests passed (v$VERSION, $CARDS cards)"
cleanup
trap - EXIT
exit 0
