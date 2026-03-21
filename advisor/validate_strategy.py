"""Validate and fix an existing strategy JSON file.

Usage:
    uv run python -m advisor.validate_strategy PATH [--fix] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Reuse the validator from the generator
from .generate_rules import _validate_and_fix_rules


def validate_strategy(path: Path, fix: bool = False) -> list[str]:
    """Validate a strategy file and optionally fix issues.

    Returns list of issues found.
    """
    data = json.loads(path.read_text())
    rules = data.get("rules", [])
    issues: list[str] = []

    # ── Check 1: Hold/use conflicts ──
    from collections import defaultdict
    _HOLD_WORDS = {"hold", "don't", "wait", "save"}
    card_refs: dict[str, list[tuple[str, bool]]] = defaultdict(list)

    for r in rules:
        # Skip mulligan rules
        if r.get("layer") == "mulligan" or (r.get("phase") and "Mulligan" in r.get("phase", [])):
            continue
        action_lower = r.get("action", "").lower()
        is_hold = any(w in action_lower for w in _HOLD_WORDS)
        for zc in r.get("require", []):
            if zc.get("zone") != "hand":
                continue
            match = zc.get("match", {})
            names = match.get("name", [])
            if isinstance(names, str):
                names = [names]
            for name in names:
                card_refs[name].append((r["id"], is_hold))

    for card, refs in card_refs.items():
        hold_ids = [rid for rid, is_h in refs if is_h]
        use_ids = [rid for rid, is_h in refs if not is_h]
        if not hold_ids or not use_ids:
            continue
        # Only check use→hold direction (use suppresses hold, not vice versa)
        for uid in use_ids:
            existing = set(next(r for r in rules if r["id"] == uid).get("conflicts_with", []))
            missing = [hid for hid in hold_ids if hid not in existing]
            if missing:
                issues.append(f"CONFLICT: {uid} (use) missing conflicts_with {missing} for card '{card}'")

    # ── Check 2: Rules without any conditions ──
    for r in rules:
        has_conditions = any([
            r.get("phase"), r.get("my_turn") is not None, r.get("turn_min"),
            r.get("turn_max"), r.get("step"), r.get("life_below"),
            r.get("life_above"), r.get("opp_life_below"), r.get("mana_min"),
            r.get("hand_lands_min"), r.get("my_creatures_min"),
            r.get("opp_creatures_min"), r.get("opp_speed"),
            r.get("opp_has_must_answer"), r.get("require"),
        ])
        if not has_conditions:
            issues.append(f"SPAM: {r['id']} has no conditions — will fire every phase")

    # ── Check 3: Hold rules without use overrides ──
    rule_ids = {r["id"] for r in rules}
    for r in rules:
        if not any(w in r.get("action", "").lower() for w in _HOLD_WORDS):
            continue
        rid = r["id"]
        # Check if there's a corresponding _low_life or _topdeck override
        base = rid.replace("threat_hold_", "threat_use_").replace("situation_hold_", "situation_use_")
        has_low_life = f"{base}_low_life" in rule_ids or any(
            uid.endswith("_low_life") and rid in next(
                (rr for rr in rules if rr["id"] == uid), {}
            ).get("conflicts_with", [])
            for uid in rule_ids
        )
        has_topdeck = f"{base}_topdeck" in rule_ids or any(
            uid.endswith("_topdeck") and rid in next(
                (rr for rr in rules if rr["id"] == uid), {}
            ).get("conflicts_with", [])
            for uid in rule_ids
        )
        if not has_low_life and "threat_hold" in rid:
            issues.append(f"MISSING_OVERRIDE: {rid} has no _low_life override")
        if not has_topdeck and "threat_hold" in rid:
            issues.append(f"MISSING_OVERRIDE: {rid} has no _topdeck override")

    # ── Check 4: Duplicate IDs ──
    seen_ids: set[str] = set()
    for r in rules:
        if r["id"] in seen_ids:
            issues.append(f"DUPLICATE: {r['id']}")
        seen_ids.add(r["id"])

    # ── Check 5: Missing action_family ──
    missing_af = [
        r for r in rules
        if not r.get("action_family")
        and r.get("layer") != "mulligan"
        and not (r.get("phase") and "Mulligan" in r.get("phase", []))
    ]
    if missing_af:
        issues.append(
            f"ACTION_FAMILY: {len(missing_af)} rules missing action_family"
            + ("" if fix else " (use --fix to auto-populate)")
        )

    if fix and issues:
        print(f"Fixing {len(issues)} issues...", file=sys.stderr)
        data["rules"] = _validate_and_fix_rules(rules)

        # Auto-populate action_family on rules that lack it
        from .actions import infer_action_family
        for r in data["rules"]:
            if r.get("action_family"):
                continue
            if r.get("layer") == "mulligan":
                continue
            if r.get("phase") and "Mulligan" in r.get("phase", []):
                continue
            family = infer_action_family(r.get("action", ""), rule_tags=r.get("tags", []))
            r["action_family"] = family.value
            print(f"  Added action_family={family.value} to rule {r['id']}", file=sys.stderr)

        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"Written: {path}", file=sys.stderr)

    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate a strategy JSON file")
    parser.add_argument("path", type=Path, help="Strategy JSON file")
    parser.add_argument("--fix", action="store_true", help="Auto-fix issues")
    parser.add_argument("--dry-run", action="store_true", help="Show issues without fixing")
    args = parser.parse_args()

    issues = validate_strategy(args.path, fix=args.fix and not args.dry_run)

    if issues:
        print(f"\n{len(issues)} issues found in {args.path.name}:")
        for issue in issues:
            print(f"  {issue}")
        if not args.fix:
            print(f"\nRun with --fix to auto-fix conflict and phase issues.")
    else:
        print(f"No issues found in {args.path.name}")


if __name__ == "__main__":
    main()
