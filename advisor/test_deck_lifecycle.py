"""Validate DeckService lifecycle — create, version, deploy, delete.

Uses the real DB (init_db ensures tables exist), test decks prefixed "test_".
Cleans up after itself.

Usage:
    uv run python -m advisor.test_deck_lifecycle
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .database import init_db, get_connection, USER_DATA_DIR
from .deck_lifecycle import DeckService, USER_RULES_DIR, USER_DECKS_DIR

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f"  -- {detail}"
        print(msg)


DECK_LIST_V1 = """\
4 Lightning Bolt (FDN) 192
4 Monastery Swiftspear (FDN) 157
4 Play with Fire (MID) 154
4 Kumano Faces Kakkazan (NEO) 152
4 Shock (FDN) 200
20 Mountain (TMT) 318
"""

DECK_LIST_V2 = """\
4 Lightning Bolt (FDN) 192
4 Monastery Swiftspear (FDN) 157
4 Play with Fire (MID) 154
2 Kumano Faces Kakkazan (NEO) 152
2 Goblin Guide (FDN) 155
4 Shock (FDN) 200
20 Mountain (TMT) 318
"""

DECK_EMPTY = ""

svc = DeckService(user_id="test_runner")

# ─── Cleanup helper ──────────────────────────────────────────

def cleanup_test_decks():
    """Remove all decks created by test_runner."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT deck_id FROM decks WHERE user_id = 'test_runner'"
        )
        ids = [r[0] for r in cur.fetchall()]
        for did in ids:
            conn.execute("DELETE FROM deck_versions WHERE deck_id = ?", (did,))
            conn.execute("DELETE FROM decks WHERE deck_id = ?", (did,))
        conn.commit()
    finally:
        conn.close()
    # Clean up files
    for did in ids:
        for p in [
            USER_RULES_DIR / f"{did}.json",
            USER_DECKS_DIR / f"{did}.txt",
        ]:
            if p.exists():
                p.unlink()
        # Version rule files
        for f in USER_RULES_DIR.glob(f"{did}_v*.json"):
            f.unlink()


# ─── Init ────────────────────────────────────────────────────

init_db()
cleanup_test_decks()

# ─── 1. Create Deck ─────────────────────────────────────────

print("\n=== 1. Create Deck ===")

result = svc.create_deck("Test Mono Red", DECK_LIST_V1)
deck_id = result["deck_id"]

check("returns deck_id", isinstance(result.get("deck_id"), str) and len(deck_id) > 0,
      f"got {result.get('deck_id')!r}")
check("returns colors list", isinstance(result.get("colors"), list),
      f"got {type(result.get('colors'))}")
check("returns archetype string", isinstance(result.get("archetype"), str),
      f"got {result.get('archetype')!r}")
check("card_count = 40", result.get("card_count") == 40,
      f"got {result.get('card_count')}")
check("version_number = 1", result.get("version_number") == 1,
      f"got {result.get('version_number')}")

# Empty deck list
empty_result = svc.create_deck("Test Empty Deck", DECK_EMPTY)
check("empty deck creates ok", empty_result.get("card_count") == 0,
      f"got {empty_result.get('card_count')}")

# Duplicate name → IntegrityError
try:
    svc.create_deck("Test Mono Red", DECK_LIST_V1)
    check("duplicate name raises error", False, "no exception raised")
except Exception as e:
    check("duplicate name raises error", "UNIQUE" in str(e) or "unique" in str(e).lower(),
          f"got {type(e).__name__}: {e}")

# Appears in list
decks = svc.list_decks()
found = [d for d in decks if d["deck_id"] == deck_id]
check("deck appears in list_decks()", len(found) == 1,
      f"found {len(found)} matches")

# ─── 2. Get Deck Detail ─────────────────────────────────────

print("\n=== 2. Get Deck Detail ===")

detail = svc.get_deck(deck_id)
check("get existing deck returns dict", detail is not None)
check("detail has versions list", isinstance(detail.get("versions"), list),
      f"got {type(detail.get('versions'))}")
check("detail has 1 version", len(detail.get("versions", [])) == 1,
      f"got {len(detail.get('versions', []))}")
