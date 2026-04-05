#!/usr/bin/env python3
"""Export conservative SFT data for MTGA action selection.

The current logs are strongest for non-combat play decisions:
- `decision_context` captures the authoritative board snapshot and legal actions
- `card_played` captures many actual spell plays
- `advice_compliance` tells us whether the recommendation was followed

This script turns those events into:
1. raw JSONL examples for curation / future transforms
2. chat-style SFT JSONL examples for instruction tuning

It intentionally filters to the subset we can label with reasonable confidence.

The exporter can also enrich examples with:
- local card knowledge from the `cards` table
- optional Scryfall oracle bulk cache for missing cards
- opponent meta profiles from `meta_decks.json`
- deck strategy metadata from local strategy JSON files
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
from advisor.database import DB_PATH as DEFAULT_DB
DEFAULT_OUT = ROOT / "data" / "training" / "llama31_action_sft"
DEFAULT_META_DECKS = ROOT / "data" / "meta" / "meta_decks.json"
DEFAULT_SCRYFALL_ORACLE = ROOT / "data" / "training" / "cache" / "scryfall-oracle-cards.json"
DEFAULT_STRATEGY_DIRS = [
    ROOT / "data" / "strategies",
    ROOT.parent / "mtg-data" / "strategies",
]

ALLOWED_REQUESTS = {"GREMessageType_ActionsAvailableReq"}
ALLOWED_PHASES = {"Phase_Main1", "Phase_Main2"}
ALLOWED_ACTION_TYPES = {"ActionType_Cast", "ActionType_Play"}
TRAIN_ACTION_TYPES = {"ActionType_Cast", "ActionType_Play", "ActionType_Pass"}
KEYWORDS = [
    "flying",
    "first strike",
    "double strike",
    "deathtouch",
    "defender",
    "flash",
    "haste",
    "hexproof",
    "indestructible",
    "lifelink",
    "menace",
    "reach",
    "trample",
    "vigilance",
    "ward",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to advisor.db")
    parser.add_argument(
        "--out-prefix",
        type=Path,
        default=DEFAULT_OUT,
        help="Output prefix without extension; writes .raw.jsonl and .chat.jsonl",
    )
    parser.add_argument(
        "--min-quality",
        choices=("low", "medium", "high"),
        default="medium",
        help="Minimum example quality to export",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of exported examples (0 = all)",
    )
    parser.add_argument(
        "--enrich",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Add card knowledge and meta context to exported examples",
    )
    parser.add_argument(
        "--meta-decks",
        type=Path,
        default=DEFAULT_META_DECKS,
        help="Path to meta_decks.json used for opponent archetype enrichment",
    )
    parser.add_argument(
        "--strategy-dir",
        action="append",
        default=[],
        help="Additional strategy directory (can be repeated)",
    )
    parser.add_argument(
        "--scryfall-oracle-cache",
        type=Path,
        default=DEFAULT_SCRYFALL_ORACLE,
        help="Optional local cache for Scryfall oracle bulk data",
    )
    parser.add_argument(
        "--fetch-scryfall-oracle",
        action="store_true",
        help="Fetch official Scryfall oracle bulk data into the cache path if missing",
    )
    return parser.parse_args()


def quality_rank(name: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}[name]


def normalize_name(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def extract_keywords(*texts: str) -> list[str]:
    haystack = " ".join(text.lower() for text in texts if text)
    return [keyword for keyword in KEYWORDS if keyword in haystack]


def legal_action_text(action: dict[str, Any]) -> str:
    action_type = action.get("action_type", "")
    name = action.get("name") or ""
    if action_type == "ActionType_Cast":
        return f"Cast {name}"
    if action_type == "ActionType_Play":
        return f"Play {name}"
    if action_type == "ActionType_Pass":
        return "Pass"
    return name or action_type


def _coerce_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _register_card_alias(target: dict[str, dict[str, Any]], name: str, card: dict[str, Any]) -> None:
    key = normalize_name(name)
    if not key or key in target:
        return
    target[key] = card


def fetch_scryfall_oracle_cache(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen("https://api.scryfall.com/bulk-data/oracle-cards") as handle:
        meta = json.load(handle)
    download_uri = meta["download_uri"]
    with urlopen(download_uri) as response, path.open("wb") as out:
        shutil.copyfileobj(response, out)
    return path


def load_card_knowledge(
    conn: sqlite3.Connection,
    scryfall_oracle_cache: Path,
    fetch_scryfall_oracle: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    stats = {
        "cards_db_rows": 0,
        "scryfall_rows": 0,
        "scryfall_cache_used": False,
        "scryfall_cache_path": str(scryfall_oracle_cache),
    }

    rows = conn.execute(
        """
        SELECT
            name, mana_cost, cmc, colors, card_types, subtypes, power, toughness,
            rarity, expansion, abilities, oracle_text, source
        FROM cards
        """
    ).fetchall()
    for row in rows:
        abilities = _coerce_json_list(row["abilities"])
        oracle_text = row["oracle_text"] or ""
        card = {
            "name": row["name"],
            "mana_cost": row["mana_cost"] or "",
            "cmc": row["cmc"] or 0,
            "colors": _coerce_json_list(row["colors"]),
            "card_types": _coerce_json_list(row["card_types"]),
            "subtypes": _coerce_json_list(row["subtypes"]),
            "power": row["power"] or "",
            "toughness": row["toughness"] or "",
            "rarity": row["rarity"] or "",
            "expansion": row["expansion"] or "",
            "abilities": abilities,
            "oracle_text": oracle_text,
            "keywords": extract_keywords(oracle_text, *abilities),
            "source": row["source"] or "mtga_db",
        }
        _register_card_alias(lookup, row["name"], card)
        stats["cards_db_rows"] += 1

    if fetch_scryfall_oracle and not scryfall_oracle_cache.exists():
        fetch_scryfall_oracle_cache(scryfall_oracle_cache)

    if not scryfall_oracle_cache.exists():
        return lookup, stats

    stats["scryfall_cache_used"] = True
    for raw_card in json.loads(scryfall_oracle_cache.read_text(encoding="utf-8")):
        name = raw_card.get("name") or ""
        if not name:
            continue
        oracle_text = raw_card.get("oracle_text") or ""
        card = {
            "name": name,
            "mana_cost": raw_card.get("mana_cost") or "",
            "cmc": raw_card.get("cmc") or 0,
            "colors": raw_card.get("colors") or [],
            "card_types": [],
            "subtypes": [],
            "power": raw_card.get("power") or "",
            "toughness": raw_card.get("toughness") or "",
            "rarity": raw_card.get("rarity") or "",
            "expansion": raw_card.get("set") or "",
            "abilities": [],
            "oracle_text": oracle_text,
            "keywords": list(raw_card.get("keywords") or extract_keywords(oracle_text)),
            "source": "scryfall_oracle",
        }
        type_line = raw_card.get("type_line") or ""
        if type_line:
            main = type_line.split("—", 1)[0].strip()
            card["card_types"] = [part for part in main.split() if part]
        _register_card_alias(lookup, name, card)
        for face in raw_card.get("card_faces") or []:
            face_name = face.get("name") or ""
            if not face_name:
                continue
            face_card = {
                **card,
                "name": face_name,
                "mana_cost": face.get("mana_cost") or card["mana_cost"],
                "oracle_text": face.get("oracle_text") or card["oracle_text"],
                "keywords": list(face.get("keywords") or card["keywords"]),
                "power": face.get("power") or card["power"],
                "toughness": face.get("toughness") or card["toughness"],
            }
            _register_card_alias(lookup, face_name, face_card)
        stats["scryfall_rows"] += 1

    return lookup, stats


def load_meta_profiles(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = _load_json(path)
    profiles: dict[str, dict[str, Any]] = {}
    for entry in data.get("meta_decks", []):
        key = normalize_name(entry.get("name", ""))
        if not key:
            continue
        profiles[key] = {
            "name": entry.get("name", ""),
            "archetype": entry.get("archetype", ""),
            "colors": entry.get("colors", []),
            "speed": entry.get("speed", ""),
            "typical_kill_turn": entry.get("typical_kill_turn"),
            "hidden_reach": entry.get("hidden_reach"),
            "signal_cards": entry.get("signal_cards", {}),
            "key_threats": entry.get("key_threats", []),
            "description": entry.get("description", ""),
        }
    return profiles


def load_strategy_profiles(extra_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    seen: set[Path] = set()
    for directory in [*DEFAULT_STRATEGY_DIRS, *extra_dirs]:
        if directory in seen or not directory.exists():
            continue
        seen.add(directory)
        for path in sorted(directory.glob("*.json")):
            try:
                data = _load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            name = data.get("name", "")
            key = normalize_name(name)
            if not key or key in profiles:
                continue
            profiles[key] = {
                "name": name,
                "archetype": data.get("archetype", ""),
                "colors": data.get("colors", []),
                "deck_signature": data.get("deck_signature", []),
                "vulnerabilities": data.get("vulnerabilities", []),
                "path": str(path),
            }
    return profiles


def build_resources(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    resources: dict[str, Any] = {
        "enabled": args.enrich,
        "card_lookup": {},
        "strategy_profiles": {},
        "meta_profiles": {},
        "missing_card_names": set(),
        "stats": {
            "strategies_loaded": 0,
            "meta_profiles_loaded": 0,
            "cards_db_rows": 0,
            "scryfall_rows": 0,
            "scryfall_cache_used": False,
            "scryfall_cache_path": str(args.scryfall_oracle_cache),
        },
    }
    if not args.enrich:
        return resources

    extra_dirs = [Path(path) for path in args.strategy_dir]
    card_lookup, card_stats = load_card_knowledge(
        conn,
        scryfall_oracle_cache=args.scryfall_oracle_cache,
        fetch_scryfall_oracle=args.fetch_scryfall_oracle,
    )
    resources["card_lookup"] = card_lookup
    resources["strategy_profiles"] = load_strategy_profiles(extra_dirs)
    resources["meta_profiles"] = load_meta_profiles(args.meta_decks)
    resources["stats"].update(card_stats)
    resources["stats"]["strategies_loaded"] = len(resources["strategy_profiles"])
    resources["stats"]["meta_profiles_loaded"] = len(resources["meta_profiles"])
    return resources


def enrich_card(card: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(card)
    lookup = resources["card_lookup"].get(normalize_name(card.get("name", "")))
    if not lookup:
        name = card.get("name", "")
        if name:
            resources["missing_card_names"].add(name)
        return enriched

    if not enriched.get("mana_cost"):
        enriched["mana_cost"] = lookup.get("mana_cost", "")
    if not enriched.get("cmc"):
        enriched["cmc"] = lookup.get("cmc", 0)
    if not enriched.get("types"):
        enriched["types"] = lookup.get("card_types", [])

    enriched["colors"] = lookup.get("colors", [])
    enriched["card_types"] = lookup.get("card_types", [])
    enriched["subtypes"] = lookup.get("subtypes", [])
    enriched["power"] = enriched.get("power") or lookup.get("power", "")
    enriched["toughness"] = enriched.get("toughness") or lookup.get("toughness", "")
    enriched["rarity"] = lookup.get("rarity", "")
    enriched["expansion"] = lookup.get("expansion", "")
    enriched["abilities"] = lookup.get("abilities", [])
    enriched["oracle_text"] = lookup.get("oracle_text", "")
    enriched["keywords"] = lookup.get("keywords", [])
    enriched["knowledge_source"] = lookup.get("source", "")
    return enriched


def enrich_state(state: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    if not resources["enabled"]:
        return state
    enriched = dict(state)
    for zone in ("my_hand", "my_battlefield", "opp_battlefield", "stack"):
        enriched[zone] = [enrich_card(card, resources) for card in state.get(zone, [])]
    return enriched


def deck_meta_context(my_deck: str, opp_deck: str, resources: dict[str, Any]) -> dict[str, Any]:
    if not resources["enabled"]:
        return {}
    my_profile = resources["strategy_profiles"].get(normalize_name(my_deck), {})
    opp_profile = resources["meta_profiles"].get(normalize_name(opp_deck), {})
    return {
        "my_strategy": my_profile,
        "opp_meta": opp_profile,
        "watch_for": [
            vulnerability.get("card", "")
            for vulnerability in my_profile.get("vulnerabilities", [])[:4]
            if vulnerability.get("card")
        ],
        "must_answer": [
            threat.get("card", "")
            for threat in opp_profile.get("key_threats", [])
            if threat.get("must_answer")
        ][:4],
    }


def compact_card(card: dict[str, Any]) -> str:
    name = card.get("name", "?")
    type_list = card.get("types", []) or card.get("card_types", [])
    types = "/".join(type_list)
    pt = ""
    if "Creature" in type_list:
        pt = f" {card.get('power', 0)}/{card.get('toughness', 0)}"
    flags: list[str] = []
    if card.get("tapped"):
        flags.append("tapped")
    if card.get("summoning_sick"):
        flags.append("summoning_sick")
    keywords = card.get("keywords") or []
    if keywords:
        flags.append(",".join(keywords[:2]))
    flag_text = f" [{' '.join(flags)}]" if flags else ""
    type_text = f" <{types}>" if types else ""
    return f"{name}{pt}{type_text}{flag_text}"


def render_chat_prompt(example: dict[str, Any]) -> list[dict[str, str]]:
    state = example["state"]
    meta = example.get("meta_context", {})
    my_strategy = meta.get("my_strategy", {})
    opp_meta = meta.get("opp_meta", {})
    lines = [
        f"Deck: {example['my_deck'] or 'unknown'}",
        f"Opponent deck: {example['opp_deck'] or 'unknown'}",
        f"Turn {example['turn_number']} {example['phase_display']}",
        f"Life: you={state['my_life']} opp={state['opp_life']}",
    ]
    if my_strategy.get("archetype"):
        lines.append(f"My archetype: {my_strategy['archetype']}")
    if opp_meta.get("archetype") or opp_meta.get("speed"):
        bits = [bit for bit in [opp_meta.get("archetype"), opp_meta.get("speed")] if bit]
        lines.append(f"Opponent profile: {' / '.join(bits)}")
    if meta.get("must_answer"):
        lines.append(f"Must-answer threats: {', '.join(meta['must_answer'][:3])}")
    if meta.get("watch_for"):
        lines.append(f"Watch for: {', '.join(meta['watch_for'][:3])}")

    lines.extend([
        "",
        "Choose exactly one legal action for this turn.",
        "",
        "Your hand:",
    ])
    if state["my_hand"]:
        lines.extend(f"- {compact_card(card)}" for card in state["my_hand"])
    else:
        lines.append("- empty")

    lines.append("")
    lines.append("Your battlefield:")
    if state["my_battlefield"]:
        lines.extend(f"- {compact_card(card)}" for card in state["my_battlefield"])
    else:
        lines.append("- empty")

    lines.append("")
    lines.append("Opponent battlefield:")
    if state["opp_battlefield"]:
        lines.extend(f"- {compact_card(card)}" for card in state["opp_battlefield"])
    else:
        lines.append("- empty")

    if state["stack"]:
        lines.append("")
        lines.append("Stack:")
        lines.extend(f"- {compact_card(card)}" for card in state["stack"])

    lines.append("")
    lines.append("LEGAL ACTIONS:")
    for idx, action in enumerate(example["candidate_actions"]):
        lines.append(f"{idx}. {legal_action_text(action)}")

    lines.append("")
    lines.append(
        'Return strict JSON only: {"action_index": <int>, "action_text": "<exact legal action>"}'
    )

    assistant = json.dumps(
        {
            "action_index": example["label"]["action_index"],
            "action_text": example["label"]["action_text"],
        },
        ensure_ascii=False,
    )
    return [
        {
            "role": "system",
            "content": (
                "You are an MTG Arena tactical policy model. "
                "Choose exactly one legal action from LEGAL ACTIONS. "
                "Do not invent cards, targets, mana, or combat steps."
            ),
        },
        {"role": "user", "content": "\n".join(lines)},
        {"role": "assistant", "content": assistant},
    ]


def derive_quality(followed: bool, result: str) -> str:
    if followed and result == "Win":
        return "high"
    if followed or result == "Win":
        return "medium"
    return "low"


def is_context_eligible(ctx: dict[str, Any]) -> bool:
    if ctx.get("request_type") not in ALLOWED_REQUESTS:
        return False
    if ctx.get("phase") not in ALLOWED_PHASES:
        return False
    if ctx.get("active_player") != ctx.get("my_seat_id"):
        return False
    if ctx.get("decision_player") != ctx.get("my_seat_id"):
        return False
    return True


def find_label_action(legal_actions: list[dict[str, Any]], played: str) -> tuple[int, dict[str, Any]] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for idx, action in enumerate(legal_actions):
        if action.get("action_type") not in ALLOWED_ACTION_TYPES:
            continue
        if action.get("name") == played:
            candidates.append((idx, action))
    if not candidates:
        return None
    return candidates[0]


def candidate_actions(legal_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        action for action in legal_actions
        if action.get("action_type") in TRAIN_ACTION_TYPES
    ]


def fetch_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    query = """
        SELECT
            e.id,
            e.match_id,
            e.game_number,
            e.turn_number,
            e.phase,
            e.event_type,
            e.data,
            m.result,
            m.started_at,
            m.my_deck_name,
            m.opp_deck_name
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type IN ('decision_context', 'advice_compliance', 'card_played')
        ORDER BY e.match_id, e.id
    """
    return conn.execute(query).fetchall()


def fetch_compliance_map(conn: sqlite3.Connection) -> dict[tuple[str, int], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT match_id, turn_number, data
        FROM match_events
        WHERE event_type = 'advice_compliance'
        ORDER BY id
        """
    ).fetchall()
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        result[(row["match_id"], row["turn_number"])] = json.loads(row["data"] or "{}")
    return result


