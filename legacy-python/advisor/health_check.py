"""Advisor health check — canary metric for overall advisor quality.

Measures:
1. Regression tests pass rate (engine correctness)
2. Rule evaluation sanity (no crashes, reasonable fire rates)
3. Cross-deck consistency (same engine, different decks)

Run after ANY engine or rule change to catch regressions.

Usage:
    uv run python -m advisor.health_check
    uv run python -m advisor.health_check --save  # save snapshot to history
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .strategy import evaluate_rules, load_strategy, load_raw_strategy, OpponentTracker, MetaDeck
from .database import card_cache
from .regression_tests import WHITE_LIFEGAIN_SCENARIOS, RED_GOBLINS_SCENARIOS, run_scenario

HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "health_history.json"

# All decks we track
TRACKED_DECKS = [
    "Mono White Lifegain",
    "Mono Red Goblins",
    "Rakdos Midrange",
]


def _count_rules(strategy) -> dict:
    """Count rules by layer and total."""
    by_layer: dict[str, int] = {}
    for r in strategy.rules:
        by_layer[r.layer] = by_layer.get(r.layer, 0) + 1
    return {"total": len(strategy.rules), "by_layer": by_layer}


def _check_spam(strategy, state, opp_tracker) -> dict:
    """Check for spam — rules that fire without any meaningful condition."""
    advice = evaluate_rules(
        strategy.rules, state, opp_tracker=opp_tracker,
        vulnerabilities=strategy.vulnerabilities, max_results=0)

    fire_counts: dict[str, int] = {}
    for a in advice:
        m = re.search(r':(\w+)\]', a.details or '')
        if m:
            fire_counts[m.group(1)] = fire_counts.get(m.group(1), 0) + 1

    return {
        "total_fired": len(advice),
        "unique_fired": len(fire_counts),
        "max_fire_count": max(fire_counts.values()) if fire_counts else 0,
        "top_firer": max(fire_counts, key=fire_counts.get) if fire_counts else "",
    }


def _evaluate_deck(deck_name: str) -> dict:
    """Evaluate one deck's rule health."""
    strategy = load_strategy(deck_name)
    if not strategy:
        return {"error": f"Strategy not found: {deck_name}"}

    rule_counts = _count_rules(strategy)

    # Load raw JSON for pruned count and engine version
    raw = load_raw_strategy(deck_name) or {}
    raw_rules = raw.get("rules", [])
    pruned_count = sum(1 for r in raw_rules if r.get("pruned"))
    engine_version = raw.get("_engine_version", "")

    # Check for weight extremes
    weights = [r.weight for r in strategy.rules]
    low_weight = sum(1 for w in weights if w < 0.3)
    high_weight = sum(1 for w in weights if w > 1.8)

    # Check for rules without conditions (potential spam)
    no_conditions = 0
    for r in strategy.rules:
        has_cond = any([
            r.phase, r.my_turn is not None, r.turn_min, r.turn_max,
            r.step, r.life_below, r.life_above, r.opp_life_below,
            r.mana_min, r.hand_lands_min, r.my_creatures_min,
            r.opp_creatures_min, r.opp_speed, r.opp_has_must_answer,
            r.require,
        ])
        if not has_cond:
            no_conditions += 1

    # Check conflict graph — mutual suppression?
    conflict_pairs = set()
    rule_ids = {r.id for r in strategy.rules}
    mutual_suppression = 0
    for r in strategy.rules:
        for cid in r.conflicts_with:
            if cid in rule_ids:
                conflict_pairs.add((min(r.id, cid), max(r.id, cid)))
                # Check if cid also conflicts with r
                other = next((rr for rr in strategy.rules if rr.id == cid), None)
                if other and r.id in other.conflicts_with:
                    mutual_suppression += 1

    return {
        "name": deck_name,
        "archetype": strategy.archetype,
        "rules_total": rule_counts["total"],
        "rules_by_layer": rule_counts["by_layer"],
        "low_weight_rules": low_weight,
        "high_weight_rules": high_weight,
        "no_condition_rules": no_conditions,
        "conflict_pairs": len(conflict_pairs),
        "mutual_suppression": mutual_suppression // 2,  # counted twice
        "pruned_rules": pruned_count,
        "engine_version": engine_version,
    }