check("active_version = 1", detail.get("active_version") == 1,
      f"got {detail.get('active_version')}")
check("missing_cards is a list", isinstance(detail.get("missing_cards"), list))

# Non-existent deck
none_result = svc.get_deck("nonexistent_deck_xyz")
check("get non-existent deck returns None", none_result is None,
      f"got {type(none_result)}")

# ─── 3. Add Version ─────────────────────────────────────────

print("\n=== 3. Add Version ===")

v2 = svc.add_version(deck_id, DECK_LIST_V2)
check("version_number increments to 2", v2.get("version_number") == 2,
      f"got {v2.get('version_number')}")
check("card_count correct", v2.get("card_count") == 40,
      f"got {v2.get('card_count')}")
check("change_summary non-empty", bool(v2.get("change_summary")),
      f"got {v2.get('change_summary')!r}")
check("change_summary has +/- format",
      "+" in v2.get("change_summary", "") or "-" in v2.get("change_summary", ""),
      f"got {v2.get('change_summary')!r}")

# Verify 2 versions in detail
detail2 = svc.get_deck(deck_id)
check("detail now has 2 versions", len(detail2.get("versions", [])) == 2,
      f"got {len(detail2.get('versions', []))}")

# Non-existent deck version
try:
    svc.add_version("nonexistent_deck_xyz", DECK_LIST_V2)
    check("add_version to missing deck raises", False, "no exception")
except ValueError:
    check("add_version to missing deck raises", True)

# ─── 4. Deploy Version ──────────────────────────────────────

print("\n=== 4. Deploy Version ===")

deploy = svc.deploy_version(deck_id, 2)
check("deploy returns deployed=True", deploy.get("deployed") is True,
      f"got {deploy.get('deployed')!r}")

# Check is_active flags
detail3 = svc.get_deck(deck_id)
versions = {v["version_number"]: v for v in detail3.get("versions", [])}
check("v2 is_active = True", versions.get(2, {}).get("is_active") is True,
      f"got {versions.get(2, {}).get('is_active')}")
check("v1 is_active = False", versions.get(1, {}).get("is_active") is False,
      f"got {versions.get(1, {}).get('is_active')}")

# Deck txt file written
deck_file = USER_DECKS_DIR / f"{deck_id}.txt"
check("deck .txt file written", deck_file.exists(),
      f"expected {deck_file}")

# Deploy non-existent version
try:
    svc.deploy_version(deck_id, 99)
    check("deploy missing version raises", False, "no exception")
except ValueError:
    check("deploy missing version raises", True)

# ─── 5. Field Name Contract ─────────────────────────────────

print("\n=== 5. Field Name Contract ===")

decks = svc.list_decks()
d = [x for x in decks if x["deck_id"] == deck_id][0]
check("list has deck_id (not id)", "deck_id" in d and "id" not in d)
check("list has active_version", "active_version" in d)

detail = svc.get_deck(deck_id)
v = detail["versions"][0]
check("version has version_number", "version_number" in v)
check("version has is_active", "is_active" in v)
check("version has created_at", "created_at" in v)
check("version has card_count", "card_count" in v)
check("version has rules_source", "rules_source" in v)

# ─── 6. Delete Deck ─────────────────────────────────────────

print("\n=== 6. Delete Deck ===")

svc.delete_deck(deck_id)
check("deleted deck returns None on get", svc.get_deck(deck_id) is None)
remaining = [d for d in svc.list_decks() if d["deck_id"] == deck_id]
check("deleted deck gone from list", len(remaining) == 0,
      f"found {len(remaining)}")

# DB versions also gone
conn = get_connection()
try:
    cur = conn.execute(
        "SELECT COUNT(*) FROM deck_versions WHERE deck_id = ?", (deck_id,)
    )
    check("versions deleted from DB", cur.fetchone()[0] == 0)
finally:
    conn.close()

# Deck file cleaned up
check("deck .txt file removed", not deck_file.exists(),
      f"{deck_file} still exists")

# ─── Cleanup ─────────────────────────────────────────────────

cleanup_test_decks()

# ─── Summary ─────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
sys.exit(0 if failed == 0 else 1)
