"""Advisor orchestrator — heuristics + layered strategy rules, LLM only on demand."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .database import card_cache, get_matchup_wr, get_observed_opp_decks, save_advice, save_match, save_match_event
from .heuristics import (
    analyze as heuristic_analyze, reset_caches as reset_heuristic_caches,
    set_my_archetype, set_opp_deck, set_opp_tracker_data,
)
from .llm_advisor import assess_threats, get_advice as llm_get_advice
from .models import Advice, CardInfo, GameState
from .strategy import (
    MetaDeck, OpponentTracker, evaluate_rules, get_or_create_strategy,
    learn_from_match, load_meta_decks, save_meta_decks,
    update_opponent_tracking, Strategy,
)

log = logging.getLogger(__name__)

_last_advice_state_id: tuple[int, str] = (-1, "")


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
    text = (" ".join(card.abilities) + " " + (card.oracle_text or "")).lower()
    if "Planeswalker" in card.card_types:
        return 5
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
    text = (" ".join(card.abilities) + " " + card.oracle_text).lower()
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
        # Advice deduplication: track all messages sent this turn
        self._advice_turn: int = -1
        self._advice_sent_this_turn: set[str] = set()
        # Threat tracking
        self._threat_cache: dict[str, dict] = {}   # card_name -> LLM assessment
        self._active_threats: dict[int, dict] = {}  # instance_id -> threat info
        self._assessing: set[str] = set()            # card names in-flight

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

    async def on_state_change(self, state: GameState):
        """Called on every game state update. Runs heuristics + strategy rules."""
        global _last_advice_state_id

        # Dedup key: state_id + pending_request (decision points change request
        # without changing state_id, e.g. DeclareBlockersReq)
        dedup_key = (state.game_state_id, state.pending_request or "")
        if dedup_key == _last_advice_state_id:
            return
        # Set immediately to prevent duplicate async calls for same state
        _last_advice_state_id = dedup_key

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

        # Run heuristics
        advice = heuristic_analyze(state)

        # Run strategy rules
        if self._strategy:
            strategy_advice = evaluate_rules(
                self._strategy.rules, state, self._opp_tracker,
                vulnerabilities=self._strategy.vulnerabilities)
            heuristic_msgs = {a.message.lower() for a in advice}
            for sa in strategy_advice:
                if sa.message.lower() not in heuristic_msgs:
                    advice.append(sa)

        if not advice:
            return

        prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        advice.sort(key=lambda a: prio.get(a.priority, 4))
        advice = advice[:3]

        # Per-turn dedup: filter out messages already sent this turn
        turn_num = state.turn_info.turn_number
        if turn_num != self._advice_turn:
            self._advice_turn = turn_num
            self._advice_sent_this_turn = set()
        advice = [a for a in advice if a.message not in self._advice_sent_this_turn]
        if not advice:
            return
        self._advice_sent_this_turn.update(a.message for a in advice)

        self._last_advice = advice

        # Track recommendations for compliance checking (accumulate within turn)
        recs = []
        for a in advice:
            recs.extend(a.recommended_cards)
        if recs:
            if state.turn_info.turn_number != self._pending_turn:
                self._pending_recs = []
                self._pending_turn = state.turn_info.turn_number
            for r in recs:
                if r not in self._pending_recs:
                    self._pending_recs.append(r)

        if self.on_advice:
            self.on_advice(advice)

        if state.match_info.match_id:
            match_id = state.match_info.match_id
            game_num = state.match_info.game_number
            turn_num = state.turn_info.turn_number
            phase = state.turn_info.phase
            advice_copy = [(a.source, a.priority, a.message, a.details or "") for a in advice]
            asyncio.get_event_loop().call_soon(
                _save_advice_batch, match_id, game_num, turn_num, phase, advice_copy)

    async def on_decision_point(self, state: GameState, request_type: str):
        await self.on_state_change(state)

    async def ask_llm(self, state: GameState) -> Advice | None:
        """Manually trigger LLM advice."""
        advice = await llm_get_advice(state, state.pending_request or "")
        if advice and advice.message:
            self._last_advice = [a for a in self._last_advice
                                 if a.source in ("heuristic", "strategy")]
            self._last_advice.append(advice)
            if state.match_info.match_id:
                save_advice(state.match_info.match_id, {
                    "game_number": state.match_info.game_number,
                    "turn_number": state.turn_info.turn_number,
                    "phase": state.turn_info.phase,
                    "source": advice.source, "priority": advice.priority,
                    "message": advice.message,
                })
            if self.on_advice:
                self.on_advice(self._last_advice)
        return advice

    def _update_threats(self, state: GameState):
        """Detect new opponent permanents and assess threats."""
        opp_bf = state.opp_battlefield()
        current_ids = {o.instance_id for o in opp_bf}

        # Remove threats that left the battlefield
        removed = [iid for iid in self._active_threats if iid not in current_ids]
        for iid in removed:
            del self._active_threats[iid]

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
                "summary": _quick_summary(card),
                "priority": ("must-remove" if danger >= 4
                             else "should-remove" if danger >= 3
                             else "monitor"),
                "source": "heuristic",
            }

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

        # Broadcast if anything changed
        if self._active_threats or removed:
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
            results = await assess_threats(cards, strategy_name, opp_deck)

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
        log.info("Broadcasting %d threats: %s",
                 len(threats), [t["name"] for t in threats])
        self.on_threat_update(threats)

    def _broadcast_strategy_info(self):
        """Send strategy/opponent info to UI."""
        if not self.on_strategy_info:
            return
        opp_deck_obj = self._opp_tracker.identified_deck
        info = {
            "strategy_name": self._strategy.name if self._strategy else None,
            "archetype": self._strategy.archetype if self._strategy else None,
            "rule_count": len(self._strategy.rules) if self._strategy else 0,
            "meta_deck_count": len(self._meta_decks),
            "opp_deck": self._last_opp_deck,
            "opp_confidence": round(self._opp_tracker.confidence * 100)
                if self._last_opp_deck else 0,
            "opp_archetype": opp_deck_obj.archetype if opp_deck_obj else None,
            "opp_speed": opp_deck_obj.speed if opp_deck_obj else None,
            "opp_kill_turn": opp_deck_obj.typical_kill_turn if opp_deck_obj else None,
            "opp_hidden_reach": opp_deck_obj.hidden_reach if opp_deck_obj else None,
            "opp_key_threats": [t.get("card", t) if isinstance(t, dict) else t
                                for t in (opp_deck_obj.key_threats[:3] if opp_deck_obj else [])],
            "opp_cards_seen": list(self._opp_tracker.seen_cards.keys()),
            "matchup_wr": self._matchup_wr.get("win_rate") if self._matchup_wr else None,
            "matchup_games": self._matchup_wr.get("total", 0) if self._matchup_wr else 0,
        }
        self.on_strategy_info(info)

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
        if not self._pending_recs or not match_id:
            return
        # Only compare plays on the same turn as the advice
        if turn != self._pending_turn:
            return
        followed = card_name in self._pending_recs
        save_match_event(
            match_id, "advice_compliance",
            game_number=game_number,
            turn_number=turn,
            phase="play",
            data={"played": card_name,
                  "recommended": self._pending_recs,
                  "followed": followed})
        self._pending_recs = []  # Clear after first play on this turn

    def on_match_start(self):
        """Called when a new match starts — force fresh strategy detection."""
        log.info("New match — resetting strategy and trackers")
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
        self._advice_turn = -1
        self._advice_sent_this_turn = set()
        self._matchup_wr = None

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
        # Reset for next match
        self.on_match_start()

    async def match_summary(self, state: GameState) -> Advice | None:
        """Generate post-match summary and lessons learned."""
        from .llm_advisor import _call_claude_cli, _call_ollama, _call_anthropic_api, get_backend
        from .database import get_connection
        import json

        match_id = state.match_info.match_id
        if not match_id:
            return Advice("llm", "low", "No match data available")

        conn = get_connection()
        events = conn.execute(
            "SELECT event_type, turn_number, phase, data FROM match_events "
            "WHERE match_id = ? ORDER BY id", (match_id,)).fetchall()
        advice_rows = conn.execute(
            "SELECT turn_number, phase, source, message FROM advice_log "
            "WHERE match_id = ? ORDER BY id", (match_id,)).fetchall()
        match_row = conn.execute(
            "SELECT opponent_name, result, game_count, format FROM matches WHERE match_id = ?",
            (match_id,)).fetchone()
        conn.close()

        me = state.my_player()
        opp = state.opp_player()
        opp_name = (match_row[0] if match_row and match_row[0] else
                    state.match_info.opponent_name or "Unknown")

        lines = [
            "Analyze this MTG Arena match and give a post-game review.",
            "Structure: 1) Result summary 2) Key turning points 3) Mistakes made "
            "4) What went well 5) One concrete lesson for next time.",
            "Keep it under 250 words.", "",
            f"Result: {match_row[1] if match_row else 'Unknown'}",
            f"Opponent: {opp_name}",
            f"Games: {match_row[2] if match_row else '?'}",
            f"Final life: Me {me.life_total if me else '?'}, Opp {opp.life_total if opp else '?'}",
            f"Turn count: {state.turn_info.turn_number}",
        ]

        if self._strategy:
            lines.append(f"\nDeck: {self._strategy.name} ({self._strategy.archetype})")
            lines.append(f"Deck win rate: {self._strategy.win_rate():.0%} "
                         f"({self._strategy.stats['games']} games)")

        if self._opp_tracker.identified_deck:
            od = self._opp_tracker.identified_deck
            lines.append(f"Opponent deck: {od.name} ({od.archetype}, speed: {od.speed})")

        if self._opp_tracker.seen_cards:
            lines.append(f"\nOpponent cards seen: {', '.join(self._opp_tracker.seen_cards.keys())}")

        life_events = [e for e in events if e[0] == "life_change"]
        if life_events:
            lines.append("\nLife changes:")
            for _, turn, phase, data_str in life_events[-15:]:
                try:
                    d = json.loads(data_str)
                    who = "Me" if d["player"] == "me" else "Opp"
                    lines.append(f"  T{turn}: {who} {d['old']}→{d['new']} ({d['delta']:+d})")
                except (json.JSONDecodeError, KeyError):
                    pass

        if advice_rows:
            lines.append(f"\nAdvisor ({len(advice_rows)} suggestions):")
            shown = advice_rows[:5] + advice_rows[-5:] if len(advice_rows) > 10 else advice_rows
            for turn, phase, source, msg in shown:
                lines.append(f"  T{turn} [{source}] {msg[:100]}")

        prompt = "\n".join(lines)
        backend = get_backend()
        try:
            if backend == "claude_cli":
                text = await _call_claude_cli(prompt)
            elif backend == "ollama":
                text = await _call_ollama(prompt)
            else:
                text = await _call_anthropic_api(prompt)
        except Exception as e:
            text = f"Error: {e}"

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
