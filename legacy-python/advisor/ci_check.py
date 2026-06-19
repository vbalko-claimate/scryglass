"""CI gate -- runs all test suites and blocks on regression.

Usage:
    uv run python -m advisor.ci_check [--strict] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .test_utils import REPLAY_PASS_THRESHOLD

CI_RESULT_PATH = Path(__file__).resolve().parent.parent / "data" / "ci_result.json"

# Gate thresholds
CANONICAL_THRESHOLD = 1.0       # 100% pass required
REGRESSION_THRESHOLD = 7        # absolute count (3 known failures)
REPLAY_THRESHOLD = REPLAY_PASS_THRESHOLD  # from shared constant


def _run_canonical_actions() -> dict:
    """Run canonical action tests, return {passed, total, ok}."""
    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, "-m", "advisor.test_canonical_actions"],
            capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).resolve().parent.parent,
        )
        # Parse "Results: N passed, M failed, T total" from output
        import re
        m = re.search(r"(\d+) passed, (\d+) failed", result.stdout)
        if m:
            passed_count = int(m.group(1))
            failed_count = int(m.group(2))
        else:
            return {"passed": 0, "total": 0, "ok": False, "error": "Could not parse test output"}
    except Exception as e:
        return {"passed": 0, "total": 0, "ok": False, "error": str(e)}

    total = passed_count + failed_count
    return {
        "passed": passed_count,
        "total": total,
        "ok": failed_count == 0,
    }


def _run_regression_tests() -> dict:
    """Run regression scenarios, return {passed, total, threshold, ok}."""
    try:
        from .regression_tests import (
            WHITE_LIFEGAIN_SCENARIOS, RED_GOBLINS_SCENARIOS, run_scenario,
        )
        from .database import card_cache
        card_cache.load()
    except Exception as e:
        return {"passed": 0, "total": 0, "threshold": REGRESSION_THRESHOLD,
                "ok": False, "error": str(e)}

    scenarios = WHITE_LIFEGAIN_SCENARIOS + RED_GOBLINS_SCENARIOS
    passed = 0
    for s in scenarios:
        ok, _ = run_scenario(s, verbose=False)
        if ok:
            passed += 1

    total = len(scenarios)
    return {
        "passed": passed,
        "total": total,
        "threshold": REGRESSION_THRESHOLD,
        "ok": passed >= REGRESSION_THRESHOLD,
    }


def _run_replay_diff() -> dict:
    """Run replay diff, return {top_1_agreement, threshold, ok}.

    Non-skippable: missing corpus or module = FAIL.
    """
    try:
        from .replay_diff import run_replay_diff  # noqa: F401
    except ImportError:
        return {"top_1_agreement": None, "threshold": REPLAY_THRESHOLD,
                "ok": False, "skipped": True, "reason": "replay_diff module not found"}
    except Exception as e:
        return {"top_1_agreement": None, "threshold": REPLAY_THRESHOLD,
                "ok": False, "skipped": True, "reason": str(e)}

    try:
        result = run_replay_diff()
        agreement = result.get("top_1_agreement")
        if agreement is None:
            return {"top_1_agreement": None, "threshold": REPLAY_THRESHOLD,
                    "ok": False, "skipped": True, "reason": "No replay corpus found"}
        return {
            "top_1_agreement": agreement,
            "flips": result.get("flips", 0),
            "total": result.get("total", 0),
            "threshold": REPLAY_THRESHOLD,
            "ok": agreement >= REPLAY_THRESHOLD,
        }
    except Exception as e:
        return {"top_1_agreement": None, "threshold": REPLAY_THRESHOLD,
                "ok": False, "skipped": True, "reason": str(e)}


def _run_schema_check() -> dict:
    """Verify all tracked strategies have current schema/engine version."""
    try:
        from .version import ENGINE_VERSION, SCHEMA_VERSION
        from .strategy import load_raw_strategy
    except ImportError as e:
        return {"ok": False, "error": str(e)}

    tracked = ["Mono White Lifegain", "Mono Red Goblins", "Rakdos Midrange"]
    mismatches = []
    for name in tracked:
        raw = load_raw_strategy(name)
        if not raw:
            continue
        sv = raw.get("_engine_version", "")
        ss = raw.get("_schema_version", "")
        if sv and sv != ENGINE_VERSION:
            mismatches.append(f"{name}: engine {sv} != {ENGINE_VERSION}")
        if ss and ss != SCHEMA_VERSION:
            mismatches.append(f"{name}: schema {ss} != {SCHEMA_VERSION}")

    return {"ok": len(mismatches) == 0, "mismatches": mismatches}


def _run_health_check(regression_result: dict) -> dict:
    """Derive health info from already-computed regression result (avoids double execution)."""
    passed = regression_result.get("passed", 0)
    total = regression_result.get("total", 0)
    rate = passed / max(1, total)
    return {"regression_pass_rate": round(rate, 2)}


def run_ci(strict: bool = False) -> dict:
    """Execute all gates and return structured result."""
    print("=== CI Gate Check ===\n")

    # 1. Canonical actions (importing the module runs the tests)
    canonical = _run_canonical_actions()
    ok_mark = "PASS" if canonical["ok"] else "FAIL"
    print(f"1. Canonical Actions: {canonical['passed']}/{canonical['total']} {ok_mark}")

    # 2. Regression tests
    regression = _run_regression_tests()
    ok_mark = "PASS" if regression["ok"] else "FAIL"
    print(f"2. Regression Tests: {regression['passed']}/{regression['total']} {ok_mark}"
          f" (threshold: {regression['threshold']})")

    # 3. Replay diff
    replay = _run_replay_diff()
    if replay.get("skipped"):
        print(f"3. Replay Diff: SKIPPED -- {replay.get('reason', 'unavailable')}")
    else:
        agr = replay["top_1_agreement"]
        ok_mark = "PASS" if replay["ok"] else "FAIL"
        print(f"3. Replay Diff: {agr:.0%} top-1 agreement {ok_mark}"
              f" (threshold: {REPLAY_THRESHOLD:.0%})")

    # 4. Schema version check
    schema = _run_schema_check()
    ok_mark = "PASS" if schema["ok"] else "FAIL"
    mismatches = schema.get("mismatches", [])
    print(f"4. Schema Check: {ok_mark}" + (f" ({len(mismatches)} mismatches)" if mismatches else ""))

    # 5. Health info (derived from regression result — no double execution)
    health = _run_health_check(regression)
    rate = health.get("regression_pass_rate")
    if rate is not None:
        print(f"5. Health Check: {rate:.0%} regression pass rate (informational)")
    else:
        print(f"5. Health Check: error -- {health.get('error', 'unknown')}")

    # Determine overall result
    gates = [canonical["ok"], regression["ok"], replay["ok"], schema["ok"]]
    all_pass = all(gates)
    gate_count = sum(gates)
    result_str = "PASS" if all_pass else "FAIL"

    print(f"\nRESULT: {result_str} ({gate_count}/{len(gates)} gates passed)")

    return {
        "timestamp": datetime.now().isoformat(),
        "gates": {
            "canonical_actions": canonical,
            "regression_tests": regression,
            "replay_diff": replay,
            "schema": schema,
        },
        "health_check": health,
        "result": result_str,
    }


def main():
    parser = argparse.ArgumentParser(description="CI gate check")
    parser.add_argument("--strict", action="store_true",
                        help="Future: stricter thresholds")
    parser.add_argument("--json", action="store_true",
                        help="Write results to data/ci_result.json")
    args = parser.parse_args()

    result = run_ci(strict=args.strict)

    if args.json:
        CI_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CI_RESULT_PATH.write_text(json.dumps(result, indent=2))
        print(f"\nResults written to {CI_RESULT_PATH}")

    sys.exit(0 if result["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
