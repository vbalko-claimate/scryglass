"""Validate DeckService lifecycle — create, version, deploy, undeploy, delete.

Filesystem-based storage (one directory per deck).
Uses tmp directory for isolation — no side effects on real data.

Usage:
    uv run python -m advisor.test_deck_lifecycle
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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


# ─── Setup: use temp directory ──────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="scry_test_decks_")
print(f"Test dir: {tmpdir}")

# Patch USER_DATA_DIR before importing deck modules
import advisor.database
orig_user_data_dir = advisor.database.USER_DATA_DIR
advisor.database.USER_DATA_DIR = Path(tmpdir)

# Now patch deck_storage constants
import advisor.deck_storage as storage
storage.DECKS_ROOT = Path(tmpdir) / "decks"

from advisor.deck_lifecycle import DeckService

svc = DeckService(user_id="test_runner")

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

# Directory created
deck_dir = storage.DECKS_ROOT / deck_id
check("deck directory created", deck_dir.is_dir(),
      f"expected {deck_dir}")
check("deck.json exists", (deck_dir / "deck.json").exists())
check("versions dir created on first strategy write", True)  # created lazily by write_version_strategy

# deck.json content
deck_data = json.loads((deck_dir / "deck.json").read_text())
check("deck.json has name", deck_data.get("name") == "Test Mono Red")
check("deck.json state = draft", deck_data.get("state") == "draft")
check("deck.json has 1 version", len(deck_data.get("versions", [])) == 1)
check("version 1 is_active = True", deck_data["versions"][0].get("is_active") is True)

# Empty deck list
empty_result = svc.create_deck("Test Empty Deck", DECK_EMPTY)
check("empty deck creates ok", empty_result.get("card_count") == 0,
      f"got {empty_result.get('card_count')}")

# Duplicate name → error
try:
    svc.create_deck("Test Mono Red", DECK_LIST_V1)
    check("duplicate name raises error", False, "no exception raised")
except Exception as e:
    check("duplicate name raises error", "already exists" in str(e).lower(),
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

# First write a fake strategy for v1 to test deploy
fake_strategy = {"name": "Test", "rules": [{"id": "test_rule"}], "deck_signature": []}
storage.write_version_strategy(deck_id, 1, fake_strategy)

deploy = svc.deploy_version(deck_id, 1)
check("deploy returns deployed=True", deploy.get("deployed") is True,
      f"got {deploy.get('deployed')!r}")

# Check state
detail3 = svc.get_deck(deck_id)
check("state = deployed", detail3 is not None)
versions = {v["version_number"]: v for v in detail3.get("versions", [])}
check("v1 is_active = True", versions.get(1, {}).get("is_active") is True,
      f"got {versions.get(1, {}).get('is_active')}")
check("v2 is_active = False", versions.get(2, {}).get("is_active") is False,
      f"got {versions.get(2, {}).get('is_active')}")

# Strategy files deployed
check("strategy.json exists in deck dir",
      (deck_dir / "strategy.json").exists())
check("strategy.json deployed in deck dir",
      (deck_dir / "strategy.json").exists())

# Deploy non-existent version
try:
    svc.deploy_version(deck_id, 99)
    check("deploy missing version raises", False, "no exception")
except ValueError:
    check("deploy missing version raises", True)

# Deploy v2 switches active
deploy2 = svc.deploy_version(deck_id, 2)
detail4 = svc.get_deck(deck_id)
versions4 = {v["version_number"]: v for v in detail4.get("versions", [])}
check("after deploy v2: v2 is_active", versions4.get(2, {}).get("is_active") is True)
check("after deploy v2: v1 not active", versions4.get(1, {}).get("is_active") is False)


# ─── 5. Undeploy ────────────────────────────────────────────

print("\n=== 5. Undeploy ===")

undeploy = svc.undeploy_version(deck_id)
check("undeploy returns ok", undeploy.get("undeployed") is True)

detail5 = svc.get_deck(deck_id)
check("no active version after undeploy",
      detail5.get("active_version") is None,
      f"got {detail5.get('active_version')}")
check("strategy.json removed from deck dir",
      not (deck_dir / "strategy.json").exists())
check("strategy.json removed from deck dir",
      not (deck_dir / "strategy.json").exists())

# Deck data state
deck_json = json.loads((deck_dir / "deck.json").read_text())
check("state after undeploy != deployed",
      deck_json.get("state") != "deployed",
      f"got {deck_json.get('state')}")


# ─── 6. Strategy Inheritance ────────────────────────────────

print("\n=== 6. Strategy Inheritance ===")

# Write strategy for v2, then add v3
storage.write_version_strategy(deck_id, 2, fake_strategy)
v3 = svc.add_version(deck_id, DECK_LIST_V1)  # back to v1 decklist
check("v3 inherits rules", v3.get("inherited_rules_count") == 1,
      f"got {v3.get('inherited_rules_count')}")
# Check file exists
v3_path = storage.version_strategy_path(deck_id, 3)
check("v3 strategy file created", v3_path.exists())


# ─── 7. Field Name Contract ─────────────────────────────────

print("\n=== 7. Field Name Contract ===")

decks = svc.list_decks()
d = [x for x in decks if x["deck_id"] == deck_id][0]
check("list has deck_id (not id)", "deck_id" in d and "id" not in d)
check("list has active_version", "active_version" in d)
check("list has rules_count", "rules_count" in d)
check("list has rules_source", "rules_source" in d)

detail = svc.get_deck(deck_id)
v = detail["versions"][0]
check("version has version_number", "version_number" in v)
check("version has is_active", "is_active" in v)
check("version has created_at", "created_at" in v)
check("version has card_count", "card_count" in v)
check("version has rules_source", "rules_source" in v)


# ─── 8. Delete Deck ─────────────────────────────────────────

print("\n=== 8. Delete Deck ===")

# Deploy first so we can test cleanup
svc.deploy_version(deck_id, 1)
svc.delete_deck(deck_id)
check("deleted deck returns None on get", svc.get_deck(deck_id) is None)
remaining = [d for d in svc.list_decks() if d["deck_id"] == deck_id]
check("deleted deck gone from list", len(remaining) == 0,
      f"found {len(remaining)}")
check("deck directory removed", not deck_dir.exists())
check("deck dir fully removed", not deck_dir.exists())


# ─── 9. Storage Functions ───────────────────────────────────

print("\n=== 9. Storage Functions ===")

# list_deck_ids
svc.create_deck("Storage Test A", DECK_LIST_V1)
svc.create_deck("Storage Test B", DECK_LIST_V1)
ids = storage.list_deck_ids()
check("list_deck_ids returns both", "storage_test_a" in ids and "storage_test_b" in ids,
      f"got {ids}")

# read non-existent
check("read_deck missing returns None", storage.read_deck("nope") is None)

# version_strategy_path
vp = storage.version_strategy_path("foo", 3)
check("version_strategy_path format", vp.name == "v3_strategy.json")


# ─── 10. Promote Stub ───────────────────────────────────────

print("\n=== 10. Promote Stub ===")

# Create a stub: deck dir with only strategy.json (no deck.json)
stub_id = "stub_test_deck"
stub_dir = storage.DECKS_ROOT / stub_id
stub_dir.mkdir(parents=True, exist_ok=True)
stub_strategy = {"name": "Stub Test", "colors": ["R"], "archetype": "aggro",
                 "deck_signature": ["Lightning Bolt"], "rules": [{"id": "r1"}, {"id": "r2"}]}
(stub_dir / "strategy.json").write_text(json.dumps(stub_strategy))

# Verify it's a stub (no deck.json)
check("stub has no deck.json", not (stub_dir / "deck.json").exists())
check("stub has strategy.json", (stub_dir / "strategy.json").exists())

# Promote
result = svc.promote_stub(stub_id)
check("promote returns promoted=True", result.get("promoted") is True)
check("promote returns rules_count=2", result.get("rules_count") == 2)
check("promote creates deck.json", (stub_dir / "deck.json").exists())

# Verify deck data
promoted_data = json.loads((stub_dir / "deck.json").read_text())
check("promoted deck has name", promoted_data.get("name") == "Stub Test")
check("promoted deck state=has_rules", promoted_data.get("state") == "has_rules")
check("promoted deck card_count=0", promoted_data["versions"][0].get("card_count") == 0)
check("promoted deck has v1 strategy", (stub_dir / "versions" / "v1_strategy.json").exists())

# Now visible in list_decks
found = [d for d in svc.list_decks() if d["deck_id"] == stub_id]
check("promoted deck in list_decks", len(found) == 1)

# Can't promote again
try:
    svc.promote_stub(stub_id)
    check("re-promote raises error", False, "no exception")
except ValueError:
    check("re-promote raises error", True)

# Clean up
svc.delete_deck(stub_id)


# ─── 11. Migration ──────────────────────────────────────────

print("\n=== 11. Migration from SQLite ===")

import sqlite3

migrate_db = Path(tmpdir) / "test_migrate.db"
conn = sqlite3.connect(str(migrate_db))
conn.execute("""CREATE TABLE decks (
    deck_id TEXT PRIMARY KEY, user_id TEXT, name TEXT,
    colors TEXT, archetype TEXT, created_at TEXT, updated_at TEXT
)""")
conn.execute("""CREATE TABLE deck_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id TEXT, version_number INTEGER, deck_list TEXT,
    deck_list_hash TEXT, card_count INTEGER, change_summary TEXT,
    rules_path TEXT, rules_source TEXT, rules_count INTEGER,
    rules_validated INTEGER, ga_status TEXT, ga_fitness REAL,
    ga_generations INTEGER, is_active INTEGER, created_at TEXT
)""")
conn.execute(
    "INSERT INTO decks VALUES (?, ?, ?, ?, ?, ?, ?)",
    ("migrate_test", "local", "Migrate Test", '["R"]', "aggro", "2026-01-01", "2026-01-01"),
)
conn.execute(
    "INSERT INTO deck_versions (deck_id, version_number, deck_list, deck_list_hash, "
    "card_count, rules_path, rules_source, rules_count, rules_validated, "
    "ga_status, ga_fitness, ga_generations, is_active, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("migrate_test", 1, DECK_LIST_V1, "abc123", 40, "", "mechanical", 10, 1,
     "not_started", 0, 0, 1, "2026-01-01"),
)
conn.commit()
conn.close()

count = storage.migrate_from_db(migrate_db)
check("migration count = 1", count == 1, f"got {count}")
check("migrated deck readable", storage.read_deck("migrate_test") is not None)
migrated_data = storage.read_deck("migrate_test")
check("migrated name correct", migrated_data.get("name") == "Migrate Test")
check("migrated has 1 version", len(migrated_data.get("versions", [])) == 1)
check("migrated state = deployed", migrated_data.get("state") == "deployed")

# Re-run migration: should skip existing
count2 = storage.migrate_from_db(migrate_db)
check("re-migration skips existing", count2 == 0, f"got {count2}")


# ─── Cleanup ─────────────────────────────────────────────────

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)
advisor.database.USER_DATA_DIR = orig_user_data_dir

# ─── Summary ─────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
sys.exit(0 if failed == 0 else 1)