def build_examples(conn: sqlite3.Connection, min_quality: str, resources: dict[str, Any]) -> list[dict[str, Any]]:
    rows = fetch_rows(conn)
    compliance_by_turn = fetch_compliance_map(conn)
    examples: list[dict[str, Any]] = []
    pending_context: dict[str, Any] | None = None

    for row in rows:
        event_type = row["event_type"]
        payload = json.loads(row["data"] or "{}")

        if event_type == "decision_context":
            payload["phase"] = row["phase"]
            payload["turn_number"] = row["turn_number"]
            if is_context_eligible(payload):
                pending_context = {
                    "match_id": row["match_id"],
                    "game_number": row["game_number"],
                    "turn_number": row["turn_number"],
                    "phase": row["phase"],
                    "phase_display": payload.get("phase_display", row["phase"]),
                    "started_at": row["started_at"],
                    "result": row["result"] or "",
                    "my_deck": row["my_deck_name"] or "",
                    "opp_deck": row["opp_deck_name"] or "",
                    "state": payload,
                }
            continue

        if event_type != "card_played" or not pending_context:
            continue
        if row["match_id"] != pending_context["match_id"]:
            pending_context = None
            continue
        if row["turn_number"] != pending_context["turn_number"]:
            continue

        if payload.get("is_land"):
            continue

        played = payload.get("name") or ""
        if not played:
            continue

        state = pending_context["state"]
        legal_actions = state.get("legal_actions", [])
        train_actions = candidate_actions(legal_actions)
        label_match = find_label_action(train_actions, played)
        if not label_match:
            pending_context = None
            continue

        compliance = compliance_by_turn.get((row["match_id"], row["turn_number"]), {})
        followed = bool(compliance.get("followed")) and compliance.get("played") == played
        quality = derive_quality(followed, pending_context["result"])
        if quality_rank(quality) < quality_rank(min_quality):
            pending_context = None
            continue

        action_index, action = label_match
        meta_context = deck_meta_context(
            pending_context["my_deck"],
            pending_context["opp_deck"],
            resources,
        )
        enriched_state = enrich_state(
            {
                "request_type": state.get("request_type"),
                "my_life": state.get("my_life"),
                "opp_life": state.get("opp_life"),
                "my_hand_size": state.get("my_hand_size"),
                "opp_hand_size": state.get("opp_hand_size"),
                "my_hand": state.get("my_hand", []),
                "my_battlefield": state.get("my_battlefield", []),
                "opp_battlefield": state.get("opp_battlefield", []),
                "stack": state.get("stack", []),
            },
            resources,
        )
        example = {
            "example_id": f"{pending_context['match_id']}:{pending_context['turn_number']}:{pending_context['phase']}:{row['id']}",
            "task": "mtga_action_selection",
            "quality": quality,
            "label_source": "human_play",
            "followed_recommendation": followed,
            "match_result": pending_context["result"],
            "started_at": pending_context["started_at"],
            "match_id": pending_context["match_id"],
            "game_number": pending_context["game_number"],
            "turn_number": pending_context["turn_number"],
            "phase": pending_context["phase"],
            "phase_display": pending_context["phase_display"],
            "my_deck": pending_context["my_deck"],
            "opp_deck": pending_context["opp_deck"],
            "recommended_cards": compliance.get("recommended", []),
            "meta_context": meta_context,
            "state": enriched_state,
            "legal_actions": legal_actions,
            "candidate_actions": train_actions,
            "label": {
                "played_card": played,
                "action_index": action_index,
                "action_type": action.get("action_type"),
                "action_text": legal_action_text(action),
            },
        }
        examples.append(example)
        pending_context = None

    return examples


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()
    conn = sqlite3.connect(str(args.db), timeout=3)
    conn.row_factory = sqlite3.Row

    resources = build_resources(conn, args)
    examples = build_examples(conn, args.min_quality, resources)
    if args.limit > 0:
        examples = examples[: args.limit]

    raw_rows = examples
    chat_rows = [
        {
            "messages": render_chat_prompt(example),
            "metadata": {
                "example_id": example["example_id"],
                "quality": example["quality"],
                "match_result": example["match_result"],
                "my_deck": example["my_deck"],
                "opp_deck": example["opp_deck"],
                "my_archetype": example.get("meta_context", {}).get("my_strategy", {}).get("archetype", ""),
                "opp_archetype": example.get("meta_context", {}).get("opp_meta", {}).get("archetype", ""),
                "opp_speed": example.get("meta_context", {}).get("opp_meta", {}).get("speed", ""),
            },
        }
        for example in examples
    ]

    raw_path = args.out_prefix.with_suffix(".raw.jsonl")
    chat_path = args.out_prefix.with_suffix(".chat.jsonl")
    write_jsonl(raw_path, raw_rows)
    write_jsonl(chat_path, chat_rows)

    quality_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for row in raw_rows:
        quality_counts[row["quality"]] += 1

    summary = {
        "raw_path": str(raw_path),
        "chat_path": str(chat_path),
        "examples": len(raw_rows),
        "quality_counts": quality_counts,
        "enrichment": {
            "enabled": args.enrich,
            **resources["stats"],
            "missing_card_names": sorted(name for name in resources["missing_card_names"] if name)[:50],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
