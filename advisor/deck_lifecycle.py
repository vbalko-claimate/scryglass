"""Deck lifecycle management — create, version, generate rules, deploy.

Pure business logic, no HTTP concerns. Used by deck_routes.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

from .database import card_cache, get_connection, USER_DATA_DIR

log = logging.getLogger(__name__)

USER_RULES_DIR = USER_DATA_DIR / "strategies"
USER_DECKS_DIR = USER_DATA_DIR / "decks"


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


class DeckService:
    """Deck lifecycle operations. All methods are synchronous."""

    def __init__(self, user_id: str = "local"):
        self.user_id = user_id

    # ─── create_deck ─────────────────────────────────────────────

    def create_deck(self, name: str, deck_list: str) -> dict:
        """Create a new deck with v1."""
        deck_id = _slugify(name)
        cards = _parse_decklist_text(deck_list)
        colors = _detect_colors(cards)
        archetype = _detect_archetype(cards)
        dl_hash = _deck_list_hash(deck_list)
        card_count = sum(c for _, c in cards)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO decks (deck_id, user_id, name, description, colors, archetype) "
                "VALUES (?, ?, ?, '', ?, ?)",
                (deck_id, self.user_id, name, json.dumps(colors), archetype),
            )
            conn.execute(
                "INSERT INTO deck_versions "
                "(deck_id, version_number, deck_list, deck_list_hash, card_count, is_active) "
                "VALUES (?, 1, ?, ?, ?, 1)",
                (deck_id, deck_list, dl_hash, card_count),
            )
            conn.commit()
        finally:
            conn.close()

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
        conn = get_connection()
        try:
            cur = conn.execute("""
                SELECT d.deck_id, d.name, d.colors, d.archetype, d.created_at, d.updated_at,
                       v.version_number, v.card_count, v.rules_count, v.rules_source,
                       v.ga_status, v.ga_fitness, v.is_active
                FROM decks d
                LEFT JOIN deck_versions v ON d.deck_id = v.deck_id AND v.is_active = 1
                WHERE d.user_id = ?
                ORDER BY d.updated_at DESC
            """, (self.user_id,))
            rows = cur.fetchall()
        finally:
            conn.close()

        return [
            {
                "deck_id": r[0],
                "name": r[1],
                "colors": json.loads(r[2]) if r[2] else [],
                "archetype": r[3],
                "created_at": r[4],
                "updated_at": r[5],
                "active_version": r[6],
                "card_count": r[7] or 0,
                "rules_count": r[8] or 0,
                "rules_source": r[9] or "",
                "ga_status": r[10] or "not_started",
                "ga_fitness": r[11] or 0,
            }
            for r in rows
        ]

    # ─── get_deck ────────────────────────────────────────────────

    def get_deck(self, deck_id: str) -> dict | None:
        """Full deck detail: metadata + all versions + missing cards."""
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT deck_id, name, description, colors, archetype, created_at, updated_at "
                "FROM decks WHERE deck_id = ? AND user_id = ?",
                (deck_id, self.user_id),
            )
            row = cur.fetchone()
            if not row:
                return None

            deck = {
                "deck_id": row[0],
                "name": row[1],
                "description": row[2],
                "colors": json.loads(row[3]) if row[3] else [],
                "archetype": row[4],
                "created_at": row[5],
                "updated_at": row[6],
                "versions": [],
                "active_version": None,
                "missing_cards": [],
            }

            vcur = conn.execute("""
                SELECT version_id, version_number, deck_list, deck_list_hash,
                       card_count, change_summary, rules_path, rules_source,
                       rules_count, rules_validated, ga_status, ga_fitness,
                       ga_generations, is_active, created_at
                FROM deck_versions WHERE deck_id = ?
                ORDER BY version_number DESC
            """, (deck_id,))

            active_deck_list = None
            for vr in vcur.fetchall():
                version = {
                    "version_id": vr[0],
                    "version_number": vr[1],
                    "deck_list": vr[2],
                    "deck_list_hash": vr[3],
                    "card_count": vr[4],
                    "change_summary": vr[5],
                    "rules_path": vr[6],
                    "rules_source": vr[7],
                    "rules_count": vr[8],
                    "rules_validated": bool(vr[9]),
                    "ga_status": vr[10],
                    "ga_fitness": vr[11],
                    "ga_generations": vr[12],
                    "is_active": bool(vr[13]),
                    "created_at": vr[14],
                }
                deck["versions"].append(version)
                if version["is_active"]:
                    deck["active_version"] = version["version_number"]
                    active_deck_list = version["deck_list"]

        finally:
            conn.close()

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

    # ─── delete_deck ─────────────────────────────────────────────

    def delete_deck(self, deck_id: str) -> None:
        """Delete deck + all versions + associated strategy files."""
        conn = get_connection()
        try:
            # Get rules paths before deletion
            cur = conn.execute(
                "SELECT rules_path FROM deck_versions WHERE deck_id = ?",
                (deck_id,),
            )
            rules_paths = [r[0] for r in cur.fetchall() if r[0]]

            conn.execute("DELETE FROM deck_versions WHERE deck_id = ?", (deck_id,))
            conn.execute(
                "DELETE FROM decks WHERE deck_id = ? AND user_id = ?",
                (deck_id, self.user_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Clean up strategy files
        for rp in rules_paths:
            path = USER_RULES_DIR / rp
            if path.exists():
                path.unlink()
                log.info("Deleted strategy file: %s", path)

        # Clean up active strategy + deck file
        active_strategy = USER_RULES_DIR / f"{deck_id}.json"
        if active_strategy.exists():
            active_strategy.unlink()
        active_deck = USER_DECKS_DIR / f"{deck_id}.txt"
        if active_deck.exists():
            active_deck.unlink()

        log.info("Deleted deck '%s' and %d version(s)", deck_id, len(rules_paths))

    # ─── add_version ─────────────────────────────────────────────

    def add_version(self, deck_id: str, deck_list: str) -> dict:
        """Add a new version with strategy inheritance."""
        conn = get_connection()
        try:
            # Get previous version info
            cur = conn.execute("""
                SELECT version_number, deck_list, rules_path, rules_count
                FROM deck_versions WHERE deck_id = ?
                ORDER BY version_number DESC LIMIT 1
            """, (deck_id,))
            prev = cur.fetchone()
            if not prev:
                raise ValueError(f"Deck '{deck_id}' not found or has no versions")

            prev_version = prev[0]
            prev_deck_list = prev[1]
            prev_rules_path = prev[2]
            prev_rules_count = prev[3] or 0

            new_version = prev_version + 1
            dl_hash = _deck_list_hash(deck_list)
            cards = _parse_decklist_text(deck_list)
            card_count = sum(c for _, c in cards)
            colors = _detect_colors(cards)
            archetype = _detect_archetype(cards)

            # Compute diff
            prev_cards = _parse_decklist_text(prev_deck_list)
            change_summary = _compute_diff(prev_cards, cards)

            # Strategy inheritance: copy previous version's rules to new version
            new_rules_path = ""
            inherited_rules_count = 0
            if prev_rules_path:
                src = USER_RULES_DIR / prev_rules_path
                if src.exists():
                    new_rules_path = f"{deck_id}_v{new_version}.json"
                    dst = USER_RULES_DIR / new_rules_path
                    USER_RULES_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    inherited_rules_count = prev_rules_count
                    log.info("Inherited rules: %s -> %s (%d rules)",
                             src.name, dst.name, inherited_rules_count)

            conn.execute(
                "INSERT INTO deck_versions "
                "(deck_id, version_number, deck_list, deck_list_hash, card_count, "
                " change_summary, rules_path, rules_source, rules_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (deck_id, new_version, deck_list, dl_hash, card_count,
                 change_summary, new_rules_path,
                 "inherited" if new_rules_path else "",
                 inherited_rules_count),
            )
            # Update deck metadata
            conn.execute(
                "UPDATE decks SET colors = ?, archetype = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE deck_id = ?",
                (json.dumps(colors), archetype, deck_id),
            )
            conn.commit()
        finally:
            conn.close()

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
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT deck_list FROM deck_versions "
                "WHERE deck_id = ? AND version_number = ?",
                (deck_id, version),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Version {version} not found for deck '{deck_id}'")
            deck_list = row[0]

            # Get deck name
            ncur = conn.execute("SELECT name FROM decks WHERE deck_id = ?", (deck_id,))
            name_row = ncur.fetchone()
            deck_name = name_row[0] if name_row else deck_id
        finally:
            conn.close()

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
                    rules_source = "mechanical"

            # Validate
            rules_path = f"{deck_id}_v{version}.json"
            out_path = USER_RULES_DIR / rules_path
            USER_RULES_DIR.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(strategy, indent=2, ensure_ascii=False) + "\n")

            from .validate_strategy import validate_strategy as validate_fn
            issues = validate_fn(out_path, fix=True)

            # Reload to get fixed rule count
            fixed_data = json.loads(out_path.read_text())
            rules_count = len(fixed_data.get("rules", []))
            validated = 1 if not issues else 0

        finally:
            tmp_path.unlink(missing_ok=True)

        # Update version row
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE deck_versions SET rules_path = ?, rules_source = ?, "
                "rules_count = ?, rules_validated = ? "
                "WHERE deck_id = ? AND version_number = ?",
                (rules_path, rules_source, rules_count, validated, deck_id, version),
            )
            conn.commit()
        finally:
            conn.close()

        log.info("Generated %d %s rules for %s v%d", rules_count, rules_source, deck_id, version)

        return {
            "deck_id": deck_id,
            "version_number": version,
            "rules_count": rules_count,
            "rules_source": rules_source,
            "rules_validated": bool(validated),
            "validation_issues": issues,
        }

    # ─── deploy_version ──────────────────────────────────────────

    def deploy_version(self, deck_id: str, version: int) -> dict:
        """Deploy a version: copy rules to active location, write decklist."""
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT rules_path, deck_list FROM deck_versions "
                "WHERE deck_id = ? AND version_number = ?",
                (deck_id, version),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Version {version} not found for deck '{deck_id}'")

            rules_path = row[0]
            deck_list = row[1]

            # Copy rules to active strategy path
            if rules_path:
                src = USER_RULES_DIR / rules_path
                if src.exists():
                    dst = USER_RULES_DIR / f"{deck_id}.json"
                    shutil.copy2(src, dst)
                    log.info("Deployed rules: %s -> %s", src.name, dst.name)

            # Write deck_list to decks dir
            USER_DECKS_DIR.mkdir(parents=True, exist_ok=True)
            deck_file = USER_DECKS_DIR / f"{deck_id}.txt"
            deck_file.write_text(deck_list)

            # Set is_active=1 on this version, 0 on all others
            conn.execute(
                "UPDATE deck_versions SET is_active = 0 WHERE deck_id = ?",
                (deck_id,),
            )
            conn.execute(
                "UPDATE deck_versions SET is_active = 1 "
                "WHERE deck_id = ? AND version_number = ?",
                (deck_id, version),
            )
            conn.execute(
                "UPDATE decks SET updated_at = CURRENT_TIMESTAMP WHERE deck_id = ?",
                (deck_id,),
            )
            conn.commit()
        finally:
            conn.close()

        log.info("Deployed version %d of deck '%s'", version, deck_id)

        return {
            "deck_id": deck_id,
            "version_number": version,
            "deployed": True,
        }
