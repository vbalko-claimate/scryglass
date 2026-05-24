"""Deterministic post-game match reports built from stored events."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import re

from .advisor_engine import _quick_danger, _quick_summary, _threat_category
from .database import card_cache, get_connection, get_match_data_for_summary, get_match_timeline

REQUEST_LABELS = {
    "GREMessageType_DeclareAttackersReq": "attack decision",
    "GREMessageType_DeclareBlockersReq": "block decision",
    "GREMessageType_SelectTargetsReq": "targeting decision",
    "GREMessageType_MulliganReq": "mulligan",
    "GREMessageType_ChooseStartingPlayerReq": "play/draw choice",
}


def get_latest_completed_match_id() -> str | None:
    """Return the newest finished match id, if any."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT match_id
        FROM matches
        WHERE result IN ('Win', 'Loss', 'Draw')
        ORDER BY started_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def build_match_report(match_id: str) -> tuple[str, str]:
    """Build a readable markdown report for a completed match."""
    data = get_match_data_for_summary(match_id)
    timeline = get_match_timeline(match_id)
    if not data or not timeline:
        raise ValueError(f"Match {match_id} not found")

    match = data["match"]
    events = data["events"]
    threat_profiles = _collect_opponent_profiles(events)
    growth_notes = _collect_growth_notes(events)
    filename = _report_filename(match)

    lines = [
        "# MTGA Match Report",
        "",
        f"- Match ID: `{match['match_id']}`",
        f"- Started: {match.get('started_at') or 'Unknown'}",
        f"- Result: {match.get('result') or 'Unknown'}",
        f"- My deck: {match.get('my_deck_name') or 'Unknown'}",
        f"- Opponent: {match.get('opponent_name') or 'Unknown'}",
        f"- Opponent deck: {match.get('opp_deck_name') or 'Unknown'}",
        f"- Games: {match.get('game_count') or 0}",
        "",
        "## Opening",
    ]

    mulligans = timeline.get("mulligans", [])
    if mulligans:
        for item in mulligans:
            hand = ", ".join(item.get("hand", [])[:7]) or "unknown hand"
            lines.append(
                f"- Game {item['game']}: {item['decision']} at {item['hand_size']} cards"
                f" | hand: {hand}"
            )
    else:
        lines.append("- No mulligan records captured.")

    lines.extend([
        "",
        "## Opponent Plan And Must-Answer Cards",
    ])
    if threat_profiles:
        for item in threat_profiles[:5]:
            label_parts = [item["category"]]
            if item["danger"] >= 4:
                label_parts.append("must-answer")
            if item["growth_count"]:
                label_parts.append(f"scaled {item['growth_count']}x")
            lines.append(
                f"- {item['name']} [{', '.join(label_parts)}]"
                f": {item['summary']} | first seen T{item['first_turn']}"
            )
    else:
        lines.append("- No strong opponent engine or must-answer permanent was detected from events.")

    if growth_notes:
        lines.extend(["", "## Board Growth / Snowball Signals"])
        for note in growth_notes[:6]:
            lines.append(f"- {note}")

    lines.extend(["", "## Turn Timeline"])
    for game in timeline.get("games", []):
        lines.append("")
        lines.append(f"### Game {game['game']}")
        opening = [m for m in mulligans if m["game"] == game["game"]]
        if opening:
            last_opening = opening[-1]
            lines.append(
                f"- Opening hand kept at {last_opening['hand_size']} cards:"
                f" {', '.join(last_opening.get('hand', [])[:7])}"
            )
        for turn in game.get("turns", []):
            if turn.get("turn", 0) <= 0:
                continue
            summary = _render_turn(turn)
            if summary:
                lines.append(f"- T{turn['turn']}: {summary}")

    decision_lines = _collect_decision_notes(timeline)
    lines.extend(["", "## Key Decision Windows"])
    if decision_lines:
        lines.extend(f"- {line}" for line in decision_lines[:10])
    else:
        lines.append("- No decision snapshots were captured.")

    lines.extend(["", "## Final Snapshot"])
    end_lines = _final_snapshot_lines(timeline)
    lines.extend(f"- {line}" for line in end_lines)
    lines.append("")

    return filename, "\n".join(lines)


def _report_filename(match: dict) -> str:
    started = match.get("started_at") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    stamp = started.replace(":", "-").replace(" ", "_")
    my_deck = _slug(match.get("my_deck_name") or "my-deck")
    opp_deck = _slug(match.get("opp_deck_name") or match.get("opponent_name") or "opponent")
    return f"mtga-report_{stamp}_{my_deck}_vs_{opp_deck}_{match['match_id'][:8]}.md"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "unknown"


