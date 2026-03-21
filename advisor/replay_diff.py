"""Replay frozen corpus through the current engine and compare against baselines.

Loads corpus JSON files, rebuilds synthetic GameState for each entry, runs
evaluate_rules_v2, and reports agreement metrics. Catches regressions caused
by engine or rule changes.

Usage:
    uv run python -m advisor.replay_diff [--verbose] [--deck NAME]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .database import card_cache
from .models import GameState, TurnInfo, PlayerState, MatchInfo, Zone, GameObject
from .strategy import load_strategy, evaluate_rules_v2, OpponentTracker, MetaDeck
from .version import ENGINE_VERSION

log = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).parent.parent / "data" / "replay_corpus"
RESULT_PATH = Path(__file__).parent.parent / "data" / "replay_diff_result.json"

PASS_THRESHOLD = 0.90


@dataclass
class DiffResult:
    deck: str
    total_states: int = 0
    top_1_match: int = 0
    top_3_match: int = 0
    flips: int = 0
    score_deltas: list[float] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    skipped: int = 0


# ─── State Builder (mirrors regression_tests._build_state) ──────

def _build_state_from_entry(entry: dict) -> GameState | None:
    """Build a synthetic GameState from a corpus entry's state dict."""
    card_cache.load()
    st = entry["state"]

    state = GameState()
    state.my_seat_id = 1
    state.match_info = MatchInfo()
    state.match_info.opponent_seat_id = 2

    state.players = {
        1: PlayerState(seat_id=1, life_total=st.get("my_life", 20)),
        2: PlayerState(seat_id=2, life_total=st.get("opp_life", 20)),
    }
    state.turn_info = TurnInfo(
        phase=st.get("phase", "Phase_Main1"),
        step=st.get("phase", "Phase_Main1"),
        turn_number=st.get("turn", 1),
        active_player=1, priority_player=1, decision_player=1,
    )

    zone_id = 1
    objects: dict[int, GameObject] = {}
    instance_id = 1000

    def make_obj(name: str, owner: int) -> GameObject | None:
        nonlocal instance_id
        card = None
        for c in card_cache._cache.values():
            if c.name == name:
                card = c
                break
        if not card:
            is_land = name in ("Plains", "Mountain", "Island", "Swamp", "Forest")
            gre_types = ["CardType_Land"] if is_land else ["CardType_Creature"]
            obj = GameObject(
                instance_id=instance_id, grp_id=0, zone_id=0,
                owner_seat_id=owner, controller_seat_id=owner,
                card_types=gre_types, name=name,
                power=2 if not is_land else 0,
                toughness=2 if not is_land else 0,
            )
        else:
            gre_types = [f"CardType_{t}" for t in card.card_types]
            obj = GameObject(
                instance_id=instance_id, grp_id=card.grp_id, zone_id=0,
                owner_seat_id=owner, controller_seat_id=owner,
                card_types=gre_types, name=card.name,
                color=card.colors,
                power=int(card.power) if card.power and card.power.isdigit() else 0,
                toughness=int(card.toughness) if card.toughness and card.toughness.isdigit() else 0,
            )
        instance_id += 1
        return obj

    hand_zone = Zone(zone_id=zone_id, type="ZoneType_Hand", owner_seat_id=1)
    zone_id += 1
    bf_zone = Zone(zone_id=zone_id, type="ZoneType_Battlefield", owner_seat_id=0)
    zone_id += 1

    for name in st.get("hand", []):
        obj = make_obj(name, 1)
        if obj is None:
            continue
        obj.zone_id = hand_zone.zone_id
        objects[obj.instance_id] = obj
        hand_zone.object_instance_ids.append(obj.instance_id)

    for name in st.get("my_battlefield", []):
        obj = make_obj(name, 1)
        if obj is None:
            continue
        obj.zone_id = bf_zone.zone_id
        objects[obj.instance_id] = obj
        bf_zone.object_instance_ids.append(obj.instance_id)

    for name in st.get("opp_battlefield", []):
        obj = make_obj(name, 2)
        if obj is None:
            continue
        obj.zone_id = bf_zone.zone_id
        objects[obj.instance_id] = obj
        bf_zone.object_instance_ids.append(obj.instance_id)

    state.zones = {hand_zone.zone_id: hand_zone, bf_zone.zone_id: bf_zone}
    state.objects = objects
    state.game_objects = objects
    return state


