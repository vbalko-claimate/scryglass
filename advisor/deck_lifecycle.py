"""Deck lifecycle management — create, version, generate rules, deploy.

Pure business logic, no HTTP concerns. Used by deck_routes.py.
Storage: filesystem-based (one directory per deck), no SQLite.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

from .database import card_cache
from . import deck_storage as storage

log = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Generate URL-safe slug from deck name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s_-]", "", slug)
    slug = re.sub(r"[\s-]+", "_", slug)
    slug = slug.strip("_")
    return slug or "deck"


def _deck_list_hash(deck_list: str) -> str:
    """Compute SHA-256 of normalized (sorted, stripped) deck list."""
    lines = sorted(line.strip() for line in deck_list.splitlines() if line.strip())
    normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _parse_decklist_text(deck_list: str) -> list[tuple[str, int]]:
    """Parse Arena-format decklist text, return [(card_name, count)]."""
    cards = []
    for line in deck_list.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        parts = line.split(None, 1)
        if not parts[0].isdigit() or len(parts) < 2:
            continue
        count = int(parts[0])
        name = re.sub(r"\s*\([A-Z0-9]+\)\s*\d*$", "", parts[1]).strip()
        cards.append((name, count))
    return cards


def _detect_colors(cards: list[tuple[str, int]]) -> list[str]:
    """Detect deck colors from card names using card_cache."""
    if not card_cache._loaded:
        card_cache.load()
    by_name: dict[str, object] = {}
    for c in card_cache._cache.values():
        by_name.setdefault(c.name, c)

    colors: set[str] = set()
    for name, _ in cards:
        info = by_name.get(name)
        if info and hasattr(info, "colors"):
            colors.update(info.colors)
    return sorted(colors)


def _detect_archetype(cards: list[tuple[str, int]]) -> str:
    """Simple archetype detection from card properties."""
    if not card_cache._loaded:
        card_cache.load()
    by_name = {}
    for c in card_cache._cache.values():
        by_name.setdefault(c.name, c)

    creature_count = 0
    instant_count = 0
    total_nonland = 0
    total_cmc = 0

    for name, count in cards:
        info = by_name.get(name)
        if not info:
            continue
        types = info.card_types if hasattr(info, "card_types") else []
        if "Land" in types:
            continue
        total_nonland += count
        total_cmc += info.cmc * count
        if "Creature" in types:
            creature_count += count
        if "Instant" in types:
            instant_count += count

    if total_nonland == 0:
        return "unknown"
    avg_cmc = total_cmc / total_nonland
    creature_ratio = creature_count / total_nonland

    if avg_cmc <= 2.3 and creature_ratio >= 0.55:
        return "aggro"
    elif instant_count > creature_count or avg_cmc >= 3.5:
        return "control"
    elif creature_ratio >= 0.4:
        return "midrange"
    return "midrange"


def _compute_diff(old_cards: list[tuple[str, int]], new_cards: list[tuple[str, int]]) -> str:
    """Compute change summary between two card lists."""
    old_map = {name: count for name, count in old_cards}
    new_map = {name: count for name, count in new_cards}
    all_names = sorted(set(old_map) | set(new_map))

    changes = []
    for name in all_names:
        old_c = old_map.get(name, 0)
        new_c = new_map.get(name, 0)
        diff = new_c - old_c
        if diff > 0:
            changes.append(f"+{diff} {name}")
        elif diff < 0:
            changes.append(f"{diff} {name}")

    return ", ".join(changes[:10])  # cap at 10 changes for readability


def _get_collection() -> dict[str, int]:
    """Load collection snapshot as {card_name: count}."""
    raw_path = Path(__file__).parent.parent / "mtga_collection_raw.json"
    if not raw_path.exists():
        return {}
    try:
        data = json.loads(raw_path.read_text())
        if not card_cache._loaded:
            card_cache.load()
        result: dict[str, int] = {}
        for grp_id_str, count in data.items():
            card = card_cache.get(int(grp_id_str))
            if card:
                result[card.name] = result.get(card.name, 0) + count
        return result
    except Exception:
        return {}


def _find_version(data: dict, version: int) -> tuple[int, dict]:
    """Find version by number in deck data. Returns (index, version_dict).
    Raises ValueError if not found."""
    for i, v in enumerate(data["versions"]):
        if v["version"] == version:
            return i, v
    raise ValueError(f"Version {version} not found for deck '{data.get('name', '?')}'")


class DeckService:
    """Deck lifecycle operations. Filesystem-based, no SQLite."""

    def __init__(self, user_id: str = "local"):
        self.user_id = user_id

    # ─── create_deck ─────────────────────────────────────────────

    def create_deck(self, name: str, deck_list: str) -> dict:
        """Create a new deck with v1."""
        deck_id = _slugify(name)

        # Check for duplicate
        existing = storage.read_deck(deck_id)
        if existing is not None:
            raise ValueError(f"Deck '{deck_id}' already exists")

        cards = _parse_decklist_text(deck_list)
        colors = _detect_colors(cards)
        archetype = _detect_archetype(cards)
        card_count = sum(c for _, c in cards)

        ts = storage.now_iso()
        deck_data = {
            "name": name,
            "format": "standard",
            "state": "draft",
            "current_version": 1,
            "colors": colors,
            "archetype": archetype,
            "created": ts,
            "updated": ts,
            "versions": [
                {
                    "version": 1,
                    "card_count": card_count,
                    "cards": deck_list,
                    "deck_list_hash": _deck_list_hash(deck_list),
                    "rules_source": "",
                    "rules_count": 0,
                    "rules_validated": False,
                    "ga_status": "not_started",
                    "ga_fitness": 0,
                    "is_active": True,
                    "change_summary": "",
                    "created": ts,
                },
            ],
        }

        storage.write_deck(deck_id, deck_data)

        log.info("Created deck '%s' (%s) — %d cards, %s %s",
                 name, deck_id, card_count, "/".join(colors), archetype)

        return {
            "deck_id": deck_id,
            "name": name,
            "colors": colors,
            "archetype": archetype,
            "version_number": 1,
            "card_count": card_count,
        }

    # ─── list_decks ──────────────────────────────────────────────

    def list_decks(self) -> list[dict]:
        """List all decks with active version summary."""
        result = []
        for deck_id in storage.list_deck_ids():
            data = storage.read_deck(deck_id)
            if not data:
                continue

            # Find active version
            active_v = None
            for v in data.get("versions", []):
                if v.get("is_active"):
                    active_v = v
                    break

            result.append({
                "deck_id": deck_id,
                "name": data.get("name", deck_id),
                "colors": data.get("colors", []),
                "archetype": data.get("archetype", "unknown"),
                "created_at": data.get("created", ""),
                "updated_at": data.get("updated", ""),
                "active_version": active_v["version"] if active_v else None,
                "card_count": active_v.get("card_count", 0) if active_v else 0,
                "rules_count": active_v.get("rules_count", 0) if active_v else 0,
                "rules_source": active_v.get("rules_source", "") if active_v else "",
                "ga_status": active_v.get("ga_status", "not_started") if active_v else "not_started",
                "ga_fitness": active_v.get("ga_fitness", 0) if active_v else 0,
            })

        return result

    # ─── get_deck ────────────────────────────────────────────────

    def get_deck(self, deck_id: str) -> dict | None:
        """Full deck detail: metadata + all versions + missing cards."""
        data = storage.read_deck(deck_id)
        if not data:
            return None

        active_version = None
        active_deck_list = None
        versions_out = []

        for v in data.get("versions", []):
            version = {
                "version_number": v["version"],
                "card_count": v.get("card_count", 0),
                "deck_list": v.get("cards", ""),
                "deck_list_hash": v.get("deck_list_hash", ""),
                "change_summary": v.get("change_summary", ""),
                "rules_source": v.get("rules_source", ""),
                "rules_count": v.get("rules_count", 0),
                "rules_validated": v.get("rules_validated", False),
                "ga_status": v.get("ga_status", "not_started"),
                "ga_fitness": v.get("ga_fitness", 0),
                "ga_generations": v.get("ga_generations", 0),
                "is_active": v.get("is_active", False),
                "created_at": v.get("created", ""),
            }
            versions_out.append(version)
            if version["is_active"]:
                active_version = version["version_number"]
                active_deck_list = version["deck_list"]

        # Sort versions descending
        versions_out.sort(key=lambda x: x["version_number"], reverse=True)

        deck = {
            "deck_id": deck_id,
            "name": data.get("name", deck_id),
            "description": data.get("description", ""),
            "colors": data.get("colors", []),
            "archetype": data.get("archetype", "unknown"),
            "created_at": data.get("created", ""),
            "updated_at": data.get("updated", ""),
            "versions": versions_out,
            "active_version": active_version,
            "missing_cards": [],
        }

        # Collection check for active version
        if active_deck_list:
            collection = _get_collection()
            if collection:
                cards = _parse_decklist_text(active_deck_list)
                missing = []
                for name, count in cards:
                    owned = collection.get(name, 0)
                    if owned < count:
                        missing.append({
                            "name": name,
                            "needed": count,
                            "owned": owned,
                            "missing": count - owned,
                        })
                deck["missing_cards"] = missing

        return deck

    # ─── update_decklist ───────────────────────────────────────────

    def update_decklist(self, deck_id: str, version: int, deck_list: str) -> dict:
        """Update decklist on an existing version (e.g. after promoting a stub)."""
        data = storage.read_deck(deck_id)
        if not data:
            raise ValueError(f"Deck '{deck_id}' not found")

        v_idx, v_data = _find_version(data, version)

        cards = _parse_decklist_text(deck_list)
        card_count = sum(c for _, c in cards)
        colors = _detect_colors(cards)
        archetype = _detect_archetype(cards)

        data["versions"][v_idx]["cards"] = deck_list
        data["versions"][v_idx]["card_count"] = card_count
        data["versions"][v_idx]["deck_list_hash"] = _deck_list_hash(deck_list)
        data["colors"] = colors
        data["archetype"] = archetype
        data["updated"] = storage.now_iso()

        storage.write_deck(deck_id, data)

        log.info("Updated decklist for %s v%d: %d cards", deck_id, version, card_count)

        return {
            "deck_id": deck_id,
            "version_number": version,
            "card_count": card_count,
            "colors": colors,
            "archetype": archetype,
        }

    # ─── delete_deck ─────────────────────────────────────────────

    def delete_deck(self, deck_id: str) -> None:
        """Delete deck directory and all associated files."""
        storage.delete_deck_dir(deck_id)
        log.info("Deleted deck '%s'", deck_id)

    # ─── add_version ─────────────────────────────────────────────

    def add_version(self, deck_id: str, deck_list: str) -> dict:
        """Add a new version with strategy inheritance."""
        data = storage.read_deck(deck_id)
        if not data or not data.get("versions"):
            raise ValueError(f"Deck '{deck_id}' not found or has no versions")

        versions = data["versions"]
        prev = max(versions, key=lambda v: v["version"])
        prev_version = prev["version"]
        new_version = prev_version + 1

        cards = _parse_decklist_text(deck_list)
        card_count = sum(c for _, c in cards)
        colors = _detect_colors(cards)
        archetype = _detect_archetype(cards)

        # Compute diff
        prev_cards = _parse_decklist_text(prev.get("cards", ""))
        change_summary = _compute_diff(prev_cards, cards)

        # Strategy inheritance: copy previous version's strategy
        inherited_rules_count = 0
        prev_strategy = storage.read_version_strategy(deck_id, prev_version)
        if prev_strategy:
            storage.write_version_strategy(deck_id, new_version, prev_strategy)
            inherited_rules_count = len(prev_strategy.get("rules", []))
            log.info("Inherited %d rules from v%d to v%d", inherited_rules_count, prev_version, new_version)

        # Add new version to deck data
        new_v = {
            "version": new_version,
            "card_count": card_count,
            "cards": deck_list,
            "deck_list_hash": _deck_list_hash(deck_list),
            "rules_source": "inherited" if prev_strategy else "",
            "rules_count": inherited_rules_count,
            "rules_validated": False,
            "ga_status": "not_started",
            "ga_fitness": 0,
            "is_active": False,
            "change_summary": change_summary,
            "created": storage.now_iso(),
        }
        data["versions"].append(new_v)
        data["current_version"] = new_version
        data["colors"] = colors
        data["archetype"] = archetype
        data["updated"] = storage.now_iso()

        storage.write_deck(deck_id, data)

        log.info("Added version %d to deck '%s': %s", new_version, deck_id, change_summary)

        return {
            "deck_id": deck_id,
            "version_number": new_version,
            "card_count": card_count,
            "change_summary": change_summary,
            "inherited_rules_count": inherited_rules_count,
        }

    # ─── generate_rules ─────────────────────────────────────────

    def generate_rules(self, deck_id: str, version: int, mode: str = "mechanical") -> dict:
        """Generate rules for a specific version."""
        data = storage.read_deck(deck_id)
        if not data:
            raise ValueError(f"Deck '{deck_id}' not found")

        v_idx, v_data = _find_version(data, version)

        deck_list = v_data.get("cards", "")
        deck_name = data.get("name", deck_id)

        # Write deck_list to temp file for generate_strategy()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(deck_list)
            tmp_path = Path(f.name)

        try:
            from .generate_rules import generate_strategy, _enrich_with_llm
            import asyncio

            strategy = generate_strategy(tmp_path, deck_name)
            rules_source = "mechanical"

            if mode == "mechanical+llm":
                try:
                    llm_rules = asyncio.run(_enrich_with_llm(tmp_path, deck_name, strategy))
                    strategy["rules"].extend(llm_rules)
                    strategy["_generated"]["llm_rules"] = len(llm_rules)
                    rules_source = "mechanical+llm"
                except Exception as e:
                    log.warning("LLM enrichment failed: %s", e)

            # Write strategy file
            out_path = storage.write_version_strategy(deck_id, version, strategy)

            # Validate
            from .validate_strategy import validate_strategy as validate_fn
            issues = validate_fn(out_path, fix=True)

            # Reload to get fixed rule count
            fixed_data = json.loads(out_path.read_text())
            rules_count = len(fixed_data.get("rules", []))
            validated = not issues

        finally:
            tmp_path.unlink(missing_ok=True)

        # Update version metadata in deck.json
        data["versions"][v_idx]["rules_source"] = rules_source
        data["versions"][v_idx]["rules_count"] = rules_count
        data["versions"][v_idx]["rules_validated"] = validated
        data["state"] = "validated" if validated else "has_rules"
        data["updated"] = storage.now_iso()
        storage.write_deck(deck_id, data)

        log.info("Generated %d %s rules for %s v%d", rules_count, rules_source, deck_id, version)

        return {
            "deck_id": deck_id,
            "version_number": version,
            "rules_count": rules_count,
            "rules_source": rules_source,
            "rules_validated": validated,
            "validation_issues": issues,
        }

    # ─── deploy_version ──────────────────────────────────────────

    def deploy_version(self, deck_id: str, version: int) -> dict:
        """Deploy a version: copy rules to active location."""
        data = storage.read_deck(deck_id)
        if not data:
            raise ValueError(f"Deck '{deck_id}' not found")

        _find_version(data, version)

        # Deploy strategy files
        deployed = storage.deploy_strategy(deck_id, version)

        # Update is_active flags
        for v in data["versions"]:
            v["is_active"] = (v["version"] == version)

        data["state"] = "deployed"
        data["current_version"] = version
        data["updated"] = storage.now_iso()
        storage.write_deck(deck_id, data)

        log.info("Deployed version %d of deck '%s' (strategy=%s)", version, deck_id, deployed)

        return {
            "deck_id": deck_id,
            "version_number": version,
            "deployed": True,
        }

    # ─── undeploy_version ────────────────────────────────────────

    def undeploy_version(self, deck_id: str) -> dict:
        """Remove deployed strategy, set state back to has_rules."""
        data = storage.read_deck(deck_id)
        if not data:
            raise ValueError(f"Deck '{deck_id}' not found")

        storage.undeploy_strategy(deck_id)

        for v in data["versions"]:
            v["is_active"] = False

        # Find best state based on whether any version has rules
        has_rules = any(v.get("rules_count", 0) > 0 for v in data["versions"])
        data["state"] = "has_rules" if has_rules else "draft"
        data["updated"] = storage.now_iso()
        storage.write_deck(deck_id, data)

        log.info("Undeployed deck '%s'", deck_id)

        return {"deck_id": deck_id, "undeployed": True}

    # ─── promote_stub ────────────────────────────────────────────

    def promote_stub(self, deck_id: str) -> dict:
        """Promote a stub (strategy-only) to a managed deck.

        Creates deck.json from strategy metadata. Decklist is empty
        until user imports it.
        """
        # Must have strategy.json but no deck.json
        if storage.read_deck(deck_id) is not None:
            raise ValueError(f"Deck '{deck_id}' already has deck.json")

        strategy = storage.read_strategy(deck_id)
        if not strategy:
            raise ValueError(f"No strategy.json found for '{deck_id}'")

        rules_count = len(strategy.get("rules", []))
        ts = storage.now_iso()

        deck_data = {
            "name": strategy.get("name", deck_id),
            "format": "standard",
            "state": "has_rules",
            "current_version": 1,
            "colors": strategy.get("colors", []),
            "archetype": strategy.get("archetype", "unknown"),
            "created": ts,
            "updated": ts,
            "versions": [
                {
                    "version": 1,
                    "card_count": 0,
                    "cards": "",
                    "deck_list_hash": "",
                    "rules_source": strategy.get("_source", "imported"),
                    "rules_count": rules_count,
                    "rules_validated": False,
                    "ga_status": "not_started",
                    "ga_fitness": 0,
                    "is_active": True,
                    "change_summary": "",
                    "created": ts,
                },
            ],
        }

        # Copy strategy.json to versions/v1
        storage.write_version_strategy(deck_id, 1, strategy)
        storage.write_deck(deck_id, deck_data)

        log.info("Promoted stub '%s' to managed deck (%d rules)", deck_id, rules_count)

        return {
            "deck_id": deck_id,
            "name": deck_data["name"],
            "rules_count": rules_count,
            "promoted": True,
        }
