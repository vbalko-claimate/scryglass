"""Canonical action inference, scoring, and rendering.

Maps every piece of advice (strategy rules + heuristics) to structured
ActionScore objects so downstream consumers get machine-readable scores
instead of parsing free-text messages.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .models import ActionFamily, ActionScore, Advice, RuleHit

if TYPE_CHECKING:
    from .models import GameState

# ─── Priority → base score mapping ────────────────────────────

_PRIORITY_BASE: dict[str, float] = {
    "critical": 0.95,
    "high": 0.75,
    "medium": 0.50,
    "low": 0.25,
}

# ─── Keyword patterns for action family inference ─────────────

_PASS_WORDS = re.compile(
    r"\b(hold|wait|save|don'?t cast|don'?t play|keep up|leave open)\b", re.I,
)
_ATTACK_WORDS = re.compile(r"\b(attack|swing|alpha)\b", re.I)
_BLOCK_WORDS = re.compile(r"\b(block|chump|trade)\b", re.I)
_LAND_WORDS = re.compile(r"\b(play (?:a )?land|land drop)\b", re.I)
_ACTIVATE_WORDS = re.compile(r"\b(activate|crew|equip|channel|sacrifice|sac)\b", re.I)


def is_hold_rule(action_family_value: str = "", action_text: str = "") -> bool:
    """Check if a rule is a hold/pass rule. Uses action_family (preferred) or text fallback."""
    if action_family_value:
        return action_family_value == ActionFamily.PASS.value or action_family_value == "pass"
    # Fallback for rules without action_family
    return bool(_PASS_WORDS.search(action_text))


def infer_action_family(
    text: str,
    phase: str = "",
    rule_tags: list[str] | None = None,
) -> ActionFamily:
    """Infer the canonical action family from advice text + context."""
    tags = rule_tags or []

    # Tag-based shortcut (most reliable)
    if "hold" in tags or "wait" in tags:
        return ActionFamily.PASS
    if "attack" in tags:
        return ActionFamily.ATTACK
    if "block" in tags:
        return ActionFamily.BLOCK
    if "land" in tags:
        return ActionFamily.PLAY_LAND
    if "activate" in tags:
        return ActionFamily.ACTIVATE

    # Keyword scan — order matters (PASS first to catch "don't cast X")
    if _PASS_WORDS.search(text):
        return ActionFamily.PASS
    if _BLOCK_WORDS.search(text):
        return ActionFamily.BLOCK
    if _ATTACK_WORDS.search(text):
        return ActionFamily.ATTACK
    if _LAND_WORDS.search(text):
        return ActionFamily.PLAY_LAND
    if _ACTIVATE_WORDS.search(text):
        return ActionFamily.ACTIVATE

    # Phase-based fallback
    if "Combat" in phase:
        return ActionFamily.ATTACK

    return ActionFamily.CAST_SPELL


def score_from_priority(priority: str, weight: float = 1.0) -> float:
    """Convert priority + weight to a 0-1 normalized score."""
    base = _PRIORITY_BASE.get(priority, 0.50)
    return max(0.0, min(1.0, base * min(weight, 2.0)))


# ─── Rendering: RuleHit → Advice ──────────────────────────────

def render_advice(hits: list[RuleHit]) -> list[Advice]:
    """Convert RuleHit objects into Advice objects with action_scores attached."""
    result: list[Advice] = []
    for hit in hits:
        confidence = min(0.9, 0.5 + hit.weight * 0.2)
        advice = Advice(
            source="strategy",
            priority=hit.priority,
            message=hit.raw_message,
            details=f"[{hit.layer}:{hit.rule_id}] w:{hit.weight:.2f}",
            confidence=confidence,
            action_scores=list(hit.action_scores),
            recommended_cards=[s.target for s in hit.action_scores if s.target],
        )
        result.append(advice)
    return result


# ─── Backfill: tag heuristic Advice with ActionScores ─────────

# Pattern to extract card name from common heuristic messages
_CARD_NAME_RE = re.compile(
    r"(?:Cast|Play|Hold|Save|Attack with|Block with|Activate)\s+(.+?)(?:\s*[—\-(]|$)",
    re.I,
)


def tag_heuristic_advice(
    advice_list: list[Advice],
    state: "GameState | None" = None,
) -> list[Advice]:
    """Backfill action_scores on heuristic Advice that lacks them."""
    phase = state.turn_info.phase if state else ""
    for advice in advice_list:
        if advice.action_scores:
            continue  # already tagged
        if advice.source not in ("heuristic", ""):
            continue

        family = infer_action_family(advice.message, phase=phase)
        target = ""
        # Try to extract target card name
        m = _CARD_NAME_RE.search(advice.message)
        if m:
            target = m.group(1).strip()
        elif advice.recommended_cards:
            target = advice.recommended_cards[0]

        score = score_from_priority(advice.priority)
        advice.action_scores = [
            ActionScore(
                family=family,
                score=score,
                target=target,
                source="heuristic",
            )
        ]
    return advice_list