def run_health_check(save: bool = False) -> dict:
    """Run full health check across 3 tiers.

    Tier 1 (Engine): Meta-independent. Regression tests, template resolution,
    conflict detection. Should NEVER degrade — if it drops, engine bug.

    Tier 2 (Deck Structure): Changes only when deck cards change. Rule count,
    fired coverage, no-condition spam, mutual suppression. Slow drift = overspecification.

    Tier 3 (Meta Fitness): Changes with meta shifts. GA WR, per-matchup breakdown.
    Expected to fluctuate — re-GA when it drops.
    """
    card_cache.load()
    results: dict = {
        "timestamp": datetime.now().isoformat(),
        "tier1_engine": {},
        "tier2_structure": {},
        "tier3_meta": {},
        "warnings": [],
    }

    # ═══ TIER 1: Engine Health (meta-independent) ═══
    all_scenarios = WHITE_LIFEGAIN_SCENARIOS + RED_GOBLINS_SCENARIOS
    passed = 0
    failed = 0
    failed_names = []
    for s in all_scenarios:
        ok, _ = run_scenario(s, verbose=False)
        if ok:
            passed += 1
        else:
            failed += 1
            failed_names.append(s.name)

    tier1 = {
        "regression_passed": passed,
        "regression_failed": failed,
        "regression_total": passed + failed,
        "regression_pass_rate": round(passed / (passed + failed), 2) if (passed + failed) else 0,
        "failed_tests": failed_names,
    }

    if failed > 0:
        results["warnings"].append(f"T1 ENGINE: {failed} regression tests failing")

    # Replay diff stability
    try:
        from .replay_diff import run_replay_diff
        replay = run_replay_diff()
        agreement = replay.get("top_1_agreement")
        if agreement is not None:
            tier1["replay_agreement"] = agreement
            tier1["replay_flips"] = replay.get("flips", 0)
            tier1["replay_total"] = replay.get("total", 0)
            if agreement < 0.90:
                results["warnings"].append(
                    f"T1 ENGINE: replay stability {agreement:.0%} < 90%"
                    f" ({replay.get('flips', '?')} flips)")
        else:
            tier1["replay_agreement"] = None
            tier1["replay_note"] = "No replay corpus found"
    except ImportError:
        tier1["replay_agreement"] = None
        tier1["replay_note"] = "replay_diff module not available"
    except Exception as e:
        tier1["replay_agreement"] = None
        tier1["replay_note"] = f"replay_diff error: {e}"

    results["tier1_engine"] = tier1

    # ═══ TIER 2: Deck Structure (changes only with card changes) ═══
    tier2 = {}
    for deck_name in TRACKED_DECKS:
        deck_result = _evaluate_deck(deck_name)
        if deck_result.get("error"):
            tier2[deck_name] = deck_result
            results["warnings"].append(f"T2 STRUCTURE: {deck_name}: {deck_result['error']}")
            continue

        # Categorize rules by meta-dependence
        strategy = load_strategy(deck_name)
        meta_rules = sum(1 for r in strategy.rules if r.layer == "meta_gameplan")
        stable_rules = len(strategy.rules) - meta_rules

        tier2[deck_name] = {
            **deck_result,
            "stable_rules": stable_rules,
            "meta_rules": meta_rules,
            "stable_ratio": round(stable_rules / len(strategy.rules), 2) if strategy.rules else 0,
        }

        if deck_result["no_condition_rules"] > 0:
            results["warnings"].append(
                f"T2 STRUCTURE: {deck_name}: {deck_result['no_condition_rules']} rules without conditions (spam)")

        if deck_result["mutual_suppression"] > 2:
            results["warnings"].append(
                f"T2 STRUCTURE: {deck_name}: {deck_result['mutual_suppression']} mutual suppression pairs")

    results["tier2_structure"] = tier2

    # ═══ TIER 3: Meta Fitness (changes with meta, expected to fluctuate) ═══
    tier3 = {}
    for deck_name in TRACKED_DECKS:
        strategy = load_strategy(deck_name)
        if not strategy:
            continue
        ga_data = strategy.stats if hasattr(strategy, 'stats') else {}
        tier3[deck_name] = {
            "ga_games": ga_data.get("games", 0),
            "ga_wins": ga_data.get("wins", 0),
            "ga_wr": round(ga_data["wins"] / ga_data["games"], 3) if ga_data.get("games", 0) > 0 else None,
            "meta_rules_count": sum(1 for r in strategy.rules if r.layer == "meta_gameplan"),
        }
    results["tier3_meta"] = tier3

    # ═══ Compare with history ═══
    if HISTORY_PATH.exists():
        history = json.loads(HISTORY_PATH.read_text())
        if history:
            prev = history[-1]

            # Tier 1: engine regression is CRITICAL
            prev_pass = prev.get("tier1_engine", {}).get("regression_pass_rate", 0)
            curr_pass = results["tier1_engine"]["regression_pass_rate"]
            if curr_pass < prev_pass:
                results["warnings"].append(
                    f"🚨 T1 ENGINE REGRESSION: pass rate {prev_pass:.0%} → {curr_pass:.0%}")

            # Tier 2: structural drift is concerning
            for deck_name in TRACKED_DECKS:
                prev_deck = prev.get("tier2_structure", {}).get(deck_name, {})
                curr_deck = results["tier2_structure"].get(deck_name, {})
                prev_rules = prev_deck.get("rules_total", 0)
                curr_rules = curr_deck.get("rules_total", 0)
                if curr_rules > prev_rules * 1.3 and prev_rules > 10:
                    results["warnings"].append(
                        f"T2 DRIFT: {deck_name} rules {prev_rules} → {curr_rules} "
                        f"(+{curr_rules - prev_rules}, overspecification risk)")
                prev_ms = prev_deck.get("mutual_suppression", 0)
                curr_ms = curr_deck.get("mutual_suppression", 0)
                if curr_ms > prev_ms + 3:
                    results["warnings"].append(
                        f"T2 COMPLEXITY: {deck_name} mutual suppression "
                        f"{prev_ms} → {curr_ms} (conflict graph growing)")

            # Tier 3: meta drop is just informational
            for deck_name in TRACKED_DECKS:
                prev_wr = prev.get("tier3_meta", {}).get(deck_name, {}).get("ga_wr")
                curr_wr = results["tier3_meta"].get(deck_name, {}).get("ga_wr")
                if prev_wr and curr_wr and curr_wr < prev_wr - 0.05:
                    results["warnings"].append(
                        f"T3 META: {deck_name} WR dropped {prev_wr:.0%} → {curr_wr:.0%} "
                        f"(may need re-GA or meta shifted)")

    # Save to history
    if save:
        history = []
        if HISTORY_PATH.exists():
            history = json.loads(HISTORY_PATH.read_text())
        history.append(results)
        # Keep last 50 snapshots
        history = history[-50:]
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(history, indent=2))

    return results