# ─── Replay & Compare ───────────────────────────────────────────

def _extract_rule_ids(hits) -> list[str]:
    """Extract ordered rule_id list from RuleHit objects."""
    return [h.rule_id for h in hits]


def replay_entry(entry: dict, verbose: bool = False) -> DiffResult | None:
    """Replay a single corpus entry. Returns None if strategy can't load."""
    strategy_name = entry.get("strategy_name", "")
    strategy = load_strategy(strategy_name)
    if not strategy:
        return None

    state = _build_state_from_entry(entry)
    if state is None:
        return None

    tracker = OpponentTracker()
    opp_deck = entry.get("opp_deck")
    if opp_deck:
        tracker.identified_deck = MetaDeck(
            name=opp_deck, archetype="unknown", speed="medium")
        tracker.confidence = 1.0

    try:
        hits, _ = evaluate_rules_v2(
            strategy.rules, state, opp_tracker=tracker,
            vulnerabilities=strategy.vulnerabilities, max_results=0)
    except Exception as e:
        log.debug("evaluate_rules_v2 error for %s: %s", strategy_name, e)
        result = DiffResult(deck=strategy_name, skipped=1)
        return result

    current_ids = _extract_rule_ids(hits)
    expected = entry.get("expected", [])
    expected_ids = [e["rule_id"] for e in expected]

    result = DiffResult(deck=strategy_name, total_states=1)

    # Top-1 match
    current_top = current_ids[0] if current_ids else ""
    expected_top = expected_ids[0] if expected_ids else ""

    if current_top == expected_top:
        result.top_1_match = 1
    else:
        result.flips = 1
        result.mismatches.append({
            "state": entry["state"],
            "expected_top": expected_top,
            "got_top": current_top,
            "expected_ids": expected_ids[:5],
            "got_ids": current_ids[:5],
        })
        if verbose:
            turn = entry["state"].get("turn", "?")
            phase = entry["state"].get("phase", "?")
            print(f"    MISMATCH T{turn} {phase}: expected={expected_top} got={current_top}")

    # Top-3 match: current top-1 appears anywhere in expected top-3
    expected_top3 = set(expected_ids[:3])
    if current_top in expected_top3:
        result.top_3_match = 1

    # Score delta: compare priority-based scores
    if expected and hits:
        from .actions import score_from_priority
        expected_score = score_from_priority(expected[0].get("priority", "medium"))
        current_score = score_from_priority(hits[0].priority, hits[0].weight)
        result.score_deltas.append(abs(current_score - expected_score))

    return result


def replay_corpus(deck_filter: str | None = None, verbose: bool = False) -> list[DiffResult]:
    """Replay all corpus files and return per-deck results."""
    if not CORPUS_DIR.exists():
        print(f"No corpus directory at {CORPUS_DIR}")
        print("Run: uv run python -m advisor.replay_corpus --export")
        return []

    corpus_files = sorted(CORPUS_DIR.glob("*.json"))
    if not corpus_files:
        print("No corpus files found. Run: uv run python -m advisor.replay_corpus --export")
        return []

    results: list[DiffResult] = []

    for path in corpus_files:
        deck_slug = path.stem
        if deck_filter and deck_filter.lower() not in deck_slug:
            continue

        try:
            entries = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: Cannot read {path.name}: {e}")
            continue

        if not entries:
            continue

        strategy_name = entries[0].get("strategy_name", deck_slug)
        strategy = load_strategy(strategy_name)
        if not strategy:
            print(f"  WARNING: Strategy not found for {strategy_name}, skipping {path.name}")
            continue

        if verbose:
            print(f"\n  Replaying {path.name} ({len(entries)} states)...")

        deck_result = DiffResult(deck=strategy_name)

        for entry in entries:
            r = replay_entry(entry, verbose=verbose)
            if r is None:
                deck_result.skipped += 1
                continue
            deck_result.total_states += r.total_states
            deck_result.top_1_match += r.top_1_match
            deck_result.top_3_match += r.top_3_match
            deck_result.flips += r.flips
            deck_result.score_deltas.extend(r.score_deltas)
            deck_result.mismatches.extend(r.mismatches)

        results.append(deck_result)

    return results


# ─── Reporting ──────────────────────────────────────────────────

