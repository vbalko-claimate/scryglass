"""Multi-backend LLM play advisor.

Backends (in priority order):
1. claude CLI — uses existing Claude Code subscription, no API costs
2. ollama — local LLM, free, fast (if installed)
3. anthropic API — pay-per-call fallback
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from .database import card_cache
from typing import Any

from .models import Advice, GameState

log = logging.getLogger(__name__)

# Debounce
_last_call_state_id = -1
_last_call_time = 0.0
MIN_INTERVAL = 3.0

# Active backend (auto-detected on first call)
_backend: str | None = None

SYSTEM_PROMPT = (
    "You are an expert MTG Arena coach. Give 1-2 SHORT actionable options. "
    "Format: 'Cast X' / 'Remove X with Y' / 'Attack with A, B' / 'Block X with Y'. "
    "No analysis or explanation unless asked. Just the best play. Under 50 words. "
    "IMPORTANT: Only suggest actions legal for the current phase. "
    "TAPPED creatures CANNOT attack or block. Only untapped creatures can. "
    "You can only attack during YOUR combat phase, block during OPPONENT'S combat."
)


def _detect_backend() -> str:
    """Auto-detect best available LLM backend."""
    # 1. Claude CLI
    if shutil.which("claude"):
        log.info("LLM backend: claude CLI (subscription)")
        return "claude_cli"

    # 2. Ollama
    if shutil.which("ollama"):
        log.info("LLM backend: ollama (local)")
        return "ollama"

    # 3. Anthropic API
    if os.environ.get("ANTHROPIC_API_KEY"):
        log.info("LLM backend: anthropic API")
        return "anthropic_api"

    log.warning("No LLM backend available")
    return "none"


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

    # Hand
    hand = state.my_hand()
    lines.append(f"\nYour hand ({len(hand)}):")
    for obj in hand:
        card = card_cache.get(obj.grp_id)
        if card:
            lines.append(f"  - {card.name} {card.mana_cost}")
        else:
            lines.append(f"  - {obj.name}")

    # Battlefield
    my_bf = state.my_battlefield()
    lines.append(f"\nYour battlefield ({len(my_bf)}):")
    for obj in my_bf:
        card = card_cache.get(obj.grp_id)
        name = card.name if card else obj.name
        tap = "TAPPED" if obj.is_tapped else "untapped"
        sick = ", summoning sick" if obj.has_summoning_sickness else ""
        if obj.is_creature:
            lines.append(f"  - {name} {obj.power}/{obj.toughness} ({tap}{sick})")
        else:
            lines.append(f"  - {name} ({tap})")

    opp_bf = state.opp_battlefield()
    lines.append(f"\nOpponent battlefield ({len(opp_bf)}):")
    for obj in opp_bf:
        card = card_cache.get(obj.grp_id)
        name = card.name if card else obj.name
        tap = "TAPPED" if obj.is_tapped else "untapped"
        if obj.is_creature:
            lines.append(f"  - {name} {obj.power}/{obj.toughness} ({tap})")
        else:
            lines.append(f"  - {name} ({tap})")

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
    lines.append(f"\nAvailable mana: {len(untapped_lands)} untapped lands ({mana_str or 'none'})")

    # Stack
    stack = state.stack()
    if stack:
        lines.append(f"\nStack ({len(stack)}):")
        for obj in stack:
            card = card_cache.get(obj.grp_id)
            lines.append(f"  - {card.name if card else obj.name}")

    # Graveyard
    my_gy = state.my_graveyard()
    if my_gy:
        lines.append(f"\nYour graveyard ({len(my_gy)}):")
        for obj in my_gy[:5]:
            card = card_cache.get(obj.grp_id)
            lines.append(f"  - {card.name if card else obj.name}")

    return "\n".join(lines)


def _build_prompt(state: GameState, request_type: str = "") -> str:
    """Build the full prompt with context and game state."""
    if "Mulligan" in (request_type or ""):
        context = "You need to decide whether to mulligan or keep this hand."
    elif "DeclareAttackers" in (request_type or ""):
        context = "You need to decide which creatures to attack with."
    elif "DeclareBlockers" in (request_type or ""):
        context = "You need to decide how to block the attacking creatures."
    elif "SelectTargets" in (request_type or ""):
        context = "You need to select targets for a spell or ability."
    else:
        context = "It's your turn. What is the best play?"

    game_state_str = _format_game_state(state)
    return f"{SYSTEM_PROMPT}\n\n{context}\n\nCurrent game state:\n{game_state_str}"


async def _call_claude_cli(prompt: str) -> str:
    """Call claude CLI as subprocess. Uses existing subscription — no API costs."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Allow nested invocation

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--max-turns", "1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        # Claude CLI has ~10-15s startup overhead, allow 60s total
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        proc.kill()
        return "LLM timeout (60s)"

    if proc.returncode != 0:
        err = stderr.decode().strip()
        log.error("claude CLI error: %s", err)
        return f"CLI error: {err[:200]}"

    return stdout.decode().strip()


async def _call_ollama(prompt: str, model: str = "llama3.1:8b") -> str:
    """Call ollama local LLM via HTTP API."""
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://localhost:11434/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
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
                         opp_deck: str | None = None) -> dict[str, dict]:
    """Assess threat level of opponent permanents via LLM.

    Returns dict mapping card name -> {danger, summary, priority}.
    """
    if not cards:
        return {}

    backend = get_backend()
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


async def get_advice(state: GameState, request_type: str = "") -> Advice | None:
    """Get LLM-based play advice using the best available backend."""
    global _last_call_state_id, _last_call_time

    # Debounce
    now = time.time()
    if (state.game_state_id == _last_call_state_id
            or now - _last_call_time < MIN_INTERVAL):
        return None

    backend = get_backend()
    if backend == "none":
        return Advice(
            source="llm", priority="low",
            message="No LLM backend available (install claude CLI or ollama)",
            confidence=0.0,
        )

    prompt = _build_prompt(state, request_type)

    _last_call_state_id = state.game_state_id
    _last_call_time = now

    log.info("Calling LLM backend: %s", backend)

    try:
        if backend == "claude_cli":
            text = await _call_claude_cli(prompt)
        elif backend == "ollama":
            text = await _call_ollama(prompt)
        elif backend == "anthropic_api":
            text = await _call_anthropic_api(prompt)
        else:
            text = "Unknown backend"

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
