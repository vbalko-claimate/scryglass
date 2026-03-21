"""Export frozen decision states from telemetry DB into replay corpus files.

The corpus captures engine decisions at specific game states so that future
engine changes can be validated against a known baseline (regression detection).

Usage:
    uv run python -m advisor.replay_corpus --export [--min-states 20] [--max-states 100]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

from .database import get_connection, card_cache, DB_PATH
from .regression_tests import WHITE_LIFEGAIN_SCENARIOS, RED_GOBLINS_SCENARIOS, Scenario
from .strategy import load_strategy, evaluate_rules_v2, OpponentTracker, MetaDeck
from .regression_tests import _build_state
from .version import ENGINE_VERSION

log = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).parent.parent / "data" / "replay_corpus"


def _slugify(name: str) -> str:
    return name.replace(" ", "_").replace("/", "_").replace("'", "").lower()


# ─── DB Export ──────────────────────────────────────────────────

def _export_from_db(max_states: int = 100) -> dict[str, list[dict]]:
    """Query match_events for decision_eval + decision_context pairs."""
    if not DB_PATH.exists():
        return {}

    conn = get_connection()
    cur = conn.cursor()

    # Get decision_eval events that have rule-based top_advice
    cur.execute("""
        SELECT match_id, game_number, turn_number, phase, data
        FROM match_events
        WHERE event_type = 'decision_eval'
        ORDER BY id
    """)
    evals = cur.fetchall()

    if not evals:
        conn.close()
        return {}

    # Build lookup for decision_context events
    cur.execute("""
        SELECT match_id, game_number, turn_number, phase, data
        FROM match_events
        WHERE event_type = 'decision_context'
        ORDER BY id
    """)
    context_rows = cur.fetchall()
    conn.close()

    # Index contexts by (match_id, game_number, turn_number, phase)
    ctx_map: dict[tuple, dict] = {}
    for match_id, gn, turn, phase, data_str in context_rows:
        try:
            ctx_map[(match_id, gn, turn, phase)] = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue

    corpus: dict[str, list[dict]] = defaultdict(list)

    for match_id, gn, turn, phase, data_str in evals:
        try:
            eval_data = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue

        top_advice = eval_data.get("top_advice", [])
        strategy_name = eval_data.get("strategy_name", "")
        if not strategy_name or not top_advice:
            continue

        # Filter to rule-based advice only
        expected = [
            {
                "rule_id": a["rule_id"],
                "action_family": a.get("action_family", ""),
                "priority": a["priority"],
                "source": a["source"],
            }
            for a in top_advice
            if a.get("rule_id")
        ]
        if not expected:
            continue

        # Find matching context
        key = (match_id, gn, turn, phase)
        context = ctx_map.get(key, {})

        entry = {
            "state": {
                "turn": turn,
                "phase": phase,
                "my_life": context.get("my_life", 20),
                "opp_life": context.get("opp_life", 20),
                "hand": context.get("hand", []),
                "my_battlefield": context.get("my_battlefield", []),
                "opp_battlefield": context.get("opp_battlefield", []),
            },
            "strategy_name": strategy_name,
            "opp_deck": eval_data.get("opp_deck"),
            "expected": expected,
            "_engine_version": eval_data.get("engine_version", ENGINE_VERSION),
        }

        slug = _slugify(strategy_name)
        if len(corpus[slug]) < max_states:
            corpus[slug].append(entry)

    return dict(corpus)


# ─── Synthetic Fallback ────────────────────────────────────────

def _synthetic_from_scenarios(scenarios: list[Scenario], max_states: int = 100) -> dict[str, list[dict]]:
    """Generate corpus entries by running regression scenarios through the engine."""
    corpus: dict[str, list[dict]] = defaultdict(list)

    for sc in scenarios:
        strategy = load_strategy(sc.deck)
        if not strategy:
            log.warning("Strategy not found for synthetic corpus: %s", sc.deck)
            continue

        state = _build_state(sc)
        tracker = OpponentTracker()
        tracker.identified_deck = MetaDeck(
            name="test_opp", archetype="unknown", speed=sc.opp_speed)
        tracker.confidence = 1.0

        hits, _ = evaluate_rules_v2(
            strategy.rules, state, opp_tracker=tracker,
            vulnerabilities=strategy.vulnerabilities, max_results=0)

        expected = []
        for h in hits:
            expected.append({
                "rule_id": h.rule_id,
                "action_family": h.action_scores[0].family.value if h.action_scores else "",
                "priority": h.priority,
                "source": "strategy",
            })

        if not expected:
            continue

        entry = {
            "state": {
                "turn": sc.turn,
                "phase": sc.phase,
                "my_life": sc.my_life,
                "opp_life": sc.opp_life,
                "hand": sc.hand,
                "my_battlefield": sc.my_battlefield,
                "opp_battlefield": sc.opp_battlefield,
            },
            "strategy_name": sc.deck,
            "opp_deck": None,
            "expected": expected,
            "_engine_version": ENGINE_VERSION,
        }

        slug = _slugify(sc.deck)
        if len(corpus[slug]) < max_states:
            corpus[slug].append(entry)

    return dict(corpus)


# ─── Write ──────────────────────────────────────────────────────

def _write_corpus(corpus: dict[str, list[dict]], label: str) -> int:
    """Write corpus files to disk. Returns number of files written."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for slug, entries in corpus.items():
        path = CORPUS_DIR / f"{slug}.json"
        path.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
        print(f"  {label}: {path.name} ({len(entries)} states)")
        written += 1
    return written


