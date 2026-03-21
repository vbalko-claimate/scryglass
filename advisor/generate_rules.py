"""Mechanical rule generator — deck → CardDB → strategy rules without LLM.

Reads a decklist, analyzes card properties from CardDB, and generates
a deck_strategy.json with template rules + default weights.

Usage:
    uv run python -m advisor.generate_rules --deck DECK.txt --name "Deck Name"
    uv run python -m advisor.generate_rules --deck DECK.txt --name "Deck Name" --llm
    uv run python -m advisor.generate_rules --deck DECK.txt --name "Deck Name" --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from .database import card_cache
from .models import CardInfo

RULES_DIR = Path(__file__).parent.parent / "data" / "strategies"
USER_RULES_DIR = Path.home() / "MTG" / "mtg-data" / "strategies"


def _parse_decklist(path: Path) -> list[tuple[str, int]]:
    """Parse Arena-format decklist, return [(card_name, count)]."""
    cards = []
    for line in path.read_text().splitlines():
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


def _resolve_cards(cards: list[tuple[str, int]]) -> list[tuple[str, int, CardInfo]]:
    """Resolve card names to CardInfo objects."""
    card_cache.load()
    by_name: dict[str, CardInfo] = {}
    for c in card_cache._cache.values():
        by_name.setdefault(c.name, c)

    resolved = []
    for name, count in cards:
        info = by_name.get(name)
        if info:
            resolved.append((name, count, info))
    return resolved


def _card_text(card: CardInfo) -> str:
    return (" ".join(card.abilities) + " " + (card.oracle_text or "")).lower()


# ─── Analysis ────────────────────────────────────────────────────

def _analyze_deck(resolved: list[tuple[str, int, CardInfo]]) -> dict:
    """Analyze deck composition and return feature dict."""
    nonland = [(n, c, info) for n, c, info in resolved if "Land" not in info.card_types]
    lands = [(n, c, info) for n, c, info in resolved if "Land" in info.card_types]

    total_nonland = sum(c for _, c, _ in nonland)
    total_lands = sum(c for _, c, _ in lands)

    creatures = [(n, c, i) for n, c, i in nonland if "Creature" in i.card_types]
    instants = [(n, c, i) for n, c, i in nonland if "Instant" in i.card_types]
    sorceries = [(n, c, i) for n, c, i in nonland if "Sorcery" in i.card_types]
    enchantments = [(n, c, i) for n, c, i in nonland if "Enchantment" in i.card_types]

    creature_count = sum(c for _, c, _ in creatures)
    instant_count = sum(c for _, c, _ in instants)

    avg_cmc = sum(i.cmc * c for _, c, i in nonland) / max(1, total_nonland)

    # Keywords
    removal, flash, etb, lifelink, flying, deathtouch, vigilance = [], [], [], [], [], [], []
    for name, count, info in nonland:
        text = _card_text(info)
        if any(w in text for w in ("destroy target", "exile target", "deals", "fight")):
            removal.append((name, count, info))
        if "flash" in text:
            flash.append((name, count, info))
        if "enters" in text:
            etb.append((name, count, info))
        if "lifelink" in text:
            lifelink.append((name, count, info))
        if "flying" in text:
            flying.append((name, count, info))
        if "deathtouch" in text:
            deathtouch.append((name, count, info))
        if "vigilance" in text:
            vigilance.append((name, count, info))

    # Colors
    colors: set[str] = set()
    for _, _, info in nonland:
        colors.update(info.colors)

    # CMC distribution
    cmc_counts: dict[int, int] = {}
    for _, count, info in nonland:
        cmc_counts[info.cmc] = cmc_counts.get(info.cmc, 0) + count

    # Archetype detection
    if avg_cmc <= 2.3 and creature_count / max(1, total_nonland) >= 0.55:
        archetype = "aggro"
    elif instant_count > creature_count:
        archetype = "control"
    elif avg_cmc >= 3.5:
        archetype = "control"
    elif creature_count / max(1, total_nonland) >= 0.4:
        archetype = "midrange"
    else:
        archetype = "midrange"

    # Signature cards (highest impact nonland)
    signature = sorted(nonland, key=lambda x: (-x[1], -x[2].cmc))[:5]

    return {
        "archetype": archetype,
        "colors": sorted(colors),
        "avg_cmc": avg_cmc,
        "total_nonland": total_nonland,
        "total_lands": total_lands,
        "creature_count": creature_count,
        "instant_count": instant_count,
        "cmc_counts": cmc_counts,
        "signature": [n for n, _, _ in signature],
        "creatures": creatures,
        "instants": instants,
        "sorceries": sorceries,
        "enchantments": enchantments,
        "removal": removal,
        "flash": flash,
        "etb": etb,
        "lifelink": lifelink,
        "flying": flying,
        "deathtouch": deathtouch,
        "vigilance": vigilance,
        "nonland": nonland,
        "lands": lands,
    }


# ─── Rule Templates ─────────────────────────────────────────────

def _make_rule(id: str, layer: str, action: str, priority: str = "medium",
               weight: float = 1.0, tags: list[str] | None = None, **kwargs) -> dict:
    rule: dict = {
        "id": id,
        "layer": layer,
        "tags": tags or [],
        "action": action,
        "priority": priority,
        "weight": weight,
        "stats": {"fired": 0, "correct": 0},
        "conflicts_with": [],
    }
    rule.update(kwargs)
    return rule


def _generate_archetype_rules(analysis: dict) -> list[dict]:
    """Generate archetype-layer rules based on deck type."""
    arch = analysis["archetype"]
    rules = []

    if arch == "aggro":
        if analysis["cmc_counts"].get(1, 0) >= 4:
            rules.append(_make_rule(
                "archetype_one_drop_t1", "archetype",
                "Lead on a 1-drop — start pressure immediately",
                priority="high", weight=1.1, tags=["tempo"],
                phase=["Main"], step="Phase_Main1", my_turn=True,
                turn_max=2,
                require=[{"zone": "hand", "match": {"type": "Creature", "cmc_max": 1, "castable": True}, "min_count": 1}],
            ))
        rules.append(_make_rule(
            "archetype_attack_aggressively", "archetype",
            "Attack with everything safe — race the opponent",
            priority="medium", weight=1.0, tags=["aggro", "tempo"],
            phase=["Combat"], my_turn=True,
            my_creatures_min=1,
        ))
        rules.append(_make_rule(
            "archetype_spend_mana", "archetype",
            "Spend all mana — don't hold back in aggro",
            priority="medium", weight=0.9, tags=["tempo"],
            phase=["Main"], my_turn=True,
            require=[{"zone": "hand", "match": {"castable": True}, "min_count": 1}],
            general_overrides=["general_hold_instant"],
        ))

    elif arch == "control":
        rules.append(_make_rule(
            "archetype_hold_mana", "archetype",
            "Hold mana open — react on opponent's turn",
            priority="medium", weight=1.1, tags=["reactive"],
            phase=["Main"], my_turn=True, mana_min=2,
            require=[{"zone": "hand", "match": {"type": "Instant", "castable": True}, "min_count": 1}],
        ))
        rules.append(_make_rule(
            "archetype_dont_tap_out", "archetype",
            "Don't tap out — keep interaction available",
            priority="high", weight=1.0, tags=["reactive"],
            phase=["Main"], step="Phase_Main1", my_turn=True,
        ))

    elif arch == "midrange":
        rules.append(_make_rule(
            "archetype_develop_board", "archetype",
            "Develop board — play threats on curve",
            priority="medium", weight=1.0, tags=["tempo"],
            phase=["Main"], my_turn=True,
            require=[{"zone": "hand", "match": {"type": "Creature", "castable": True}, "min_count": 1}],
        ))
        rules.append(_make_rule(
            "archetype_trade_up", "archetype",
            "Trade favorably — 2-for-1 when possible",
            priority="medium", weight=1.0, tags=["value"],
        ))

    return rules


def _generate_removal_rules(analysis: dict) -> list[dict]:
    """Generate threat_response rules for removal spells.

    For each instant removal spell, generates a hold rule (save it) AND
    override rules that fire when it's clearly time to use it:
    - Opponent has a big creature and we're at low life
    - We topdeck removal with an empty/near-empty hand
    """
    rules = []
    hold_ids = []
    for name, count, info in analysis["removal"]:
        safe_id = re.sub(r"[^a-z0-9]", "_", name.lower()).strip("_")
        is_instant = "Instant" in info.card_types
        hold_id = f"threat_hold_{safe_id}"

        if is_instant and analysis["archetype"] != "aggro":
            rules.append(_make_rule(
                hold_id, "threat_response",
                f"Hold {name} for high-value targets — don't waste removal",
                priority="high", weight=1.1, tags=["reactive", "removal"],
                my_turn=True,
                require=[
                    {"zone": "hand", "match": {"name": name, "castable": True}, "min_count": 1},
                    {"zone": "opp_battlefield", "match": {"type": "Creature"}, "min_count": 1},
                ],
            ))
            hold_ids.append(hold_id)

            # Override: use removal when at low life vs big creature
            rules.append(_make_rule(
                f"threat_use_{safe_id}_low_life", "threat_response",
                f"Use {name} NOW — low life, must remove threat",
                priority="critical", weight=1.3, tags=["reactive", "removal", "survival"],
                my_turn=True,
                life_below=11,
                conflicts_with=[hold_id, "meta_save_removal_for_must_answer"],
                require=[
                    {"zone": "hand", "match": {"name": name, "castable": True}, "min_count": 1},
                    {"zone": "opp_battlefield", "match": {"type": "Creature", "power_min": 3}, "min_count": 1},
                ],
            ))

            # Override: use removal when topdecking (hand size 1 = just this card)
            rules.append(_make_rule(
                f"threat_use_{safe_id}_topdeck", "threat_response",
                f"Use {name} — topdecked removal, no reason to hold",
                priority="high", weight=1.2, tags=["reactive", "removal"],
                my_turn=True,
                hand_size_max=2,
                conflicts_with=[hold_id, "meta_save_removal_for_must_answer"],
                require=[
                    {"zone": "hand", "match": {"name": name, "castable": True}, "min_count": 1},
                    {"zone": "opp_battlefield", "match": {"type": "Creature", "power_min": 2}, "min_count": 1},
                ],
            ))
        else:
            rules.append(_make_rule(
                f"threat_use_{safe_id}", "threat_response",
                f"Use {name} on the biggest threat",
                priority="medium", weight=1.0, tags=["removal"],
                my_turn=True,
                require=[
                    {"zone": "hand", "match": {"name": name, "castable": True}, "min_count": 1},
                    {"zone": "opp_battlefield", "match": {"type": "Creature", "power_min": 3}, "min_count": 1},
                ],
            ))

    return rules


def _generate_synergy_rules(analysis: dict) -> list[dict]:
    """Generate card_synergy rules from keyword interactions."""
    rules = []

    # Lifelink + lifegain payoff
    lifelink_names = [n for n, _, _ in analysis["lifelink"]]
    etb_names = [n for n, _, i in analysis["etb"]
                 if any(w in _card_text(i) for w in ("gain", "life", "counter", "+1/+1"))]
    if lifelink_names and etb_names:
        rules.append(_make_rule(
            "synergy_lifelink_payoff", "card_synergy",
            f"Cast lifegain payoff before attacking with lifelink creatures",
            priority="high", weight=1.2, tags=["synergy", "sequence"],
            phase=["Main"], step="Phase_Main1", my_turn=True,
        ))

    # Flying creatures — prioritize when opponent has no flyers
    if len(analysis["flying"]) >= 2:
        fly_names = [n for n, _, _ in analysis["flying"] if (n, _, _) in analysis["creatures"] or any(
            n == nc for nc, _, _ in analysis["creatures"])]
        if fly_names:
            rules.append(_make_rule(
                "synergy_flying_pressure", "card_synergy",
                f"Deploy flyers — evasive damage wins races",
                priority="medium", weight=1.1, tags=["evasion", "tempo"],
                phase=["Main"], my_turn=True,
                require=[
                    {"zone": "opp_battlefield", "match": {"keyword": "flying|reach"}, "absent": True},
                ],
            ))

    # Flash creatures — hold for opponent's turn
    for name, count, info in analysis["flash"]:
        if "Creature" not in info.card_types:
            continue
        safe_id = re.sub(r"[^a-z0-9]", "_", name.lower()).strip("_")
        rules.append(_make_rule(
            f"synergy_hold_flash_{safe_id}", "card_synergy",
            f"Hold {name} — cast on opponent's turn for surprise blocker",
            priority="medium", weight=1.0, tags=["reactive", "flash"],
            my_turn=True,
            require=[{"zone": "hand", "match": {"name": name, "castable": True}, "min_count": 1}],
        ))

    # ETB creatures with synergy targets
    for name, count, info in analysis["etb"]:
        text = _card_text(info)
        if "Creature" not in info.card_types:
            continue
        safe_id = re.sub(r"[^a-z0-9]", "_", name.lower()).strip("_")
        if "+1/+1 counter" in text or "gets +" in text:
            rules.append(_make_rule(
                f"synergy_etb_{safe_id}_with_board", "card_synergy",
                f"Cast {name} when you have creatures to buff",
                priority="medium", weight=1.0, tags=["synergy"],
                phase=["Main"], my_turn=True,
                require=[
                    {"zone": "hand", "match": {"name": name, "castable": True}, "min_count": 1},
                    {"zone": "battlefield", "match": {"type": "Creature"}, "min_count": 1},
                ],
            ))

    return rules


def _generate_situation_rules(analysis: dict) -> list[dict]:
    """Generate situation-layer rules."""
    rules = []
    arch = analysis["archetype"]

    if arch == "aggro" and analysis["creature_count"] >= 10:
        rules.append(_make_rule(
            "situation_low_creatures_deploy", "situation",
            "Low board presence — deploy creatures over interaction",
            priority="high", weight=1.1, tags=["tempo"],
            phase=["Main"], my_turn=True,
            my_creatures_min=0,  # Will use max to check "few creatures"
            require=[
                {"zone": "battlefield", "match": {"type": "Creature"}, "max_count": 1},
                {"zone": "hand", "match": {"type": "Creature", "castable": True}, "min_count": 1},
            ],
        ))

    rules.append(_make_rule(
        "situation_flood_activate", "situation",
        "Flooding — use activated abilities or hold interaction",
        priority="medium", weight=0.9, tags=["defensive"],
        hand_lands_min=4, turn_min=4,
    ))

    return rules


def _generate_meta_rules(analysis: dict) -> list[dict]:
    """Generate meta_gameplan template rules."""
    rules = []
    arch = analysis["archetype"]

    rules.append(_make_rule(
        "meta_vs_fast_preserve_life", "meta_gameplan",
        "Vs fast decks — block aggressively, preserve life total",
        priority="high", weight=1.1, tags=["defensive"],
        phase=["Main"], step="Phase_Main1",
        opp_speed="fast",
    ))
    rules.append(_make_rule(
        "meta_vs_slow_push_damage", "meta_gameplan",
        "Vs slow decks — push damage before they stabilize",
        priority="high", weight=1.1, tags=["aggressive"],
        phase=["Main"], step="Phase_Main1",
        opp_speed="slow",
    ))

    if analysis["removal"]:
        rules.append(_make_rule(
            "meta_save_removal_for_must_answer", "meta_gameplan",
            "Save removal for must-answer threats",
            priority="high", weight=1.2, tags=["reactive"],
            opp_has_must_answer=True,
            require=[
                {"zone": "hand", "match": {"keyword": "destroy|exile", "castable": True}, "min_count": 1},
            ],
        ))

    return rules


def _validate_and_fix_rules(rules: list[dict]) -> list[dict]:
    """Post-process rules: auto-detect conflicts, add missing phase restrictions.

    1. Hold/use conflict detection: when rules reference the same card with
       opposite intent (hold vs cast/use), add conflicts_with so the engine
       can resolve them at runtime.
    2. Combat-only synergies: rules about dying/trading/blocking get phase
       restrictions if they don't already have them.
    """
    from collections import defaultdict

    _HOLD_WORDS = {"hold", "don't", "wait", "save"}
    _COMBAT_WORDS = {"die", "dying", "block", "trade", "combat damage", "attacks"}

    rule_by_id = {r["id"]: r for r in rules}

    # ── 1. Auto-detect hold/use conflicts ──
    # Index: card_name → [(rule_id, is_hold)]
    card_refs: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for r in rules:
        # Skip mulligan rules — they don't conflict with in-game hold/use
        if r.get("layer") == "mulligan" or (r.get("phase") and "Mulligan" in r.get("phase", [])):
            continue
        action_lower = r.get("action", "").lower()
        is_hold = any(w in action_lower for w in _HOLD_WORDS)
        for zc in r.get("require", []):
            # Only consider hand-zone requires (cards you're deciding to cast/hold)
            if zc.get("zone") != "hand":
                continue
            match = zc.get("match", {})
            names = match.get("name", [])
            if isinstance(names, str):
                names = [names]
            if not names:
                continue
            for name in names:
                card_refs[name].append((r["id"], is_hold))

    conflicts_added = 0
    for card, refs in card_refs.items():
        hold_ids = [rid for rid, is_h in refs if is_h]
        use_ids = [rid for rid, is_h in refs if not is_h]
        if not hold_ids or not use_ids:
            continue
        # Use rules suppress hold rules (one-directional).
        # When a "use" rule fires, it means conditions justify using the card NOW,
        # overriding the "hold" advice. The reverse (hold suppressing use) is
        # handled by the runtime auto-conflict detector which compares layer/priority.
        for uid in use_ids:
            existing = set(rule_by_id[uid].get("conflicts_with", []))
            for hid in hold_ids:
                if hid not in existing:
                    rule_by_id[uid].setdefault("conflicts_with", []).append(hid)
                    conflicts_added += 1

    # ── 2. Phase restrictions for combat-only synergies ──
    phase_added = 0
    for r in rules:
        if r.get("phase"):
            continue  # already has phase restriction
        action_lower = r.get("action", "").lower()
        # If the action text mentions combat-specific concepts, restrict to Combat
        if r.get("layer") == "card_synergy" and any(w in action_lower for w in _COMBAT_WORDS):
            r["phase"] = ["Combat"]
            phase_added += 1

    if conflicts_added or phase_added:
        import sys
        print(f"  Rule validation: added {conflicts_added} conflict links, "
              f"{phase_added} phase restrictions", file=sys.stderr)

    return rules


def _generate_vulnerability_list(analysis: dict) -> list[dict]:
    """Detect deck vulnerabilities."""
    vulns = []
    low_cmc_creatures = sum(c for _, c, i in analysis["creatures"] if i.cmc <= 2)

    if low_cmc_creatures >= 10:
        vulns.append({"card": "Temporary Lockdown", "reason": "Exiles most of your board", "severity": "critical"})
        vulns.append({"card": "Day of Judgment", "reason": "Full board wipe", "severity": "critical"})

    if analysis["creature_count"] >= 15:
        vulns.append({"card": "Slagstorm", "reason": "Mass damage to small creatures", "severity": "high"})

    if len(analysis["enchantments"]) >= 3:
        vulns.append({"card": "Back to Nature", "reason": "Destroys enchantment-heavy strategy", "severity": "high"})

    return vulns


# ─── Main ────────────────────────────────────────────────────────

def generate_strategy(deck_path: Path, deck_name: str) -> dict:
    """Generate complete strategy JSON for a deck."""
    cards = _parse_decklist(deck_path)
    resolved = _resolve_cards(cards)
    analysis = _analyze_deck(resolved)

    rules = []
    rules.extend(_generate_archetype_rules(analysis))
    rules.extend(_generate_removal_rules(analysis))
    rules.extend(_generate_synergy_rules(analysis))
    rules.extend(_generate_situation_rules(analysis))
    rules.extend(_generate_meta_rules(analysis))

    # Post-processing: fix conflicts, add missing overrides
    rules = _validate_and_fix_rules(rules)

    overrides = []
    if analysis["archetype"] == "aggro":
        overrides.extend(["general_hold_instant", "general_dont_overextend"])

    return {
        "name": deck_name,
        "deck_signature": analysis["signature"],
        "colors": analysis["colors"],
        "archetype": analysis["archetype"],
        "general_overrides": overrides,
        "vulnerabilities": _generate_vulnerability_list(analysis),
        "rules": rules,
        "stats": {"games": 0, "wins": 0, "losses": 0},
        "_generated": {
            "method": "mechanical",
            "avg_cmc": round(analysis["avg_cmc"], 2),
            "creature_count": analysis["creature_count"],
            "nonland_count": analysis["total_nonland"],
        },
    }


CONTRACT_PATH = RULES_DIR / "rule_contract.md"


def _build_enrich_prompt(deck_path: Path, deck_name: str, strategy: dict) -> str:
    existing_ids = [r["id"] for r in strategy["rules"]]
    existing_summary = "\n".join(
        f"  - [{r['layer']}] {r['id']}: {r['action']}" for r in strategy["rules"]
    )
    return "\n".join([
        "You are an expert MTG Arena competitive coach.",
        f"You are enriching the strategy for '{deck_name}' ({strategy['archetype']}).",
        "",
        f"Deck list: {deck_path}",
        f"Rule contract: {CONTRACT_PATH}",
        "",
        "The deck already has these mechanically-generated rules:",
        existing_summary,
        "",
        "Your job: ADD 8-15 NEW rules that the mechanical generator CANNOT create.",
        "Focus on:",
        "1. **Specific card sequencing** — which card to play before which and WHY",
        "2. **Non-obvious synergies** — interactions the mechanical analyzer misses",
        "3. **Matchup-specific tactics** — how to play vs aggro/control/combo specifically",
        "4. **Timing decisions** — when to hold vs deploy, when to trade vs race",
        "5. **Win condition protection** — how to play around specific removal/wipes",
        "",
        "IMPORTANT:",
        f"- Do NOT duplicate existing rule IDs: {existing_ids}",
        "- Follow the rule contract format EXACTLY",
        "- Use layers: card_synergy, threat_response, situation, meta_gameplan",
        "- Set weight to 1.0 for all new rules (GA will optimize later)",
        "- Keep action text under 80 characters",
        "",
        "Return ONLY a JSON array of new Rule objects. No explanation, no markdown.",
        "Example: [{\"id\": \"synergy_example\", \"layer\": \"card_synergy\", ...}, ...]",
    ])


async def _enrich_with_llm(
    deck_path: Path,
    deck_name: str,
    strategy: dict,
    backend: str = "claude_cli",
) -> list[dict]:
    """Call LLM to generate additional rules on top of mechanical base."""
    from .compile_rules import _call_llm

    prompt = _build_enrich_prompt(deck_path, deck_name, strategy)
    print(f"  Calling LLM to enrich {len(strategy['rules'])} mechanical rules...")

    response = await _call_llm(
        prompt, backend, max_turns=5, timeout=300.0,
        allow_web_search=False,
    )

    # Parse JSON array from response
    text = response.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    if text.startswith("json"):
        text = text[4:].strip()

    try:
        new_rules = json.loads(text)
        if not isinstance(new_rules, list):
            new_rules = [new_rules]
    except json.JSONDecodeError:
        # Try to find JSON array in response
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                new_rules = json.loads(match.group())
            except json.JSONDecodeError:
                print("  WARNING: Could not parse LLM response as JSON")
                return []
        else:
            print("  WARNING: No JSON array found in LLM response")
            return []

    # Validate and deduplicate
    existing_ids = {r["id"] for r in strategy["rules"]}
    valid_rules = []
    for rule in new_rules:
        if not isinstance(rule, dict):
            continue
        if "id" not in rule or "layer" not in rule or "action" not in rule:
            continue
        if rule["id"] in existing_ids:
            continue
        # Ensure defaults
        rule.setdefault("weight", 1.0)
        rule.setdefault("priority", "medium")
        rule.setdefault("tags", [])
        rule.setdefault("stats", {"fired": 0, "correct": 0})
        rule.setdefault("conflicts_with", [])
        valid_rules.append(rule)
        existing_ids.add(rule["id"])

    return valid_rules


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate strategy rules from deck + CardDB")
    parser.add_argument("--deck", required=True, type=Path, help="Path to Arena-format decklist")
    parser.add_argument("--name", required=True, help="Deck name")
    parser.add_argument("--output", type=Path, help="Output path (default: user strategies dir)")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of writing")
    parser.add_argument("--llm", action="store_true", help="Enrich with LLM-generated rules on top of mechanical base")
    parser.add_argument("--backend", default="claude_cli", choices=["claude_cli", "anthropic_api", "ollama"],
                        help="LLM backend for --llm")
    args = parser.parse_args()

    strategy = generate_strategy(args.deck, args.name)
    mechanical_count = len(strategy["rules"])
    print(f"Generated {mechanical_count} mechanical rules")

    if args.llm:
        llm_rules = asyncio.run(_enrich_with_llm(args.deck, args.name, strategy, args.backend))
        strategy["rules"].extend(llm_rules)
        strategy["_generated"]["llm_rules"] = len(llm_rules)
        print(f"  LLM added {len(llm_rules)} rules → total {len(strategy['rules'])}")

    if args.dry_run:
        print(json.dumps(strategy, indent=2))
        return

    if args.output:
        out_path = args.output
    else:
        safe_name = re.sub(r"[^a-z0-9]+", "_", args.name.lower()).strip("_")
        USER_RULES_DIR.mkdir(parents=True, exist_ok=True)
        out_path = USER_RULES_DIR / f"{safe_name}.json"

    out_path.write_text(json.dumps(strategy, indent=2) + "\n")
    print(f"Written {len(strategy['rules'])} rules → {out_path}")
    print(f"  Archetype: {strategy['archetype']}")
    print(f"  Mechanical: {mechanical_count}, LLM: {len(strategy['rules']) - mechanical_count}")
    print(f"  Vulnerabilities: {len(strategy['vulnerabilities'])}")


if __name__ == "__main__":
    main()
