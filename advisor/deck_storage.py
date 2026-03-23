"""Filesystem-based deck storage.

Each deck = one directory under USER_DATA_DIR/decks/{deck_id}/:
    deck.json           — metadata + decklist + state
    strategy.json       — deployed rules (copy of active version)
    versions/
        v1_strategy.json
        v2_strategy.json
        ...

No SQLite. The filesystem IS the database. ls decks/ = list of decks.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .database import USER_DATA_DIR

log = logging.getLogger(__name__)

DECKS_ROOT = USER_DATA_DIR / "decks"

# Legacy flat strategies dir (still used by strategy.py for matching)
LEGACY_STRATEGIES_DIR = USER_DATA_DIR / "strategies"

_SENTINEL = ".migrated"


def _deck_dir(deck_id: str) -> Path:
    return DECKS_ROOT / deck_id


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict | None:
    """Read and parse a JSON file. Returns None if missing or invalid."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        if not isinstance(e, FileNotFoundError):
            log.error("Failed to read %s: %s", path, e)
        return None


def _write_json(path: Path, data: dict) -> None:
    """Write a dict as JSON to a file."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ─── Read ──────────────────────────────────────────────────────


def list_deck_ids() -> list[str]:
    """Return all deck_ids (directory names) sorted by mtime desc."""
    try:
        dirs = [d for d in DECKS_ROOT.iterdir() if d.is_dir() and (d / "deck.json").exists()]
    except FileNotFoundError:
        return []
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [d.name for d in dirs]


def read_deck(deck_id: str) -> dict | None:
    """Read deck.json, return parsed dict or None if not found."""
    return _read_json(_deck_dir(deck_id) / "deck.json")


def read_strategy(deck_id: str) -> dict | None:
    """Read deployed strategy.json for a deck."""
    return _read_json(_deck_dir(deck_id) / "strategy.json")


def read_version_strategy(deck_id: str, version: int) -> dict | None:
    """Read a specific version's strategy file."""
    return _read_json(_deck_dir(deck_id) / "versions" / f"v{version}_strategy.json")


def version_strategy_path(deck_id: str, version: int) -> Path:
    """Return path to a version's strategy file (may not exist yet)."""
    return _deck_dir(deck_id) / "versions" / f"v{version}_strategy.json"


# ─── Write ─────────────────────────────────────────────────────


def write_deck(deck_id: str, data: dict) -> None:
    """Write deck.json. Creates deck directory if needed."""
    d = _deck_dir(deck_id)
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "deck.json", data)


def write_version_strategy(deck_id: str, version: int, data: dict) -> Path:
    """Write strategy JSON for a specific version. Returns path."""
    d = _deck_dir(deck_id) / "versions"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"v{version}_strategy.json"
    _write_json(path, data)
    return path


def deploy_strategy(deck_id: str, version: int) -> bool:
    """Copy version strategy to deck's strategy.json + legacy strategies dir.

    Returns True if strategy file was found and deployed.
    """
    src = version_strategy_path(deck_id, version)
    try:
        # Copy to deck dir as strategy.json
        dst = _deck_dir(deck_id) / "strategy.json"
        shutil.copy2(src, dst)

        # Also copy to legacy flat dir so strategy.py can find it
        LEGACY_STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        legacy_dst = LEGACY_STRATEGIES_DIR / f"{deck_id}.json"
        shutil.copy2(src, legacy_dst)
    except FileNotFoundError:
        return False

    log.info("Deployed strategy: %s v%d → %s + %s", deck_id, version, dst, legacy_dst)
    return True


def undeploy_strategy(deck_id: str) -> None:
    """Remove deployed strategy files."""
    for path in [
        _deck_dir(deck_id) / "strategy.json",
        LEGACY_STRATEGIES_DIR / f"{deck_id}.json",
    ]:
        path.unlink(missing_ok=True)


def delete_deck_dir(deck_id: str) -> bool:
    """Delete entire deck directory. Returns True if it existed."""
    d = _deck_dir(deck_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    (LEGACY_STRATEGIES_DIR / f"{deck_id}.json").unlink(missing_ok=True)
    log.info("Deleted deck directory: %s", d)
    return True


# ─── Migration ─────────────────────────────────────────────────


def migrate_from_db(db_path: Path) -> int:
    """Migrate decks from SQLite (old format) to filesystem.

    Reads decks + deck_versions tables, creates directory structure.
    Returns number of decks migrated. Skips if already done (sentinel file).
    """
    sentinel = DECKS_ROOT / _SENTINEL
    if sentinel.exists():
        return 0

    import sqlite3

    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        # Check if tables exist
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "decks" not in tables or "deck_versions" not in tables:
            return 0

        decks = conn.execute(
            "SELECT deck_id, name, colors, archetype, created_at, updated_at FROM decks"
        ).fetchall()

        migrated = 0
        for deck_id, name, colors_json, archetype, created_at, updated_at in decks:
            # Skip if already migrated
            if (_deck_dir(deck_id) / "deck.json").exists():
                continue

            # Get versions
            versions = conn.execute(
                "SELECT version_number, deck_list, rules_path, rules_source, "
                "rules_count, rules_validated, ga_status, ga_fitness, is_active, created_at "
                "FROM deck_versions WHERE deck_id = ? ORDER BY version_number",
                (deck_id,),
            ).fetchall()

            if not versions:
                continue

            # Build deck.json from latest active (or last) version
            active_version = None
            for v in versions:
                if v[8]:  # is_active
                    active_version = v
            if not active_version:
                active_version = versions[-1]

            colors = json.loads(colors_json) if colors_json else []

            # Determine state
            state = "draft"
            if active_version[8]:  # is_active
                state = "deployed"
            elif active_version[3]:  # rules_source
                state = "has_rules"

            ts = now_iso()
            deck_data = {
                "name": name,
                "format": "standard",
                "state": state,
                "current_version": active_version[0],  # version_number
                "colors": colors,
                "archetype": archetype or "unknown",
                "created": created_at or ts,
                "updated": updated_at or ts,
                "versions": [],
            }

            # Add version metadata
            for vnum, dl, rpath, rsource, rcount, rvalid, ga_st, ga_fit, is_act, v_created in versions:
                v_meta = {
                    "version": vnum,
                    "card_count": sum(1 for _ in dl.splitlines() if _.strip()) if dl else 0,
                    "cards": dl,
                    "rules_source": rsource or "",
                    "rules_count": rcount or 0,
                    "rules_validated": bool(rvalid),
                    "ga_status": ga_st or "not_started",
                    "ga_fitness": ga_fit or 0,
                    "is_active": bool(is_act),
                    "created": v_created or ts,
                }
                deck_data["versions"].append(v_meta)

                # Copy strategy file if exists
                if rpath:
                    old_path = LEGACY_STRATEGIES_DIR / rpath
                    strategy_data = _read_json(old_path)
                    if strategy_data:
                        write_version_strategy(deck_id, vnum, strategy_data)

            write_deck(deck_id, deck_data)

            # Deploy if was active
            if state == "deployed":
                deploy_strategy(deck_id, active_version[0])

            migrated += 1
            log.info("Migrated deck: %s (%d versions)", deck_id, len(versions))

        # Write sentinel after successful migration
        if migrated > 0:
            DECKS_ROOT.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(now_iso())

        return migrated
    finally:
        conn.close()