def _collect_opponent_profiles(events: list[dict]) -> list[dict]:
    profiles: dict[str, dict] = {}
    growth = Counter()

    for event in events:
        data = event.get("data", {})
        if event["type"] == "opp_card_played":
            name = data.get("name")
            if not name:
                continue
            profile = profiles.setdefault(name, {
                "name": name,
                "count": 0,
                "first_turn": event.get("turn") or 0,
                "grp_id": data.get("grp_id"),
                "types": data.get("card_types", []),
            })
            profile["count"] += 1
            if event.get("turn"):
                profile["first_turn"] = min(profile["first_turn"], event["turn"])
            if not profile.get("grp_id") and data.get("grp_id"):
                profile["grp_id"] = data["grp_id"]
            if not profile.get("types") and data.get("card_types"):
                profile["types"] = data["card_types"]
        elif event["type"] == "permanent_stats_changed":
            if data.get("controller") == "opp" and data.get("name"):
                growth[data["name"]] += 1

    result = []
    for name, profile in profiles.items():
        card = card_cache.get(profile.get("grp_id")) if profile.get("grp_id") else None
        category = _profile_category(card, profile.get("types", []), growth[name])
        danger = _profile_danger(card, category, growth[name])
        summary = _profile_summary(card, category, growth[name], profile["count"])
        if category != "engine" and danger < 4:
            continue
        result.append({
            "name": name,
            "category": category,
            "danger": danger,
            "summary": summary,
            "first_turn": profile["first_turn"] or 0,
            "count": profile["count"],
            "growth_count": growth[name],
        })

    result.sort(
        key=lambda item: (
            item["category"] == "engine",
            item["danger"],
            item["growth_count"],
            item["count"],
        ),
        reverse=True,
    )
    return result


def _profile_category(card, types: list[str], growth_count: int) -> str:
    if card:
        return _threat_category(card)
    if "Planeswalker" in types or "Enchantment" in types or "Artifact" in types:
        return "engine"
    if growth_count:
        return "body"
    return "support"


def _profile_danger(card, category: str, growth_count: int) -> int:
    if card:
        return max(_quick_danger(card), 4 if growth_count else 1)
    if category == "engine":
        return 4
    if growth_count >= 2:
        return 4
    if growth_count == 1:
        return 3
    return 2


def _profile_summary(card, category: str, growth_count: int, count: int) -> str:
    if card:
        summary = _quick_summary(card)
    elif category == "engine":
        summary = "engine permanent observed multiple times"
    elif growth_count:
        summary = "board body that kept scaling on battlefield"
    else:
        summary = "support permanent"
    if growth_count:
        summary += f"; on-board growth seen {growth_count} time(s)"
    elif count > 1:
        summary += f"; appeared {count} time(s)"
    return summary


def _collect_growth_notes(events: list[dict]) -> list[str]:
    growth_notes: dict[str, dict] = {}
    for event in events:
        if event["type"] != "permanent_stats_changed":
            continue
        data = event.get("data", {})
        if data.get("controller") != "opp" or not data.get("name"):
            continue
        note = growth_notes.setdefault(data["name"], {
            "count": 0,
            "from": f"{data.get('old_power', 0)}/{data.get('old_toughness', 0)}",
            "to": f"{data.get('new_power', 0)}/{data.get('new_toughness', 0)}",
        })
        note["count"] += 1
        note["to"] = f"{data.get('new_power', 0)}/{data.get('new_toughness', 0)}"

    ordered = sorted(growth_notes.items(), key=lambda item: item[1]["count"], reverse=True)
    return [
        f"{name}: {info['from']} -> {info['to']} ({info['count']} growth events)"
        for name, info in ordered
    ]


