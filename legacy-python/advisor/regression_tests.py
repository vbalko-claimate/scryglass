"""Regression test suite for strategy rule changes.

Runs a fixed set of game scenarios and checks that the rules engine
produces expected recommendations. Catches regressions when we change
engine code, rule conditions, or weights.

Usage:
    uv run python -m advisor.regression_tests
    uv run python -m advisor.regression_tests --deck "Mono White Lifegain"
    uv run python -m advisor.regression_tests --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .strategy import evaluate_rules, load_strategy, OpponentTracker, MetaDeck
from .database import card_cache
from .models import GameState, TurnInfo, PlayerState, MatchInfo, Zone, GameObject


@dataclass
class Scenario:
    """A test scenario with expected outcome."""
    name: str
    deck: str  # strategy name
    turn: int
    phase: str
    my_life: int
    opp_life: int
    hand: list[str]
    my_battlefield: list[str]
    opp_battlefield: list[str]
    opp_speed: str = "medium"
    # Expected: rule IDs that SHOULD fire
    expect_fired: list[str] = field(default_factory=list)
    # Expected: rule IDs that MUST NOT fire
    expect_not_fired: list[str] = field(default_factory=list)
    # Expected: which rule should have highest priority in output
    expect_top_rule: str = ""


# ─── White Lifegain Scenarios ────────────────────────────────

WHITE_LIFEGAIN_SCENARIOS = [
    Scenario(
        name="WL-01: Hold Ghosts when opp has no creatures",
        deck="Mono White Lifegain",
        turn=4, phase="Phase_Main1", my_life=20, opp_life=18,
        hand=["Sheltered by Ghosts", "Plains"],
        my_battlefield=["Healer's Hawk", "Plains", "Plains", "Plains"],
        opp_battlefield=["Island", "Island", "Plains"],
        expect_fired=["card_synergy_ghosts_hold_for_exile"],
        expect_not_fired=["card_synergy_ghosts_pridemate", "card_synergy_ghosts_flyer_engine"],
    ),
    Scenario(
        name="WL-02: Cast Ghosts when opp HAS creature",
        deck="Mono White Lifegain",
        turn=4, phase="Phase_Main1", my_life=20, opp_life=18,
        hand=["Sheltered by Ghosts", "Plains"],
        my_battlefield=["Healer's Hawk", "Ajani's Pridemate", "Plains", "Plains", "Plains"],
        opp_battlefield=["Grizzly Bears", "Island", "Plains"],
        # ghosts_flyer_engine may fire instead of ghosts_pridemate due to weight/priority
        expect_fired=["card_synergy_ghosts_flyer_engine"],
        expect_not_fired=["card_synergy_ghosts_hold_for_exile"],
    ),
    Scenario(
        name="WL-03: Vanguard needs 3 creatures — deploy precombat",
        deck="Mono White Lifegain",
        turn=3, phase="Phase_Main1", my_life=20, opp_life=18,
        hand=["Healer's Hawk", "Plains"],
        my_battlefield=["Leonin Vanguard", "Ruin-Lurker Bat", "Plains", "Plains"],
        opp_battlefield=["Mountain", "Mountain"],
        expect_fired=["card_synergy_vanguard_three_creatures"],
    ),
    # NOTE: crackback requires power_min 6 on Pridemate/Channeler.
    # Can't easily simulate pumped creatures in synthetic state.
    # Test deferred until we can set power on GameObjects.

    Scenario(
        name="WL-05: Attack when opp is low (<=10)",
        deck="Mono White Lifegain",
        turn=6, phase="Phase_Combat", my_life=15, opp_life=9,
        hand=["Plains"],
        my_battlefield=["Ajani's Pridemate", "Healer's Hawk", "Plains", "Plains", "Plains"],
        opp_battlefield=["Goblin Guide", "Mountain", "Mountain"],
        expect_fired=["situation_attack_opp_low"],
    ),
    Scenario(
        name="WL-06: Never block Screaming Nemesis",
        deck="Mono White Lifegain",
        turn=5, phase="Phase_Main1", my_life=16, opp_life=14,
        hand=[],
        my_battlefield=["Healer's Hawk", "Ajani's Pridemate", "Plains", "Plains", "Plains"],
        opp_battlefield=["Screaming Nemesis", "Mountain", "Mountain"],
        expect_fired=["threat_response_never_block_nemesis"],
    ),
    Scenario(
        name="WL-07: Channeler transfer text shows Channeler not Hawk",
        deck="Mono White Lifegain",
        turn=5, phase="Phase_Main1", my_life=18, opp_life=14,
        hand=[],
        my_battlefield=["Essence Channeler", "Healer's Hawk", "Plains", "Plains", "Plains"],
        opp_battlefield=["Mountain"],
        expect_fired=["card_synergy_channeler_transfer"],
        # After fix: {card} should resolve to "Essence Channeler", not "Healer's Hawk"
    ),
]

# ─── Red Goblins Scenarios ────────────────────────────────

RED_GOBLINS_SCENARIOS = [
    Scenario(
        name="RG-01: Second Rite at exactly 10 life",
        deck="Mono Red Goblins",
        turn=5, phase="Phase_Main1", my_life=14, opp_life=10,
        hand=["Hidetsugu's Second Rite", "Mountain"],
        my_battlefield=["Courageous Goblin", "Mountain", "Mountain", "Mountain", "Mountain"],
        opp_battlefield=["Island", "Island", "Plains"],
        expect_fired=["synergy_second_rite_lethal"],
    ),
    Scenario(
        name="RG-02: Don't attack before Second Rite",
        deck="Mono Red Goblins",
        turn=5, phase="Phase_Main1", my_life=14, opp_life=10,
        hand=["Hidetsugu's Second Rite"],
        my_battlefield=["Courageous Goblin", "Goblin Boarders", "Mountain", "Mountain", "Mountain", "Mountain"],
        opp_battlefield=["Island", "Plains"],
        expect_fired=["synergy_second_rite_freeze"],
    ),
    Scenario(
        name="RG-03: Imodane burn combo — burn creature not face",
        deck="Mono Red Goblins",
        turn=5, phase="Phase_Main1", my_life=16, opp_life=12,
        hand=["Burst Lightning", "Mountain"],
        my_battlefield=["Imodane, the Pyrohammer", "Mountain", "Mountain", "Mountain", "Mountain"],
        opp_battlefield=["Grizzly Bears", "Mountain", "Mountain"],
        expect_fired=["synergy_imodane_burn_combo"],
    ),
    Scenario(
        name="RG-04: Burn face for lethal",
        deck="Mono Red Goblins",
        turn=7, phase="Phase_Main1", my_life=8, opp_life=5,
        hand=["Burst Lightning"],
        my_battlefield=["Courageous Goblin", "Mountain", "Mountain", "Mountain"],
        opp_battlefield=["Mountain", "Mountain"],
        expect_fired=["situation_burn_face_lethal"],
        expect_not_fired=["threat_hold_burst_lightning"],
    ),
]


def _build_state(scenario: Scenario) -> GameState:
    """Build a synthetic GameState from a scenario."""
    from .test_utils import build_synthetic_state
    return build_synthetic_state(
        turn=scenario.turn, phase=scenario.phase,
        my_life=scenario.my_life, opp_life=scenario.opp_life,
        hand=scenario.hand, my_battlefield=scenario.my_battlefield,
        opp_battlefield=scenario.opp_battlefield,
    )


def run_scenario(scenario: Scenario, verbose: bool = False) -> tuple[bool, list[str]]:
    """Run one scenario, return (passed, issues)."""
    strategy = load_strategy(scenario.deck)
    if not strategy:
        return False, [f"Strategy not found: {scenario.deck}"]

    state = _build_state(scenario)

    # Build opp tracker
    tracker = OpponentTracker()
    tracker.identified_deck = MetaDeck(
        name="test_opp", archetype="unknown", speed=scenario.opp_speed)
    tracker.confidence = 1.0

    advice = evaluate_rules(
        strategy.rules, state, opp_tracker=tracker,
        vulnerabilities=strategy.vulnerabilities, max_results=0)

    fired_ids = set()
    advice_texts = {}
    for a in advice:
        import re
        m = re.search(r':(\w+)\]', a.details or '')
        if m:
            rid = m.group(1)
            fired_ids.add(rid)
            advice_texts[rid] = a.message

    issues = []

    # Check expected fired
    for rid in scenario.expect_fired:
        if rid not in fired_ids:
            issues.append(f"EXPECTED {rid} to fire but it didn't")

    # Check expected NOT fired
    for rid in scenario.expect_not_fired:
        if rid in fired_ids:
            issues.append(f"EXPECTED {rid} NOT to fire but it did: \"{advice_texts.get(rid, '?')}\"")

    # Check top rule
    if scenario.expect_top_rule and advice:
        import re
        top_m = re.search(r':(\w+)\]', advice[0].details or '')
        top_id = top_m.group(1) if top_m else ""
        if top_id != scenario.expect_top_rule:
            issues.append(f"EXPECTED top rule {scenario.expect_top_rule} but got {top_id}")

    passed = len(issues) == 0

    if verbose or not passed:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} {scenario.name}")
        if verbose:
            print(f"    Fired: {sorted(fired_ids)}")
        for issue in issues:
            print(f"    {issue}")

    return passed, issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deck", help="Only run scenarios for this deck")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    all_scenarios = WHITE_LIFEGAIN_SCENARIOS + RED_GOBLINS_SCENARIOS

    if args.deck:
        all_scenarios = [s for s in all_scenarios if args.deck.lower() in s.deck.lower()]

    passed = 0
    failed = 0
    for scenario in all_scenarios:
        ok, issues = run_scenario(scenario, verbose=args.verbose)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
