"""Multi-backend LLM play advisor.

Backends (in priority order):
1. claude CLI — uses existing Claude Code subscription, no API costs
2. ollama — local LLM, free, fast (if installed)
3. anthropic API — pay-per-call fallback
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from .database import card_cache
from typing import Any

from .models import Advice, GameState

log = logging.getLogger(__name__)

# Debounce
_last_call_state_id = -1
_last_call_time = 0.0
_last_backend_usage: dict[str, Any] | None = None
MIN_INTERVAL = 3.0

# Active backend (auto-detected on first call)
_backend: str | None = None
SESSION_MAX_AGE_S = 60 * 60 * 3
SESSION_MAX_COUNT = 24
SESSION_BACKENDS = {"claude_cli"}
CLAUDE_SESSION_PROMPT = (
    "You are embedded inside an MTG Arena advisor app. "
    "Answer the game prompt directly. "
    "Do not ask to inspect files, run tools, or explain your environment."
)


@dataclass
class LlmSession:
    key: str
    backend: str
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_sessions: dict[str, LlmSession] = {}

SYSTEM_PROMPT = (
    "You are an expert MTG Arena coach. Give ONE SHORT actionable line. "
    "Format: 'Cast X' / 'Remove X with Y' / 'Attack with A, B' / 'Block X with Y'. "
    "No analysis or explanation unless asked. Just the best play. Under 45 words. "
    "IMPORTANT: Only suggest actions legal for the current phase. "
    "TAPPED creatures CANNOT attack or block. Only untapped creatures can. "
    "You can only attack during YOUR combat phase, block during OPPONENT'S combat. "
    "Use exact card names from LEGAL ACTIONS when possible. "
    "Never recommend a sequence that spends more mana than currently available. "
    "Only suggest multiple spells if the total mana clearly fits this turn. "
    "If the best line is to move to combat, say 'Go to combat, attack with ...' instead of bare 'Pass'. "
    "Avoid spending mana on Clue or other card draw if a stronger board, removal, or combat line exists. "
    "If the best action is to do nothing, say 'Pass'."
)


def _prune_sessions():
    now = time.time()
    expired = [
        key for key, session in _sessions.items()
        if now - session.last_used_at > SESSION_MAX_AGE_S
    ]
    for key in expired:
        _sessions.pop(key, None)

    if len(_sessions) <= SESSION_MAX_COUNT:
        return

    oldest = sorted(_sessions.values(), key=lambda s: s.last_used_at)
    for session in oldest[:-SESSION_MAX_COUNT]:
        _sessions.pop(session.key, None)


def _session_key_for_state(state: GameState) -> str | None:
    match_id = (state.match_info.match_id or "").strip()
    if not match_id:
        return None
    game_number = state.match_info.game_number or 1
    return f"{match_id}:g{game_number}"


def _get_or_create_session(key: str, backend: str) -> LlmSession:
    _prune_sessions()
    session = _sessions.get(key)
    if session and session.backend != backend:
        _sessions.pop(key, None)
        session = None
    if session is None:
        session = LlmSession(key=key, backend=backend)
        _sessions[key] = session
    session.last_used_at = time.time()
    return session


def reset_sessions(match_id: str | None = None):
    """Drop cached LLM conversations."""
    if not match_id:
        _sessions.clear()
        return
    prefix = f"{match_id}:"
    doomed = [key for key in _sessions if key.startswith(prefix)]
    for key in doomed:
        _sessions.pop(key, None)


def _has_active_session(key: str | None, backend: str) -> bool:
    if not key or backend not in SESSION_BACKENDS:
        return False
    session = _sessions.get(key)
    return bool(session and session.session_id)


def _is_session_resume_error(text: str) -> bool:
    lower = (text or "").lower()
    return (
        lower.startswith("cli error:")
        and "session" in lower
        and any(word in lower for word in ("resume", "already in use", "not found", "invalid"))
    )


def _detect_backend() -> str:
    """Auto-detect best available LLM backend."""
    # 1. Claude CLI
    if shutil.which("claude"):
        log.info("LLM backend: claude CLI (subscription)")
        return "claude_cli"

    # 2. Ollama
    if ollama_available():
        log.info("LLM backend: ollama (remote/local HTTP)")
        return "ollama"

    # 3. Anthropic API
    if os.environ.get("ANTHROPIC_API_KEY"):
        log.info("LLM backend: anthropic API")
        return "anthropic_api"

    log.warning("No LLM backend available")
    return "none"


def _ollama_base_url() -> str:
    base = os.environ.get("SCRY_OLLAMA_BASE_URL") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    base = base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base


def _ollama_model(default: str = "llama3.1:8b") -> str:
    return os.environ.get("SCRY_OLLAMA_MODEL") or os.environ.get("OLLAMA_MODEL") or default


def ollama_available() -> bool:
    return bool(
        os.environ.get("SCRY_OLLAMA_BASE_URL")
        or os.environ.get("OLLAMA_HOST")
        or shutil.which("ollama")
    )


def get_backend() -> str:
    global _backend
    if _backend is None:
        _backend = _detect_backend()
    return _backend


def set_backend(name: str):
    """Manually override LLM backend."""
    global _backend
    _backend = name
    log.info("LLM backend set to: %s", name)


def consume_last_usage() -> dict[str, Any] | None:
    """Return usage metadata for the most recent backend call."""
    global _last_backend_usage
    usage = _last_backend_usage
    _last_backend_usage = None
    return usage


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_card_name(obj, card) -> str:
    if card and card.name:
        return card.name
    return obj.name or f"Unknown({obj.grp_id})"


def _format_object_line(obj, card_cache_entry) -> str:
    name = _format_card_name(obj, card_cache_entry)
    parts = [name]

    if card_cache_entry and card_cache_entry.mana_cost:
        parts.append(card_cache_entry.mana_cost)

    type_line = card_cache_entry.type_line if card_cache_entry else ""
    if type_line:
        parts.append(f"[{type_line}]")

    if obj.is_creature:
        parts.append(f"{obj.power}/{obj.toughness}")

    states = []
    states.append("tapped" if obj.is_tapped else "untapped")
    if obj.has_summoning_sickness:
        states.append("summoning sick")
    if obj.attack_state:
        states.append(obj.attack_state.replace("AttackState_", "").lower())
    if obj.block_state:
        states.append(obj.block_state.replace("BlockState_", "").lower())
    if states:
        parts.append(f"({', '.join(states)})")

    oracle_text = ""
    if card_cache_entry:
        oracle_text = card_cache_entry.oracle_text or "; ".join(card_cache_entry.abilities[:2])
    if oracle_text:
        parts.append(f"- {_clip(oracle_text, 110)}")

    return " ".join(p for p in parts if p)


def _format_action(state: GameState, action) -> str:
    obj = state.objects.get(action.instance_id) if action.instance_id else None
    card = card_cache.get(obj.grp_id) if obj else None
    name = card.name if card else (obj.name if obj else "")
    action_type = action.action_type.replace("ActionType_", "")

    if action_type == "Cast":
        cost = ""
        if action.mana_cost:
            cost_parts = []
            for part in action.mana_cost:
                colors = "/".join(c.replace("ManaColor_", "") for c in part.get("color", []))
                count = part.get("count", 0)
                cost_parts.append(f"{count}{colors}" if colors else str(count))
            cost = f" cost={','.join(cost_parts)}"
        return f"Cast {name or 'spell'} [iid={action.instance_id}]{cost}"

    if action_type == "Play":
        return f"Play {name or 'card'} [iid={action.instance_id}]"

    if action_type == "Activate_Mana":
        return f"Activate mana from {name or 'permanent'} [iid={action.instance_id}]"

    if action_type == "Pass":
        return "Pass"

    if action_type == "FloatMana":
        return "Float mana"

    if action_type == "Attack":
        return f"Attack with {name or 'creature'} [iid={action.instance_id}]"

    if action_type == "Block":
        return f"Block with {name or 'creature'} [iid={action.instance_id}]"

    if name:
        return f"{action_type} {name} [iid={action.instance_id}]"
    return action_type


def _object_name(obj) -> str:
    card = card_cache.get(obj.grp_id)
    if card and card.name:
        return card.name
    return obj.name or f"Unknown({obj.grp_id})"


def _object_types(obj) -> set[str]:
    card = card_cache.get(obj.grp_id)
    raw_types = card.card_types if card else obj.card_types
    types: set[str] = set()
    for type_name in raw_types or []:
        if isinstance(type_name, str) and type_name.startswith("CardType_"):
            types.add(type_name.split("_", 1)[1])
        elif type_name:
            types.add(str(type_name))
    return types


def _target_hints_for_card(state: GameState, card_name: str) -> list[str]:
    my_creatures = state.my_creatures()
    opp_battlefield = state.opp_battlefield()
    stack = state.stack()

    if card_name == "Stasis Snare":
        targets = [_object_name(obj) for obj in opp_battlefield if "Creature" in _object_types(obj)]
        return [f"Stasis Snare legal targets: {', '.join(targets)}"] if targets else []

    if card_name == "Sheltered by Ghosts":
        hosts = [_object_name(obj) for obj in my_creatures]
        exile_targets = [
            _object_name(obj) for obj in opp_battlefield
            if "Land" not in _object_types(obj)
        ]
        hints: list[str] = []
        if hosts:
            hints.append(f"Sheltered by Ghosts host: {', '.join(hosts)}")
        if exile_targets:
            hints.append(f"Sheltered by Ghosts exile targets: {', '.join(exile_targets[:10])}")
        return hints

    if card_name == "Get Lost":
        targets = [
            _object_name(obj) for obj in opp_battlefield
            if _object_types(obj) & {"Creature", "Enchantment", "Planeswalker"}
        ]
        return [f"Get Lost legal targets: {', '.join(targets)}"] if targets else []

    if card_name == "Valorous Stance":
        kill_targets = [
            _object_name(obj) for obj in opp_battlefield
            if "Creature" in _object_types(obj) and obj.toughness >= 4
        ]
        protect_targets = [_object_name(obj) for obj in my_creatures]
        hints: list[str] = []
        if kill_targets:
            hints.append(f"Valorous Stance kill targets: {', '.join(kill_targets)}")
        if protect_targets:
            hints.append(f"Valorous Stance protect targets: {', '.join(protect_targets)}")
        return hints

    if card_name == "Aven Interrupter":
        spell_targets = [_object_name(obj) for obj in stack if _object_name(obj) != "Unknown(0)"]
        if spell_targets:
            return [f"Aven Interrupter spell targets: {', '.join(spell_targets)}"]
        return ["Aven Interrupter only targets spells on the stack; none are targetable right now"]

    return []


def _has_invalid_targeting(state: GameState, text: str) -> bool:
    lower = (text or "").lower()
    opp_battlefield = [(_object_name(obj), _object_types(obj)) for obj in state.opp_battlefield()]
    stack_names = {_object_name(obj).lower() for obj in state.stack()}

    if "stasis snare" in lower:
        for name, types in opp_battlefield:
            if name and name.lower() in lower and "Creature" not in types:
                return True

    if "get lost" in lower:
        for name, types in opp_battlefield:
            if name and name.lower() in lower and not (types & {"Creature", "Enchantment", "Planeswalker"}):
                return True

    if "aven interrupter" in lower:
        mentions_battlefield = any(name and name.lower() in lower for name, _ in opp_battlefield)
        mentions_stack = any(name in lower for name in stack_names)
        if mentions_battlefield and not mentions_stack:
            return True

    return False


def _has_impossible_sequence(state: GameState, text: str) -> bool:
    lower = (text or "").lower()
    available_mana = len(state.my_untapped_lands())

    castable: dict[str, int] = {}
    for action in state.available_actions:
        if action.seat_id != state.my_seat_id or action.action_type != "ActionType_Cast":
            continue
        obj = state.objects.get(action.instance_id) if action.instance_id else None
        card = card_cache.get(obj.grp_id) if obj else (card_cache.get(action.grp_id) if action.grp_id else None)
        name = card.name if card and card.name else (obj.name if obj else "")
        if not name:
            continue
        castable[name] = int(card.cmc if card and card.cmc is not None else 0)

    cast_mentions: list[tuple[int, str, int]] = []
    for name, cmc in castable.items():
        match = re.search(rf"\bcast\s+{re.escape(name.lower())}\b", lower)
        if match:
            cast_mentions.append((match.start(), name, cmc))

    cast_mentions.sort(key=lambda item: item[0])
    if len(cast_mentions) >= 2:
        total_cmc = sum(cmc for _, _, cmc in cast_mentions)
        if total_cmc > available_mana:
            return True

    if "activate clue" in lower and cast_mentions:
        total_cmc = sum(cmc for _, _, cmc in cast_mentions) + 2
        if total_cmc > available_mana:
            return True

    return False


def _legal_action_names(state: GameState, action_types: set[str]) -> set[str]:
    names: set[str] = set()
    for action in state.available_actions:
        if action.seat_id != state.my_seat_id or action.action_type not in action_types:
            continue
        obj = state.objects.get(action.instance_id) if action.instance_id else None
        card = card_cache.get(obj.grp_id) if obj else (
            card_cache.get(action.grp_id) if action.grp_id else None
        )
        name = card.name if card and card.name else (obj.name if obj else "")
        if name:
            names.add(name)
    return names


def _extract_action_fragments(text: str, verb: str) -> list[str]:
    pattern = re.compile(
        rf"\b{verb}\s+(.+?)(?=(?:\bthen\b|\band then\b|[.;\n]|$|(?:\s[—-]\s)))",
        re.IGNORECASE,
    )
    return [m.group(1).strip().lower() for m in pattern.finditer(text or "")]


def _fragment_matches_name(fragment: str, names: set[str]) -> bool:
    ordered = sorted((name.lower() for name in names), key=len, reverse=True)
    return any(fragment.startswith(name) for name in ordered)


def _available_attacker_names(state: GameState) -> set[str]:
    names: set[str] = set()
    for obj in state.my_battlefield():
        if obj.is_tapped or obj.has_summoning_sickness:
            continue
        types = _object_types(obj)
        if "Creature" not in types:
            continue
        card = card_cache.get(obj.grp_id)
        abilities = " ".join(a.lower() for a in (card.abilities if card else []))
        if "defender" in abilities:
            continue
        names.add(_object_name(obj))
    return names


def _available_blocker_names(state: GameState) -> set[str]:
    names: set[str] = set()
    for obj in state.my_battlefield():
        if obj.is_tapped:
            continue
        if "Creature" not in _object_types(obj):
            continue
        names.add(_object_name(obj))
    return names


def _has_unavailable_action_reference(state: GameState, text: str) -> bool:
    lower = (text or "").lower()
    display = (state.turn_info.phase_display or "").lower()
    is_my_turn = state.turn_info.active_player == state.my_seat_id

    cast_names = _legal_action_names(state, {"ActionType_Cast"})
    play_names = _legal_action_names(state, {"ActionType_Play"})
    activate_names = _legal_action_names(state, {"ActionType_Activate"})

    for fragment in _extract_action_fragments(text, "cast"):
        if not cast_names or not _fragment_matches_name(fragment, cast_names):
            return True

    for fragment in _extract_action_fragments(text, "play"):
        if not play_names or not _fragment_matches_name(fragment, play_names):
            return True

    for fragment in _extract_action_fragments(text, "activate"):
        if not activate_names or not _fragment_matches_name(fragment, activate_names):
            return True

    all_my_creatures = {
        _object_name(obj) for obj in state.my_battlefield()
        if "Creature" in _object_types(obj)
    }

    if "attack with" in lower:
        if not is_my_turn:
            return True
        legal_attackers = _available_attacker_names(state)
        if "attack with all" in lower and not legal_attackers:
            return True
        attack_fragments = _extract_action_fragments(text, "attack with")
        mentioned = {
            name for name in all_my_creatures
            if any(name.lower() in fragment for fragment in attack_fragments)
        }
        if mentioned and any(name not in legal_attackers for name in mentioned):
            return True
        if ("combat" not in display and "main 1" not in display
                and state.pending_request != "GREMessageType_DeclareAttackersReq"):
            return True

    if "block " in lower and "can't block" not in lower:
        legal_blockers = _available_blocker_names(state)
        block_fragments = re.findall(
            r"\bblock\b.+?\bwith\b\s+(.+?)(?=(?:\bthen\b|\band then\b|[.;\n]|$|(?:\s[—-]\s)))",
            lower,
            flags=re.IGNORECASE,
        )
        mentioned = {
            name for name in all_my_creatures
            if any(name.lower() in fragment for fragment in block_fragments)
        }
        if mentioned and any(name not in legal_blockers for name in mentioned):
            return True
        if ("declare blockers" not in display
                and state.pending_request != "GREMessageType_DeclareBlockersReq"):
            return True

    return False


def _format_game_state(state: GameState) -> str:
    """Format current game state as a concise prompt for the LLM."""
    me = state.my_player()
    opp = state.opp_player()
    ti = state.turn_info

    is_my_turn = ti.active_player == state.my_seat_id
    lines = []
    whose = "YOUR TURN" if is_my_turn else "OPPONENT'S TURN"
    lines.append(f"=== Turn {ti.turn_number} | {ti.phase_display} | {whose} ===")
    lines.append(f"Your life: {me.life_total if me else '?'}, Opponent life: {opp.life_total if opp else '?'}")
    lines.append(
        "Priority: "
        f"{'you' if ti.priority_player == state.my_seat_id else 'opponent'}"
        + f", Decision player: {'you' if ti.decision_player == state.my_seat_id else 'opponent'}"
    )
    if state.pending_request:
        lines.append(f"Decision request: {state.pending_request}")

    my_hand_zone = state.zone_by_type("ZoneType_Hand", state.my_seat_id)
    opp_hand_zone = state.zone_by_type("ZoneType_Hand", state.match_info.opponent_seat_id)
    lines.append(
        f"Cards in hand: you={len(my_hand_zone.object_instance_ids) if my_hand_zone else 0}, "
        f"opponent={len(opp_hand_zone.object_instance_ids) if opp_hand_zone else 0}"
    )

    # Hand
    hand = state.my_hand()
    lines.append(f"\nYour hand ({len(hand)}):")
    for obj in hand:
        card = card_cache.get(obj.grp_id)
        if card:
            type_line = f" [{card.type_line}]" if card.type_line else ""
            text = _clip(card.oracle_text or "; ".join(card.abilities[:2]), 90)
            suffix = f" - {text}" if text else ""
            lines.append(f"  - {card.name} {card.mana_cost}{type_line}{suffix}")
        else:
            lines.append(f"  - {obj.name}")

    # Battlefield
    my_bf = state.my_battlefield()
    lines.append(f"\nYour battlefield ({len(my_bf)}):")
    for obj in my_bf:
        card = card_cache.get(obj.grp_id)
        lines.append(f"  - {_format_object_line(obj, card)}")

    opp_bf = state.opp_battlefield()
    lines.append(f"\nOpponent battlefield ({len(opp_bf)}):")
    for obj in opp_bf:
        card = card_cache.get(obj.grp_id)
        lines.append(f"  - {_format_object_line(obj, card)}")

    # Available mana (with colors)
    untapped_lands = state.my_untapped_lands()
    mana_colors = []
    for land in untapped_lands:
        c = card_cache.get(land.grp_id)
        if c and c.colors:
            mana_colors.extend(c.colors)
        else:
            mana_colors.append("C")
    color_counts = {}
    for mc in mana_colors:
        color_counts[mc] = color_counts.get(mc, 0) + 1
    mana_str = ", ".join(f"{v}{k}" for k, v in sorted(color_counts.items()))
    lines.append(
        f"\nAvailable mana: {len(untapped_lands)} untapped lands ({mana_str or 'none'})"
    )
    lines.append(f"Hard mana limit this turn: {len(untapped_lands)}")

    # Stack
    stack = state.stack()
    if stack:
        lines.append(f"\nStack ({len(stack)}):")
        for obj in stack:
            card = card_cache.get(obj.grp_id)
            lines.append(f"  - {_format_object_line(obj, card)}")

    legal_actions = [
        action for action in state.available_actions
        if action.seat_id == state.my_seat_id
    ]
    if legal_actions:
        lines.append(f"\nLEGAL ACTIONS ({len(legal_actions)}):")
        for action in legal_actions:
            lines.append(f"  - {_format_action(state, action)}")
        hint_lines: list[str] = []
        seen_cards: set[str] = set()
        for action in legal_actions:
            if action.action_type != "ActionType_Cast":
                continue
            obj = state.objects.get(action.instance_id or 0) if action.instance_id else None
            card = card_cache.get(obj.grp_id) if obj else (
                card_cache.get(action.grp_id) if action.grp_id else None
            )
            name = card.name if card else (obj.name if obj else "")
            if not name or name in seen_cards:
                continue
            seen_cards.add(name)
            hint_lines.extend(_target_hints_for_card(state, name))
        if hint_lines:
            lines.append("\nTARGET HINTS:")
            for hint in hint_lines:
                lines.append(f"  - {hint}")

    return "\n".join(lines)


def _format_extra_context(context: dict | None) -> str:
    if not context:
        return ""

    lines = []

    my_deck = context.get("my_deck_name")
    my_arch = context.get("my_deck_archetype")
    my_sig = context.get("my_deck_signature") or []
    if my_deck or my_arch:
        deck_line = f"Your deck: {my_deck or 'unknown'}"
        if my_arch:
            deck_line += f" ({my_arch})"
        lines.append(deck_line)
        if my_sig:
            lines.append(f"Your deck signature cards: {', '.join(my_sig[:6])}")

    opp_deck = context.get("opp_deck_name")
    opp_conf = context.get("opp_confidence")
    opp_arch = context.get("opp_archetype")
    opp_speed = context.get("opp_speed")
    opp_reach = context.get("opp_hidden_reach")
    if opp_deck:
        opp_line = f"Opponent likely deck: {opp_deck}"
        if opp_conf is not None:
            opp_line += f" ({opp_conf}% confidence)"
        meta_bits = [b for b in [opp_arch, opp_speed] if b]
        if meta_bits:
            opp_line += f" [{' / '.join(meta_bits)}]"
        lines.append(opp_line)
        if opp_reach:
            lines.append(f"Estimated hidden reach from hand: {opp_reach} damage")

    opp_seen = context.get("opp_seen_cards") or []
    if opp_seen:
        lines.append(f"Opponent cards seen so far: {', '.join(opp_seen[:10])}")

    wr = context.get("matchup_wr")
    games = context.get("matchup_games", 0)
    if wr is not None and games:
        lines.append(f"Historical matchup win rate: {wr:.0f}% over {games} games")

    recent = context.get("recent_history") or []
    if recent:
        lines.append("Recent game history:")
        for item in recent[-8:]:
            lines.append(f"  - {item}")

    if not lines:
        return ""

    return "\n".join(lines)


def _build_prompt(state: GameState, request_type: str = "",
                  context: dict | None = None,
                  include_system: bool = True) -> str:
    """Build the full prompt with context and game state."""
    req = request_type or state.pending_request or ""
    if "Mulligan" in req:
        task_context = (
            "You need to decide whether to mulligan or keep this hand. "
            "Answer with Keep or Mulligan, plus one short reason."
        )
    elif "ChooseStartingPlayer" in req:
        task_context = (
            "You need to choose play or draw. "
            "Answer with Play or Draw, plus one short reason."
        )
    elif "DeclareAttackers" in req:
        task_context = (
            "You need to decide which creatures to attack with. "
            "Only use attackers that are legal right now."
        )
    elif "DeclareBlockers" in req:
        task_context = (
            "You need to decide how to block the attacking creatures. "
            "Only use blockers that are legal right now."
        )
    elif "SelectTargets" in req:
        task_context = (
            "You need to select targets for a spell or ability. "
            "Use exact card names from the visible board state."
        )
    elif "ActionsAvailable" in req:
        task_context = (
            "It is your priority. Choose the best legal action from LEGAL ACTIONS. "
            "If the best line is combat, say 'Go to combat, attack with ...'. "
            "If none improve the position, say Pass."
        )
    else:
        task_context = (
            "Choose the best legal action from LEGAL ACTIONS for the current position. "
            "If you should hold up interaction, say Pass. "
            "If combat is the best line, say 'Go to combat, attack with ...'."
        )

    game_state_str = _format_game_state(state)
    extra_context = _format_extra_context(context)
    prompt_parts = []
    if include_system:
        prompt_parts.append(SYSTEM_PROMPT)
    prompt_parts.append(
        "This is the same MTG Arena game as the previous turn. "
        "Use earlier turns as memory, but treat the latest Strategic context and "
        "Current game state below as authoritative."
    )
    prompt_parts.append(task_context)
    if extra_context:
        prompt_parts.append(f"Strategic context:\n{extra_context}")
    prompt_parts.append(f"Current game state:\n{game_state_str}")
    return "\n\n".join(prompt_parts)


async def _call_claude_cli_raw(prompt: str, session: LlmSession | None = None) -> tuple[str, str | None]:
    """Call claude CLI as subprocess. Uses existing subscription — no API costs."""
    global _last_backend_usage
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Allow nested invocation
    _last_backend_usage = None

    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--append-system-prompt",
        CLAUDE_SESSION_PROMPT,
    ]
    if session and session.session_id:
        cmd.extend(["--resume", session.session_id])
    elif not session:
        cmd.append("--no-session-persistence")
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        # Claude CLI has ~10-15s startup overhead, allow 120s total
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except asyncio.TimeoutError:
        proc.kill()
        return "LLM timeout (120s)", None

    if proc.returncode != 0:
        err = stderr.decode().strip()
        log.error("claude CLI error: %s", err)
        return f"CLI error: {err[:200]}", None

    raw = stdout.decode().strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("claude CLI returned non-JSON output")
        return raw, None

    usage = payload.get("usage") or {}
    _last_backend_usage = {
        "backend": "claude_cli",
        "session_id": payload.get("session_id"),
        "total_cost_usd": payload.get("total_cost_usd"),
        "duration_ms": payload.get("duration_ms"),
        "num_turns": payload.get("num_turns"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
    }

    return payload.get("result", "").strip(), payload.get("session_id")


async def _call_claude_cli(prompt: str, conversation_key: str | None = None) -> str:
    if not conversation_key:
        text, _ = await _call_claude_cli_raw(prompt)
        return text

    session = _get_or_create_session(conversation_key, "claude_cli")
    async with session.lock:
        text, session_id = await _call_claude_cli_raw(prompt, session=session)
        if _is_session_resume_error(text) and session.session_id:
            session.session_id = None
            text, session_id = await _call_claude_cli_raw(prompt, session=session)
        if session_id:
            session.session_id = session_id
        session.last_used_at = time.time()
        return text


async def _call_ollama(prompt: str, model: str = "llama3.1:8b") -> str:
    """Call ollama via HTTP API (local or remote)."""
    import httpx
    global _last_backend_usage

    model = _ollama_model(model)
    base_url = _ollama_base_url()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            prompt_eval = data.get("prompt_eval_count") or 0
            eval_count = data.get("eval_count") or 0
            _last_backend_usage = {
                "backend": "ollama",
                "session_id": "",
                "total_cost_usd": 0.0,
                "duration_ms": int((data.get("total_duration") or 0) / 1_000_000),
                "input_tokens": prompt_eval,
                "output_tokens": eval_count,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            return data.get("response", "").strip()
    except Exception as e:
        log.error("ollama error: %s", e)
        return f"Ollama error: {e}"


async def _call_anthropic_api(prompt: str) -> str:
    """Call Anthropic API directly."""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text if response.content else ""
    except Exception as e:
        log.error("Anthropic API error: %s", e)
        return f"API error: {e}"


THREAT_PROMPT_TEMPLATE = (
    "You are an MTG expert. Assess these opponent permanents.\n"
    "My deck: {strategy}. Opponent likely: {opp_deck}.\n\n"
    "{cards_text}\n\n"
    "For EACH card respond in EXACTLY this format (one block per card):\n"
    "CARD: exact card name\n"
    "DANGER: number 1-5 (1=harmless, 3=threatening, 5=game-ending)\n"
    "SUMMARY: max 15 words explaining what it does and why it matters\n"
    "PRIORITY: must-remove | should-remove | monitor | ignore"
)


def _format_threat_cards(cards: list[dict]) -> str:
    lines = []
    for i, c in enumerate(cards, 1):
        text = c.get("oracle_text") or "; ".join(c.get("abilities", []))
        lines.append(f"{i}. {c['name']} ({c['type_line']}) [{c['mana_cost']}] — {text}")
    return "\n".join(lines)


def _parse_threat_response(text: str) -> dict[str, dict]:
    """Parse structured threat assessments from LLM response."""
    results: dict[str, dict] = {}
    current: dict[str, Any] = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("CARD:"):
            if current.get("name"):
                results[current["name"]] = current
            current = {"name": line[5:].strip()}
        elif upper.startswith("DANGER:"):
            try:
                current["danger"] = min(5, max(1, int(line[7:].strip()[0])))
            except (ValueError, IndexError):
                current["danger"] = 3
        elif upper.startswith("SUMMARY:"):
            current["summary"] = line[8:].strip()
        elif upper.startswith("PRIORITY:"):
            val = line[9:].strip().lower()
            for p in ("must-remove", "should-remove", "monitor", "ignore"):
                if p in val:
                    current["priority"] = p
                    break
            else:
                current["priority"] = "monitor"
    if current.get("name"):
        results[current["name"]] = current
    return results


async def assess_threats(cards: list[dict], strategy_name: str,
                         opp_deck: str | None = None,
                         backend_override: str | None = None) -> dict[str, dict]:
    """Assess threat level of opponent permanents via LLM.

    Returns dict mapping card name -> {danger, summary, priority}.
    """
    if not cards:
        return {}

    backend = backend_override or ("ollama" if ollama_available() else get_backend())
    if backend == "none":
        return {}

    cards_text = _format_threat_cards(cards)
    prompt = THREAT_PROMPT_TEMPLATE.format(
        strategy=strategy_name or "unknown",
        opp_deck=opp_deck or "unknown",
        cards_text=cards_text,
    )

    log.info("Assessing %d threats via %s", len(cards), backend)

    try:
        if backend == "claude_cli":
            text = await _call_claude_cli(prompt)
        elif backend == "ollama":
            text = await _call_ollama(prompt)
        else:
            text = await _call_anthropic_api(prompt)

        return _parse_threat_response(text)
    except Exception as e:
        log.error("Threat assessment error: %s", e)
        return {}


async def get_advice(state: GameState, request_type: str = "",
                     context: dict | None = None,
                     backend_override: str | None = None) -> Advice | None:
    """Get LLM-based play advice using the best available backend."""
    global _last_call_state_id, _last_call_time

    # Debounce
    now = time.time()
    if (state.game_state_id == _last_call_state_id
            or now - _last_call_time < MIN_INTERVAL):
        return None

    backend = backend_override or get_backend()
    if backend == "none":
        return Advice(
            source="llm", priority="low",
            message="No LLM backend available (install claude CLI or ollama)",
            confidence=0.0,
        )

    conversation_key = _session_key_for_state(state)
    use_session = bool(conversation_key and backend in SESSION_BACKENDS)
    prompt = _build_prompt(
        state,
        request_type,
        context,
        include_system=not _has_active_session(conversation_key, backend),
    )

    _last_call_state_id = state.game_state_id
    _last_call_time = now

    log.info("Calling LLM backend: %s", backend)

    try:
        if backend == "claude_cli":
            text = await _call_claude_cli(
                prompt,
                conversation_key=conversation_key if use_session else None,
            )
        elif backend == "ollama":
            text = await _call_ollama(prompt)
        elif backend == "anthropic_api":
            text = await _call_anthropic_api(prompt)
        else:
            text = "Unknown backend"

        if _has_invalid_targeting(state, text):
            log.warning("Discarding invalid LLM targeting advice: %s", text)
            return None
        if _has_impossible_sequence(state, text):
            log.warning("Discarding impossible LLM sequencing advice: %s", text)
            return None
        if _has_unavailable_action_reference(state, text):
            log.warning("Discarding illegal/stale LLM action advice: %s", text)
            return None

        return Advice(
            source=f"llm ({backend})",
            priority="medium",
            message=text,
            details=f"(Turn {state.turn_info.turn_number}, {state.turn_info.phase_display})",
            confidence=0.7,
        )

    except Exception as e:
        log.error("LLM error (%s): %s", backend, e)
        return Advice(
            source="llm", priority="low",
            message=f"LLM error: {e}",
            confidence=0.0,
        )
