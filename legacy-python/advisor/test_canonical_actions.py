"""Validate Phase 1 canonical actions implementation.

Usage:
    uv run python -m advisor.test_canonical_actions
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .models import (
    ActionFamily, ActionScore, Advice, GameState, MatchInfo,
    PlayerState, RuleHit, TurnInfo, Zone, GameObject,
)
from .actions import infer_action_family, score_from_priority, render_advice, tag_heuristic_advice
from .strategy import Rule, _rule_to_dict, _rule_from_dict, evaluate_rules_v2

GENERAL_JSON = Path(__file__).parent.parent / "data" / "strategies" / "general.json"

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


# ─── 1. ActionFamily inference ─────────────────────────────────

print("\n=== 1. ActionFamily inference ===")

_INFER_CASES = [
    ("Cast Lightning Bolt", {}, ActionFamily.CAST_SPELL),
    ("Play a land before spells", {}, ActionFamily.PLAY_LAND),
    ("Hold removal for their turn", {}, ActionFamily.PASS),
    ("Attack with all creatures", {}, ActionFamily.ATTACK),
    ("Block with Grizzly Bears", {}, ActionFamily.BLOCK),
    ("Activate Witch's Oven", {}, ActionFamily.ACTIVATE),
    ("Don't cast anything — save mana", {}, ActionFamily.PASS),
    ("land drop", {}, ActionFamily.PLAY_LAND),
    ("Sacrifice the token", {}, ActionFamily.ACTIVATE),
]

for text, kw, expected in _INFER_CASES:
    result = infer_action_family(text, **kw)
    check(f"infer '{text}' -> {expected.value}", result == expected,
          f"got {result.value}")

# Phase fallback
result = infer_action_family("", phase="Phase_Combat")
check("phase fallback: empty + Combat -> ATTACK", result == ActionFamily.ATTACK,
      f"got {result.value}")

# Default fallback
result = infer_action_family("something ambiguous here")
check("default fallback -> CAST_SPELL", result == ActionFamily.CAST_SPELL,
      f"got {result.value}")


# ─── 2. score_from_priority ────────────────────────────────────

print("\n=== 2. score_from_priority ===")

s = score_from_priority("critical", 1.0)
check("critical/1.0 in [0.9, 1.0]", 0.9 <= s <= 1.0, f"got {s:.3f}")

s = score_from_priority("high", 1.0)
check("high/1.0 in [0.7, 0.8]", 0.7 <= s <= 0.8, f"got {s:.3f}")

s = score_from_priority("medium", 1.0)
check("medium/1.0 in [0.4, 0.6]", 0.4 <= s <= 0.6, f"got {s:.3f}")

s = score_from_priority("low", 1.0)
check("low/1.0 in [0.2, 0.3]", 0.2 <= s <= 0.3, f"got {s:.3f}")

s_hi2 = score_from_priority("high", 2.0)
s_hi1 = score_from_priority("high", 1.0)
check("weight scaling: high/2.0 > high/1.0", s_hi2 > s_hi1,
      f"{s_hi2:.3f} vs {s_hi1:.3f}")

s = score_from_priority("critical", 2.0)
check("clamping: critical/2.0 <= 1.0", s <= 1.0, f"got {s:.3f}")


# ─── 3. tag_heuristic_advice ──────────────────────────────────

print("\n=== 3. tag_heuristic_advice ===")

heuristic_no_scores = Advice(source="heuristic", priority="high",
                             message="Cast Lightning Bolt")
strategy_with_scores = Advice(
    source="strategy", priority="high", message="Cast X",
    action_scores=[ActionScore(family=ActionFamily.CAST_SPELL, score=0.8)])
llm_advice = Advice(source="llm", priority="medium", message="Consider removal")

batch = [heuristic_no_scores, strategy_with_scores, llm_advice]
tag_heuristic_advice(batch)

check("heuristic without scores gets tagged",
      len(heuristic_no_scores.action_scores) == 1,
      f"got {len(heuristic_no_scores.action_scores)} scores")
check("strategy with scores untouched",
      len(strategy_with_scores.action_scores) == 1 and
      strategy_with_scores.action_scores[0].score == 0.8)
check("llm advice untouched", len(llm_advice.action_scores) == 0)

# 100% coverage after tagging
heuristic_batch = [
    Advice(source="heuristic", priority="low", message="Play a land"),
    Advice(source="heuristic", priority="medium", message="Attack now"),
]
tag_heuristic_advice(heuristic_batch)
all_tagged = all(len(a.action_scores) > 0 for a in heuristic_batch)
check("100% heuristic advice has action_scores after tagging", all_tagged)


# ─── 4. render_advice ─────────────────────────────────────────

print("\n=== 4. render_advice ===")

hit = RuleHit(
    rule_id="test_rule", layer="general", weight=1.5, priority="high",
    raw_message="Cast Lightning Bolt",
    action_scores=[ActionScore(family=ActionFamily.CAST_SPELL, score=0.75,
                               source="strategy", rule_id="test_rule")],
)
advices = render_advice([hit])
check("render produces 1 advice", len(advices) == 1)
a = advices[0]
check("action_scores copied", len(a.action_scores) == 1 and
      a.action_scores[0].family == ActionFamily.CAST_SPELL)
check("details format '[layer:rule_id] w:X.XX'",
      a.details == "[general:test_rule] w:1.50",
      f"got '{a.details}'")
expected_conf = min(0.9, 0.5 + 1.5 * 0.2)
check("confidence calculated correctly",
      abs(a.confidence - expected_conf) < 0.001,
      f"got {a.confidence:.3f}, expected {expected_conf:.3f}")


# ─── 5. Mulligan guard ────────────────────────────────────────

print("\n=== 5. Mulligan guard ===")

mull_rule = Rule(id="mull_test", layer="mulligan", phase=["Mulligan"],
                 hand_lands_max=0, action="Mulligan — no lands",
                 priority="critical", weight=1.0)
cast_rule = Rule(id="cast_test", layer="general", phase=["Main"],
                 my_turn=True, action="Cast something",
                 action_family="cast_spell", priority="medium", weight=1.0)

# Mulligan state: 7-card no-land hand
mull_state = GameState()
mull_state.my_seat_id = 1
mull_state.match_info = MatchInfo(opponent_seat_id=2)
mull_state.players = {
    1: PlayerState(seat_id=1, life_total=20),
    2: PlayerState(seat_id=2, life_total=20),
}
mull_state.turn_info = TurnInfo(phase="Phase_Main1", active_player=1,
                                priority_player=1, decision_player=1)
mull_state.pending_request = "GREMessageType_MulliganReq"

# Build hand zone with 7 non-land cards
hand_zone = Zone(zone_id=1, type="ZoneType_Hand", owner_seat_id=1)
for i in range(7):
    obj = GameObject(instance_id=100 + i, grp_id=0, zone_id=1,
                     owner_seat_id=1, controller_seat_id=1,
                     card_types=["CardType_Creature"], name=f"Bear{i}")
    mull_state.objects[obj.instance_id] = obj
    hand_zone.object_instance_ids.append(obj.instance_id)
mull_state.zones = {1: hand_zone}

hits, _ = evaluate_rules_v2([mull_rule, cast_rule], mull_state)

mull_hits = [h for h in hits if h.rule_id == "mull_test"]
cast_hits = [h for h in hits if h.rule_id == "cast_test"]

check("mulligan rule fires", len(mull_hits) == 1,
      f"got {len(mull_hits)} hits")
if mull_hits:
    check("mulligan rule has empty action_scores",
          len(mull_hits[0].action_scores) == 0,
          f"got {len(mull_hits[0].action_scores)}")
check("non-mulligan rule does NOT fire during mulligan",
      len(cast_hits) == 0, f"got {len(cast_hits)} hits")


# ─── 6. Schema-first roundtrip ────────────────────────────────

print("\n=== 6. Schema-first roundtrip ===")

r1 = Rule(id="rt_cast", layer="general", action="Cast X",
          action_family="cast_spell", priority="high", weight=1.0)
d1 = _rule_to_dict(r1)
r1b = _rule_from_dict(d1)
check("roundtrip preserves action_family",
      r1b.action_family == "cast_spell",
      f"got {r1b.action_family!r}")

r2 = Rule(id="rt_none", layer="general", action="Do something",
          priority="medium", weight=1.0)
d2 = _rule_to_dict(r2)
r2b = _rule_from_dict(d2)
check("roundtrip preserves None action_family",
      r2b.action_family is None, f"got {r2b.action_family!r}")

# Declared action_family is used (not inference)
declared_rule = Rule(id="decl_test", layer="general", phase=["Main"],
                     my_turn=True, action="Hold removal for later",
                     action_family="cast_spell",  # text says "hold" but family says cast
                     priority="medium", weight=1.0)
decl_state = GameState()
decl_state.my_seat_id = 1
decl_state.match_info = MatchInfo(opponent_seat_id=2)
decl_state.players = {
    1: PlayerState(seat_id=1, life_total=20),
    2: PlayerState(seat_id=2, life_total=20),
}
decl_state.turn_info = TurnInfo(phase="Phase_Main1", active_player=1,
                                priority_player=1, decision_player=1)
decl_state.zones = {}
decl_state.pending_request = None

hits, _ = evaluate_rules_v2([declared_rule], decl_state)
if hits:
    fam = hits[0].action_scores[0].family if hits[0].action_scores else None
    check("declared action_family overrides inference",
          fam == ActionFamily.CAST_SPELL,
          f"got {fam}")
else:
    check("declared action_family rule fires", False, "rule did not fire")

# Invalid action_family falls back to inference
bad_rule = Rule(id="bad_fam", layer="general", phase=["Main"],
                my_turn=True, action="Attack with all",
                action_family="not_a_real_family",
                priority="medium", weight=1.0)
hits, _ = evaluate_rules_v2([bad_rule], decl_state)
if hits and hits[0].action_scores:
    fam = hits[0].action_scores[0].family
    check("invalid action_family falls back to inference",
          fam == ActionFamily.ATTACK,
          f"got {fam}")
else:
    check("invalid action_family rule fires with scores",
          bool(hits), "rule did not fire or no scores")


# ─── 7. Coverage assertion on general.json ─────────────────────

print("\n=== 7. general.json coverage ===")

with open(GENERAL_JSON) as f:
    gen_data = json.load(f)

rules_data = gen_data.get("rules", [])
valid_families = {e.value for e in ActionFamily}

non_mull = [r for r in rules_data if r.get("layer") != "mulligan"]
mull = [r for r in rules_data if r.get("layer") == "mulligan"]

non_mull_missing = [r["id"] for r in non_mull if not r.get("action_family")]
check("all non-mulligan rules have action_family",
      len(non_mull_missing) == 0,
      f"missing: {non_mull_missing}")

mull_with_af = [r["id"] for r in mull if r.get("action_family")]
check("no mulligan rules have action_family",
      len(mull_with_af) == 0,
      f"unexpected: {mull_with_af}")

bad_values = [(r["id"], r.get("action_family"))
              for r in non_mull if r.get("action_family") not in valid_families]
check("all action_family values are valid ActionFamily enums",
      len(bad_values) == 0,
      f"invalid: {bad_values}")


# ─── Summary ──────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
sys.exit(0 if failed == 0 else 1)