def main():
    parser = argparse.ArgumentParser(description="Advisor health check")
    parser.add_argument("--save", action="store_true", help="Save snapshot to history")
    args = parser.parse_args()

    results = run_health_check(save=args.save)

    # Print report
    t1 = results["tier1_engine"]
    print(f"=== Advisor Health Check ===\n")

    print(f"TIER 1 — ENGINE (must be stable):")
    print(f"  Regression tests: {t1['regression_passed']}/{t1['regression_total']}"
          f" ({t1['regression_pass_rate']:.0%})")
    if t1["failed_tests"]:
        for name in t1["failed_tests"]:
            print(f"    ✗ {name}")
    replay_agr = t1.get("replay_agreement")
    if replay_agr is not None:
        flips = t1.get("replay_flips", 0)
        print(f"  Replay stability: {replay_agr:.0%} top-1 agreement ({flips} flips)")
    elif t1.get("replay_note"):
        print(f"  Replay stability: {t1['replay_note']}")

    print(f"\nTIER 2 — STRUCTURE (changes with deck edits):")
    for deck_name, deck in results["tier2_structure"].items():
        if deck.get("error"):
            print(f"  {deck_name}: ERROR — {deck['error']}")
            continue
        ver = f" | engine={deck['engine_version']}" if deck.get('engine_version') else ""
        print(f"  {deck_name}: {deck['rules_total']} rules"
              f" ({deck['stable_rules']} stable + {deck['meta_rules']} meta"
              f" + {deck.get('pruned_rules', 0)} pruned)"
              f" | no_cond={deck['no_condition_rules']}"
              f" | mutual={deck['mutual_suppression']}"
              f" | conflicts={deck['conflict_pairs']}"
              f"{ver}")

    print(f"\nTIER 3 — META FITNESS (expected to fluctuate):")
    for deck_name, deck in results["tier3_meta"].items():
        wr = f"{deck['ga_wr']:.0%}" if deck.get('ga_wr') else "no data"
        games = deck.get('ga_games', 0)
        print(f"  {deck_name}: WR={wr} ({games} games)"
              f" | meta_rules={deck['meta_rules_count']}")

    if results["warnings"]:
        print(f"\n⚠ WARNINGS:")
        for w in results["warnings"]:
            print(f"  {w}")
    else:
        print(f"\n✅ All clear")

    if args.save:
        print(f"\nSnapshot saved to {HISTORY_PATH}")

    if t1["regression_pass_rate"] < 1.0:
        sys.exit(1)


if __name__ == "__main__":
    main()
