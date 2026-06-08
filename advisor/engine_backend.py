"""Glass-engine advice backend (P1).

Queries the Rust engine's `POST /advise` endpoint (glass-server, run as a
local sidecar) to rank the current main-phase decision, and maps the
response into the advisor's `Advice` shape.

ADDITIVE & SAFE: this is a NEW advice source tagged `source="engine"`. It
does not change any existing advisor behavior — it's invoked via the
manual `ask_engine` WebSocket action (mirroring `ask_llm`). If the engine
sidecar isn't reachable, `get_engine_advice` returns None and the advisor
is unaffected.

The engine reconstructs a GameState from the board we send (it has no
serde for live state — see the glass-shard advisor-integration design),
so we send a Checkpoint-shaped position + our hand. The default pilot is
`heuristic` (instant, and measured to match human main-phase plays as
well as the stronger MCTS pilot); set SCRY_ENGINE_AI=mcts for an explicit
deep read.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .models import ActionFamily, ActionScore, Advice, GameState

log = logging.getLogger(__name__)

_FAMILY = {
    "land": ActionFamily.PLAY_LAND,
    "cast": ActionFamily.CAST_SPELL,
    "activate": ActionFamily.ACTIVATE,
    "unlock": ActionFamily.ACTIVATE,
    "cycle": ActionFamily.ACTIVATE,
    "unearth": ActionFamily.CAST_SPELL,
    "pass": ActionFamily.PASS,
}


def engine_url() -> str:
    return os.environ.get("SCRY_ENGINE_URL", "http://localhost:3000").rstrip("/")


def _plus_counters(counters: dict[str, int]) -> int:
    return sum(
        v for k, v in counters.items() if "p1p1" in k.lower() or "+1/+1" in k.lower()
    )


def _loyalty(counters: dict[str, int]) -> int:
    for k, v in counters.items():
        if "loyalty" in k.lower():
            return v
    return 0


def _bf_entry(o: Any) -> dict:
    return {
        "name": o.name,
        "tapped": bool(o.is_tapped),
        "plus_counters": int(_plus_counters(o.counters)),
        "loyalty": int(_loyalty(o.counters)),
        "summoning_sick": bool(o.has_summoning_sickness),
    }


def build_request(
    state: GameState,
    ai: str = "heuristic",
    opp_deck_names: list[str] | None = None,
) -> dict | None:
    """Build the /advise payload from the live game state, or None if the
    state isn't ready (no players seated)."""
    me = state.my_player()
    opp = state.opp_player()
    if me is None or opp is None:
        return None
    hand = [o.name for o in state.my_hand() if o.name]
    phase = "main2" if "main2" in (state.turn_info.phase or "").lower() else "main1"
    opp_hand = state.objects_in_zone("ZoneType_Hand", state.match_info.opponent_seat_id)
    return {
        "position": {
            "my_life": int(me.life_total),
            "opp_life": int(opp.life_total),
            "my_battlefield": [_bf_entry(o) for o in state.my_battlefield()],
            "opp_battlefield": [_bf_entry(o) for o in state.opp_battlefield()],
            "my_hand_size": len(hand),
            "opp_hand_size": len(opp_hand),
            "my_graveyard": [o.name for o in state.my_graveyard() if o.name],
            "opp_graveyard": [o.name for o in state.opp_graveyard() if o.name],
        },
        "hand": hand,
        "opp_deck_names": opp_deck_names or [],
        "ai": ai,
        "phase": phase,
    }


async def get_engine_advice(
    state: GameState,
    *,
    ai: str = "heuristic",
    opp_deck_names: list[str] | None = None,
    timeout: float = 8.0,
) -> Advice | None:
    """Query the engine sidecar and return a single `Advice`, or None when
    the engine is unreachable / there's nothing to rank."""
    req = build_request(state, ai=ai, opp_deck_names=opp_deck_names)
    if req is None or not req["hand"]:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{engine_url()}/advise", json=req)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # connection refused, timeout, bad JSON — degrade silently
        log.info("engine /advise unavailable: %s", e)
        return None

    ranked = data.get("ranked") or []
    if not ranked:
        return None

    scores = [
        ActionScore(
            family=_FAMILY.get(a.get("kind", ""), ActionFamily.CAST_SPELL),
            score=float(a.get("score", 0.0)),
            target=a.get("card", ""),
            source="engine",
        )
        for a in ranked
    ]
    rec = data.get("recommended")
    rec_kind = data.get("recommended_kind") or "play"
    win = float(data.get("win_prob", 0.0))
    coverage = f"{data.get('hand_resolved', '?')}/{data.get('hand_total', '?')}"
    top = "  ".join(
        f"{a.get('card') or a.get('kind')} {float(a.get('score', 0.0)):.0%}"
        for a in ranked[:4]
    )
    msg = f"Engine: {rec_kind} {rec}" if rec else "Engine: pass / no play"
    return Advice(
        source="engine",
        priority="medium",
        message=msg,
        details=f"[engine {data.get('pilot', '')}] win {win:.0%} · coverage {coverage} · {top}",
        confidence=win,
        recommended_cards=[rec] if rec else [],
        action_scores=scores,
    )