def _render_turn(turn: dict) -> str:
    bits: list[str] = []
    snapshot = turn.get("board_snapshot")
    if snapshot:
        start_bits = []
        if turn.get("mana") is not None:
            start_bits.append(f"mana {turn['mana']}")
        if turn.get("lands") is not None:
            start_bits.append(f"lands {turn['lands']}")
        if snapshot.get("my_life") is not None or snapshot.get("opp_life") is not None:
            start_bits.append(f"life {snapshot.get('my_life', '?')}-{snapshot.get('opp_life', '?')}")
        if snapshot.get("my_hand_size") is not None:
            start_bits.append(f"hand {snapshot.get('my_hand_size', 0)}")
        if start_bits:
            bits.append("start " + ", ".join(start_bits))

    if turn.get("my_plays"):
        bits.append("you played " + _format_named(turn["my_plays"], with_types=True))
    if turn.get("opp_plays"):
        bits.append("opp played " + _format_named(turn["opp_plays"], with_types=True))
    if turn.get("enchantments"):
        bits.append("attachments " + ", ".join(
            f"{a.get('aura', '?')} -> {a.get('target', '?')}" for a in turn["enchantments"][:3]
        ))
    if turn.get("permanent_changes"):
        bits.append("growth " + ", ".join(
            f"{c.get('name', '?')} {c.get('old_power', 0)}/{c.get('old_toughness', 0)}"
            f"->{c.get('new_power', 0)}/{c.get('new_toughness', 0)}"
            for c in turn["permanent_changes"][:3]
        ))
    if turn.get("removals"):
        bits.append("removed " + ", ".join(
            _format_removal(r) for r in turn["removals"][:3]
        ))
    if turn.get("attacks"):
        bits.append("you attacked with " + _format_named(turn["attacks"], with_stats=True))
    if turn.get("opp_attacks"):
        bits.append("opp attacked with " + _format_named(turn["opp_attacks"], with_stats=True))
    if turn.get("life_changes"):
        bits.append("life " + ", ".join(
            f"{item.get('player', '?')} {item.get('old', '?')}->{item.get('new', '?')}"
            for item in turn["life_changes"][:4]
        ))
    return " | ".join(bits)


def _collect_decision_notes(timeline: dict) -> list[str]:
    notes: list[str] = []
    for game in timeline.get("games", []):
        for turn in game.get("turns", []):
            for decision in turn.get("decision_points", []):
                request = REQUEST_LABELS.get(
                    decision.get("request_type", ""),
                    decision.get("request_type", "decision"),
                )
                actions = _format_actions(decision.get("legal_actions", []))
                hand = _format_hand(decision.get("my_hand", []))
                notes.append(
                    f"Game {game['game']} T{turn['turn']}: {request}"
                    f" | hand: {hand or 'unknown'}"
                    f" | legal: {actions or 'unknown'}"
                )
    return notes


def _final_snapshot_lines(timeline: dict) -> list[str]:
    lines: list[str] = []
    if timeline.get("game_ends"):
        last_end = timeline["game_ends"][-1]
        lines.append(
            f"Game {last_end['game']} ended with life {last_end.get('my_life', '?')}"
            f" to {last_end.get('opp_life', '?')} | reason: {last_end.get('reason') or 'unknown'}"
        )

    last_snapshot = None
    for game in reversed(timeline.get("games", [])):
        for turn in reversed(game.get("turns", [])):
            if turn.get("board_snapshot"):
                last_snapshot = turn["board_snapshot"]
                break
        if last_snapshot:
            break

    if not last_snapshot:
        return lines or ["No final board snapshot available."]

    lines.append("Your battlefield: " + _format_hand(last_snapshot.get("my_battlefield", []), limit=8))
    lines.append("Opponent battlefield: " + _format_hand(last_snapshot.get("opp_battlefield", []), limit=8))
    lines.append("Your hand: " + _format_hand(last_snapshot.get("my_hand", []), limit=8))
    if last_snapshot.get("stack"):
        lines.append("Stack: " + _format_hand(last_snapshot.get("stack", []), limit=6))
    return lines


def _format_named(items: list[dict], with_types: bool = False, with_stats: bool = False) -> str:
    result = []
    for item in items[:4]:
        label = item.get("name", "?")
        if with_stats and item.get("power") is not None and item.get("toughness") is not None:
            label += f" {item.get('power', 0)}/{item.get('toughness', 0)}"
        elif with_types and item.get("types"):
            label += f" ({'/'.join(item['types'][:2])})"
        result.append(label)
    return ", ".join(result)


def _format_hand(cards: list[dict], limit: int = 6) -> str:
    formatted = []
    for card in cards[:limit]:
        name = card.get("name", "?")
        power = card.get("power")
        toughness = card.get("toughness")
        if power is not None and toughness is not None and (power or toughness):
            name += f" {power}/{toughness}"
        formatted.append(name)
    if len(cards) > limit:
        formatted.append(f"... +{len(cards) - limit} more")
    return ", ".join(formatted)


def _format_actions(actions: list[dict], limit: int = 6) -> str:
    names = []
    seen = set()
    for action in actions:
        label = action.get("name") or action.get("action_type", "")
        if not label or label in seen:
            continue
        seen.add(label)
        names.append(label)
        if len(names) >= limit:
            break
    return ", ".join(names)


def _format_removal(item: dict) -> str:
    label = f"{item.get('name', '?')} ({item.get('destination', 'removed')})"
    if item.get("caused_by"):
        label += f" by {item['caused_by']}"
    return label
