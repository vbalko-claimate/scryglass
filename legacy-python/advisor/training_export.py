"""Export candidate-centric training data from telemetry DB.

Each JSONL row = one candidate action per decision point.
Multiple rows share the same decision_id when a decision had multiple candidates.

Usage:
    uv run python -m advisor.training_export [--output PATH] [--min-candidates 2]
"""
from __future__ import annotations

import argparse, json, sys
from collections import defaultdict
from pathlib import Path

from .database import get_connection, DB_PATH

from .database import PERSISTENT_DIR
DEFAULT_OUTPUT = PERSISTENT_DIR / "training" / "reranker_v1.jsonl"


def _join_key(row: dict) -> tuple:
    """Build join key: prefer decision_id, fall back to composite."""
    did = row.get("decision_id")
    if did:
        return ("did", did)
    return ("comp", row.get("match_id"), row.get("game_number"),
            row.get("turn_number"), row.get("phase"))


def _count_type(cards: list[dict], ctype: str) -> int:
    n = 0
    for c in cards:
        for k in ("types", "card_types"):
            if ctype in (c.get(k) or []):
                n += 1; break
    return n


def _count_untapped_lands(cards: list[dict]) -> int:
    n = 0
    for c in cards:
        for k in ("types", "card_types"):
            if "Land" in (c.get(k) or []):
                if not c.get("tapped", False):
                    n += 1
                break
    return n


def _extract_state(ctx: dict, turn: int, phase: str) -> dict:
    my_bf, opp_bf = ctx.get("my_battlefield") or [], ctx.get("opp_battlefield") or []
    hand = ctx.get("my_hand") or []
    return {"turn": turn, "phase": phase,
            "my_life": ctx.get("my_life", 20), "opp_life": ctx.get("opp_life", 20),
            "hand_size": ctx.get("my_hand_size") or len(hand),
            "board_creature_count": _count_type(my_bf, "Creature"),
            "opp_creature_count": _count_type(opp_bf, "Creature"),
            "mana_available": _count_untapped_lands(my_bf)}


def _load_events(conn) -> dict[str, list[tuple]]:
    events: dict[str, list[tuple]] = defaultdict(list)
    for et in ("decision_eval", "decision_context", "decision_outcome", "advice_compliance"):
        cur = conn.execute(
            "SELECT match_id, game_number, turn_number, phase, data "
            "FROM match_events WHERE event_type = ? ORDER BY id", (et,))
        events[et] = cur.fetchall()
    return events


def _index_by_key(rows: list[tuple]) -> dict[tuple, dict]:
    idx: dict[tuple, dict] = {}
    for match_id, gn, turn, phase, data_str in rows:
        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue
        data.update(match_id=match_id, game_number=gn, turn_number=turn, phase=phase)
        key = _join_key(data)
        if key not in idx:
            idx[key] = data
    return idx


def _lookup(idx: dict, key: tuple, match_id, gn, turn, phase) -> dict:
    return idx.get(key) or idx.get(("comp", match_id, gn, turn, phase)) or {}


def _build_compliance_turn_index(idx: dict) -> dict[tuple, dict]:
    """Build secondary index keyed by (match_id, game_number, turn) for O(1) fallback."""
    turn_idx: dict[tuple, dict] = {}
    for key, data in idx.items():
        if key[0] == "comp":
            turn_key = (key[1], key[2], key[3])  # match_id, gn, turn
            if turn_key not in turn_idx:
                turn_idx[turn_key] = data
    return turn_idx


def _lookup_compliance(idx: dict, turn_idx: dict, key: tuple, match_id, gn, turn) -> dict:
    """Compliance has phase='play' (hardcoded), so fallback ignores phase."""
    return idx.get(key) or turn_idx.get((match_id, gn, turn)) or {}


