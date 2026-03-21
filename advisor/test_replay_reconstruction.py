"""Phase 0 replay reconstruction test.

Verifies that persisted telemetry can reconstruct >95% of decision states
from completed matches. This is the final Phase 0 success gate.

Usage:
    uv run python -m advisor.test_replay_reconstruction
    uv run python -m advisor.test_replay_reconstruction --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from .database import get_connection


def _load_match_events(match_id: str) -> list[dict]:
    """Load all events for a match, ordered by insertion."""
    conn = get_connection()
    cur = conn.execute(
        "SELECT event_type, game_number, turn_number, phase, data "
        "FROM match_events WHERE match_id = ? ORDER BY rowid",
        (match_id,),
    )
    rows = []
    for row in cur:
        rows.append({
            "type": row[0],
            "game": row[1],
            "turn": row[2],
            "phase": row[3],
            "data": json.loads(row[4]) if row[4] else {},
        })
    conn.close()
    return rows


def _get_telemetry_matches() -> list[str]:
    """Return match_ids that have decision_eval telemetry."""
    conn = get_connection()
    cur = conn.execute(
        "SELECT DISTINCT match_id FROM match_events "
        "WHERE event_type = 'decision_eval' ORDER BY rowid"
    )
    ids = [row[0] for row in cur]
    conn.close()
    return ids


def check_reconstruction(match_id: str, verbose: bool = False) -> dict:
    """Check replay reconstruction quality for a single match.

    Returns dict with pass/fail and metrics.
    """
    events = _load_match_events(match_id)
    if not events:
        return {"match_id": match_id, "pass": False, "reason": "no events"}

    # Partition events
    decision_evals = [e for e in events if e["type"] == "decision_eval"]
    decision_contexts = [e for e in events if e["type"] == "decision_context"]
    decision_outcomes = [e for e in events if e["type"] == "decision_outcome"]
    compliances = [e for e in events if e["type"] == "advice_compliance"]
    card_plays = [e for e in events if e["type"] == "card_played"]
    turn_starts = [e for e in events if e["type"] == "turn_start"]
    game_ends = [e for e in events if e["type"] == "game_end"]

    issues = []

    # ── 1. Decision coverage: every decision_context should have a decision_eval ──
    # Group by (game, turn, phase, game_state_id) to match contexts to evals
    ctx_keys = set()
    for e in decision_contexts:
        gsid = e["data"].get("game_state_id", "?")
        ctx_keys.add((e["game"], e["turn"], e["phase"], gsid))

    eval_keys = set()
    for e in decision_evals:
        gsid = e["data"].get("game_state_id", "?")
        eval_keys.add((e["game"], e["turn"], e["phase"], gsid))

    # Contexts without matching eval (by turn+phase, relaxed — same gsid not required)
    ctx_turns = defaultdict(int)
    for e in decision_contexts:
        ctx_turns[(e["game"], e["turn"], e["phase"])] += 1

    eval_turns = defaultdict(int)
    for e in decision_evals:
        eval_turns[(e["game"], e["turn"], e["phase"])] += 1

    missing_eval_turns = set()
    for key in ctx_turns:
        if key not in eval_turns:
            missing_eval_turns.add(key)

    if missing_eval_turns:
        issues.append(
            f"{len(missing_eval_turns)} decision points without eval telemetry"
        )

    total_ctx = len(ctx_turns)
    covered_ctx = total_ctx - len(missing_eval_turns)
    ctx_coverage = covered_ctx / max(1, total_ctx)

    # ── 2. Eval data quality: required fields present ──
    required_eval_fields = ["game_state_id", "advice_count", "engine_version"]
    optional_eval_fields = ["top_advice", "strategy_name", "opp_deck", "recommended_cards"]
    missing_fields = defaultdict(int)
    eval_with_advice = 0
    eval_with_rule_id = 0

    for e in decision_evals:
        d = e["data"]
        for f in required_eval_fields:
            if f not in d:
                missing_fields[f] += 1
        if d.get("advice_count", 0) > 0:
            eval_with_advice += 1
        for a in d.get("top_advice", []):
            if a.get("rule_id"):
                eval_with_rule_id += 1
                break

    if missing_fields:
        for f, cnt in missing_fields.items():
            issues.append(f"decision_eval missing '{f}' in {cnt}/{len(decision_evals)} records")

    # ── 3. Context data quality: board state reconstructable ──
    required_ctx_fields = ["game_state_id", "my_life", "opp_life", "my_battlefield", "opp_battlefield"]
    ctx_missing = defaultdict(int)
    for e in decision_contexts:
        d = e["data"]
        for f in required_ctx_fields:
            if f not in d:
                ctx_missing[f] += 1

    if ctx_missing:
        for f, cnt in ctx_missing.items():
            issues.append(f"decision_context missing '{f}' in {cnt}/{len(decision_contexts)} records")

    ctx_field_coverage = 1.0
    if decision_contexts:
        total_checks = len(decision_contexts) * len(required_ctx_fields)
        total_missing = sum(ctx_missing.values())
        ctx_field_coverage = (total_checks - total_missing) / max(1, total_checks)

    # ── 4. Turn timeline completeness ──
    all_turns = set()
    for e in events:
        if e["turn"] is not None and e["turn"] > 0:
            all_turns.add((e["game"], e["turn"]))

    eval_turn_set = set()
    for e in decision_evals:
        if e["turn"] is not None and e["turn"] > 0:
            eval_turn_set.add((e["game"], e["turn"]))

    turns_without_eval = all_turns - eval_turn_set
    # Filter: opponent-only turns may not generate evals (opponent's turn, no priority)
    # We consider a turn "our turn" if we have a decision_context or card_played on it
    our_turns = set()
    for e in events:
        if e["type"] in ("decision_context", "card_played", "spell_cast", "attack_declared"):
            if e["turn"] is not None and e["turn"] > 0:
                our_turns.add((e["game"], e["turn"]))

    our_turns_without_eval = our_turns - eval_turn_set
    turn_coverage = (len(our_turns) - len(our_turns_without_eval)) / max(1, len(our_turns))

    if our_turns_without_eval:
        issues.append(
            f"{len(our_turns_without_eval)}/{len(our_turns)} of our turns lack decision_eval"
        )

    # ── 5. Compliance chain: card_played → compliance ──
    plays_on_turns = set()
    for e in card_plays:
        plays_on_turns.add((e["game"], e["turn"]))

    compliance_turns = set()
    for e in compliances:
        compliance_turns.add((e["game"], e["turn"]))

    plays_without_compliance = plays_on_turns - compliance_turns
    compliance_coverage = (
        (len(plays_on_turns) - len(plays_without_compliance)) / max(1, len(plays_on_turns))
    )

    if plays_without_compliance and len(plays_without_compliance) > len(plays_on_turns) * 0.3:
        issues.append(
            f"{len(plays_without_compliance)}/{len(plays_on_turns)} card plays without compliance"
        )

    # ── 6. Outcome coverage ──
    # Outcomes need 2 turns of future — so last 2 turns can't have outcomes
    max_turn = max((e["turn"] for e in events if e["turn"]), default=0)
    eligible_evals = [e for e in decision_evals if e["turn"] is not None and e["turn"] <= max_turn - 2]
    outcome_turn_set = set()
    for e in decision_outcomes:
        outcome_turn_set.add((e["game"], e["turn"]))

    eligible_without_outcome = 0
    for e in eligible_evals:
        if (e["game"], e["turn"]) not in outcome_turn_set:
            eligible_without_outcome += 1

    # Outcome is per-turn, not per-eval — count unique turns
    eligible_turns = set()
    for e in eligible_evals:
        eligible_turns.add((e["game"], e["turn"]))
    outcome_turn_coverage = (
        (len(eligible_turns) - len(eligible_turns - outcome_turn_set))
        / max(1, len(eligible_turns))
    )

    # ── Aggregate score ──
    # Weighted: context coverage (40%), turn coverage (30%), field quality (20%), compliance (10%)
    reconstruction_rate = (
        ctx_coverage * 0.40
        + turn_coverage * 0.30
        + ctx_field_coverage * 0.20
        + compliance_coverage * 0.10
    )

    passed = reconstruction_rate >= 0.95

    result = {
        "match_id": match_id[:20] + "...",
        "pass": passed,
        "reconstruction_rate": round(reconstruction_rate, 4),
        "metrics": {
            "decision_evals": len(decision_evals),
            "decision_contexts": len(decision_contexts),
            "decision_outcomes": len(decision_outcomes),
            "compliances": len(compliances),
            "card_plays": len(card_plays),
            "turns": len(all_turns),
            "our_turns": len(our_turns),
            "ctx_coverage": round(ctx_coverage, 4),
            "turn_coverage": round(turn_coverage, 4),
            "ctx_field_coverage": round(ctx_field_coverage, 4),
            "compliance_coverage": round(compliance_coverage, 4),
            "outcome_turn_coverage": round(outcome_turn_coverage, 4),
            "eval_with_advice_pct": round(eval_with_advice / max(1, len(decision_evals)), 4),
            "eval_with_rule_id_pct": round(eval_with_rule_id / max(1, len(decision_evals)), 4),
        },
        "issues": issues,
    }

    if verbose:
        print(f"\n{'='*60}")
        print(f"Match: {match_id[:30]}...")
        print(f"  Reconstruction rate: {reconstruction_rate:.1%}")
        print(f"  Context coverage:    {ctx_coverage:.1%} ({covered_ctx}/{total_ctx})")
        print(f"  Turn coverage:       {turn_coverage:.1%} ({len(our_turns) - len(our_turns_without_eval)}/{len(our_turns)})")
        print(f"  Field quality:       {ctx_field_coverage:.1%}")
        print(f"  Compliance coverage: {compliance_coverage:.1%} ({len(plays_on_turns) - len(plays_without_compliance)}/{len(plays_on_turns)})")
        print(f"  Outcome coverage:    {outcome_turn_coverage:.1%} (eligible turns)")
        print(f"  Decision evals:      {len(decision_evals)}")
        print(f"  Contexts:            {len(decision_contexts)}")
        print(f"  Advice coverage:     {eval_with_advice / max(1, len(decision_evals)):.0%}")
        print(f"  Rule provenance:     {eval_with_rule_id / max(1, len(decision_evals)):.0%}")
        if issues:
            print(f"  Issues:")
            for i in issues:
                print(f"    - {i}")
        print(f"  RESULT: {'PASS' if passed else 'FAIL'}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Phase 0 replay reconstruction test")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--match", help="Test specific match_id")
    args = parser.parse_args()

    if args.match:
        match_ids = [args.match]
    else:
        match_ids = _get_telemetry_matches()

    if not match_ids:
        print("No matches with decision_eval telemetry found.")
        print("Play a game with scryglass running to generate telemetry.")
        sys.exit(1)

    print(f"Testing replay reconstruction on {len(match_ids)} match(es)...")

    results = []
    for mid in match_ids:
        r = check_reconstruction(mid, verbose=args.verbose)
        results.append(r)

    # Summary
    print(f"\n{'='*60}")
    print("PHASE 0 REPLAY RECONSTRUCTION SUMMARY")
    print(f"{'='*60}")

    all_pass = True
    rates = []
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        rate = r["reconstruction_rate"]
        rates.append(rate)
        m = r["metrics"]
        print(f"  [{status}] {r['match_id']} — {rate:.1%} "
              f"(evals={m['decision_evals']}, ctx={m['decision_contexts']}, "
              f"outcomes={m['decision_outcomes']})")
        if not r["pass"]:
            all_pass = False
            for issue in r["issues"]:
                print(f"         ⚠ {issue}")

    avg_rate = sum(rates) / len(rates) if rates else 0
    print(f"\n  Average reconstruction rate: {avg_rate:.1%} (target: >95%)")
    print(f"  Matches tested: {len(results)}")
    print(f"  Passed: {sum(1 for r in results if r['pass'])}/{len(results)}")

    if all_pass:
        print("\n  ✅ PHASE 0 REPLAY RECONSTRUCTION: PASS")
    else:
        print("\n  ❌ PHASE 0 REPLAY RECONSTRUCTION: FAIL")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
