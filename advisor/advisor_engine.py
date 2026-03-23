"""Advisor orchestrator — heuristics + layered strategy rules, LLM only on demand."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
from typing import Callable

from .database import (
    card_cache, get_connection, get_matchup_wr, get_observed_opp_decks,
    save_advice, save_llm_call, save_match, save_match_event,
)
from .heuristics import (
    analyze as heuristic_analyze, reset_caches as reset_heuristic_caches,
    set_my_archetype, set_opp_deck, set_opp_tracker_data,
)
from .llm_advisor import (
    assess_threats,
    consume_last_usage,
    get_advice as llm_get_advice,
    ollama_available,
    reset_sessions as reset_llm_sessions,
)
from .actions import tag_heuristic_advice
from .models import Advice, CardInfo, GameState
from .version import ENGINE_VERSION
from .strategy import (
    MetaDeck, OpponentTracker, evaluate_rules, evaluate_rules_v2,
    get_or_create_strategy, learn_from_match, load_meta_decks,
    save_meta_decks, update_opponent_tracking, Strategy,
)

log = logging.getLogger(__name__)

_last_advice_state_id: tuple[int, str] = (-1, "")
VALID_ADVICE_MODES = {"hybrid", "llm_first", "llm_only"}
VALID_LLM_SCOPES = {"full", "budget"}
AUTO_LLM_IGNORED_REQUESTS = {
    "GREMessageType_MulliganReq",
    "GREMessageType_ChooseStartingPlayerReq",
}
BUDGET_ALWAYS_REQUESTS = {
    "GREMessageType_DeclareAttackersReq",
    "GREMessageType_DeclareBlockersReq",
    "GREMessageType_SelectTargetsReq",
}
LOW_VALUE_ACTION_TYPES = {
    "ActionType_Activate_Mana",
    "ActionType_FloatMana",
    "ActionType_Pass",
}


def _card_text(card: CardInfo) -> str:
    return (" ".join(card.abilities) + " " + (card.oracle_text or "")).lower()


def _threat_category(card: CardInfo) -> str:
    text = _card_text(card)
    if "Planeswalker" in card.card_types:
        return "engine"
    if any(t in card.card_types for t in ["Artifact", "Enchantment"]):
        if any(kw in text for kw in ["landfall", "whenever", "at the beginning"]):
            return "engine"
        if "token" in text or "create" in text or "+1/+1 counter" in text or "draw a card" in text:
            return "engine"
        if "exile target" in text or "destroy target" in text:
            return "interaction"
        return "support"
    if card.is_creature and any(kw in text for kw in ["whenever", "at the beginning", "whenever another"]):
        return "engine"
    return "body"


def _threat_role(card: CardInfo) -> str:
    """Fine-grained role label for UI and target priority explanation."""
    text = _card_text(card)
    category = _threat_category(card)

    if any(kw in text for kw in [
        "becomes a copy", "copy of target creature card in your graveyard",
        "copy of any creature", "from your graveyard to the battlefield",
        "from your graveyard to your hand", "return target creature card from your graveyard",
        "return target nonland permanent card with mana value",
    ]):
        return "payoff"
    if any(kw in text for kw in [
        "surveil", "mill", "draw a card, then discard", "draw, then discard",
        "loot", "connive", "map token", "create a map",
    ]):
        return "enabler"
    if any(kw in text for kw in [
        "haste", "double strike", "each opponent", "deals damage",
        "total mana value", "can't be blocked",
    ]):
        return "kill-piece"
    if category == "engine":
        return "engine"
    if ("destroy target" in text or "exile target" in text or "counter target" in text) and "dies" not in text.lower():
        return "interaction"
    return "body"


def _threat_decision_hint(card: CardInfo) -> str:
    """Short player-facing explanation of why the card matters."""
    text = _card_text(card)
    role = _threat_role(card)
    if role == "payoff":
        if "copy" in text and "graveyard" in text:
            return "payoff — turns graveyard setup into a real threat"
        if "graveyard" in text:
            return "payoff — converts graveyard/value setup into board advantage"
        return "payoff — this is the card that cashes in their setup"
    if role == "enabler":
        if "surveil" in text or "mill" in text:
            return "enabler — fills graveyard and turns on their better cards"
        if "discard" in text:
            return "enabler — smooths draws and sets up later payoff cards"
        return "enabler — supports the real payoff pieces"
    if role == "kill-piece":
        return "kill piece — can end the game quickly if it gets one clean turn"
    if role == "engine":
        return "engine — generates repeatable cards, tokens, or counters"
    if role == "interaction":
        return "interaction piece — punishes your main game plan if left alone"
    return "board piece — mostly stats unless it gets buffed"


def _is_notable(card: CardInfo, obj: "GameObject | None" = None) -> bool:
    """Check if a permanent warrants threat assessment."""
    if card.is_land:
        return False
    if "Planeswalker" in card.card_types:
        return True
    if any(t in card.card_types for t in ["Artifact", "Enchantment"]):
        return True
    if card.is_creature:
        text = " ".join(a.lower() for a in card.abilities)
        # Keywords that make a creature threatening
        if any(kw in text for kw in ["ward", "hexproof", "indestructible",
                                      "double strike", "deathtouch",
                                      "whenever", "counter", "destroy",
                                      "exile", "create", "+1/+1"]):
            return True
        # Complex abilities
        if len(text) > 80:
            return True
        # Buffed beyond base stats (auras, counters)
        if obj:
            base_p = int(card.power) if card.power else 0
            base_t = int(card.toughness) if card.toughness else 0
            if obj.power > base_p + 1 or obj.toughness > base_t + 1:
                return True
    return False


def _quick_danger(card: CardInfo, obj: "GameObject | None" = None) -> int:
    """Quick heuristic danger level 1-5."""
    text = _card_text(card)
    category = _threat_category(card)
    if "Planeswalker" in card.card_types:
        return 5
    if category == "engine":
        if (
            ("landfall" in text and ("token" in text or "+1/+1 counter" in text))
            or ("draw a card" in text and "token" in text)
            or ("token" in text and ("+2/+2" in text or "copy target token" in text))
        ):
            return 5
        if "exile target" in text and "until" in text and "leaves the battlefield" in text:
            return 4
        return 4
    if any(kw in text for kw in ["destroy all", "exile all", "-x/-x",
                                  "all creatures get", "each creature"]):
        return 5
    if any(kw in text for kw in ["draw", "destroy target", "exile target",
                                  "counter target", "each opponent",
                                  "protection from everything"]):
        return 4
    base = 2
    if any(kw in text for kw in ["create", "token", "+1/+1 counter",
                                  "deals damage", "whenever", "search your library"]):
        base = 3
    # Buffed creatures are more dangerous — escalate based on live power
    if obj and card.is_creature:
        base_p = int(card.power) if card.power else 0
        if obj.power >= 5:
            base = max(base, 4)
        elif obj.power >= base_p + 2:
            base = max(base, 3)
    # Ward/hexproof makes it harder to answer
    if any(kw in text for kw in ["ward", "hexproof", "indestructible"]):
        base = max(base, 3)
    return base


def _quick_summary(card: CardInfo) -> str:
    """Generate quick heuristic summary from card abilities."""
    text = _card_text(card)
    if "becomes a copy" in text and "graveyard" in text:
        return "graveyard payoff — copies the best creature they milled or discarded"
    if "draw a card, then discard" in text or "surveil" in text or "mill" in text:
        return "graveyard enabler — sets up later recursion or copy payoffs"
    if "return target creature card from your graveyard" in text or "from your graveyard to the battlefield" in text:
        return "recursion payoff — turns graveyard setup back into board presence"
    if "landfall" in text and ("token" in text or "+1/+1 counter" in text):
        return "landfall engine — every land makes tokens or pumps the board"
    if "draw a card" in text and "token" in text:
        return "token engine — turns token makers into card draw"
    if "copy target token" in text or ("token" in text and "+2/+2" in text):
        return "token payoff — copies or massively buffs tokens"
    if "exile target" in text and "until" in text and "leaves the battlefield" in text:
        return "removal engine on board — keeps exiling while it stays"
    effects = []
    if "destroy" in text and "all" in text:
        effects.append("board wipe")
    elif "destroy" in text:
        effects.append("removal")
    if "exile" in text and "all" in text:
        effects.append("mass exile")
    elif "exile" in text and "destroy" not in text:
        effects.append("exile")
    if "draw" in text:
        effects.append("card draw")
    if "token" in text or "create" in text:
        effects.append("creates tokens")
    if "counter target" in text:
        effects.append("counters spells")
    if "+1/+1" in text or "gets +" in text:
        effects.append("buffs creatures")
    if "damage" in text and "each" in text:
        effects.append("mass damage")
    elif "damage" in text:
        effects.append("damage")
    if "gain" in text and "life" in text:
        effects.append("lifegain")
    if "can't attack" in text or "can't block" in text:
        effects.append("restricts combat")
    if "return" in text and "hand" in text:
        effects.append("bounce")
    if "search" in text and "library" in text:
        effects.append("tutors")
    if "discard" in text:
        effects.append("forces discard")
    if effects:
        return "; ".join(effects[:3])
    # Fallback: truncate first ability
    if card.abilities:
        first = card.abilities[0]
        return first[:60] + ("..." if len(first) > 60 else "")
    return card.type_line


def _save_advice_batch(match_id, game_num, turn_num, phase, advice_tuples):
    """Save advice to DB in background."""
    for source, priority, message, details in advice_tuples:
        try:
            save_advice(match_id, {
                "game_number": game_num, "turn_number": turn_num,
                "phase": phase, "source": source,
                "priority": priority, "message": message,
                "details": details,
            })
        except Exception:
            pass


class AdvisorEngine:

    def __init__(self):
        self.on_advice: Callable[[list[Advice]], None] | None = None
        self.on_strategy_info: Callable[[dict], None] | None = None
        self.on_threat_update: Callable[[list[dict]], None] | None = None
        self.on_llm_status: Callable[[dict], None] | None = None
        self._last_advice: list[Advice] = []
        self._strategy: Strategy | None = None
        self._strategy_loaded = False
        self._meta_decks: list[MetaDeck] = []
        self._opp_tracker = OpponentTracker()
        self._opp_seen_ids: set[int] = set()
        self._last_opp_deck: str | None = None
        self._matchup_wr: dict | None = None
        # Advice compliance tracking
        self._pending_recs: list[str] = []
        self._pending_turn: int = -1
        # Advice deduplication: track all messages sent for the current spot
        self._advice_spot: tuple[int, str, str] | None = None
        self._advice_sent_this_spot: set[str] = set()
        self._advice_generation: int = 0
        self._last_auto_llm_key: tuple | None = None
        # Threat tracking
        self._threat_cache: dict[str, dict] = {}   # card_name -> LLM assessment
        self._active_threats: dict[int, dict] = {}  # instance_id -> threat info
        self._assessing: set[str] = set()            # card names in-flight
        self._last_threat_signature: tuple | None = None
        self._last_intel_signature: tuple | None = None
        self._last_intel_decision: tuple | None = None
        configured_mode = os.environ.get("SCRY_ADVICE_MODE", "hybrid").strip().lower()
        self._advice_mode = (configured_mode
                             if configured_mode in VALID_ADVICE_MODES
                             else "hybrid")
        configured_scope = os.environ.get("SCRY_LLM_SCOPE", "full").strip().lower()
        self._llm_scope = (configured_scope
                           if configured_scope in VALID_LLM_SCOPES
                           else "full")
        self._auto_llm = (
            self._advice_mode in {"llm_first", "llm_only"}
            or (self._advice_mode == "hybrid" and self._llm_scope == "budget")
        )
        self._llm_status: dict = {
            "state": "idle",
            "source": "auto",
            "wait": False,
            "label": "LLM idle",
            "turn_number": 0,
            "phase_display": "",
        }

    @property
    def advice_mode(self) -> str:
        return self._advice_mode

    @property
    def auto_llm_enabled(self) -> bool:
        return self._auto_llm

    @property
    def llm_scope(self) -> str:
        return self._llm_scope

    def set_auto_llm(self, enabled: bool):
        self._auto_llm = bool(enabled)
        log.info("Auto LLM %s (mode=%s, scope=%s)",
                 "enabled" if self._auto_llm else "disabled",
                 self._advice_mode, self._llm_scope)
        self._set_llm_status(
            state="idle" if self._auto_llm else "disabled",
            wait=False,
            source="auto",
        )

    @property
    def llm_status(self) -> dict:
        return dict(self._llm_status)

    def _set_llm_status(
        self,
        *,
        state: str,
        wait: bool,
        source: str = "auto",
        turn_number: int = 0,
        phase_display: str = "",
    ):
        if state == "pending":
            label = "LLM thinking..." if source == "manual" else "LLM thinking, supplement incoming"
        elif state == "done":
            label = "AI answer ready" if source == "manual" else "LLM supplement ready"
        elif state == "disabled":
            label = "Auto LLM off"
        else:
            label = "LLM idle"

        payload = {
            "state": state,
            "source": source,
            "wait": wait,
            "label": label,
            "turn_number": turn_number,
            "phase_display": phase_display,
        }
        if payload == self._llm_status:
            return
        self._llm_status = payload
        if self.on_llm_status:
            self.on_llm_status(dict(payload))

    def _my_legal_actions(self, state: GameState) -> list:
        return [a for a in state.available_actions if a.seat_id == state.my_seat_id]

    def _record_llm_call(
        self,
        state: GameState,
        source: str,
        usage: dict | None,
        advice: Advice | None,
    ):
        if not usage or not state.match_info.match_id:
            return
        try:
            save_llm_call({
                "match_id": state.match_info.match_id,
                "game_number": state.match_info.game_number,
                "turn_number": state.turn_info.turn_number,
                "phase": state.turn_info.phase,
                "request_type": state.pending_request or "",
                "source": source,
                "backend": usage.get("backend", ""),
                "advice_mode": self._advice_mode,
                "llm_scope": self._llm_scope,
                "state_id": state.game_state_id,
                "accepted": bool(advice and advice.message),
                "message": advice.message if advice and advice.message else "",
                "duration_ms": usage.get("duration_ms"),
                "total_cost_usd": usage.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "session_id": usage.get("session_id", ""),
            })
        except Exception:
            log.exception("Failed to persist llm_call telemetry")

    def _is_low_value_llm_action(self, state: GameState, action) -> bool:
        if action.action_type in LOW_VALUE_ACTION_TYPES:
            return True

        obj = state.objects.get(action.instance_id) if action.instance_id else None
        card = card_cache.get(obj.grp_id) if obj else (
            card_cache.get(action.grp_id) if action.grp_id else None
        )
        name = (card.name if card and card.name else (obj.name if obj else "")).strip()

        if action.action_type == "ActionType_Play" and card and card.is_land:
            return True

        if action.action_type == "ActionType_Activate":
            if name in {"Clue", "Map"}:
                return True

        return False

    def _budget_should_run_llm(self, state: GameState) -> bool:
        req = state.pending_request or ""
        if req in BUDGET_ALWAYS_REQUESTS:
            return True

        legal_actions = self._my_legal_actions(state)
        if not legal_actions:
            return False

        meaningful = [
            action for action in legal_actions
            if not self._is_low_value_llm_action(state, action)
        ]
        if not meaningful:
            return False
        spell_choices = [
            action for action in meaningful
            if action.action_type in {"ActionType_Cast", "ActionType_Activate"}
        ]

        me = state.my_player()
        opp = state.opp_player()
        threat_pressure = any(
            (threat.get("danger", 0) >= 4) or threat.get("must_answer")
            for threat in self._active_threats.values()
        )

        if state.stack() and state.turn_info.priority_player == state.my_seat_id:
            return True
        if req == "GREMessageType_ActionsAvailableReq":
            if state.turn_info.priority_player != state.my_seat_id:
                return False
            if me and me.life_total <= 6:
                return True
            if opp and opp.life_total <= 6:
                return True
            if len(spell_choices) >= 2:
                return True
            if threat_pressure and spell_choices:
                return True
            if (state.turn_info.active_player != state.my_seat_id
                    and len(meaningful) >= 3):
                return True
        return False

    def _should_run_auto_llm(self, state: GameState) -> bool:
        if not self._auto_llm or not state.match_info.match_id:
            return False
        if state.match_info.stage == "GameStage_GameOver":
            return False
        if not state.my_seat_id:
            return False
        if (state.pending_request or "") in AUTO_LLM_IGNORED_REQUESTS:
            return False
        if self._llm_scope == "budget":
            return self._budget_should_run_llm(state)
        if state.pending_request:
            return True
        if state.turn_info.priority_player != state.my_seat_id:
            return False
        return any(a.seat_id == state.my_seat_id for a in state.available_actions)

    def _should_use_fast_llm(self, state: GameState) -> bool:
        if not ollama_available():
            return False
        req = state.pending_request or ""
        if req in {"GREMessageType_DeclareAttackersReq", "GREMessageType_DeclareBlockersReq"}:
            return True
        if req == "GREMessageType_ActionsAvailableReq":
            meaningful = [
                action for action in self._my_legal_actions(state)
                if not self._is_low_value_llm_action(state, action)
            ]
            if not meaningful:
                return False
            target_selection = any(
                action.action_type == "ActionType_SelectTargets" for action in meaningful
            )
            complex_pressure = any(
                (threat.get("danger", 0) >= 4) or threat.get("must_answer")
                for threat in self._active_threats.values()
            )
            return len(meaningful) <= 2 and not target_selection and not complex_pressure
        return False

    def _auto_llm_key(self, state: GameState) -> tuple:
        spot = self._normalized_advice_spot(state)
        meaningful_actions = tuple(sorted(
            (
                action.action_type,
                action.instance_id or 0,
                action.grp_id or 0,
                action.ability_grp_id or 0,
            )
            for action in self._my_legal_actions(state)
            if not self._is_low_value_llm_action(state, action)
        ))
        stack_sig = tuple(sorted(
            (obj.instance_id, obj.grp_id, obj.controller_seat_id)
            for obj in state.stack()
        ))
        return (
            state.match_info.match_id,
            state.match_info.game_number,
            state.turn_info.turn_number,
            state.turn_info.phase,
            state.turn_info.step,
            spot,
            meaningful_actions[:12],
            stack_sig[:8],
        )

    def _normalized_advice_spot(self, state: GameState) -> str:
        """Collapse GRE request variants into one practical advice spot."""
        display = (state.turn_info.phase_display or "").lower()
        req = state.pending_request or ""
        is_my_turn = state.turn_info.active_player == state.my_seat_id

        if is_my_turn and (
            req == "GREMessageType_DeclareAttackersReq"
            or "begin combat" in display
            or "declare attackers" in display
        ):
            return "combat_attack"

        if (not is_my_turn) and (
            req == "GREMessageType_DeclareBlockersReq"
            or "declare blockers" in display
        ):
            return "combat_block"

        if req:
            return req

        return f"{state.turn_info.phase}:{state.turn_info.step or ''}"

    def _is_combat_focus_spot(self, state: GameState) -> bool:
        return self._normalized_advice_spot(state) in {"combat_attack", "combat_block"}

    def _combat_primary_advice(
        self,
        state: GameState,
        base_advice: list[Advice],
        intel_advice: list[Advice],
    ) -> list[Advice]:
        """Keep attack/block calls visible instead of burying them under intel."""
        spot = self._normalized_advice_spot(state)
        if spot not in {"combat_attack", "combat_block"}:
            return []

        if spot == "combat_attack":
            markers = ("attack", "lethal", "combat")
        else:
            markers = ("block", "incoming", "survive", "chump", "trade", "combat")

        def is_primary(item: Advice) -> bool:
            msg = item.message.lower()
            return any(marker in msg for marker in markers)

        prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        primary = [a for a in base_advice if is_primary(a)]
        secondary = [a for a in base_advice if a not in primary]
        primary.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
        secondary.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
        intel_sorted = sorted(intel_advice, key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))

        if not primary and secondary:
            primary = secondary[:1]
            secondary = secondary[1:]

        focused = primary[:2]
        room = 3 - len(focused)
        if room > 0:
            focused.extend(secondary[:room])
            room = 3 - len(focused)
        if room > 0 and intel_sorted:
            focused.extend(intel_sorted[:1])
        return focused[:3]

    def _ensure_strategy(self, state: GameState):
        """Load or generate strategy for current deck (once per match)."""
        if self._strategy_loaded:
            return
        if not state.my_deck:
            return
        self._strategy = get_or_create_strategy(state)
        self._strategy_loaded = True
        self._threat_cache.clear()  # Reassess with new strategy context
        # Load global meta_decks (shared across all decks)
        self._meta_decks = load_meta_decks()
        self._enrich_meta_decks()
        if self._strategy:
            set_my_archetype(self._strategy.archetype)
            log.info("Strategy active: %s (%s, %d rules, %d meta decks)",
                     self._strategy.name, self._strategy.archetype,
                     len(self._strategy.rules), len(self._meta_decks))
            # Persist deck name to match record
            if state.match_info.match_id:
                try:
                    save_match(state.match_info.match_id,
                               my_deck_name=self._strategy.name)
                except Exception:
                    pass
        self._broadcast_strategy_info()

    def _recent_llm_history(self, match_id: str, limit: int = 8) -> list[str]:
        """Summarize the most recent meaningful match events for prompt context."""
        if not match_id:
            return []

        conn = get_connection()
        rows = conn.execute(
            "SELECT event_type, turn_number, phase, data "
            "FROM match_events WHERE match_id = ? "
            "AND event_type IN ("
            "'card_played','opp_card_played','spell_cast','opp_spell_cast',"
            "'ability','opp_ability','attack_declared','opp_attack_declared',"
            "'block_declared','life_change','creature_left_bf','enchantment_attached'"
            ") "
            "ORDER BY id DESC LIMIT ?",
            (match_id, limit),
        ).fetchall()
        conn.close()

        lines: list[str] = []
        for etype, turn, phase, data_str in reversed(rows):
            try:
                data = json.loads(data_str) if data_str else {}
            except (json.JSONDecodeError, TypeError):
                data = {}

            prefix = f"T{turn} {phase}:"
            if etype == "card_played":
                lines.append(f"{prefix} you played {data.get('name', '?')}")
            elif etype == "opp_card_played":
                lines.append(f"{prefix} opponent played {data.get('name', '?')}")
            elif etype == "spell_cast":
                lines.append(f"{prefix} you cast {data.get('name', '?')}")
            elif etype == "opp_spell_cast":
                lines.append(f"{prefix} opponent cast {data.get('name', '?')}")
            elif etype == "ability":
                lines.append(f"{prefix} your ability from {data.get('name', '?')} triggered")
            elif etype == "opp_ability":
                lines.append(f"{prefix} opponent ability from {data.get('name', '?')} triggered")
            elif etype == "attack_declared":
                lines.append(f"{prefix} you attacked with {data.get('name', '?')}")
            elif etype == "opp_attack_declared":
                lines.append(f"{prefix} opponent attacked with {data.get('name', '?')}")
            elif etype == "block_declared":
                if data.get("no_blocks"):
                    lines.append(f"{prefix} you made no blocks")
                else:
                    blocker = data.get("blocker", "?")
                    attackers = ", ".join(a.get("name", "?") for a in data.get("attackers", []))
                    lines.append(f"{prefix} block {blocker} -> {attackers or '?'}")
            elif etype == "life_change":
                who = "you" if data.get("player") == "me" else "opponent"
                lines.append(
                    f"{prefix} {who} life {data.get('old', '?')} -> {data.get('new', '?')}"
                    f" ({data.get('delta', 0):+d})"
                )
            elif etype == "creature_left_bf":
                owner = "your" if data.get("owner") == "me" else "opponent"
                cause = f" by {data['caused_by']}" if data.get("caused_by") else ""
                lines.append(
                    f"{prefix} {owner} {data.get('name', '?')} "
                    f"{data.get('destination', 'removed')}{cause}"
                )
            elif etype == "enchantment_attached":
                lines.append(
                    f"{prefix} {data.get('aura', '?')} attached to {data.get('target', '?')}"
                )

        return lines[-limit:]

    def _build_llm_context(self, state: GameState) -> dict:
        opp_deck_obj = self._opp_tracker.identified_deck
        matchup_wr = None
        matchup_games = 0
        if self._matchup_wr and self._matchup_wr.get("total", 0) >= 1:
            matchup_wr = self._matchup_wr.get("win_rate")
            matchup_games = self._matchup_wr.get("total", 0)

        return {
            "my_deck_name": self._strategy.name if self._strategy else None,
            "my_deck_archetype": self._strategy.archetype if self._strategy else None,
            "my_deck_signature": self._strategy.deck_signature[:8] if self._strategy else [],
            "opp_deck_name": self._last_opp_deck,
            "opp_confidence": round(self._opp_tracker.confidence * 100)
            if self._last_opp_deck else None,
            "opp_archetype": opp_deck_obj.archetype if opp_deck_obj else None,
            "opp_speed": opp_deck_obj.speed if opp_deck_obj else None,
            "opp_hidden_reach": opp_deck_obj.hidden_reach if opp_deck_obj else None,
            "opp_seen_cards": list(self._opp_tracker.seen_cards.keys())[:10],
            "matchup_wr": matchup_wr,
            "matchup_games": matchup_games,
            "recent_history": self._recent_llm_history(state.match_info.match_id),
        }

    def _meta_threat_for_card(self, card_name: str) -> dict | None:
        opp_deck_obj = self._opp_tracker.identified_deck
        if not opp_deck_obj:
            return None
        needle = (card_name or "").casefold()
        for threat in opp_deck_obj.key_threats:
            if not isinstance(threat, dict):
                continue
            if str(threat.get("card", "")).casefold() == needle:
                return threat
        return None

    def _enrich_meta_decks(self):
        """Add observed opponent decks from match history to global meta_decks."""
        try:
            observed = get_observed_opp_decks()
            if not observed:
                return
            existing_names = {md.name for md in self._meta_decks}
            added = 0
            for od in observed:
                if od["name"] in existing_names:
                    continue
                self._meta_decks.append(
                    MetaDeck(
                        name=od["name"], archetype=od["archetype"],
                        colors=od["colors"], signal_cards=od["signal_cards"],
                    ))
                added += 1
            if added:
                save_meta_decks(self._meta_decks)
                log.info("Added %d observed opponent decks to global meta_decks "
                         "(total: %d)", added, len(self._meta_decks))
        except Exception as e:
            log.error("Failed to enrich meta decks: %s", e)

    async def on_state_change(self, state: GameState, allow_auto_llm: bool = False):
        """Called on every game state update. Runs heuristics + strategy rules."""
        global _last_advice_state_id

        # Dedup key: normalize repeated GRE request variants for the same real
        # combat spot so attack/block advice isn't published multiple times.
        spot = self._normalized_advice_spot(state)
        if state.pending_request or self._is_combat_focus_spot(state):
            dedup_key = (
                state.match_info.match_id,
                state.match_info.game_number,
                state.turn_info.turn_number,
                state.turn_info.phase,
                spot,
            )
        else:
            dedup_key = (state.game_state_id, spot)
        if dedup_key == _last_advice_state_id:
            return
        # Set immediately to prevent duplicate async calls for same state
        _last_advice_state_id = dedup_key
        self._advice_generation += 1
        generation = self._advice_generation

        if not allow_auto_llm:
            self._set_llm_status(
                state="idle" if self._auto_llm else "disabled",
                wait=False,
                source="auto",
                turn_number=state.turn_info.turn_number,
                phase_display=state.turn_info.phase_display,
            )

        self._ensure_strategy(state)

        # Track opponent's cards for meta recognition
        if self._strategy:
            self._opp_seen_ids = update_opponent_tracking(
                self._opp_tracker, state, self._opp_seen_ids)
            # Try to identify opponent's deck (uses global meta_decks)
            self._sync_tracker_data()
            opp_deck = self._opp_tracker.identify(self._meta_decks)
            if opp_deck:
                set_opp_deck(opp_deck)  # wire meta threats into heuristic scoring
                opp_name = opp_deck.name
                if opp_name != self._last_opp_deck:
                    self._last_opp_deck = opp_name
                    log.info("Opponent identified: %s (%.0f%%)",
                             opp_name, self._opp_tracker.confidence * 100)
                    # Persist opponent deck name
                    if state.match_info.match_id:
                        try:
                            save_match(state.match_info.match_id,
                                       opp_deck_name=opp_name)
                        except Exception:
                            pass
                    # Look up historical matchup WR
                    if self._strategy:
                        self._matchup_wr = get_matchup_wr(
                            self._strategy.name, opp_name)
                        if self._matchup_wr and self._matchup_wr.get("total", 0) >= 2:
                            wr = self._matchup_wr["win_rate"]
                            total = self._matchup_wr["total"]
                            log.info("Matchup history: %.0f%% WR (%d games)",
                                     wr, total)
                self._broadcast_strategy_info()

        # Detect and assess opponent threats (skip finished games)
        if state.match_info.match_id and state.match_info.stage != "GameStage_GameOver":
            self._update_threats(state)

        advice: list[Advice] = []

        # Phase 0: check pending decision outcomes (2-turn delta)
        self._check_decision_outcomes(state)

        if self._advice_mode != "llm_only":
            # Run heuristics
            base_advice = heuristic_analyze(state)
            base_advice = tag_heuristic_advice(base_advice, state)

            # Run strategy rules
            if self._strategy:
                _, strategy_advice = evaluate_rules_v2(
                    self._strategy.rules, state, self._opp_tracker,
                    vulnerabilities=self._strategy.vulnerabilities)
                heuristic_msgs = {a.message.lower() for a in base_advice}
                for sa in strategy_advice:
                    if sa.message.lower() not in heuristic_msgs:
                        base_advice.append(sa)

            # Apply GA-optimized global biases
            if self._strategy and self._strategy.global_biases:
                for adv in base_advice:
                    if adv.action_scores:
                        family = adv.action_scores[0].family.value
                        bias = self._strategy.global_biases.get(family, 0.0)
                        if bias:
                            adv.action_scores[0].score = max(0.0, min(1.0, adv.action_scores[0].score + bias))

            intel_advice = self._build_intel_advice(state, base_advice)
            if self._is_combat_focus_spot(state):
                advice = self._combat_primary_advice(state, base_advice, intel_advice)
            else:
                advice = base_advice + intel_advice

            prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            advice.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
            advice = advice[:3 if self._advice_mode == "hybrid" and self._auto_llm else 4]

        if self._advice_mode == "hybrid":
            committed = self._finalize_advice(state, advice) if advice else []
            # Phase 0: log no-advice decision points for coverage analysis
            if not advice and state.match_info.match_id:
                try:
                    save_match_event(
                        state.match_info.match_id, "decision_eval",
                        game_number=state.match_info.game_number,
                        turn_number=state.turn_info.turn_number,
                        phase=state.turn_info.phase,
                        data={
                            "game_state_id": state.game_state_id,
                            "advice_count": 0,
                            "top_advice": [],
                            "strategy_name": self._strategy.name if self._strategy else None,
                            "engine_version": ENGINE_VERSION,
                            "no_advice": True,
                        },
                    )
                except Exception:
                    pass
            llm_advice: Advice | None = None
            llm_usage: dict | None = None
            llm_state = copy.deepcopy(state) if allow_auto_llm else None
            llm_started = False
            if llm_state and self._should_run_auto_llm(llm_state):
                llm_key = self._auto_llm_key(llm_state)
                if llm_key != self._last_auto_llm_key:
                    self._last_auto_llm_key = llm_key
                    llm_started = True
                    self._set_llm_status(
                        state="pending",
                        wait=True,
                        source="auto",
                        turn_number=llm_state.turn_info.turn_number,
                        phase_display=llm_state.turn_info.phase_display,
                    )
                    llm_advice = await llm_get_advice(
                        llm_state,
                        llm_state.pending_request or "",
                        context=self._build_llm_context(llm_state),
                        backend_override="ollama" if self._should_use_fast_llm(llm_state) else None,
                    )
                    llm_usage = consume_last_usage()
                    self._record_llm_call(llm_state, "auto", llm_usage, llm_advice)
            elif allow_auto_llm:
                self._set_llm_status(
                    state="idle" if self._auto_llm else "disabled",
                    wait=False,
                    source="auto",
                    turn_number=state.turn_info.turn_number,
                    phase_display=state.turn_info.phase_display,
                )
            if generation != self._advice_generation:
                return
            if not llm_advice or not llm_advice.message:
                if llm_started:
                    self._set_llm_status(
                        state="idle",
                        wait=False,
                        source="auto",
                        turn_number=state.turn_info.turn_number,
                        phase_display=state.turn_info.phase_display,
                    )
                return
            if committed:
                if all(a.message.lower() != llm_advice.message.lower()
                       for a in self._last_advice):
                    llm_item = Advice(
                        source=llm_advice.source,
                        priority="low",
                        message=llm_advice.message,
                        details=llm_advice.details or "LLM supplement",
                        confidence=llm_advice.confidence,
                    )
                    merged = self._last_advice + [llm_item]
                    prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                    merged.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
                    self._last_advice = merged[:5]
                    if self.on_advice:
                        self.on_advice(self._last_advice)
                    if llm_state and llm_state.match_info.match_id:
                        save_advice(llm_state.match_info.match_id, {
                            "game_number": llm_state.match_info.game_number,
                            "turn_number": llm_state.turn_info.turn_number,
                            "phase": llm_state.turn_info.phase,
                            "source": llm_item.source,
                            "priority": llm_item.priority,
                            "message": llm_item.message,
                            "details": llm_item.details or "",
                            "game_state_summary": {"llm_usage": llm_usage or {}},
                        })
                self._set_llm_status(
                    state="done",
                    wait=False,
                    source="auto",
                    turn_number=state.turn_info.turn_number,
                    phase_display=state.turn_info.phase_display,
                )
                return
            if llm_state:
                self._finalize_advice(llm_state, [llm_advice])
            self._set_llm_status(
                state="done",
                wait=False,
                source="auto",
                turn_number=state.turn_info.turn_number,
                phase_display=state.turn_info.phase_display,
            )
            return

        llm_advice: Advice | None = None
        llm_usage: dict | None = None
        llm_state = copy.deepcopy(state) if allow_auto_llm else None
        llm_started = False
        if llm_state and self._should_run_auto_llm(llm_state):
            llm_key = self._auto_llm_key(llm_state)
            if llm_key != self._last_auto_llm_key:
                self._last_auto_llm_key = llm_key
                llm_started = True
                self._set_llm_status(
                    state="pending",
                    wait=True,
                    source="auto",
                    turn_number=llm_state.turn_info.turn_number,
                    phase_display=llm_state.turn_info.phase_display,
                )
                llm_advice = await llm_get_advice(
                    llm_state,
                    llm_state.pending_request or "",
                    context=self._build_llm_context(llm_state),
                    backend_override="ollama" if self._should_use_fast_llm(llm_state) else None,
                )
                llm_usage = consume_last_usage()
                self._record_llm_call(llm_state, "auto", llm_usage, llm_advice)
        elif allow_auto_llm:
            self._set_llm_status(
                state="idle" if self._auto_llm else "disabled",
                wait=False,
                source="auto",
                turn_number=state.turn_info.turn_number,
                phase_display=state.turn_info.phase_display,
            )
        if generation != self._advice_generation:
            return

        if self._advice_mode == "llm_only":
            advice = [llm_advice] if llm_advice and llm_advice.message else []
        elif self._advice_mode == "llm_first" and llm_advice and llm_advice.message:
            supporting = [a for a in advice if a.message != llm_advice.message][:2]
            advice = [llm_advice] + supporting
        elif self._advice_mode == "hybrid" and self._auto_llm and llm_advice and llm_advice.message:
            if all(a.message.lower() != llm_advice.message.lower() for a in advice):
                advice.append(Advice(
                    source=llm_advice.source,
                    priority="low",
                    message=llm_advice.message,
                    details=llm_advice.details or "LLM supplement",
                    confidence=llm_advice.confidence,
                ))
            prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            advice.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
            advice = advice[:4]

        if not advice and llm_advice and llm_advice.message:
            advice = [llm_advice]

        self._finalize_advice(state, advice)
        if llm_started:
            self._set_llm_status(
                state="done" if llm_advice and llm_advice.message else "idle",
                wait=False,
                source="auto",
                turn_number=state.turn_info.turn_number,
                phase_display=state.turn_info.phase_display,
            )

    async def on_decision_point(self, state: GameState, request_type: str):
        await self.on_state_change(state, allow_auto_llm=True)

    async def ask_llm(self, state: GameState) -> Advice | None:
        """Manually trigger LLM advice."""
        llm_state = copy.deepcopy(state)
        self._set_llm_status(
            state="pending",
            wait=True,
            source="manual",
            turn_number=llm_state.turn_info.turn_number,
            phase_display=llm_state.turn_info.phase_display,
        )
        advice = await llm_get_advice(
            llm_state,
            llm_state.pending_request or "",
            context=self._build_llm_context(llm_state),
        )
        llm_usage = consume_last_usage()
        self._record_llm_call(llm_state, "manual", llm_usage, advice)
        if advice and advice.message:
            base = [a for a in self._last_advice
                    if not a.source.lower().startswith("llm")]
            if all(a.message.lower() != advice.message.lower() for a in base):
                base.append(advice)
            prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            base.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
            self._last_advice = base[:5]
            if llm_state.match_info.match_id:
                save_advice(llm_state.match_info.match_id, {
                    "game_number": llm_state.match_info.game_number,
                    "turn_number": llm_state.turn_info.turn_number,
                    "phase": llm_state.turn_info.phase,
                    "source": advice.source, "priority": advice.priority,
                    "message": advice.message,
                    "game_state_summary": {"llm_usage": llm_usage or {}},
                })
            if self.on_advice:
                self.on_advice(self._last_advice)
            self._set_llm_status(
                state="done",
                wait=False,
                source="manual",
                turn_number=llm_state.turn_info.turn_number,
                phase_display=llm_state.turn_info.phase_display,
            )
        else:
            self._set_llm_status(
                state="idle",
                wait=False,
                source="manual",
                turn_number=llm_state.turn_info.turn_number,
                phase_display=llm_state.turn_info.phase_display,
            )

        return advice

    def _update_threats(self, state: GameState):
        """Detect new opponent permanents and assess threats."""
        opp_bf = state.opp_battlefield()
        current_ids = {o.instance_id for o in opp_bf}
        threats_changed = False

        # Remove threats that left the battlefield
        removed = [iid for iid in self._active_threats if iid not in current_ids]
        for iid in removed:
            del self._active_threats[iid]
            threats_changed = True

        # Detect new notable permanents + re-evaluate buffed existing ones
        new_cards: list[dict] = []
        for obj in opp_bf:
            if obj.instance_id in self._active_threats:
                # Re-evaluate if creature got significantly buffed
                existing = self._active_threats[obj.instance_id]
                card = card_cache.get(obj.grp_id)
                if card and card.is_creature:
                    new_danger = _quick_danger(card, obj)
                    if new_danger > existing["danger"]:
                        existing["danger"] = new_danger
                        existing["priority"] = (
                            "must-remove" if new_danger >= 4
                            else "should-remove" if new_danger >= 3
                            else "monitor")
                        threats_changed = True
                continue
            card = card_cache.get(obj.grp_id)
            if not card or not _is_notable(card, obj):
                continue

            # Quick heuristic assessment — include live stats
            danger = _quick_danger(card, obj)
            threat: dict = {
                "instance_id": obj.instance_id,
                "name": card.name,
                "type_line": card.type_line,
                "mana_cost": card.mana_cost,
                "danger": danger,
                "category": _threat_category(card),
                "role": _threat_role(card),
                "summary": _quick_summary(card),
                "decision_hint": _threat_decision_hint(card),
                "priority": ("must-remove" if danger >= 4
                             else "should-remove" if danger >= 3
                             else "monitor"),
                "source": "heuristic",
            }
            meta_threat = self._meta_threat_for_card(card.name)
            if meta_threat:
                reason = meta_threat.get("reason")
                if reason:
                    threat["reason"] = reason
                    threat["summary"] = reason
                if meta_threat.get("must_answer"):
                    threat["must_answer"] = True
                removal_priority = meta_threat.get("removal_priority")
                if removal_priority == 1 or meta_threat.get("must_answer"):
                    threat["danger"] = max(threat["danger"], 4)
                    threat["priority"] = "must-remove"
                elif removal_priority == 2:
                    threat["danger"] = max(threat["danger"], 3)
                    threat["priority"] = "should-remove"
                threat["source"] = "meta"

            # Use cached LLM assessment if available
            if card.name in self._threat_cache:
                threat.update(self._threat_cache[card.name])
                threat["source"] = "llm"
            elif card.name not in self._assessing:
                new_cards.append({
                    "name": card.name,
                    "type_line": card.type_line,
                    "mana_cost": card.mana_cost,
                    "oracle_text": card.oracle_text,
                    "abilities": card.abilities,
                })
                self._assessing.add(card.name)

            self._active_threats[obj.instance_id] = threat
            threats_changed = True

        # Broadcast if anything changed
        if threats_changed:
            log.info("Threat update: %d active, %d removed, %d new for LLM",
                     len(self._active_threats), len(removed), len(new_cards))
            self._broadcast_threats()

        # Fire LLM assessment for new uncached cards
        if new_cards:
            asyncio.ensure_future(self._assess_new_threats(new_cards))

    async def _assess_new_threats(self, cards: list[dict]):
        """Background LLM assessment of new threats."""
        card_names = [c["name"] for c in cards]
        try:
            strategy_name = self._strategy.name if self._strategy else None
            opp_deck = self._last_opp_deck
            results = await assess_threats(
                cards,
                strategy_name,
                opp_deck,
                backend_override="ollama" if ollama_available() else None,
            )

            # Update cache and active threats
            for name, assessment in results.items():
                self._threat_cache[name] = assessment
                for threat in self._active_threats.values():
                    if threat["name"] == name:
                        threat.update(assessment)
                        threat["source"] = "llm"

            if results:
                self._broadcast_threats()
        except Exception as e:
            log.error("Threat assessment failed: %s", e)
        finally:
            for name in card_names:
                self._assessing.discard(name)

    def _broadcast_threats(self):
        """Send active threats to UI, sorted by danger."""
        if not self.on_threat_update:
            log.warning("on_threat_update callback not set!")
            return
        threats = sorted(self._active_threats.values(),
                         key=lambda t: t.get("danger", 0), reverse=True)
        signature = tuple(
            (
                threat.get("instance_id"),
                threat.get("name"),
                threat.get("danger"),
                threat.get("priority"),
                bool(threat.get("must_answer")),
                threat.get("role"),
                threat.get("source"),
                threat.get("summary"),
                threat.get("reason"),
                threat.get("decision_hint"),
            )
            for threat in threats
        )
        if signature == self._last_threat_signature:
            return
        self._last_threat_signature = signature
        log.info("Broadcasting %d threats: %s",
                 len(threats), [t["name"] for t in threats])
        self.on_threat_update(threats)

    def _derived_plan_from_threats(self) -> str | None:
        engines = [
            t for t in self._active_threats.values()
            if t.get("category") == "engine"
        ]
        if not engines:
            return None
        engines = sorted(
            engines,
            key=lambda t: (bool(t.get("must_answer")), t.get("danger", 0)),
            reverse=True,
        )
        names = ", ".join(t["name"] for t in engines[:2])
        if any("landfall" in (t.get("summary", "")).lower() for t in engines):
            return f"Board snowball engine around {names}; land drops convert into a much bigger board."
        if any("token" in (t.get("summary", "")).lower() for t in engines):
            return f"Token engine around {names}; if left alone it snowballs cards and board size."
        return f"Board-centric engine deck built around {names}; unanswered permanents will take over."

    def _broadcast_strategy_info(self):
        """Send strategy/opponent info to UI."""
        if not self.on_strategy_info:
            return
        self.on_strategy_info(self.current_strategy_info())

    def current_strategy_info(self) -> dict:
        """Return the latest opponent/strategy snapshot for the UI."""
        opp_deck_obj = self._opp_tracker.identified_deck
        derived_plan = self._derived_plan_from_threats()
        strat = self._strategy
        return {
            "strategy_name": strat.name if strat else None,
            "archetype": strat.archetype if strat else None,
            "rule_count": len(strat.rules) if strat else 0,
            "meta_deck_count": len(self._meta_decks),
            "opp_deck": self._last_opp_deck,
            "opp_confidence": round(self._opp_tracker.confidence * 100)
                if self._last_opp_deck else 0,
            "opp_archetype": opp_deck_obj.archetype if opp_deck_obj else None,
            "opp_speed": opp_deck_obj.speed if opp_deck_obj else None,
            "opp_kill_turn": opp_deck_obj.typical_kill_turn if opp_deck_obj else None,
            "opp_hidden_reach": opp_deck_obj.hidden_reach if opp_deck_obj else None,
            "opp_plan": (
                opp_deck_obj.description
                if opp_deck_obj and opp_deck_obj.description
                else derived_plan
            ),
            "opp_key_threats": opp_deck_obj.key_threats[:4] if opp_deck_obj else [],
            "opp_cards_seen": list(self._opp_tracker.seen_cards.keys()),
            "matchup_wr": self._matchup_wr.get("win_rate") if self._matchup_wr else None,
            "matchup_games": self._matchup_wr.get("total", 0) if self._matchup_wr else 0,
            "global_biases": strat.global_biases if strat else {},
            # Debug metadata
            "debug": {
                "engine_version": ENGINE_VERSION,
                "colors": strat.colors if strat else [],
                "deck_signature": strat.deck_signature[:8] if strat else [],
                "general_overrides": len(strat.general_overrides) if strat else 0,
                "vulnerabilities": strat.vulnerabilities[:5] if strat else [],
                "stats": strat.stats if strat else {},
                "rules_by_layer": self._rules_by_layer() if strat else {},
                "biases": strat.global_biases if strat else {},
            },
        }

    def _rules_by_layer(self) -> dict[str, int]:
        """Count rules per layer for debug info."""
        counts: dict[str, int] = {}
        if self._strategy:
            for r in self._strategy.rules:
                counts[r.layer] = counts.get(r.layer, 0) + 1
        return counts

    def _build_intel_advice(self, state: GameState,
                            base_advice: list[Advice]) -> list[Advice]:
        intel: list[Advice] = []
        seen_messages = {a.message.lower() for a in base_advice}
        opp_deck_obj = self._opp_tracker.identified_deck

        if opp_deck_obj and self._opp_tracker.confidence >= 0.55:
            plan = opp_deck_obj.description or (
                f"{opp_deck_obj.name} ({opp_deck_obj.archetype}, {opp_deck_obj.speed})"
            )
            details_bits = [opp_deck_obj.name]
            if opp_deck_obj.typical_kill_turn:
                details_bits.append(f"kills ~T{opp_deck_obj.typical_kill_turn}")
            if opp_deck_obj.hidden_reach:
                details_bits.append(f"reach {opp_deck_obj.hidden_reach} dmg")
            intel.append(Advice(
                source="intel",
                priority="low",
                message=f"Their plan: {plan}",
                details=" | ".join(details_bits),
            ))

        live_threats = sorted(
            self._active_threats.values(),
            key=lambda t: (
                bool(t.get("must_answer")),
                t.get("category") == "engine",
                t.get("danger", 0),
            ),
            reverse=True,
        )
        engine_threats = [t for t in live_threats if t.get("category") == "engine"]
        if not intel:
            derived_plan = self._derived_plan_from_threats()
            if derived_plan:
                intel.append(Advice(
                    source="intel",
                    priority="low",
                    message=f"Their plan: {derived_plan}",
                    details="derived from live battlefield",
                ))
        if len(engine_threats) >= 2:
            pair = engine_threats[:2]
            intel.append(Advice(
                source="intel",
                priority="high",
                message=(
                    f"Engine online: {pair[0]['name']} + {pair[1]['name']} — "
                    "their board will snowball if unchecked"
                ),
                details=" / ".join(t.get("summary", "") for t in pair if t.get("summary")),
            ))
        for threat in live_threats:
            name = threat.get("name")
            reason = threat.get("reason") or threat.get("summary")
            if not name or not reason:
                continue
            msg = (
                f"Must answer {threat.get('role', threat.get('category', 'threat'))} {name} — {reason}"
                if threat.get("category") == "engine"
                and (threat.get("must_answer") or threat.get("priority") == "must-remove")
                else f"Must answer {threat.get('role', 'threat')} {name} — {reason}"
                if threat.get("must_answer") or threat.get("priority") == "must-remove"
                else f"Primary {threat.get('role', 'threat')}: {name} — {reason}"
            )
            if msg.lower() in seen_messages:
                continue
            intel.append(Advice(
                source="intel",
                priority=("high" if threat.get("must_answer")
                          or threat.get("priority") == "must-remove"
                          else "medium"),
                message=msg,
                details=" | ".join(x for x in [
                    threat.get("type_line", ""),
                    threat.get("decision_hint", ""),
                ] if x),
            ))
            break
        else:
            if opp_deck_obj and opp_deck_obj.key_threats:
                seen_cards = {c.casefold() for c in self._opp_tracker.seen_cards}
                for kt in opp_deck_obj.key_threats:
                    if not isinstance(kt, dict):
                        continue
                    card_name = str(kt.get("card", ""))
                    if not card_name or card_name.casefold() in seen_cards:
                        continue
                    reason = kt.get("reason")
                    if not reason:
                        continue
                    intel.append(Advice(
                        source="intel",
                        priority="low",
                        message=f"Watch for {card_name} — {reason}",
                        details=opp_deck_obj.name,
                    ))
                    break

        unique: list[Advice] = []
        emitted = set()
        for item in intel:
            key = item.message.lower()
            if key in seen_messages or key in emitted:
                continue
            emitted.add(key)
            unique.append(item)
        trimmed = unique[:2]
        if not trimmed:
            return []

        intel_signature = tuple(
            (item.priority, item.message, item.details or "")
            for item in trimmed
        )
        decision_key = None
        spot = self._normalized_advice_spot(state)
        if state.pending_request or spot in {"combat_attack", "combat_block"}:
            decision_key = (
                state.turn_info.turn_number,
                state.turn_info.phase,
                spot,
            )

        signature_changed = intel_signature != self._last_intel_signature
        decision_changed = (
            decision_key is not None
            and decision_key != self._last_intel_decision
        )
        if not signature_changed and not decision_changed:
            return []

        self._last_intel_signature = intel_signature
        if decision_key is not None:
            self._last_intel_decision = decision_key
        return trimmed

    def _sync_tracker_data(self):
        """Push opponent tracker data to heuristics module."""
        set_opp_tracker_data(
            self._opp_tracker.ability_triggers,
            self._opp_tracker.spent_removal)

    def on_stack_observed(self, event_type: str, data: dict):
        """Called when a spell or ability is observed on the stack."""
        name = data.get("name", "")
        colors = data.get("colors", [])

        if event_type == "opp_spell_cast":
            card_types = data.get("card_types", [])
            oracle = data.get("oracle_text", "")
            self._opp_tracker.observe_spell(name, colors, card_types, oracle)
            # Re-identify after new spell data
            if self._meta_decks:
                opp_deck = self._opp_tracker.identify(self._meta_decks)
                if opp_deck:
                    set_opp_deck(opp_deck)
                    opp_name = opp_deck.name
                    if opp_name != self._last_opp_deck:
                        self._last_opp_deck = opp_name
                        log.info("Opponent re-identified via spell: %s (%.0f%%)",
                                 opp_name, self._opp_tracker.confidence * 100)
                        self._broadcast_strategy_info()
        elif event_type == "opp_ability":
            self._opp_tracker.observe_ability(name)

        self._sync_tracker_data()

    def check_card_played(self, card_name: str, match_id: str,
                           turn: int, game_number: int):
        """Called when player plays a non-land card. Compare with recommendations."""
        if not match_id:
            return
        # Only compare plays on the same turn as the advice
        if self._pending_recs and turn != self._pending_turn:
            return

        # Determine compliance
        if not self._pending_recs:
            followed = False
            reason = "no_advice"
        elif card_name in self._pending_recs:
            followed = True
            reason = "followed"
        else:
            followed = False
            reason = "ignored_with_alternative"

        decision_id = f"{match_id}_{game_number}_{self._pending_game_state_id}"
        save_match_event(
            match_id, "advice_compliance",
            game_number=game_number,
            turn_number=turn,
            phase="play",
            data={"decision_id": decision_id,
                  "played": card_name,
                  "recommended": self._pending_recs,
                  "followed": followed,
                  "reason": reason,
                  "rec_count": len(self._pending_recs)})
        if self._pending_recs:
            self._pending_recs = []  # Clear after first play on this turn

    def _check_decision_outcomes(self, state: GameState):
        """Check if any pending decision outcomes can be resolved (2 turns later)."""
        if not hasattr(self, '_pending_outcomes'):
            self._pending_outcomes = []
        turn = state.turn_info.turn_number
        mid = state.match_info.match_id
        if not mid:
            return

        resolved = []
        game_num = state.match_info.game_number
        for po in self._pending_outcomes:
            # Only resolve within same game (BO3 boundary check)
            if po.get("game_number") != game_num:
                resolved.append(po)  # discard cross-game outcomes
                continue
            if turn >= po["turn"] + 2:
                # 2 turns have passed — compute delta
                me = state.my_player()
                opp = state.opp_player()
                my_creatures = len(state.my_creatures())
                opp_creatures = len(state.opp_creatures())
                try:
                    decision_id = f"{mid}_{po.get('game_number', game_num)}_{po.get('game_state_id', -1)}"
                    save_match_event(
                        mid, "decision_outcome",
                        game_number=po.get("game_number", game_num),
                        turn_number=po["turn"],
                        phase=po["phase"],
                        data={
                            "decision_id": decision_id,
                            "original_turn": po["turn"],
                            "resolved_turn": turn,
                            "game_number": po.get("game_number", game_num),
                            "life_delta": (me.life_total if me else 0) - po["my_life"],
                            "opp_life_delta": (opp.life_total if opp else 0) - po["opp_life"],
                            "creature_delta": my_creatures - po["my_creatures"],
                            "opp_creature_delta": opp_creatures - po["opp_creatures"],
                            "advice_count": po["advice_count"],
                        },
                    )
                except Exception:
                    pass
                resolved.append(po)
        for r in resolved:
            self._pending_outcomes.remove(r)

    def _record_pending_outcome(self, state: GameState, advice_count: int):
        """Record a snapshot for future outcome evaluation."""
        if not hasattr(self, '_pending_outcomes'):
            self._pending_outcomes = []
        me = state.my_player()
        opp = state.opp_player()
        self._pending_outcomes.append({
            "turn": state.turn_info.turn_number,
            "phase": state.turn_info.phase,
            "game_number": state.match_info.game_number,
            "game_state_id": state.game_state_id,
            "my_life": me.life_total if me else 0,
            "opp_life": opp.life_total if opp else 0,
            "my_creatures": len(state.my_creatures()),
            "opp_creatures": len(state.opp_creatures()),
            "advice_count": advice_count,
        })
        # Keep max 10 pending
        if len(self._pending_outcomes) > 10:
            self._pending_outcomes = self._pending_outcomes[-10:]

    def on_match_start(self):
        """Called when a new match starts — force fresh strategy detection."""
        log.info("New match — resetting strategy and trackers")
        self._pending_outcomes = []
        reset_llm_sessions()
        self._strategy = None
        self._strategy_loaded = False
        self._opp_tracker.reset()
        self._opp_seen_ids = set()
        self._last_opp_deck = None
        self._active_threats.clear()
        self._assessing.clear()
        self._last_advice = []
        self._pending_recs = []
        self._pending_turn = -1
        self._pending_game_state_id = -1
        self._advice_spot = None
        self._advice_sent_this_spot = set()
        self._advice_generation = 0
        self._last_auto_llm_key = None
        self._matchup_wr = None
        self._last_threat_signature = None
        self._last_intel_signature = None
        self._last_intel_decision = None
        self._set_llm_status(
            state="idle" if self._auto_llm else "disabled",
            wait=False,
            source="auto",
        )

    def on_match_end(self, won: bool):
        """Called when match ends — triggers learning."""
        if self._strategy:
            learn_from_match(self._strategy, won)
        # Refresh heuristic caches so next match uses latest data
        reset_heuristic_caches()
        if self._opp_tracker.identified_deck:
            log.info("Opponent was: %s (%.0f%% confidence)",
                     self._opp_tracker.identified_deck.name,
                     self._opp_tracker.confidence * 100)
        reset_llm_sessions()
        # Reset for next match
        self.on_match_start()

    async def match_summary(self, state: GameState) -> Advice | None:
        """Generate post-match summary and lessons learned."""
        match_id = state.match_info.match_id
        if not match_id:
            return Advice("llm", "low", "No match data available")
        text = await generate_match_summary(match_id)

        result = Advice("llm", "medium", text, details="Post-match summary", confidence=0.7)
        if match_id:
            save_advice(match_id, {
                "game_number": state.match_info.game_number,
                "turn_number": state.turn_info.turn_number,
                "phase": "post_match", "source": "llm_summary",
                "priority": "medium", "message": text,
            })
        return result

    @property
    def last_advice(self) -> list[Advice]:
        return self._last_advice

    @property
    def active_threats(self) -> list[dict]:
        return sorted(self._active_threats.values(),
                       key=lambda t: t.get("danger", 0), reverse=True)

    def _finalize_advice(self, state: GameState, advice: list[Advice]) -> list[Advice]:
        if not advice:
            return []

        turn_num = state.turn_info.turn_number
        spot_key = (turn_num, state.turn_info.phase, self._normalized_advice_spot(state))
        if spot_key != self._advice_spot:
            self._advice_spot = spot_key
            self._advice_sent_this_spot = set()
            self._last_advice = []
        advice = [a for a in advice if a.message not in self._advice_sent_this_spot]
        if not advice:
            return []
        self._advice_sent_this_spot.update(a.message for a in advice)

        merged = list(self._last_advice)
        seen_messages = {a.message.lower() for a in merged}
        for item in advice:
            key = item.message.lower()
            if key in seen_messages:
                continue
            merged.append(item)
            seen_messages.add(key)

        prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        merged.sort(key=lambda a: (prio.get(a.priority, 4), -(a.action_scores[0].score if a.action_scores else 0.0)))
        self._last_advice = merged[:5]

        # ── Phase 1 Telemetry: decision_eval ──
        # Log rule contributions, advice ranking, and canonical actions.
        if state.match_info.match_id and advice:
            import re as _re
            from .reranker import build_mini_ctx_from_state
            decision_id = f"{state.match_info.match_id}_{state.match_info.game_number}_{state.game_state_id}"
            spot_key_str = f"{turn_num}_{state.turn_info.phase}_{self._normalized_advice_spot(state)}"
            eval_data = {
                "decision_id": decision_id,
                "game_state_id": state.game_state_id,
                "advice_count": len(merged),
                "top_advice": [],
                "recommended_cards": [],
                "strategy_name": self._strategy.name if self._strategy else None,
                "opp_deck": (self._opp_tracker.identified_deck.name
                             if self._opp_tracker and self._opp_tracker.identified_deck else None),
                "engine_version": ENGINE_VERSION,
                "spot_key": spot_key_str,
                "mini_ctx": build_mini_ctx_from_state(state),
                "mini_ctx_v": 1,
            }
            for a in merged[:5]:
                # Prefer structured action_scores; fall back to regex for old-style advice
                top_score = a.action_scores[0] if a.action_scores else None
                if top_score:
                    rule_id = top_score.rule_id
                    weight = top_score.rule_weight
                else:
                    rule_id = ""
                    weight = 0.0
                    if a.details:
                        m = _re.search(r':(\w+)\]', a.details)
                        if m:
                            rule_id = m.group(1)
                        w = _re.search(r'w:([\d.]+)', a.details)
                        if w:
                            weight = float(w.group(1))
                entry = {
                    "source": a.source,
                    "priority": a.priority,
                    "rule_id": rule_id,
                    "weight": weight,
                    "message": a.message[:80],
                    "confidence": a.confidence,
                    "recommended_cards": a.recommended_cards,
                }
                if top_score:
                    entry["action_family"] = top_score.family.value
                    entry["action_target"] = top_score.target
                    entry["action_score"] = top_score.score
                eval_data["top_advice"].append(entry)
            try:
                save_match_event(
                    state.match_info.match_id, "decision_eval",
                    game_number=state.match_info.game_number,
                    turn_number=turn_num,
                    phase=state.turn_info.phase,
                    data=eval_data,
                )
            except Exception:
                pass  # telemetry must never break gameplay
            # Record snapshot for 2-turn outcome evaluation
            self._record_pending_outcome(state, len(merged))

            # Shadow reranker (logs only, doesn't affect advice)
            try:
                if not hasattr(self, "_reranker"):
                    self._reranker = None
                if self._reranker is None:
                    from .reranker import Reranker
                    _model_path = Path(__file__).parent.parent / "data" / "models" / "reranker_v1.npz"
                    if _model_path.exists():
                        self._reranker = Reranker()
                        self._reranker.load(_model_path)
                    else:
                        self._reranker = False  # sentinel: no model file
                if self._reranker and self._reranker.trained:
                    shadow_state = eval_data["mini_ctx"]  # reuse already-computed mini_ctx
                    shadow_candidates = []
                    for a in merged[:5]:
                        top_score = a.action_scores[0] if a.action_scores else None
                        shadow_candidates.append({
                            "rank": len(shadow_candidates),
                            "rule_id": top_score.rule_id if top_score else "",
                            "action_family": top_score.family.value if top_score else "",
                            "score": top_score.score if top_score else 0.0,
                            "priority": a.priority,
                        })
                    if len(shadow_candidates) >= 2:
                        reranked = self._reranker.rerank(shadow_state, shadow_candidates)
                        save_match_event(
                            state.match_info.match_id, "reranker_shadow",
                            game_number=state.match_info.game_number,
                            turn_number=state.turn_info.turn_number,
                            phase=state.turn_info.phase,
                            data={
                                "decision_id": decision_id,
                                "engine_top": shadow_candidates[0].get("rule_id", ""),
                                "reranker_top": reranked[0].get("rule_id", ""),
                                "agreed": shadow_candidates[0].get("rule_id") == reranked[0].get("rule_id"),
                                "reranker_scores": [
                                    {"rule_id": c["rule_id"], "prob": c.get("reranker_prob", 0)}
                                    for c in reranked[:3]
                                ],
                            })
            except Exception:
                pass  # shadow must never break gameplay

        # Always update game_state_id to latest decision point (even without recs)
        self._pending_game_state_id = state.game_state_id

        recs = []
        for item in advice:
            recs.extend(item.recommended_cards)
        if recs:
            if state.turn_info.turn_number != self._pending_turn:
                self._pending_recs = []
                self._pending_turn = state.turn_info.turn_number
            for rec in recs:
                if rec not in self._pending_recs:
                    self._pending_recs.append(rec)

        if self.on_advice:
            self.on_advice(self._last_advice)

        if state.match_info.match_id:
            advice_copy = [
                (item.source, item.priority, item.message, item.details or "")
                for item in advice
            ]
            asyncio.get_event_loop().call_soon(
                _save_advice_batch,
                state.match_info.match_id,
                state.match_info.game_number,
                state.turn_info.turn_number,
                state.turn_info.phase,
                advice_copy,
            )
        return advice


async def generate_match_summary(match_id: str) -> str:
    """Generate LLM summary for any historical match by match_id."""
    from .llm_advisor import _call_claude_cli, _call_ollama, _call_anthropic_api, get_backend
    from .database import get_match_data_for_summary, get_cached_summary, save_cached_summary

    # Check cache first
    cached = get_cached_summary(match_id)
    if cached:
        return cached

    data = get_match_data_for_summary(match_id)
    if not data:
        return "No match data found for this match."

    m = data["match"]
    events = data["events"]
    advice = data["advice"]

    lines = [
        "Analyze this MTG Arena match and give a post-game review.",
        "Structure: 1) Result summary 2) Key turning points 3) Mistakes made "
        "4) What went well 5) One concrete lesson for next time.",
        "Keep it under 250 words.", "",
        f"Result: {m['result']}",
        f"Opponent: {m['opponent_name']}",
        f"Games: {m['game_count']}",
    ]

    if m["my_deck_name"]:
        lines.append(f"My deck: {m['my_deck_name']}")
    if m["opp_deck_name"]:
        lines.append(f"Opponent deck: {m['opp_deck_name']}")

    # Cards played
    my_cards = [e for e in events if e["type"] == "card_played"]
    if my_cards:
        card_names = [e["data"].get("name", "?") for e in my_cards
                      if not e["data"].get("is_land")]
        if card_names:
            lines.append(f"\nCards I played: {', '.join(card_names[:20])}")

    # Opponent cards
    opp_cards = [e for e in events if e["type"] == "opp_card_played"]
    if opp_cards:
        opp_names = list(dict.fromkeys(
            e["data"].get("name", "?") for e in opp_cards))
        lines.append(f"Opponent cards seen: {', '.join(opp_names[:15])}")

    # Opponent spells (instants/sorceries)
    opp_spells = [e for e in events if e["type"] == "opp_spell_cast"]
    if opp_spells:
        spell_names = [f"T{e['turn']}: {e['data'].get('name', '?')}"
                       for e in opp_spells]
        lines.append(f"Opponent spells cast: {', '.join(spell_names[:10])}")

    # Opponent ability triggers
    opp_abilities = [e for e in events if e["type"] == "opp_ability"]
    if opp_abilities:
        from collections import Counter
        ab_counts = Counter(e["data"].get("name", "?") for e in opp_abilities)
        ab_strs = [f"{name} ({count}x)" for name, count in ab_counts.most_common(5)]
        lines.append(f"Opponent ability triggers: {', '.join(ab_strs)}")

    board_growth = [e for e in events if e["type"] == "permanent_stats_changed"]
    if board_growth:
        lines.append("Board growth / scaling:")
        for e in board_growth[-12:]:
            d = e["data"]
            lines.append(
                f"  T{e['turn']}: {d.get('controller', '?')} {d.get('name', '?')} "
                f"{d.get('old_power', 0)}/{d.get('old_toughness', 0)}"
                f"→{d.get('new_power', 0)}/{d.get('new_toughness', 0)}"
            )

    # Life changes
    life_events = [e for e in events if e["type"] == "life_change"]
    if life_events:
        lines.append("\nLife changes:")
        for e in life_events[-15:]:
            d = e["data"]
            who = "Me" if d.get("player") == "me" else "Opp"
            delta = d.get("delta", 0)
            lines.append(f"  T{e['turn']}: {who} {d.get('old', '?')}"
                         f"→{d.get('new', '?')} ({delta:+d})")

    # Mulligan
    mulls = [e for e in events if e["type"] == "mulligan"]
    if mulls:
        for e in mulls:
            dec = e["data"].get("decision", "")
            dec_str = "kept" if "Accept" in dec else "mulliganed"
            lines.append(f"\nGame {e['game']}: {dec_str}")

    # Opponent attacks
    opp_attacks = [e for e in events if e["type"] == "opp_attack_declared"]
    if opp_attacks:
        lines.append("\nOpponent attacks:")
        for e in opp_attacks[-15:]:
            d = e["data"]
            lines.append(f"  T{e['turn']}: {d.get('name', '?')} "
                         f"{d.get('power', 0)}/{d.get('toughness', 0)}")

    # Creature removals (deaths, exile, bounce) with B1 cause info
    removals = [e for e in events if e["type"] == "creature_left_bf"]
    if removals:
        lines.append("\nCreature removals:")
        for e in removals[-15:]:
            d = e["data"]
            owner = "My" if d.get("owner") == "me" else "Opp"
            dest = d.get("destination", "removed")
            temp = " [was under temporary exile!]" if d.get("temporary_exile") else ""
            cause = f" by {d['caused_by']}" if d.get("caused_by") else ""
            lines.append(f"  T{e['turn']}: {owner} {d.get('name', '?')} "
                         f"{d.get('power', 0)}/{d.get('toughness', 0)} — {dest}{cause}{temp}")

    # B2: Block declarations
    blocks = [e for e in events if e["type"] == "block_declared"]
    if blocks:
        lines.append("\nBlock declarations:")
        for e in blocks[-10:]:
            d = e["data"]
            if d.get("no_blocks"):
                lines.append(f"  T{e['turn']}: No blocks declared")
            elif d.get("blocker"):
                atks = ", ".join(a.get("name", "?") for a in d.get("attackers", []))
                lines.append(f"  T{e['turn']}: {d['blocker']} "
                             f"{d.get('blocker_power', 0)}/{d.get('blocker_toughness', 0)} "
                             f"blocked {atks}")

    # B3: Enchantment attachments
    enchants = [e for e in events if e["type"] == "enchantment_attached"]
    if enchants:
        lines.append("\nEnchantment targets:")
        for e in enchants[-10:]:
            d = e["data"]
            owner = "my" if d.get("target_owner") == "me" else "opp"
            lines.append(f"  T{e['turn']}: {d.get('aura', '?')} on {owner} "
                         f"{d.get('target', '?')}")

    decision_contexts = [e for e in events if e["type"] == "decision_context"]
    if decision_contexts:
        last_ctx = decision_contexts[-1]["data"]
        opp_board = [
            c.get("name", "?") for c in last_ctx.get("opp_battlefield", [])
            if "Land" not in c.get("types", [])
        ]
        hand = [c.get("name", "?") for c in last_ctx.get("my_hand", [])]
        legal = [a.get("action_type", "") + (f":{a.get('name')}" if a.get("name") else "")
                 for a in last_ctx.get("legal_actions", [])[:10]]
        if opp_board or hand or legal:
            lines.append("\nLast decision context:")
            if opp_board:
                lines.append(f"  Opp board: {', '.join(opp_board[:12])}")
            if hand:
                lines.append(f"  My hand: {', '.join(hand[:10])}")
            if legal:
                lines.append(f"  Legal actions: {', '.join(legal)}")

    # Compliance
    compliance = [e for e in events if e["type"] == "advice_compliance"]
    if compliance:
        followed = sum(1 for e in compliance if e["data"].get("followed"))
        ignored = len(compliance) - followed
        lines.append(f"\nAdvice compliance: {followed} followed, {ignored} ignored")

    # Key advisor suggestions
    if advice:
        lines.append(f"\nAdvisor gave {len(advice)} suggestions:")
        shown = advice[:5] + advice[-5:] if len(advice) > 10 else advice
        for a in shown:
            lines.append(f"  T{a['turn']} [{a['source']}] {a['message'][:100]}")

    # Max turn
    turn_nums = [e["turn"] for e in events if e["turn"]]
    if turn_nums:
        lines.append(f"\nMatch lasted {max(turn_nums)} turns")

    prompt = "\n".join(lines)
    backend = get_backend()

    try:
        if backend == "claude_cli":
            text = await _call_claude_cli(prompt)
        elif backend == "ollama":
            text = await _call_ollama(prompt)
        elif backend == "anthropic_api":
            text = await _call_anthropic_api(prompt)
        else:
            return "No LLM backend available."
    except Exception as e:
        return f"Error generating summary: {e}"

    # Cache the result
    save_cached_summary(match_id, text)
    return text