def export(min_states: int = 20, max_states: int = 100) -> None:
    """Export corpus from DB, with synthetic fallback."""
    print("Exporting replay corpus...")

    # Try DB first
    db_corpus = _export_from_db(max_states=max_states)
    db_total = sum(len(v) for v in db_corpus.values())

    if db_total >= min_states:
        written = _write_corpus(db_corpus, "db")
        print(f"\nExported {db_total} states across {written} files from DB.")
    else:
        if db_total > 0:
            print(f"  DB has {db_total} states (< {min_states} minimum), supplementing with synthetic.")
        else:
            print("  No decision_eval data in DB, generating synthetic corpus.")

    # Always generate synthetic corpus from regression scenarios
    all_scenarios = WHITE_LIFEGAIN_SCENARIOS + RED_GOBLINS_SCENARIOS
    synth_corpus = _synthetic_from_scenarios(all_scenarios, max_states=max_states)
    synth_total = sum(len(v) for v in synth_corpus.values())

    if synth_total > 0:
        # Merge: synthetic fills in decks not covered by DB
        merged = dict(db_corpus)
        for slug, entries in synth_corpus.items():
            if slug not in merged:
                merged[slug] = entries
            else:
                # Add synthetic entries up to max_states
                room = max_states - len(merged[slug])
                if room > 0:
                    merged[slug].extend(entries[:room])

        written = _write_corpus(merged, "merged")
        total = sum(len(v) for v in merged.values())
        print(f"\nTotal: {total} states across {written} corpus files.")
    elif db_total > 0:
        _write_corpus(db_corpus, "db")
        print(f"\nTotal: {db_total} states (DB only, no regression scenarios matched).")
    else:
        print("\nNo corpus generated. Add regression scenarios or play games to populate DB.")
        sys.exit(1)


def rebaseline() -> None:
    """Re-run current engine on existing corpus and update expected output."""
    from .replay_diff import _build_state_from_entry, _extract_rule_ids
    from .strategy import load_strategy, evaluate_rules_v2, OpponentTracker, MetaDeck

    corpus_dir = Path(__file__).parent.parent / "data" / "replay_corpus"
    if not corpus_dir.exists():
        print("No corpus directory found. Run --export first.")
        sys.exit(1)

    card_cache.load()
    total_updated = 0

    for path in sorted(corpus_dir.glob("*.json")):
        entries = json.loads(path.read_text())
        updated = 0
        for entry in entries:
            strategy_name = entry.get("strategy_name", "")
            strategy = load_strategy(strategy_name)
            if not strategy:
                continue

            state = _build_state_from_entry(entry)
            if state is None:
                continue

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
            except Exception:
                continue

            new_expected = []
            for h in hits[:5]:
                exp = {"rule_id": h.rule_id, "priority": h.priority, "source": "strategy"}
                if h.action_scores:
                    exp["action_family"] = h.action_scores[0].family.value
                new_expected.append(exp)

            entry["expected"] = new_expected
            entry["_engine_version"] = ENGINE_VERSION
            updated += 1

        path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
        print(f"  Rebaselined {path.name}: {updated}/{len(entries)} entries updated")
        total_updated += updated

    print(f"\nTotal: {total_updated} entries rebaselined to engine {ENGINE_VERSION}")


def main():
    parser = argparse.ArgumentParser(description="Export frozen replay corpus")
    parser.add_argument("--export", action="store_true", help="Export corpus from DB + synthetic")
    parser.add_argument("--rebaseline", action="store_true", help="Re-run engine on existing corpus, update expected")
    parser.add_argument("--min-states", type=int, default=20, help="Minimum states before DB-only export")
    parser.add_argument("--max-states", type=int, default=100, help="Max states per deck")
    args = parser.parse_args()

    if args.rebaseline:
        rebaseline()
        return

    if not args.export:
        parser.print_help()
        print("\nUse --export to generate corpus files, or --rebaseline to update expected output.")
        sys.exit(0)

    export(min_states=args.min_states, max_states=args.max_states)


if __name__ == "__main__":
    main()
