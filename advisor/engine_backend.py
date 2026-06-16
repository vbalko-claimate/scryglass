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
    mode: str = "main",
    attackers: list[str] | None = None,
    targets: list[str] | None = None,
    bottom_count: int = 0,
) -> dict | None:
    """Build the /advise payload from the live game state, or None if the
    state isn't ready (no players seated).

    `mode` selects the engine decision routed to:
      "main"      → rank main-phase plays (needs `hand`)
      "attackers" → which of my creatures to attack with (board only)
      "blockers"  → how to block; pass the opp's attacking creature
                    names in `attackers`
      "target"    → which target for a spell; pass candidate names in
                    `targets`
      "mulligan"  → keep/mull the opening `hand`; with `bottom_count` > 0
                    (the London rule), which cards to put on the bottom
    """
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
        "mode": mode,
        "attackers": attackers or [],
        "targets": targets or [],
        "bottom_count": bottom_count,
    }


async def get_engine_advice(
    state: GameState,
    *,
    ai: str = "heuristic",
    opp_deck_names: list[str] | None = None,
    mode: str = "main",
    attackers: list[str] | None = None,
    targets: list[str] | None = None,
    bottom_count: int = 0,
    timeout: float = 8.0,
) -> Advice | None:
    """Query the engine sidecar and return a single `Advice`, or None when
    the engine is unreachable / there's nothing to rank.

    `mode` mirrors `build_request`: "main" ranks plays; "attackers" /
    "blockers" / "target" route to the engine's combat/targeting choosers
    (corpus-validated ~61% / ~65% / ~72% human agreement); "mulligan" gives
    keep/mull advice and, with `bottom_count` > 0, which cards to bottom."""
    req = build_request(
        state, ai=ai, opp_deck_names=opp_deck_names,
        mode=mode, attackers=attackers, targets=targets, bottom_count=bottom_count,
    )
    if req is None:
        return None
    # main + mulligan modes need the hand; combat/target modes work off the
    # board + the attacker/target names already in the request.
    if mode in ("main", "mulligan") and not req["hand"]:
        return None
    if mode == "blockers" and not req["attackers"]:
        return None
    if mode == "target" and not req["targets"]:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{engine_url()}/advise", json=req)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # connection refused, timeout, bad JSON — degrade silently
        log.info("engine /advise unavailable: %s", e)
        return None

    if mode == "mulligan":
        return _mulligan_advice(data)
    if mode != "main":
        return _combat_advice(data, mode)

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
    resolved = int(data.get("hand_resolved", 0) or 0)
    total = int(data.get("hand_total", 0) or 0)
    coverage = f"{resolved}/{total}"
    cov_ratio = resolved / total if total > 0 else 0.0

    # COVERAGE GATING: when much of the hand is off-catalog (the engine
    # can't represent those cards), the ranking may miss the real best
    # play — so downgrade priority and flag low confidence rather than
    # advise confidently. The advisor's other sources (heuristic/LLM)
    # still cover the decision.
    low_cov = cov_ratio < 0.7
    priority = "low" if low_cov else "medium"
    confidence = win * (cov_ratio if low_cov else 1.0)
    flag = " ⚠ partial card coverage" if low_cov else ""

    top = "  ".join(
        f"{a.get('card') or a.get('kind')} {float(a.get('score', 0.0)):.0%}"
        for a in ranked[:4]
    )
    msg = f"Engine: {rec_kind} {rec}" if rec else "Engine: pass / no play"
    # The engine's teaching rationale (the WHY) goes in its own field, rendered
    # as a prominent overlay element; `details` keeps the diagnostic.
    rationale = (data.get("rationale") or "").strip()
    diag = f"[engine {data.get('pilot', '')}] win {win:.0%} · coverage {coverage}{flag} · {top}"
    return Advice(
        source="engine",
        priority=priority,
        message=msg,
        details=diag,
        rationale=rationale,
        confidence=confidence,
        confidence_tier=(data.get("confidence") or ""),
        confidence_basis=(data.get("confidence_basis") or ""),
        recommended_cards=[rec] if rec else [],
        action_scores=scores,
    )


def _combat_advice(data: dict, mode: str) -> Advice | None:
    """Map an attackers/blockers/target /advise response into a single
    high-priority `Advice`. These modes return a one-line `recommended`
    (e.g. "Attack with: A, B" / "Block: X → Y" / "Target: Z") plus the
    structured picks — no per-action softmax, so `action_scores` is empty.
    Returns None only when there's genuinely nothing to advise (target
    mode with no good pick)."""
    rec = data.get("recommended")
    win = float(data.get("win_prob", 0.0))
    if mode == "target":
        tgt = data.get("target")
        if not tgt or not rec:
            return None
        cards = [tgt]
    elif mode == "attackers":
        cards = list(data.get("attackers") or [])
    else:  # blockers
        cards = [b.get("blocker", "") for b in (data.get("blocks") or []) if b.get("blocker")]
    if not rec:
        return None
    rationale = (data.get("rationale") or "").strip()
    return Advice(
        source="engine",
        priority="high",
        message=f"Engine: {rec}",
        details=f"[engine {data.get('pilot', '')}] win {win:.0%}",
        rationale=rationale,
        confidence=win,
        confidence_tier=(data.get("confidence") or ""),
        confidence_basis=(data.get("confidence_basis") or ""),
        recommended_cards=cards,
        action_scores=[],
    )


async def optimize_deck(
    deck_list: str,
    *,
    games: int = 12,
    steps: int = 3,
    timeout: float = 180.0,
) -> dict | None:
    """Ask the engine's `POST /optimize` endpoint to suggest cut→add swaps for
    a deck, returning the raw OptimizeReport dict (base win-rate + CI, confirmed
    suggestions with per-swap CIs, reactive/coverage flags, manabase analysis)
    or None if the engine sidecar is unreachable.

    `deck_list` is Arena-format text — the engine parses inline lists. This is
    a SIM workload (seconds to ~a minute), so the timeout is generous and the
    caller should show a progress state."""
    req = {"deck": deck_list, "games": int(games), "steps": int(steps)}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{engine_url()}/optimize", json=req)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # connection refused, timeout, bad JSON — degrade
        log.info("engine /optimize unavailable: %s", e)
        return None


async def portfolio_deck(
    deck_list: str,
    *,
    iters: int = 150,
    games: int = 12,
    timeout: float = 180.0,
) -> dict | None:
    """Ask the engine's `POST /portfolio` for a DIVERSE set of viable builds of
    the deck (MAP-Elites quality-diversity), returning the PortfolioReport dict
    or None if the sidecar is unreachable. Sim workload, generous timeout."""
    req = {"deck": deck_list, "iters": int(iters), "games": int(games)}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{engine_url()}/portfolio", json=req)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.info("engine /portfolio unavailable: %s", e)
        return None


def _mulligan_advice(data: dict) -> Advice | None:
    """Map a mode="mulligan" /advise response into a single high-priority
    `Advice`: keep/mull (the `recommended` line) plus, on a London keep, the
    cards to put on the bottom (`bottom`). `recommended_cards` carries the
    bottom list so the overlay can highlight which cards to take out."""
    rec = data.get("recommended")
    if not rec:
        return None
    rationale = (data.get("rationale") or "").strip()
    bottom = [c for c in (data.get("bottom") or []) if c]
    return Advice(
        source="engine",
        priority="high",
        message=f"Engine: {rec}",
        details="",
        rationale=rationale,
        confidence=0.0,
        confidence_tier=(data.get("confidence") or ""),
        confidence_basis=(data.get("confidence_basis") or ""),
        recommended_cards=bottom,
        action_scores=[],
    )