def export(output: Path, min_candidates: int = 2) -> None:
    if not DB_PATH.exists():
        print("No database found at", DB_PATH); sys.exit(1)

    conn = get_connection()
    events = _load_events(conn)
    results = dict(conn.execute("SELECT match_id, result FROM matches").fetchall())
    conn.close()

    ctx_idx = _index_by_key(events["decision_context"])
    out_idx = _index_by_key(events["decision_outcome"])
    comp_idx = _index_by_key(events["advice_compliance"])
    comp_turn_idx = _build_compliance_turn_index(comp_idx)

    seen, seen_spots, rows_written, total_dec, chosen_count = set(), set(), 0, 0, 0
    source_counts: dict[str, int] = defaultdict(int)
    output.parent.mkdir(parents=True, exist_ok=True)
    fh = output.open("w", encoding="utf-8")

    for match_id, gn, turn, phase, data_str in events["decision_eval"]:
        try:
            ev = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue

        ver = ev.get("engine_version", "")
        # Accept phase1_v1 (old) and 2.x.x (new) engine versions
        if "phase1" not in ver and not ver.startswith("2."):
            continue

        top_advice = [a for a in (ev.get("top_advice") or []) if a.get("rule_id")]
        if len(top_advice) < min_candidates:
            continue

        ev.update(match_id=match_id, game_number=gn, turn_number=turn, phase=phase)
        decision_id = ev.get("decision_id") or f"{match_id}_{gn}_{turn}_{phase}"
        if decision_id in seen:
            continue
        seen.add(decision_id)

        # Dedup near-duplicates by spot_key (same turn+phase+spot)
        spot_key = ev.get("spot_key", "")
        if spot_key and spot_key in seen_spots:
            continue
        if spot_key:
            seen_spots.add(spot_key)

        # Try full context first, fall back to mini_ctx from eval
        key = _join_key(ev)
        ctx = _lookup(ctx_idx, key, match_id, gn, turn, phase)
        mini_ctx = ev.get("mini_ctx")
        if not ctx and not mini_ctx:
            continue

        if ctx:
            state = _extract_state(ctx, turn, phase)
        else:
            # Use mini_ctx directly — same schema as build_state_dict output
            state = dict(mini_ctx)
        outcome_data = _lookup(out_idx, key, match_id, gn, turn, phase)
        compliance = _lookup_compliance(comp_idx, comp_turn_idx, key, match_id, gn, turn)
        played = (compliance.get("played") or "").lower()
        outcome = {"life_delta": outcome_data.get("life_delta", 0),
                   "opp_life_delta": outcome_data.get("opp_life_delta", 0),
                   "creature_delta": outcome_data.get("creature_delta", 0)} if outcome_data else {}

        total_dec += 1
        chosen_assigned = False  # only one chosen per decision
        for rank, adv in enumerate(top_advice):
            target = (adv.get("action_target") or adv.get("card") or "").lower()
            chosen = bool(not chosen_assigned and played and target and played in target)
            if chosen:
                chosen_count += 1
                chosen_assigned = True
            src = adv.get("source", "strategy")
            source_counts[src] += 1
            row = {"decision_id": decision_id, "source": "live", "engine_version": ver,
                   "state": state,
                   "candidate": {"rank": rank, "rule_id": adv["rule_id"],
                                 "action_family": adv.get("action_family", ""),
                                 "score": adv.get("action_score", adv.get("score", 0.0)),
                                 "priority": adv.get("priority", "medium"), "source": src},
                   "chosen": chosen, "outcome": outcome,
                   "match_result": results.get(match_id, ""),
                   "deck": ev.get("strategy_name", ""), "opp_deck": ev.get("opp_deck", "")}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows_written += 1

    fh.close()
    print(f"Exported to {output}")
    print(f"  Decisions:  {total_dec}")
    print(f"  Candidates: {rows_written}")
    print(f"  Chosen:     {chosen_count}")
    print(f"  Sources:    {dict(source_counts)}")
    if rows_written == 0:
        print("\nNo qualifying rows. Need decision_eval events with engine_version 'phase1_v1' or '2.x'.")


def main():
    p = argparse.ArgumentParser(description="Export candidate-centric training data (reranker JSONL)")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"Output JSONL path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--min-candidates", type=int, default=2,
                   help="Skip decisions with fewer candidates (default: 2)")
    a = p.parse_args()
    export(a.output, a.min_candidates)


if __name__ == "__main__":
    main()
