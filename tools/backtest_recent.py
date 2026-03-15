#!/usr/bin/env python3
"""Replay recent MTGA matches from Player.log and backtest the current LLM advisor.

Example:
    python tools/backtest_recent.py --last 4 --max-checkpoints 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import advisor.advisor_engine as advisor_engine_mod
import advisor.database as database_mod
import advisor.game_state as game_state_mod
import advisor.llm_advisor as llm_advisor_mod
from advisor.advisor_engine import AdvisorEngine
from advisor.database import DB_PATH, card_cache, init_db
from advisor.game_state import GameStateTracker
from advisor.log_parser import (
    HEADER_RE,
    IMPORTANT_TYPES,
    extract_gre_messages,
    parse_message,
)


REQUEST_TYPES = {
    "GREMessageType_MulliganReq",
    "GREMessageType_ChooseStartingPlayerReq",
    "GREMessageType_ActionsAvailableReq",
    "GREMessageType_DeclareAttackersReq",
    "GREMessageType_DeclareBlockersReq",
    "GREMessageType_SelectTargetsReq",
}


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def noop(*_args, **_kwargs):
    return None


def disable_writes():
    """Prevent replay/backtest from mutating the main DB."""
    database_mod.save_match = noop
    database_mod.save_match_event = noop
    database_mod.save_advice = noop
    game_state_mod.save_match = noop
    game_state_mod.save_match_event = noop
    advisor_engine_mod.save_match = noop
    advisor_engine_mod.save_match_event = noop
    advisor_engine_mod.save_advice = noop
    advisor_engine_mod._save_advice_batch = noop
    llm_advisor_mod.reset_sessions()


def load_recent_matches(limit: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT match_id, started_at, opponent_name, my_deck_name, opp_deck_name, result "
        "FROM matches ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def normalize_timestamp(raw: Any) -> float | None:
    """Normalize Arena timestamps to unix seconds.

    Incoming GRE payloads use unix milliseconds.
    Outgoing client payloads use .NET ticks.
    """
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    if value > 100_000_000_000_000_000:
        return (value - 621355968000000000) / 10_000_000
    if value > 1_000_000_000_000:
        return value / 1000.0
    return float(value)


def usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    if not rows:
        return totals
    for field in TOKEN_FIELDS:
        values = [r.get(field) for r in rows if isinstance(r.get(field), (int, float))]
        if values:
            totals[field] = int(sum(values))
    cost_values = [r.get("total_cost_usd") for r in rows if isinstance(r.get("total_cost_usd"), (int, float))]
    if cost_values:
        totals["total_cost_usd"] = round(sum(cost_values), 6)
    duration_values = [r.get("duration_ms") for r in rows if isinstance(r.get("duration_ms"), (int, float))]
    if duration_values:
        totals["duration_ms"] = int(sum(duration_values))
    totals["calls"] = len(rows)
    effective = 0
    for field in TOKEN_FIELDS:
        effective += totals.get(field, 0)
    if effective:
        totals["effective_total_tokens"] = effective
    return totals


def iter_messages_from_file(path: Path):
    """Stream-parse Player.log without reading the entire file into memory."""
    header_line: str | None = None
    json_lines: list[str] = []
    brace_count = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n\r")

            if header_line is None:
                m = HEADER_RE.match(line)
                if not m:
                    continue
                if m.group(5) not in IMPORTANT_TYPES:
                    continue
                header_line = line
                json_lines = []
                brace_count = 0
                continue

            if not json_lines:
                if line.startswith("{"):
                    json_lines.append(line)
                    brace_count = line.count("{") - line.count("}")
                    if brace_count <= 0:
                        parsed = parse_message(header_line, json_lines)
                        if parsed:
                            yield parsed
                        header_line = None
                        json_lines = []
                    continue

                # Missing JSON payload, reset and treat this line as a fresh header if possible.
                header_line = None
                m = HEADER_RE.match(line)
                if m and m.group(5) in IMPORTANT_TYPES:
                    header_line = line
                continue

            json_lines.append(line)
            brace_count += line.count("{") - line.count("}")
            if brace_count <= 0:
                parsed = parse_message(header_line, json_lines)
                if parsed:
                    yield parsed
                header_line = None
                json_lines = []


def object_name_for_action(state, action_data: dict) -> str:
    instance_id = action_data.get("instanceId")
    grp_id = action_data.get("grpId", 0)
    obj = state.objects.get(instance_id) if instance_id else None
    if obj and obj.grp_id:
        grp_id = obj.grp_id
    card = card_cache.get(grp_id) if grp_id else None
    if card:
        return card.name
    if obj and obj.name:
        return obj.name
    return ""


def summarize_response(inner: dict, state, checkpoint: dict) -> dict[str, Any]:
    msg_type = inner.get("type", "")

    if msg_type == "ClientMessageType_MulliganResp":
        decision = inner.get("mulliganResp", {}).get("decision", "")
        label = "Keep" if "Accept" in decision else "Mulligan"
        return {"label": label, "names": [label.lower()], "kind": "mulligan"}

    if msg_type == "ClientMessageType_ChooseStartingPlayerResp":
        payload = inner.get("chooseStartingPlayerResp", {})
        team_type = payload.get("teamType", "")
        label = "Play" if "Starting" in team_type or "Play" in team_type else team_type or "Choose"
        return {"label": label, "names": [label.lower()], "kind": "choose_start"}

    if msg_type == "ClientMessageType_PerformActionResp":
        actions = inner.get("performActionResp", {}).get("actions", [])
        if not actions:
            return {"label": "Pass", "names": ["pass"], "kind": "pass"}
        action = actions[0]
        action_type = action.get("actionType", "").replace("ActionType_", "")
        name = object_name_for_action(state, action)
        if action_type == "Pass":
            return {"label": "Pass", "names": ["pass"], "kind": "pass"}
        if action_type == "Play":
            return {"label": f"Play {name}".strip(), "names": [name.lower()], "kind": "play"}
        if action_type == "Cast":
            return {"label": f"Cast {name}".strip(), "names": [name.lower()], "kind": "cast"}
        if action_type.startswith("Activate"):
            label = f"{action_type} {name}".strip()
            return {"label": label, "names": [name.lower()] if name else [], "kind": "activate"}
        return {"label": action_type or "PerformAction", "names": [name.lower()] if name else [], "kind": "action"}

    if msg_type == "ClientMessageType_DeclareAttackersResp":
        req = checkpoint.get("request", {})
        attackers = req.get("declareAttackersReq", {}).get("qualifiedAttackers", [])
        names = []
        for entry in attackers:
            iid = entry.get("attackerInstanceId")
            obj = state.objects.get(iid)
            card = card_cache.get(obj.grp_id) if obj else None
            if card:
                names.append(card.name)
        if inner.get("declareAttackersResp", {}).get("autoDeclare"):
            return {
                "label": "Attack with all legal attackers",
                "names": [n.lower() for n in names],
                "kind": "attack",
            }
        return {"label": "Declare attackers", "names": [n.lower() for n in names], "kind": "attack"}

    if msg_type == "ClientMessageType_DeclareBlockersResp":
        selected = inner.get("declareBlockersResp", {}).get("selectedBlockers", [])
        if not selected:
            return {"label": "No blocks", "names": ["no blocks"], "kind": "block"}
        labels = []
        names = []
        for block in selected:
            blocker_iid = block.get("blockerInstanceId")
            blocker_obj = state.objects.get(blocker_iid)
            blocker_card = card_cache.get(blocker_obj.grp_id) if blocker_obj else None
            blocker_name = blocker_card.name if blocker_card else "?"
            attacker_names = []
            for attacker_iid in block.get("selectedAttackerInstanceIds") or block.get("attackerInstanceIds", []):
                attacker_obj = state.objects.get(attacker_iid)
                attacker_card = card_cache.get(attacker_obj.grp_id) if attacker_obj else None
                if attacker_card:
                    attacker_names.append(attacker_card.name)
            labels.append(f"{blocker_name} -> {', '.join(attacker_names) or '?'}")
            names.append(blocker_name.lower())
            names.extend(a.lower() for a in attacker_names)
        return {"label": "; ".join(labels), "names": names, "kind": "block"}

    if msg_type in {"ClientMessageType_SelectTargetsResp", "ClientMessageType_SubmitTargetsReq"}:
        return {"label": "Select targets", "names": [], "kind": "target"}

    return {"label": msg_type, "names": [], "kind": "other"}


def response_matches_checkpoint(checkpoint: dict, response_type: str) -> bool:
    req = checkpoint["request_type"]
    mapping = {
        "GREMessageType_MulliganReq": {"ClientMessageType_MulliganResp"},
        "GREMessageType_ChooseStartingPlayerReq": {"ClientMessageType_ChooseStartingPlayerResp"},
        "GREMessageType_ActionsAvailableReq": {
            "ClientMessageType_PerformActionResp",
            "ClientMessageType_PerformAutoTapActionsResp",
        },
        "GREMessageType_DeclareAttackersReq": {
            "ClientMessageType_DeclareAttackersResp",
            "ClientMessageType_SubmitAttackersReq",
        },
        "GREMessageType_DeclareBlockersReq": {
            "ClientMessageType_DeclareBlockersResp",
            "ClientMessageType_SubmitBlockersReq",
        },
        "GREMessageType_SelectTargetsReq": {
            "ClientMessageType_SelectTargetsResp",
            "ClientMessageType_SubmitTargetsReq",
        },
    }
    return response_type in mapping.get(req, set())


def score_checkpoint(advice_text: str, actual: dict[str, Any]) -> bool | None:
    if not advice_text or not actual:
        return None
    text = advice_text.lower()
    kind = actual.get("kind")

    if kind == "mulligan":
        if "keep" in text:
            return actual["label"].lower() == "keep"
        if "mulligan" in text:
            return actual["label"].lower() == "mulligan"
        return None

    if kind == "choose_start":
        if "play" in text:
            return True
        if "draw" in text:
            return actual["label"].lower() == "draw"
        return None

    if kind == "pass":
        return "pass" in text or "hold" in text

    names = [n for n in actual.get("names", []) if n]
    if not names:
        return None

    if kind == "attack" and "attack" not in text:
        return False
    if kind == "block" and "block" not in text:
        return False

    return any(name in text for name in names)


async def run_backtest(args) -> dict[str, Any]:
    init_db()
    card_cache.load()
    disable_writes()

    llm_advisor_mod.MIN_INTERVAL = 0.0
    llm_advisor_mod._last_call_state_id = -1
    llm_advisor_mod._last_call_time = 0.0
    llm_advisor_mod.set_backend(args.backend)

    matches = load_recent_matches(args.last)
    if not matches:
        raise SystemExit("No matches found in advisor.db")

    wanted_ids = {m["match_id"] for m in matches}
    per_match_limit = defaultdict(int)
    reports: dict[str, dict[str, Any]] = {
        m["match_id"]: {
            **m,
            "checkpoints": [],
        }
        for m in matches
    }

    tracker = GameStateTracker()
    advisor = AdvisorEngine()
    advisor.set_auto_llm(False)
    advisor.on_advice = noop
    advisor.on_strategy_info = noop
    advisor.on_threat_update = noop
    tracker.on_match_start = advisor.on_match_start
    tracker.on_match_end = None
    tracker.on_stack_observed = advisor.on_stack_observed
    tracker.on_my_card_played = noop

    active_checkpoint: dict[str, dict[str, Any]] = {}

    for msg in iter_messages_from_file(Path(args.log)):
        tracker.process_message(msg)
        current_match_id = tracker.state.match_info.match_id

        if current_match_id in wanted_ids:
            await advisor.on_state_change(tracker.state)

        if msg["type"] == "GreToClientEvent":
            if current_match_id not in wanted_ids:
                continue
            gre_messages = extract_gre_messages(msg)
            for gre_msg in gre_messages:
                req_type = gre_msg.get("type", "")
                if req_type not in REQUEST_TYPES:
                    continue
                if per_match_limit[current_match_id] >= args.max_checkpoints:
                    continue

                checkpoint = {
                    "match_id": current_match_id,
                    "game_number": tracker.state.match_info.game_number,
                    "turn": tracker.state.turn_info.turn_number,
                    "phase": tracker.state.turn_info.phase_display,
                    "state_id": tracker.state.game_state_id,
                    "request_type": req_type,
                    "request": gre_msg.get("gre_msg", gre_msg),
                    "request_ts": normalize_timestamp(msg.get("payload", {}).get("timestamp")),
                }

                llm_start = time.monotonic()
                llm_advisor_mod._last_call_state_id = -1
                llm_advisor_mod._last_call_time = 0.0
                advice = await llm_advisor_mod.get_advice(
                    tracker.state,
                    req_type,
                    context=advisor._build_llm_context(tracker.state),
                )
                usage = llm_advisor_mod.consume_last_usage()
                llm_elapsed = time.monotonic() - llm_start
                checkpoint["advice"] = advice.message if advice else ""
                checkpoint["actual"] = None
                checkpoint["score"] = None
                checkpoint["llm_elapsed_s"] = round(llm_elapsed, 3)
                checkpoint["usage"] = usage
                reports[current_match_id]["checkpoints"].append(checkpoint)
                active_checkpoint[current_match_id] = checkpoint
                per_match_limit[current_match_id] += 1

        elif msg["type"] == "ClientToGremessage":
            if current_match_id not in wanted_ids:
                continue
            checkpoint = active_checkpoint.get(current_match_id)
            if not checkpoint or checkpoint.get("actual") is not None:
                continue
            inner = msg.get("payload", {}).get("payload", {})
            response_type = inner.get("type", "")
            if not response_matches_checkpoint(checkpoint, response_type):
                continue
            actual = summarize_response(inner, tracker.state, checkpoint)
            checkpoint["actual"] = actual
            checkpoint["score"] = score_checkpoint(checkpoint["advice"], actual)
            response_ts = normalize_timestamp(msg.get("payload", {}).get("timestamp"))
            checkpoint["response_ts"] = response_ts
            if checkpoint.get("request_ts") is not None and response_ts is not None:
                checkpoint["human_elapsed_s"] = round(response_ts - checkpoint["request_ts"], 3)
            else:
                checkpoint["human_elapsed_s"] = None
            active_checkpoint.pop(current_match_id, None)

    total = 0
    scored = 0
    matched = 0
    by_request = Counter()
    human_times = []
    llm_times = []
    faster = 0
    slower = 0
    for report in reports.values():
        usage_rows = [cp["usage"] for cp in report["checkpoints"] if cp.get("usage")]
        report["token_totals"] = usage_totals(usage_rows)
        for cp in report["checkpoints"]:
            total += 1
            by_request[cp["request_type"]] += 1
            if cp["score"] is not None:
                scored += 1
                matched += int(bool(cp["score"]))
            if cp.get("human_elapsed_s") is not None:
                human_times.append(cp["human_elapsed_s"])
            if cp.get("llm_elapsed_s") is not None:
                llm_times.append(cp["llm_elapsed_s"])
            if cp.get("human_elapsed_s") is not None and cp.get("llm_elapsed_s") is not None:
                if cp["llm_elapsed_s"] < cp["human_elapsed_s"]:
                    faster += 1
                else:
                    slower += 1

    all_usage = [
        cp["usage"]
        for report in reports.values()
        for cp in report["checkpoints"]
        if cp.get("usage")
    ]
    token_summary = usage_totals(all_usage)

    return {
        "matches": matches,
        "reports": reports,
        "summary": {
            "total_checkpoints": total,
            "scored_checkpoints": scored,
            "matched_checkpoints": matched,
            "match_rate": (matched / scored) if scored else None,
            "by_request": dict(by_request),
            "backend": args.backend,
            "log": str(args.log),
            "avg_human_s": round(sum(human_times) / len(human_times), 3) if human_times else None,
            "avg_llm_s": round(sum(llm_times) / len(llm_times), 3) if llm_times else None,
            "llm_faster_count": faster,
            "llm_slower_count": slower,
            "token_usage": token_summary,
        },
    }


def print_report(data: dict[str, Any]):
    summary = data["summary"]
    print(
        f"Backtest: {summary['total_checkpoints']} checkpoints "
        f"across {len(data['reports'])} matches | backend={summary['backend']}"
    )
    if summary["scored_checkpoints"]:
        rate = summary["match_rate"] * 100 if summary["match_rate"] is not None else 0
        print(
            f"Scored: {summary['scored_checkpoints']} | "
            f"Advice matched actual action: {summary['matched_checkpoints']} ({rate:.0f}%)"
        )
    if summary.get("avg_human_s") is not None and summary.get("avg_llm_s") is not None:
        print(
            f"Average timing | you: {summary['avg_human_s']:.2f}s | "
            f"LLM: {summary['avg_llm_s']:.2f}s | "
            f"LLM faster/slower: {summary['llm_faster_count']}/{summary['llm_slower_count']}"
        )
    if summary.get("token_usage"):
        usage = summary["token_usage"]
        print(
            "Token usage | "
            f"calls={usage.get('calls', 0)} | "
            f"in={usage.get('input_tokens', 0)} | "
            f"out={usage.get('output_tokens', 0)} | "
            f"cache_create={usage.get('cache_creation_input_tokens', 0)} | "
            f"cache_read={usage.get('cache_read_input_tokens', 0)} | "
            f"effective_total={usage.get('effective_total_tokens', 0)}"
        )
        if usage.get("total_cost_usd") is not None:
            print(f"Estimated billed cost: ${usage['total_cost_usd']:.4f}")
    print(f"Log source: {summary['log']}")
    print()

    for match_id, report in data["reports"].items():
        title = (
            f"{report['started_at']} | {report['my_deck_name'] or '?'} vs "
            f"{report['opp_deck_name'] or report['opponent_name'] or '?'} | "
            f"{report['result'] or '?'} | {match_id[:8]}"
        )
        print(title)
        print("-" * len(title))
        if not report["checkpoints"]:
            print("  No checkpoints captured.")
            print()
            continue
        for cp in report["checkpoints"]:
            actual = cp["actual"]["label"] if cp.get("actual") else "(no response captured)"
            score = (
                "match" if cp["score"] is True
                else "miss" if cp["score"] is False
                else "n/a"
            )
            print(
                f"  T{cp['turn']} | {cp['phase']} | {cp['request_type'].replace('GREMessageType_', '')}"
            )
            print(f"    advice: {cp['advice'] or '(none)'}")
            print(f"    actual: {actual}")
            print(f"    score : {score}")
            print(
                "    time  : "
                f"you={cp.get('human_elapsed_s', 'n/a')}s | "
                f"llm={cp.get('llm_elapsed_s', 'n/a')}s"
            )
            if cp.get("usage"):
                usage = cp["usage"]
                print(
                    "    usage : "
                    f"in={usage.get('input_tokens', 0)} | "
                    f"out={usage.get('output_tokens', 0)} | "
                    f"cache_create={usage.get('cache_creation_input_tokens', 0)} | "
                    f"cache_read={usage.get('cache_read_input_tokens', 0)}"
                )
        if report.get("token_totals"):
            usage = report["token_totals"]
            print(
                "  Token totals: "
                f"in={usage.get('input_tokens', 0)} | "
                f"out={usage.get('output_tokens', 0)} | "
                f"cache_create={usage.get('cache_creation_input_tokens', 0)} | "
                f"cache_read={usage.get('cache_read_input_tokens', 0)} | "
                f"effective_total={usage.get('effective_total_tokens', 0)}"
            )
        print()

    if summary["by_request"]:
        print("By request type:")
        for key, count in sorted(summary["by_request"].items()):
            print(f"  {key.replace('GREMessageType_', '')}: {count}")


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest the current LLM advisor on recent MTGA logs")
    parser.add_argument("--last", type=int, default=4, help="How many recent matches to replay")
    parser.add_argument(
        "--max-checkpoints",
        type=int,
        default=4,
        help="Maximum LLM-evaluated checkpoints per match",
    )
    parser.add_argument(
        "--backend",
        default="claude_cli",
        choices=["claude_cli", "ollama", "anthropic_api"],
        help="LLM backend to use",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path.home() / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log",
        help="Player.log path to replay",
    )
    parser.add_argument("--json-out", type=Path, help="Optional JSON report path")
    return parser.parse_args()


async def amain():
    logging.basicConfig(level=logging.WARNING)
    args = parse_args()
    data = await run_backtest(args)
    if args.json_out:
        args.json_out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print_report(data)


if __name__ == "__main__":
    asyncio.run(amain())
