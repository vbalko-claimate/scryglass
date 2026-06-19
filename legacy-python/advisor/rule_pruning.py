"""Rule pruning — identify low-value and redundant rules for removal.

Uses RuleMetrics (trigger_rate, selection_swing, redundancy) to find
rules that should be pruned or demoted.

Usage:
    uv run python -m advisor.rule_pruning PATH [--apply] [--min-decisions N]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .models import RuleMetrics


@dataclass
class PruneCandidate:
    rule_id: str
    reason: str  # "low_trigger" | "no_swing" | "redundant_with:RULE_ID"
    metric_value: float


def metrics_from_dict(d: dict) -> RuleMetrics:
    """Build RuleMetrics from a serialized dict (rule JSON 'metrics' field)."""
    return RuleMetrics(
        fired=d.get("fired", 0),
        decisions=d.get("decisions", 0),
        selected=d.get("selected", 0),
        wins_when_fired=d.get("wins_when_fired", 0),
        games_when_fired=d.get("games_when_fired", 0),
        cofire_rules=d.get("cofire_rules", {}),
    )


def compute_redundancy(
    metrics: dict[str, RuleMetrics],
    action_families: dict[str, str],
) -> dict[str, list[str]]:
    """Return rule_id -> list of rules it is redundant with.

    Rule A is redundant with rule B if:
    - cofire(A,B) / fired(A) > 0.9 (A almost always fires with B)
    - A.selection_swing < B.selection_swing (B is more influential)
    - A and B have the same action_family
    """
    redundant: dict[str, list[str]] = {}

    for rule_id, rm in metrics.items():
        if rm.fired < 10:
            continue  # too few fires to judge redundancy
        family_a = action_families.get(rule_id, "")

        for partner_id, cofire_count in rm.cofire_rules.items():
            if partner_id not in metrics:
                continue
            partner_rm = metrics[partner_id]
            family_b = action_families.get(partner_id, "")

            # Same action family required
            if family_a != family_b:
                continue

            cofire_rate = cofire_count / max(1, rm.fired)
            if cofire_rate < 0.9:
                continue

            # A is redundant if B has higher swing
            if rm.selection_swing < partner_rm.selection_swing:
                redundant.setdefault(rule_id, []).append(partner_id)

    return redundant


def prune_candidates(
    rules: list[dict],
    *,
    min_trigger_rate: float = 0.01,
    min_selection_swing: float = 0.05,
    min_decisions: int = 200,
    min_fires_for_swing: int = 50,
) -> list[PruneCandidate]:
    """Identify rules that should be pruned or demoted.

    Args:
        rules: list of rule dicts from strategy JSON
        min_trigger_rate: prune if trigger_rate below this after min_decisions
        min_selection_swing: prune if swing below this after min_fires
        min_decisions: minimum decisions before judging trigger_rate
        min_fires_for_swing: minimum fires before judging swing
    """
    candidates: list[PruneCandidate] = []

    # Build metrics and family lookups
    metrics: dict[str, RuleMetrics] = {}
    families: dict[str, str] = {}
    for r in rules:
        rid = r["id"]
        m = r.get("metrics", {})
        rm = metrics_from_dict(m)
        # Also fold in legacy stats.fired
        stats = r.get("stats", {})
        if rm.fired == 0 and stats.get("fired", 0) > 0:
            rm.fired = stats["fired"]
        metrics[rid] = rm
        families[rid] = r.get("action_family", "")

    # Skip mulligan rules
    non_mulligan = [
        r for r in rules
        if r.get("layer") != "mulligan"
        and not (r.get("phase") and "Mulligan" in r.get("phase", []))
        and not r.get("pruned")
    ]

    for r in non_mulligan:
        rid = r["id"]
        rm = metrics[rid]

        # Low trigger rate
        if rm.decisions >= min_decisions and rm.trigger_rate < min_trigger_rate:
            candidates.append(PruneCandidate(
                rule_id=rid,
                reason="low_trigger",
                metric_value=rm.trigger_rate,
            ))
            continue

        # No selection swing (fires but never influences outcome)
        # Only judge if we have new-style metrics (decisions > 0), not just legacy stats
        if (rm.decisions > 0 and rm.fired >= min_fires_for_swing
                and rm.selection_swing < min_selection_swing):
            candidates.append(PruneCandidate(
                rule_id=rid,
                reason="no_swing",
                metric_value=rm.selection_swing,
            ))
            continue

    # Redundancy check
    redundant = compute_redundancy(metrics, families)
    for rid, partners in redundant.items():
        # Skip if already a candidate
        if any(c.rule_id == rid for c in candidates):
            continue
        candidates.append(PruneCandidate(
            rule_id=rid,
            reason=f"redundant_with:{partners[0]}",
            metric_value=metrics[rid].selection_swing,
        ))

    # Flag rules that have NEVER fired — but only if the strategy has been
    # through at least one GA/validation cycle (metrics.decisions > 0 on any rule).
    # Fresh strategies with no evaluation data should not be pruned.
    has_eval_data = any(rm.decisions > 0 for rm in metrics.values())
    if has_eval_data:
        for r in non_mulligan:
            rid = r["id"]
            rm = metrics[rid]
            if rm.fired == 0 and rm.decisions >= min_decisions:
                if any(c.rule_id == rid for c in candidates):
                    continue
                candidates.append(PruneCandidate(
                    rule_id=rid,
                    reason="never_fired",
                    metric_value=0.0,
                ))

    return candidates


def apply_pruning(rules: list[dict], candidates: list[PruneCandidate]) -> int:
    """Set weight=0.0 and pruned=true on candidate rules. Returns count pruned."""
    prune_ids = {c.rule_id for c in candidates}
    count = 0
    for r in rules:
        if r["id"] in prune_ids:
            r["weight"] = 0.0
            r["pruned"] = True
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Identify and prune low-value rules")
    parser.add_argument("path", type=Path, help="Strategy JSON file")
    parser.add_argument("--apply", action="store_true", help="Apply pruning (set weight=0)")
    parser.add_argument("--min-decisions", type=int, default=200,
                        help="Min decisions before judging trigger_rate (default: 200)")
    parser.add_argument("--min-fires", type=int, default=50,
                        help="Min fires before judging selection_swing (default: 50)")
    args = parser.parse_args()

    data = json.loads(args.path.read_text())
    rules = data.get("rules", [])
    name = data.get("name", args.path.stem)

    candidates = prune_candidates(
        rules,
        min_decisions=args.min_decisions,
        min_fires_for_swing=args.min_fires,
    )

    if not candidates:
        print(f"No prune candidates in {name} ({len(rules)} rules)")
        return

    # Group by reason
    by_reason: dict[str, list[PruneCandidate]] = {}
    for c in candidates:
        reason = c.reason.split(":")[0]
        by_reason.setdefault(reason, []).append(c)

    print(f"=== Prune candidates for {name} ({len(rules)} rules) ===\n")
    for reason, cands in sorted(by_reason.items()):
        print(f"  {reason}: {len(cands)} rules")
        for c in cands[:10]:
            print(f"    {c.rule_id} (metric={c.metric_value:.4f}) — {c.reason}")
        if len(cands) > 10:
            print(f"    ... and {len(cands) - 10} more")
        print()

    print(f"Total: {len(candidates)} prune candidates")

    if args.apply:
        count = apply_pruning(rules, candidates)
        args.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"\nApplied: {count} rules pruned (weight=0.0) in {args.path.name}")
    else:
        print("\nRun with --apply to prune these rules.")


if __name__ == "__main__":
    main()