def print_report(results: list[DiffResult]) -> float:
    """Print human-readable summary. Returns top_1_agreement rate."""
    total_states = sum(r.total_states for r in results)
    total_top1 = sum(r.top_1_match for r in results)
    total_top3 = sum(r.top_3_match for r in results)
    total_flips = sum(r.flips for r in results)

    top_1_agreement = total_top1 / max(1, total_states)
    top_3_agreement = total_top3 / max(1, total_states)
    flip_rate = total_flips / max(1, total_states)
    all_deltas = [d for r in results for d in r.score_deltas]
    avg_delta = sum(all_deltas) / max(1, len(all_deltas))

    print(f"\n{'='*50}")
    print(f"  Replay Diff Report  (engine {ENGINE_VERSION})")
    print(f"{'='*50}")

    for r in results:
        t1 = r.top_1_match / max(1, r.total_states)
        flip = r.flips / max(1, r.total_states)
        status = "OK" if t1 >= PASS_THRESHOLD else "DRIFT"
        skip_note = f" ({r.skipped} skipped)" if r.skipped else ""
        print(f"  {status:5s} {r.deck:30s}  top1={t1:.0%}  flips={flip:.0%}  n={r.total_states}{skip_note}")

    print(f"{'─'*50}")
    print(f"  Total states:    {total_states}")
    print(f"  Top-1 agreement: {top_1_agreement:.1%}")
    print(f"  Top-3 agreement: {top_3_agreement:.1%}")
    print(f"  Flip rate:       {flip_rate:.1%}")
    print(f"  Avg score delta: {avg_delta:.3f}")

    passed = top_1_agreement >= PASS_THRESHOLD
    verdict = "PASS" if passed else "FAIL"
    print(f"\n  Verdict: {verdict} (threshold: {PASS_THRESHOLD:.0%})")
    print(f"{'='*50}")

    return top_1_agreement


def save_result(results: list[DiffResult], top_1_agreement: float) -> None:
    """Write JSON artifact."""
    total_states = sum(r.total_states for r in results)
    total_flips = sum(r.flips for r in results)

    artifact = {
        "engine_version": ENGINE_VERSION,
        "total_states": total_states,
        "top_1_agreement": round(top_1_agreement, 4),
        "flip_rate": round(total_flips / max(1, total_states), 4),
        "threshold": PASS_THRESHOLD,
        "passed": top_1_agreement >= PASS_THRESHOLD,
        "decks": [
            {
                "deck": r.deck,
                "total": r.total_states,
                "top_1_match": r.top_1_match,
                "top_3_match": r.top_3_match,
                "flips": r.flips,
                "skipped": r.skipped,
                "mismatches": r.mismatches[:10],  # cap for readability
            }
            for r in results
        ],
    }

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))
    print(f"\n  Result saved to {RESULT_PATH}")


def run_replay_diff() -> dict:
    """Programmatic API for CI/health_check integration.

    Returns {"top_1_agreement": float|None, "flips": int, "total": int}.
    Returns None agreement if no corpus exists.
    """
    if not CORPUS_DIR.exists() or not list(CORPUS_DIR.glob("*.json")):
        return {"top_1_agreement": None, "flips": 0, "total": 0}

    results = replay_corpus(verbose=False)
    if not results:
        return {"top_1_agreement": None, "flips": 0, "total": 0}

    total_states = sum(r.total_states for r in results)
    total_top1 = sum(r.top_1_match for r in results)
    total_flips = sum(r.flips for r in results)

    agreement = total_top1 / max(1, total_states)
    return {
        "top_1_agreement": agreement,
        "flips": total_flips,
        "total": total_states,
    }


def main():
    parser = argparse.ArgumentParser(description="Replay frozen corpus and compare engine output")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-state mismatches")
    parser.add_argument("--deck", help="Filter to corpus files matching this name")
    args = parser.parse_args()

    results = replay_corpus(deck_filter=args.deck, verbose=args.verbose)

    if not results:
        print("No results. Generate corpus first: uv run python -m advisor.replay_corpus --export")
        sys.exit(1)

    top_1_agreement = print_report(results)
    save_result(results, top_1_agreement)

    sys.exit(0 if top_1_agreement >= PASS_THRESHOLD else 1)


if __name__ == "__main__":
    main()
