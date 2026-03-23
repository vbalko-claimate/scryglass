"""Heuristic-based play advisor — short, actionable suggestions."""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from .database import card_cache
from .models import Advice, GameState, GameObject

if TYPE_CHECKING:
    from .strategy import MetaDeck

# Current opponent deck (set by advisor_engine when opponent identified)
_current_opp_deck: "MetaDeck | None" = None
# My deck archetype (set by advisor_engine when strategy loaded)
_my_archetype: str = "unknown"
# Opponent tracking data (set by advisor_engine)
_opp_ability_triggers: dict[str, int] = {}  # source_card_name: count
_opp_spent_removal: list[str] = []


def set_opp_deck(deck: "MetaDeck | None"):
    """Set identified opponent deck for threat scoring."""
    global _current_opp_deck
    _current_opp_deck = deck


def set_my_archetype(archetype: str):
    """Set player's deck archetype for deck-aware heuristics."""
    global _my_archetype
    _my_archetype = archetype


def set_opp_tracker_data(ability_triggers: dict[str, int],
                         spent_removal: list[str]):
    """Update opponent tracking data for heuristic scoring."""
    global _opp_ability_triggers, _opp_spent_removal
    _opp_ability_triggers = ability_triggers
    _opp_spent_removal = spent_removal


# Cached card win rates + player preferences (loaded once per session)
_card_wr: dict[str, float] | None = None
_player_prefs: dict[str, float] | None = None


def _get_card_wr() -> dict[str, float]:
    global _card_wr
    if _card_wr is not None:
        return _card_wr
    from .database import get_card_win_rates
    _card_wr = get_card_win_rates()
    return _card_wr


def _get_player_prefs() -> dict[str, float]:
    global _player_prefs
    if _player_prefs is not None:
        return _player_prefs
    from .database import get_player_preferences
    _player_prefs = get_player_preferences()
    return _player_prefs


def _card_score(name: str) -> float:
    """Effective card score: WR + player preference adjustment."""
    wr = _get_card_wr().get(name, 50)
    pref = _get_player_prefs().get(name, 0)
    return wr + pref * 2  # Each preference point = ~2% WR shift


def reset_caches():
    """Reset WR/preference caches (called on match end to pick up new data)."""
    global _card_wr, _player_prefs, _current_opp_deck, _my_archetype
    global _opp_ability_triggers, _opp_spent_removal
    _card_wr = None
    _player_prefs = None
    _current_opp_deck = None
    _my_archetype = "unknown"
    _opp_ability_triggers = {}
    _opp_spent_removal = []


def hand_synergy_score(candidate_grp_id: int, hand: list[GameObject]) -> int:
    """C1: Score how well a card synergizes with the rest of the hand.

    Checks if playing the candidate enables triggers on other hand cards.
    Works across all color combinations.
    """
    candidate = card_cache.get(candidate_grp_id)
    if not candidate:
        return 0

    cand_text = " ".join(a.lower() for a in candidate.abilities)
    cand_oracle = (candidate.oracle_text or "").lower()
    cand_combined = cand_text + " " + cand_oracle

    # What does this candidate provide?
    is_creature = candidate.is_creature
    is_noncreature = not is_creature and not candidate.is_land
    has_lifelink = "lifelink" in cand_combined
    has_passive_lifegain = ("gain" in cand_combined and "life" in cand_combined
                            and "whenever" in cand_combined)
    has_etb_lifegain = (has_passive_lifegain
                        and ("enters" in cand_combined or "creature" in cand_combined))
    provides_lifegain = has_lifelink or has_passive_lifegain
    cand_subtypes = set(s.lower() for s in candidate.subtypes)

    score = 0
    for obj in hand:
        if obj.grp_id == candidate_grp_id:
            continue
        other = card_cache.get(obj.grp_id)
        if not other:
            continue
        other_text = " ".join(a.lower() for a in other.abilities)
        other_oracle = (other.oracle_text or "").lower()
        other_combined = other_text + " " + other_oracle

        # --- Lifegain synergy (W/WB) ---
        if "whenever you gain life" in other_combined:
            if has_etb_lifegain:
                score += 3
            elif has_lifelink:
                score += 1
            elif has_passive_lifegain:
                score += 2

        # --- Creature-enters synergy (all colors) ---
        if "whenever" in other_combined and "enters" in other_combined and is_creature:
            score += 1

        # --- Tribal synergy (all colors) ---
        for st in cand_subtypes:
            if "whenever" in other_combined and st in other_combined:
                score += 1

        # --- Spellslinger / Prowess synergy (R/U) ---
        _spell_triggers = [
            "prowess", "whenever you cast a noncreature",
            "whenever you cast an instant", "whenever you cast a sorcery",
            "magecraft",
        ]
        if is_noncreature and any(t in other_combined for t in _spell_triggers):
            score += 2

        # --- Death / Sacrifice synergy (B/BR) ---
        _death_triggers = [
            "whenever a creature dies", "whenever another creature",
            "whenever you sacrifice", "whenever a creature you control dies",
        ]
        if is_creature and any(t in other_combined for t in _death_triggers):
            score += 1
        # Sac outlets boost creatures with death triggers
        if "sacrifice" in other_combined and "dies" in cand_combined:
            score += 2

        # --- Attack synergy (R/W aggro) ---
        if is_creature and "whenever" in other_combined and "attack" in other_combined:
            score += 1

        # --- +1/+1 counter payoffs (all colors) ---
        if "+1/+1 counter" in other_combined:
            if provides_lifegain:
                score += 1
            if "adapt" in cand_combined or "+1/+1 counter" in cand_combined:
                score += 1

    return score


def evaluate_opponent_board(state: GameState) -> list[tuple[GameObject, float, str]]:
    """C2: Score opponent creatures by threat level.

    Returns list of (game_object, score, reason) sorted by score descending.
    """
    opp_creatures = state.opp_creatures()
    if not opp_creatures:
        return []

    # Count subtypes for tribal bonus
    subtype_counts: dict[str, int] = {}
    for obj in opp_creatures:
        c = card_cache.get(obj.grp_id)
        if c:
            for st in c.subtypes:
                subtype_counts[st] = subtype_counts.get(st, 0) + 1

    my_player = state.my_player()
    visible_attackers = [o for o in opp_creatures if not o.is_tapped]
    visible_power = sum(max(0, o.power) for o in visible_attackers)

    scored: list[tuple[GameObject, float, str]] = []
    for obj in opp_creatures:
        card = card_cache.get(obj.grp_id)
        if not card:
            scored.append((obj, obj.power * 1.5, "unknown card"))
            continue

        reasons = []
        text = " ".join(a.lower() for a in card.abilities)
        oracle = (card.oracle_text or "").lower()
        combined = text + " " + oracle

        # Double strike effectively doubles combat damage
        power_mult = 2.0 if "double strike" in combined else 1.0
        score = obj.power * 1.5 * power_mult + obj.toughness * 0.5

        # Keyword bonuses — conditional protection gets lower bonus + label
        kw_bonuses = [
            ("flying", 3, "flying"),
            ("deathtouch", 4, "deathtouch"),
            ("lifelink", 3, "lifelink"),
            ("trample", 2, "trample"),
            ("menace", 2, "menace"),
            ("haste", 2, "haste"),
            ("hexproof", 5, "hexproof"),
            ("indestructible", 6, "indestructible"),
            ("double strike", 4, "double strike"),
            ("first strike", 2, "first strike"),
            ("ward", 3, "ward"),
        ]
        for kw, bonus, label in kw_bonuses:
            if kw in combined:
                # Conditional protection keywords get reduced bonus
                if kw in ("hexproof", "indestructible", "ward"):
                    prot = _protection_status(card, kw)
                    if prot == "conditional":
                        score += bonus // 2  # half bonus — might not be active
                        reasons.append(f"{label} (conditional)")
                        continue
                score += bonus
                reasons.append(label)

        # Context adjustments based on MY board
        my_creatures = state.my_creatures()
        my_flyer_count = sum(
            1 for m in my_creatures
            if any("flying" in a.lower()
                   for a in (card_cache.get(m.grp_id).abilities
                             if card_cache.get(m.grp_id) else []))
        )
        has_flying = "flying" in combined
        # If most of my creatures fly, opponent's flying is less threatening
        # (I can block it) — reduce the flying bonus
        if has_flying and my_flyer_count >= 2:
            score -= 2  # partially cancel the +3 flying bonus
        # If opponent creature does NOT fly and I mostly fly over it,
        # it's less of a removal priority (I can ignore it in combat)
        if not has_flying and my_flyer_count >= 2 and obj.power <= 3:
            score -= 2
            if "can't block" not in reasons:
                reasons.append("can fly over")

        # Synergy engine detection
        if "whenever" in combined:
            score += 4
            # Extract what triggers it
            if "dies" in combined:
                reasons.append("death trigger")
                score += 2  # extra for death synergy
            elif "enters" in combined:
                reasons.append("ETB trigger")
            elif "attacks" in combined:
                reasons.append("attack trigger")
            else:
                reasons.append("triggered ability")

        if "each opponent" in combined or "each player" in combined:
            score += 3
            reasons.append("drain effect")

        if "create" in combined and "token" in combined:
            score += 3
            reasons.append("token generator")

        if "+1/+1 counter" in combined:
            score += 2
            reasons.append("grows")

        # Role classification for cards you may not recognize from the meta.
        # These labels also feed removal priority: payoff > enabler when both exist.
        if "becomes a copy" in combined or "copy of target creature card in your graveyard" in combined:
            score += 8
            reasons.insert(0, "graveyard payoff")
        elif "copy of any creature" in combined:
            score += 6
            reasons.insert(0, "copy payoff")

        if any(p in combined for p in [
            "return target creature card from your graveyard",
            "return target nonland permanent card with mana value",
            "from your graveyard to the battlefield",
            "from your graveyard to your hand",
        ]):
            score += 5
            reasons.insert(0, "recursion payoff")

        if any(p in combined for p in [
            "surveil", "mill", "draw a card, then discard", "map token", "create a map",
        ]):
            score += 2
            reasons.append("graveyard setup")

        # Prowess / spell-trigger growth detection
        _prowess_patterns = [
            "prowess", "whenever you cast a noncreature",
            "whenever you cast an instant or sorcery",
            "whenever you cast a", "magecraft",
        ]
        if any(p in combined for p in _prowess_patterns):
            score += 5
            reasons.append("grows with spells")

        # Tribal synergy bonus
        for st in card.subtypes:
            tribal_count = subtype_counts.get(st, 0)
            if tribal_count >= 2:
                score += (tribal_count - 1) * 2
                # Extra bonus if this creature references its own tribe
                if st.lower() in combined and "whenever" in combined:
                    score += 5
                    reasons.append(f"{st} synergy engine")
                elif tribal_count >= 3:
                    reasons.append(f"{st} tribal ({tribal_count})")

        # Growth potential: current P/T above card base
        try:
            base_p = int(card.power) if card.power else 0
        except ValueError:
            base_p = 0
        if obj.power > base_p:
            score += 2
            reasons.append(f"buffed to {obj.power}/{obj.toughness}")

        # Immediate crackback pressure should beat slower engine value.
        # If the visible board is near lethal, prefer the body whose removal
        # most reduces next-turn damage.
        if my_player and obj in visible_attackers:
            remaining_if_removed = visible_power - max(0, obj.power)
            if visible_power >= my_player.life_total and remaining_if_removed < my_player.life_total:
                score += 14
                reasons.insert(0, "prevents lethal crackback")
            elif visible_power >= my_player.life_total - 4 and obj.power >= 5:
                score += 8
                reasons.insert(0, "immediate crackback threat")
            elif obj.power >= 7:
                score += 5
                reasons.insert(0, "huge immediate clock")
            elif obj.power >= 5 and ("flying" in combined or "trample" in combined or "menace" in combined):
                score += 4
                reasons.insert(0, "hard-to-race attacker")

        # Ability trigger frequency boost — cards that trigger a lot are higher priority
        trigger_count = _opp_ability_triggers.get(card.name, 0)
        if trigger_count >= 3:
            score += min(trigger_count, 8)  # cap at +8
            reasons.append(f"triggered {trigger_count}x")
        elif trigger_count >= 1:
            score += trigger_count
            reasons.append(f"triggered {trigger_count}x")

        # Meta threat boost — identified opponent's key threats get priority
        opp_deck = _current_opp_deck
        if opp_deck and opp_deck.key_threats:
            for kt in opp_deck.key_threats:
                if card.name == kt.get("card"):
                    priority = kt.get("removal_priority", 2)
                    bonus = {1: 15, 2: 8, 3: 4}.get(priority, 4)
                    score += bonus
                    if kt.get("must_answer"):
                        score += 5
                        reasons.insert(0, "MUST ANSWER")
                    reasons.insert(0, kt.get("reason", "meta threat"))
                    break

        aura_bonus, aura_reasons = _attached_aura_pressure(obj, state)
        if aura_bonus:
            score += aura_bonus
            for aura_reason in reversed(aura_reasons):
                if aura_reason not in reasons:
                    reasons.insert(0, aura_reason)

        reason_str = ", ".join(reasons[:4]) if reasons else f"{obj.power}/{obj.toughness} body"
        scored.append((obj, score, reason_str))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def evaluate_opponent_engines(state: GameState) -> list[tuple[GameObject, float, str]]:
    """Score noncreature engine permanents that are worth clean removal.

    This is intentionally narrower than creature threat scoring: it only looks
    at noncreature artifacts / enchantments / planeswalkers that are likely to
    snowball the game if left in play.
    """
    known_engines = {
        "A Most Helpful Weaver": (8, "MUST ANSWER engine"),
        "Caretaker's Talent": (10, "MUST ANSWER token engine"),
        "Enduring Innocence": (9, "card draw engine"),
        "Enduring Curiosity": (9, "card draw engine"),
        "Felidar Retreat": (10, "MUST ANSWER landfall engine"),
        "High Noon": (8, "tempo lock piece"),
        "Temporary Lockdown": (9, "locks your cheap board"),
        "Unholy Annex": (9, "MUST ANSWER demon engine"),
        "Bandit's Talent": (7, "repeatable damage engine"),
        "Primeval Bounty": (10, "MUST ANSWER snowball engine"),
        "Ritual Chamber": (8, "demon payoff engine"),
    }

    scored: list[tuple[GameObject, float, str]] = []
    for obj in state.opp_battlefield():
        card = card_cache.get(obj.grp_id)
        if not card or card.is_creature:
            continue
        if not any(t in card.card_types for t in ("Enchantment", "Artifact", "Planeswalker")):
            continue

        oracle = (card.oracle_text or "").lower()
        combined = oracle + " " + " ".join(a.lower() for a in card.abilities)
        score = 0.0
        reasons: list[str] = []

        if "Planeswalker" in card.card_types:
            score += 10
            reasons.append("planeswalker engine")
        if "Enchantment" in card.card_types:
            score += 4
            reasons.append("enchantment engine")
        if "Artifact" in card.card_types:
            score += 2
            reasons.append("artifact value")

        if any(p in combined for p in ("whenever", "at the beginning", "at the end step", "at end step")):
            score += 4
            reasons.append("repeatable trigger")
        if "draw" in combined:
            score += 4
            reasons.append("card advantage")
        if "create" in combined and "token" in combined:
            score += 4
            reasons.append("token engine")
        if "role token" in combined or "map token" in combined:
            score += 3
            reasons.append("snowballs small bodies")
        if "+1/+1 counter" in combined:
            score += 2
            reasons.append("grows board")
        if "can't cast more than one spell" in combined:
            score += 5
            reasons.append("locks your sequencing")

        trigger_count = _opp_ability_triggers.get(card.name, 0)
        if trigger_count >= 1:
            score += min(trigger_count, 4)
            reasons.append(f"triggered {trigger_count}x")

        if card.name in known_engines:
            bonus, reason = known_engines[card.name]
            score += bonus
            reasons.insert(0, reason)

        opp_deck = _current_opp_deck
        if opp_deck and opp_deck.key_threats:
            for kt in opp_deck.key_threats:
                if card.name != kt.get("card"):
                    continue
                priority = kt.get("removal_priority", 2)
                bonus = {1: 12, 2: 7, 3: 4}.get(priority, 4)
                score += bonus
                if kt.get("must_answer"):
                    score += 4
                    reasons.insert(0, "MUST ANSWER")
                reason = kt.get("reason")
                if reason:
                    reasons.insert(0, reason)
                break

        if score < 4:
            continue

        reason_str = ", ".join(dict.fromkeys(reasons)) if reasons else "engine permanent"
        scored.append((obj, score, reason_str))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def analyze(state: GameState) -> list[Advice]:
    """Run all checks. Returns short actionable advice — max 2-3 items."""
    if not state.my_seat_id or not state.players:
        return []

    ti = state.turn_info
    combat_window = _combat_window(state)
    is_my_turn = ti.active_player == state.my_seat_id
    if combat_window == "attack":
        is_my_turn = True
    elif combat_window == "block":
        is_my_turn = False

    advice = []

    # Mulligan
    advice.extend(_check_mulligan(state))

    # Hand disruption warning (fires once, turn-agnostic)
    advice.extend(_check_hand_disruption(state))
    advice.extend(_red_exact_ten_setup_warning(state))

    if is_my_turn:
        # Only suggest attacks/lethal on our turn
        advice.extend(_check_lethal(state))
        if combat_window == "attack":
            advice.extend(_suggest_attacks(state))
        else:
            advice.extend(_suggest_plays(state))
    else:
        # Opponent's turn — check their lethal and suggest blocks
        advice.extend(_check_opponent_lethal(state))
        if combat_window == "block":
            advice.extend(_check_combat_blocks(state))

    # Cap at 3 most important
    advice.sort(key=lambda a: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(a.priority, 4))
    return advice[:3]


def _combat_window(state: GameState) -> str | None:
    """Normalize the current combat decision window.

    GRE often emits multiple request variants for the same practical spot
    (`DeclareAttackersReq`, `ActionsAvailableReq`, `Begin Combat`). Using the
    visible phase text is more reliable than the raw request type alone.
    """
    display = (state.turn_info.phase_display or "").lower()
    req = state.pending_request or ""
    is_my_turn = state.turn_info.active_player == state.my_seat_id

    if is_my_turn:
        if req == "GREMessageType_DeclareAttackersReq":
            return "attack"
        if "begin combat" in display or "declare attackers" in display:
            return "attack"
    else:
        if req == "GREMessageType_DeclareBlockersReq":
            return "block"
        if "declare blockers" in display:
            return "block"

    return None


def _check_hand_disruption(state: GameState) -> list[Advice]:
    """Warn when opponent has exiled/discarded cards from our hand this game."""
    count = state.hand_disrupted_count
    if count <= 0:
        return []
    hand_size = len(state.my_hand())
    if count == 1:
        msg = f"Opponent disrupted your hand — 1 card lost. Hand: {hand_size} cards left."
        priority = "medium"
    else:
        msg = f"Opponent disrupted your hand — {count} cards lost. Hand: {hand_size} cards left. Play around further disruption."
        priority = "high"
    return [Advice("heuristic", priority, msg)]


def _red_exact_ten_setup_warning(state: GameState) -> list[Advice]:
    """Warn about Hidetsugu's Second Rite style exact-10 setups."""
    me = state.my_player()
    if not me or me.life_total <= 0:
        return []

    opp_mana, opp_colors = _opp_open_mana_colors(state)
    deck_colors = set(_current_opp_deck.colors) if _current_opp_deck else set()
    deck_name = (_current_opp_deck.name.lower() if _current_opp_deck else "")
    seen_burn = any(name in {
        "Burst Lightning",
        "Shock",
        "Play with Fire",
        "Lightning Strike",
        "Wizard's Lightning",
    } for name in _opp_spent_removal)
    is_red = "R" in opp_colors or "R" in deck_colors or "red" in deck_name
    if not is_red:
        return []

    my_life = me.life_total
    if my_life == 10 and opp_mana >= 4:
        return [Advice(
            "heuristic",
            "critical",
            "EXACT-10 danger — don't pass at exactly 10 life versus red; Hidetsugu's Second Rite kills from 10.",
            confidence=0.95,
        )]

    if my_life < 11 or my_life > 14:
        return []

    # Second Rite itself costs four mana. A one- or two-mana burn spell after combat
    # is the common setup that converts small chip damage into exact 10.
    if opp_mana < 5:
        return []

    if _combat_window(state) == "block":
        attack_powers = [
            max(0, a.power)
            for a in state.opp_creatures()
            if a.attack_state
        ]
    else:
        attack_powers = [
            max(0, c.power)
            for c in state.opp_creatures()
            if not c.is_tapped and not _has_keyword(c, "Defender")
        ]

    # Small burn plus chip damage is the dangerous range. We keep the numbers
    # conservative so the warning only fires for plausible exact-10 setups.
    burn_options = {1, 2}
    if opp_mana >= 6:
        burn_options.add(3)
    if _current_opp_deck and _current_opp_deck.hidden_reach >= 3:
        burn_options.add(min(4, _current_opp_deck.hidden_reach))
    if seen_burn:
        burn_options.add(2)

    achievable_chip = {0}
    for power in attack_powers[:6]:
        next_vals = set()
        for total in achievable_chip:
            next_total = total + power
            if next_total <= 4:
                next_vals.add(next_total)
        achievable_chip |= next_vals

    delta = my_life - 10
    if any(chip + burn == delta for chip in achievable_chip for burn in burn_options):
        return [Advice(
            "heuristic",
            "high",
            f"Red exact-10 setup exists — avoid ending combat at exactly 10 life (you are at {my_life}).",
            confidence=0.8,
        )]

    return []


def _check_lethal(state: GameState) -> list[Advice]:
    """Only suggest lethal when we can actually attack (pre-combat or combat)."""
    ti = state.turn_info
    # Don't suggest attacks after combat is over
    if ti.phase in ("Phase_Ending", "Phase_Cleanup"):
        return []

    opp = state.opp_player()
    if not opp:
        return []

    attackers = [c for c in state.my_creatures() if _can_attack(c)]
    if not attackers:
        return []

    total_power = sum(c.power for c in attackers)
    untapped_blockers = [c for c in state.opp_creatures()
                         if not c.is_tapped and not _has_keyword(c, "Defender")]

    # Unblockable creatures always deal damage
    unblockable = [a for a in attackers if _is_unblockable(a)]
    unblockable_dmg = sum(a.power for a in unblockable)

    # Opponent lifelink blockers — they gain life when blocking
    opp_lifelink_tough = sum(b.toughness for b in untapped_blockers if _has_keyword(b, "Lifelink"))

    # Clean lethal — no blockers
    if not untapped_blockers and total_power >= opp.life_total:
        names = ", ".join(a.name for a in attackers)
        return [Advice("heuristic", "critical",
                        f"LETHAL — attack with all: {names}", confidence=0.95,
                        recommended_cards=[a.name for a in attackers])]

    # Unblockable lethal
    if unblockable_dmg >= opp.life_total:
        names = ", ".join(a.name for a in unblockable)
        return [Advice("heuristic", "critical",
                        f"LETHAL — unblockable: {names}", confidence=0.95,
                        recommended_cards=[a.name for a in unblockable])]

    # Flying lethal — only count guaranteed flyers
    flyers = [a for a in attackers if _has_guaranteed_keyword(a, "Flying")]
    if flyers:
        fly_blockers = [b for b in untapped_blockers if _has_keyword(b, "Flying") or _has_keyword(b, "Reach")]
        # Trample flyers push damage through
        trample_fly = [a for a in flyers if _has_keyword(a, "Trample")]
        non_trample_fly = [a for a in flyers if not _has_keyword(a, "Trample")]
        # Trample: excess over total blocker toughness gets through
        fly_block_tough = sum(b.toughness for b in fly_blockers)
        trample_dmg = max(0, sum(a.power for a in trample_fly) - fly_block_tough) if trample_fly else 0
        # Non-trample: opponent blocks strongest first to minimize damage
        sorted_fly = sorted(non_trample_fly, key=lambda a: a.power, reverse=True)
        n_blocked = min(len(fly_blockers), len(sorted_fly))
        unblocked = sorted_fly[n_blocked:]
        unblocked_dmg = sum(a.power for a in unblocked)
        air_dmg = trample_dmg + unblocked_dmg + unblockable_dmg
        # Subtract lifelink gain from fly blockers
        fly_lifelink_gain = sum(min(b.toughness, max(a.power for a in flyers) if flyers else 0)
                                for b in fly_blockers if _has_keyword(b, "Lifelink")) if fly_blockers else 0
        effective_life = opp.life_total + fly_lifelink_gain
        if air_dmg >= effective_life:
            names = ", ".join(a.name for a in flyers + unblockable)
            return [Advice("heuristic", "critical",
                            f"LETHAL in air — attack with {names}", confidence=0.9)]

    # Trample lethal through blockers — only guaranteed trample
    tramplers = [a for a in attackers if _has_guaranteed_keyword(a, "Trample")]
    if tramplers:
        blocker_tough = sum(b.toughness for b in untapped_blockers)
        trample_total = sum(a.power for a in tramplers)
        excess = max(0, trample_total - blocker_tough)
        # Add non-trampler power if there are more attackers than blockers
        others = [a for a in attackers if a not in tramplers and a not in unblockable]
        free_attackers = max(0, len(others) - max(0, len(untapped_blockers) - len(tramplers)))
        others_dmg = sum(sorted([a.power for a in others], reverse=True)[:free_attackers])
        total_lethal_dmg = excess + others_dmg + unblockable_dmg
        # Account for opponent lifelink gain
        effective_life = opp.life_total + opp_lifelink_tough
        if total_lethal_dmg >= effective_life:
            names = ", ".join(a.name for a in attackers)
            return [Advice("heuristic", "critical",
                            f"LETHAL with trample — attack with all: {names}", confidence=0.85)]

    return []


def _check_opponent_lethal(state: GameState) -> list[Advice]:
    me = state.my_player()
    if not me:
        return []

    opp_creatures = state.opp_creatures()
    opp_attackers = [c for c in opp_creatures
                     if c.can_attack and not _has_keyword(c, "Defender")]
    my_blockers = [c for c in state.my_creatures() if not c.is_tapped]
    my_flyers = [c for c in my_blockers if _has_keyword(c, "Flying")]
    advice = []

    # Standard lethal check (total power vs life)
    opp_power = sum(
        c.power * (2 if _has_keyword(c, "double strike") else 1)
        for c in opp_attackers)
    if opp_power >= me.life_total and len(my_blockers) < len(opp_attackers):
        advice.append(Advice("heuristic", "critical",
                             f"DANGER — opponent has {opp_power} power, you have {me.life_total} life",
                             confidence=0.8))

    # Trampler accumulation warning: tramplers on board can't be chumped
    # Warn when total trample power exceeds our ability to absorb
    tramplers = [c for c in opp_creatures
                 if _has_keyword(c, "Trample") and not _has_keyword(c, "Defender")]
    if len(tramplers) >= 2:
        trample_power = sum(c.power for c in tramplers)
        # Blockers absorb toughness points worth of damage, excess tramples through
        blocker_tough = sum(c.toughness for c in my_blockers
                            if not _has_keyword(c, "Flying"))  # ground blockers only
        trample_through = max(0, trample_power - blocker_tough)
        if trample_through >= me.life_total * 0.5 and trample_through > 0:
            names = ", ".join(f"{c.name}({c.power}/{c.toughness})"
                              for c in tramplers[:3])
            advice.append(Advice("heuristic", "high",
                                 f"Trample accumulation — {trample_power} power trampling, "
                                 f"~{trample_through} gets through: {names}",
                                 confidence=0.7))

    # Haste creature warning: opponent summoned creature this turn that can attack immediately
    # Ball Lightning pattern: high power + haste (can appear out of nowhere)
    haste_threats = [c for c in opp_creatures if _has_keyword(c, "Haste")]
    for ht in haste_threats:
        card = card_cache.get(ht.grp_id)
        if card and ht.power >= 4:
            total_with_haste = ht.power + sum(
                c.power for c in opp_attackers if c.instance_id != ht.instance_id)
            if total_with_haste >= me.life_total * 0.6:
                advice.append(Advice("heuristic", "critical",
                                     f"HASTE THREAT: {card.name} ({ht.power}/{ht.toughness}) "
                                     f"can attack this turn — {total_with_haste} total power!",
                                     confidence=0.85))
                break

    return advice


def _check_mulligan(state: GameState) -> list[Advice]:
    if state.pending_request != "GREMessageType_MulliganReq":
        return []

    hand = state.my_hand()
    if not hand:
        return []

    lands = sum(1 for c in hand if c.is_land)
    nonlands = [c for c in hand if not c.is_land]
    hand_size = len(hand)

    # Count ETB-tapped lands (always tapped or conditionally tapped)
    tapped_lands = 0
    for obj in hand:
        if not obj.is_land:
            continue
        card = card_cache.get(obj.grp_id)
        if card:
            first_ab = card.abilities[0].lower() if card.abilities else ""
            if "enters tapped" in first_ab:
                tapped_lands += 1
    untapped_lands = lands - tapped_lands

    # Show hand contents
    hand_cards = []
    for obj in hand:
        card = card_cache.get(obj.grp_id)
        if card:
            hand_cards.append(card.name)
        else:
            hand_cards.append(obj.name)

    # Determine colors available from lands in hand (for color-aware mulligan)
    # T1: one untapped land; T2: two lands (one may be tapped)
    hand_land_colors_t1: set[str] = set()  # colors from one untapped land
    hand_land_colors_t2: set[str] = set()  # colors from up to two lands by T2
    land_cards_in_hand = []
    for obj in hand:
        if not obj.is_land:
            continue
        card = card_cache.get(obj.grp_id)
        if card:
            produced = _land_produces_colors(card)
            land_cards_in_hand.append((card, produced))
            hand_land_colors_t2.update(produced)
            first_ab = card.abilities[0].lower() if card.abilities else ""
            if "enters tapped" not in first_ab:
                hand_land_colors_t1.update(produced)

    def _spell_colors_available(card_info, by_turn: int) -> bool:
        """Check if lands in hand produce colors needed for this spell by given turn."""
        colored_pips, _ = _parse_mana_pips(card_info.mana_cost)
        if not colored_pips:
            return True  # colorless/generic only
        available = hand_land_colors_t1 if by_turn <= 1 else hand_land_colors_t2
        return all(c in available for c in set(colored_pips))

    # Count castable spells (assuming we play one land per turn)
    # For aggro/tempo decks, reactive-only spells (combat tricks, protection,
    # counterspells) don't count as "early plays" — you need proactive threats.
    # For control decks, reactive spells ARE valid early plays.
    is_proactive_deck = _my_archetype in ("aggro", "tempo", "unknown")
    castable_t1 = 0
    castable_t2 = 0
    early_creatures = 0  # creatures castable by T2
    color_blocked_spells = 0  # spells that would be castable but wrong colors
    for obj in nonlands:
        card = card_cache.get(obj.grp_id)
        if card:
            if is_proactive_deck and _is_reactive_instant(card):
                continue
            if card.cmc <= 1:
                if _spell_colors_available(card, 1):
                    castable_t1 += 1
                else:
                    color_blocked_spells += 1
            if card.cmc <= 2:
                if _spell_colors_available(card, 2):
                    castable_t2 += 1
                    if card.is_creature:
                        early_creatures += 1
                else:
                    color_blocked_spells += 1

    hand_str = ", ".join(hand_cards)

    if hand_size >= 6:
        if lands <= 1:
            return [Advice("heuristic", "high",
                           f"Mulligan — only {lands} land: {hand_str}",
                           confidence=0.9)]
        if lands >= 5:
            return [Advice("heuristic", "high",
                           f"Mulligan — {lands} lands: {hand_str}",
                           confidence=0.85)]
        # Aggro/tempo: 4+ lands means only 3 spells — not enough pressure
        if is_proactive_deck and lands >= 4 and castable_t2 <= 2:
            return [Advice("heuristic", "medium",
                           f"Risky keep — {lands} lands, only {hand_size - lands} "
                           f"spells for aggro: {hand_str}",
                           confidence=0.6)]
        # Color screw: lands missing a color that most spells need
        if lands >= 2 and len(nonlands) >= 3:
            # Check how many spells are castable even with 3+ lands (by T3)
            castable_by_t3 = 0
            for obj in nonlands:
                card = card_cache.get(obj.grp_id)
                if card:
                    pips, _ = _parse_mana_pips(card.mana_cost)
                    if not pips or all(c in hand_land_colors_t2 for c in set(pips)):
                        castable_by_t3 += 1
            missing_colors = set()
            for obj in nonlands:
                card = card_cache.get(obj.grp_id)
                if card and card.mana_cost:
                    pips, _ = _parse_mana_pips(card.mana_cost)
                    for p in set(pips):
                        if p not in hand_land_colors_t2:
                            missing_colors.add(p)
            if missing_colors and castable_by_t3 <= 1:
                missing_str = "/".join(sorted(missing_colors))
                return [Advice("heuristic", "high",
                               f"Color screw — lands don't produce {missing_str}. "
                               f"Only {castable_by_t3}/{len(nonlands)} spells castable: {hand_str}",
                               confidence=0.85)]

        # All lands ETB tapped — no play until T2+ (devastating for aggro)
        if tapped_lands > 0 and untapped_lands == 0 and lands <= 3:
            return [Advice("heuristic", "high",
                           f"Risky keep — all {lands} lands enter tapped, "
                           f"no play until T{lands + 1}: {hand_str}",
                           confidence=0.7)]
        # Aggro: early plays exist but no creatures — removal without board is dead
        if is_proactive_deck and castable_t2 >= 1 and early_creatures == 0:
            return [Advice("heuristic", "medium",
                           f"Risky keep — {castable_t2} early spell(s) but no "
                           f"creatures before T3: {hand_str}",
                           confidence=0.5)]
        if lands >= 2 and castable_t2 >= 1:
            # Warn if most lands are tapped
            tapped_warn = ""
            if tapped_lands >= lands - 1 and tapped_lands > 0:
                tapped_warn = f" ({tapped_lands} tapped!)"
            # Warn about color-blocked spells
            color_warn = ""
            if color_blocked_spells > 0:
                missing = set()
                for obj in nonlands:
                    card = card_cache.get(obj.grp_id)
                    if card and card.cmc <= 2:
                        pips, _ = _parse_mana_pips(card.mana_cost)
                        for p in pips:
                            if p not in hand_land_colors_t2:
                                missing.add(p)
                if missing:
                    color_warn = f" (no {'/'.join(sorted(missing))} mana!)"
            conf = 0.75
            if tapped_warn or color_warn:
                conf = 0.55
            return [Advice("heuristic", "medium",
                           f"Keep — {lands} lands{tapped_warn}{color_warn}, "
                           f"{castable_t2} early play(s): {hand_str}",
                           confidence=conf)]
        if lands >= 2 and castable_t2 == 0 and color_blocked_spells > 0:
            missing = set()
            for obj in nonlands:
                card = card_cache.get(obj.grp_id)
                if card and card.cmc <= 2:
                    pips, _ = _parse_mana_pips(card.mana_cost)
                    for p in pips:
                        if p not in hand_land_colors_t2:
                            missing.add(p)
            missing_str = "/".join(sorted(missing)) if missing else "?"
            return [Advice("heuristic", "high",
                           f"Risky keep — {lands} lands but no {missing_str} mana "
                           f"for early plays: {hand_str}",
                           confidence=0.7)]
        if lands >= 2 and castable_t2 == 0:
            return [Advice("heuristic", "medium",
                           f"Risky keep — {lands} lands but no early plays: {hand_str}",
                           confidence=0.5)]
    elif hand_size <= 5:
        if lands >= 1:
            return [Advice("heuristic", "medium",
                           f"Keep — {hand_size} cards, {lands} land(s): {hand_str}",
                           confidence=0.8)]
        else:
            return [Advice("heuristic", "high",
                           f"Mulligan — {hand_size} cards, no lands: {hand_str}",
                           confidence=0.85)]

    return []


def _suggest_plays(state: GameState) -> list[Advice]:
    """Suggest what to cast on main phase."""
    ti = state.turn_info
    if "Main" not in ti.phase:
        return []

    hand = state.my_hand()
    untapped_lands = state.my_untapped_lands()
    lands_in_hand = [c for c in hand if c.is_land]
    land_options = _main_phase_land_options(untapped_lands, lands_in_hand)
    mana = len(untapped_lands)
    legal_cast_ids = {
        a.instance_id for a in state.available_actions
        if a.seat_id == state.my_seat_id and a.action_type == "ActionType_Cast" and a.instance_id
    }
    legal_cast_name_counts = Counter(
        card_cache.get(a.grp_id).name if a.grp_id and card_cache.get(a.grp_id) else ""
        for a in state.available_actions
        if a.seat_id == state.my_seat_id and a.action_type == "ActionType_Cast"
    )
    seen_cast_name_counts: Counter[str] = Counter()

    # Account for land drop: only if MTGA says land play is still available
    land_drop_available = any(
        a.action_type == "ActionType_Play" for a in state.available_actions
        if a.seat_id == state.my_seat_id
    )
    if land_drop_available and land_options:
        effective_mana = max((len(option) for option in land_options), default=mana)
    else:
        effective_mana = mana  # no land drop left — use actual untapped mana

    advice = []

    # Check for opponent cast-penalty enchantments (Painful Quandary etc.)
    opp_bf = state.opp_battlefield()
    cast_penalty = _detect_cast_penalty(opp_bf)
    if cast_penalty:
        advice.append(Advice("heuristic", "high",
                              cast_penalty, confidence=0.85))

    # Detect Seam Rip — on battlefield OR likely in opponent's deck
    opp_has_seam_rip = any(
        card_cache.get(o.grp_id) and card_cache.get(o.grp_id).name == "Seam Rip"
        for o in opp_bf
        if not o.is_creature
    )
    opp_seam_rip_likely = False
    if not opp_has_seam_rip and _current_opp_deck:
        sig = getattr(_current_opp_deck, "signal_cards", {})
        if sig.get("Seam Rip", 0) >= 0.08:
            opp_seam_rip_likely = True

    # Suggest castable spells (biggest first)
    my_creatures = state.my_creatures()
    castable = []
    needs_land_first: set[str] = set()  # card names that require playing a land first
    for obj in hand:
        if obj.is_land:
            continue
        card = card_cache.get(obj.grp_id)
        if not card or card.cmc > effective_mana:
            continue
        if legal_cast_ids:
            if obj.instance_id not in legal_cast_ids:
                continue
        else:
            seen_cast_name_counts[card.name] += 1
            if seen_cast_name_counts[card.name] > legal_cast_name_counts.get(card.name, 0):
                continue
        # Color check: verify lands can pay the colored pips
        if card.mana_cost:
            can_pay = any(_can_pay_mana_cost(card.mana_cost, option)
                          for option in land_options)
            if not can_pay:
                continue
        # Auras need a valid target on the battlefield
        if _is_aura(card) and not _has_aura_target(card, my_creatures, opp_bf):
            continue
        # Buff/protection instants need own creature on board
        if not card.is_creature and not _is_aura(card) and _needs_own_creature(card) and not my_creatures:
            continue
        # Skip reactive instants in Main phase — they're combat tricks / protection / counters
        if _is_reactive_instant(card):
            continue
        # Track cards that need a land drop first (CMC > current mana)
        if card.cmc > mana and land_drop_available:
            needs_land_first.add(card.name)
        castable.append(card)

    # Check if we have removal and opponent has threats
    removal_cards = []
    for card in castable:
        if _is_battlefield_removal(card):
            removal_cards.append(card)

    # Check burn-to-face: if burn spells can reach lethal in 1-2 turns, suggest face
    opp = state.opp_player()
    if opp and removal_cards:
        burn_cards = []
        for card in removal_cards:
            abs_lower = " ".join(a.lower() for a in card.abilities)
            # Extract damage amount from "deals N damage to any target"
            import re
            m = re.search(r"deals (\d+) damage to any target", abs_lower)
            if m:
                burn_cards.append((card, int(m.group(1))))
        if burn_cards:
            total_burn = sum(dmg for _, dmg in burn_cards)
            # Add attack damage from creatures that can attack
            attack_dmg = sum(c.power for c in state.my_creatures() if _can_attack(c))
            if total_burn + attack_dmg >= opp.life_total and opp.life_total <= total_burn + 6:
                burn_names = " + ".join(f"{c.name} ({d})" for c, d in burn_cards)
                advice.append(Advice("heuristic", "high",
                                      f"Burn face for lethal — {burn_names} = {total_burn} dmg "
                                      f"(opp at {opp.life_total})",
                                      confidence=0.8,
                                      recommended_cards=[c.name for c, _ in burn_cards]))

    # A2a: Clean answers for noncreature engine permanents
    if removal_cards:
        scored_engines = evaluate_opponent_engines(state)
        for top_engine, engine_score, engine_reason in scored_engines:
            if engine_score < 6:
                break
            engine_card = card_cache.get(top_engine.grp_id)
            if not engine_card:
                continue
            valid_removal = [r for r in removal_cards
                             if _removal_can_hit_noncreature(r, top_engine)]
            if not valid_removal:
                continue
            # Prefer clean answers over aura-based removal for engines.
            valid_removal.sort(key=lambda r: (_is_aura_removal(r), r.cmc, r.name))
            best_removal = valid_removal[0]
            prio = "critical" if engine_score >= 10 else "high"
            advice.append(Advice(
                "heuristic",
                prio,
                f"Remove {engine_card.name} with {best_removal.name} — {engine_reason}"
                f"{_spree_bonus_note(best_removal, state, effective_mana)}",
                confidence=0.82 if prio == "critical" else 0.74,
                recommended_cards=[best_removal.name],
            ))
            break

    # A2: Use board evaluator to find best removal target
    if removal_cards:
        scored_threats = evaluate_opponent_board(state)
        for top_threat, threat_score, threat_reason in scored_threats:
            if threat_score < 3:
                break  # not worth removing
            threat_card = card_cache.get(top_threat.grp_id)
            if not threat_card:
                continue
            threat_abs = " ".join(a.lower() for a in threat_card.abilities)
            # Also check auras on this creature for granted keywords
            aura_abs = _get_aura_abilities(top_threat, state)
            combined_abs = threat_abs + " " + aura_abs

            # Check hexproof/shroud — skip permanent, warn+urgent for conditional
            hex_status = _protection_status(threat_card, "hexproof")
            shroud_status = _protection_status(threat_card, "shroud")
            # Aura-granted hexproof/shroud — check if conditional
            if hex_status == "absent" and "hexproof" in aura_abs:
                hex_status = _aura_keyword_status(aura_abs, "hexproof")
            if shroud_status == "absent" and "shroud" in aura_abs:
                shroud_status = _aura_keyword_status(aura_abs, "shroud")

            if hex_status == "permanent" or shroud_status == "permanent":
                continue  # can't target at all
            threat_name = threat_card.name
            warn = ""
            is_urgent = False
            if hex_status == "conditional" or shroud_status == "conditional":
                # Conditional protection — might activate soon, remove NOW
                warn = " (URGENT — gains hexproof if condition met!)"
                is_urgent = True

            available_removal = removal_cards[:]

            # Check indestructible — permanent vs conditional
            indest_status = _protection_status(threat_card, "indestructible")
            if indest_status == "absent" and "indestructible" in aura_abs:
                indest_status = _aura_keyword_status(aura_abs, "indestructible")

            if indest_status == "permanent":
                # Permanent indestructible: exile or bounce only
                bypass_removal = [
                    r for r in available_removal
                    if any(kw in " ".join(a.lower() for a in r.abilities)
                           for kw in ["exile", "return target", "return up to"])
                ]
                if bypass_removal:
                    available_removal = bypass_removal
                    warn += " (indestructible — exile/bounce!)"
                else:
                    continue
            elif indest_status == "conditional":
                # Conditional indestructible: any removal works now, but be urgent
                warn += " (gains indestructible — remove now!)"
                is_urgent = True
            valid_removal = [r for r in available_removal
                             if _removal_kills_creature(r, top_threat)]
            if not valid_removal:
                # No removal can kill it — check if we can at least target it
                # (e.g. damage spell that won't kill but might be useful with combat)
                targetable = [r for r in available_removal
                              if _removal_can_target(r, top_threat)]
                if targetable and threat_score >= 8:
                    # High-value target: mention it but note it won't kill
                    best = targetable[0]
                    advice.append(Advice("heuristic", "low",
                        f"{best.name} can't kill {threat_card.name} "
                        f"({top_threat.power}/{top_threat.toughness}) alone "
                        f"— need combat damage first",
                        confidence=0.4,
                        recommended_cards=[best.name]))
                continue
            if "ward" in combined_abs:
                ward_cost = _parse_ward_cost(combined_abs)
                if ward_cost:
                    total_ward_cost = ward_cost + valid_removal[0].cmc
                    if total_ward_cost > effective_mana:
                        # Ward too expensive even with current mana — try aura removal
                        aura_advice = _suggest_aura_removal(
                            top_threat, state, removal_cards, effective_mana, land_options,
                            threat_name, threat_score, threat_reason)
                        if aura_advice:
                            advice.append(aura_advice)
                            break
                        continue
                    warn += f" (ward — costs {total_ward_cost} total)"
                else:
                    warn += " (has ward)"
            # Warn about aura-based exile returning if aura is destroyed
            best_removal = valid_removal[0]
            is_aura_rem = _is_aura_removal(best_removal)
            removal_abs = " ".join(a.lower() for a in best_removal.abilities)
            if is_aura_rem and "exile" in removal_abs:
                # Aura exile is temporary — returns when aura leaves
                warn += " (temporary — returns if aura removed)"
                # Also warn if target has ETB (re-triggers on return)
                threat_oracle = (threat_card.oracle_text or "").lower()
                threat_abs_lower = " ".join(a.lower() for a in threat_card.abilities)
                if (("enters" in threat_oracle or "enters" in threat_abs_lower)
                        and any(kw in threat_oracle + threat_abs_lower
                                for kw in ["draw", "create", "gain", "deal",
                                           "destroy", "exile", "return"])):
                    warn += " (has ETB — bad if it returns!)"
            elif ("enchant" in removal_abs and "exile" in removal_abs):
                # Non-aura enchantment exile (Banishing Light etc.)
                warn += " (returns if enchantment destroyed)"
            # Flash removal: hold for opp turn unless threat is urgent
            if _has_flash(best_removal) and threat_score < 10 and not is_urgent:
                advice.append(Advice("heuristic", "medium",
                                      f"Hold {best_removal.name} — flash removal, "
                                      f"use on opp turn vs {threat_name}",
                                      confidence=0.6,
                                      recommended_cards=[best_removal.name]))
            elif is_aura_rem:
                first_ab = best_removal.abilities[0].lower() if best_removal.abilities else ""
                if "you control" in first_ab:
                    target_note = "on your creature"
                else:
                    target_note = "on creature"
                prio = "critical" if is_urgent else "high"
                advice.append(Advice("heuristic", prio,
                                      f"Cast {best_removal.name} {target_note} — "
                                      f"exiles {threat_name} "
                                      f"({top_threat.power}/{top_threat.toughness})"
                                      f"{_spree_bonus_note(best_removal, state, effective_mana)}"
                                      f"{warn}",
                                      confidence=0.85 if is_urgent else 0.7,
                                      recommended_cards=[best_removal.name]))
            else:
                prio = "critical" if is_urgent else "high"
                collapse_note = _attached_aura_collapse_note(top_threat, state)
                detail = threat_reason
                if collapse_note:
                    detail = f"{detail}; {collapse_note}"
                advice.append(Advice("heuristic", prio,
                                      f"Remove {threat_name} ({top_threat.power}/{top_threat.toughness}) "
                                      f"with {best_removal.name} — {detail}"
                                      f"{_spree_bonus_note(best_removal, state, effective_mana)}"
                                      f"{warn}",
                                      confidence=0.7,
                                      recommended_cards=[best_removal.name]))
            break

    # Suggest creature/spell to cast
    # Early turns: curve out (cheapest first); late game: biggest impact
    creatures = [c for c in castable if c.is_creature and c not in removal_cards]
    spells = [c for c in castable if not c.is_creature and c not in removal_cards]

    # Separate flash cards — prefer holding them for opponent's turn
    flash_creatures = [c for c in creatures if _has_flash(c)]
    non_flash_creatures = [c for c in creatures if not _has_flash(c)]
    flash_spells = [c for c in spells if _has_flash(c)]
    non_flash_spells = [c for c in spells if not _has_flash(c)]

    # Detect if opponent is fast — suppress "hold flash" and deploy instead
    opp_is_fast = False
    if _current_opp_deck:
        opp_speed = getattr(_current_opp_deck, "speed", "")
        opp_is_fast = opp_speed in ("fast", "very_fast")

    hold_main_phase_flash: dict[str, str] = {}
    for card in flash_creatures:
        hold_reason = _main_phase_flash_hold_reason(card, state, opp_is_fast)
        if hold_reason:
            hold_main_phase_flash[card.name] = hold_reason

    proactive_flash_creatures = [
        c for c in flash_creatures
        if c.name not in hold_main_phase_flash
    ]

    # Against fast decks: deploy flash creatures immediately (board presence > trick value)
    if opp_is_fast and proactive_flash_creatures and not non_flash_creatures:
        active_creatures = non_flash_creatures + proactive_flash_creatures
    else:
        # Use non-flash creatures first; only suggest flash if nothing else to play
        if non_flash_creatures:
            active_creatures = non_flash_creatures
        else:
            active_creatures = proactive_flash_creatures

    # Seam Rip warning: if opp has Seam Rip (or likely runs it), warn about CMC ≤ 2
    if (opp_has_seam_rip or opp_seam_rip_likely) and active_creatures:
        low_cmc = [c for c in active_creatures if c.cmc <= 2]
        high_cmc = [c for c in active_creatures if c.cmc >= 3]
        if low_cmc and high_cmc:
            if opp_has_seam_rip:
                advice.append(Advice("heuristic", "medium",
                                      f"Opponent has Seam Rip — prefer CMC 3+ creatures "
                                      f"({', '.join(c.name for c in high_cmc[:2])})",
                                      confidence=0.7))
            else:
                advice.append(Advice("heuristic", "low",
                                      f"Opponent's deck likely runs Seam Rip — "
                                      f"consider CMC 3+ creatures "
                                      f"({', '.join(c.name for c in high_cmc[:2])})",
                                      confidence=0.4))

    if active_creatures:
        turn = state.turn_info.turn_number
        my_creature_count = len(my_creatures)
        if turn <= 4:
            # C1: hand-aware priority — synergy score breaks ties
            # On empty board, prefer evasion (flyers) to establish clock
            active_creatures.sort(key=lambda c: (
                -c.cmc if c.cmc <= mana else 99,
                0 if my_creature_count == 0 and _has_evasion(c) else 1,
                -hand_synergy_score(c.grp_id, hand),
                -_card_score(c.name),
                c.name,
            ))
        else:
            active_creatures.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        # Prefer creatures castable NOW over those needing a land drop
        castable_now = [c for c in active_creatures if c.name not in needs_land_first]
        if castable_now:
            active_creatures = castable_now
        best = active_creatures[0]
        wr = _get_card_wr()
        wr_note = f" [{wr[best.name]:.0f}% WR]" if best.name in wr else ""
        land_note = " (play land first)" if best.name in needs_land_first else ""
        # Upgrade priority vs fast decks when we have no board
        prio = "medium"
        conf = 0.6
        if opp_is_fast and not my_creatures:
            prio = "high"
            conf = 0.75
        # Downgrade confidence if land drop required
        if best.name in needs_land_first:
            conf *= 0.8
        advice.append(Advice("heuristic", prio,
                              f"Cast {best.name} ({best.mana_cost}){wr_note}{land_note}",
                              confidence=conf,
                              recommended_cards=[best.name]))

    # Flash vs reactive instant trade-off detection
    reactive_instants = [c for c in hand
                         if not c.is_land and card_cache.get(c.grp_id)
                         and _is_reactive_instant(card_cache.get(c.grp_id))
                         and card_cache.get(c.grp_id).cmc <= mana]
    if flash_creatures and reactive_instants and my_creatures:
        # Both compete for open mana — warn about the trade-off
        best_fc = max(flash_creatures, key=lambda c: c.cmc)
        best_ri = reactive_instants[0]
        ri_card = card_cache.get(best_ri.grp_id)
        ri_name = ri_card.name if ri_card else best_ri.name
        # Only warn if they actually compete (combined cost > available mana)
        if best_fc.cmc + (ri_card.cmc if ri_card else 0) > mana:
            advice.append(Advice("heuristic", "medium",
                                  f"Trade-off: {best_fc.name} (flash) OR hold mana for "
                                  f"{ri_name} (protection) — pick one",
                                  confidence=0.6))

    # Suggest holding flash cards for opponent's turn.
    # If a flash creature is intentionally suppressed in Main phase, surface that
    # explicitly so the user knows the "no cast" result is on purpose.
    flash_holdable = flash_creatures + flash_spells
    intentionally_held_flash = [
        c for c in flash_holdable
        if c.name in hold_main_phase_flash
    ]
    if intentionally_held_flash:
        intentionally_held_flash.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        best_flash = intentionally_held_flash[0]
        already_recommended = any(
            best_flash.name.lower() in a.message.lower()
            and ("cast " in a.message.lower() or "hold" in a.message.lower())
            for a in advice
        )
        if not already_recommended:
            advice.append(Advice(
                "heuristic",
                "medium",
                f"Hold {best_flash.name} — {hold_main_phase_flash[best_flash.name]}",
                confidence=0.72,
                recommended_cards=[best_flash.name],
            ))
    elif (flash_holdable and not opp_is_fast
            and (non_flash_creatures or non_flash_spells or removal_cards)):
        flash_holdable.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        best_flash = flash_holdable[0]
        # Skip if we already recommended casting this card (contradicting advice)
        already_recommended = any(
            best_flash.name.lower() in a.message.lower()
            and ("cast " in a.message.lower() or "hold" in a.message.lower())
            for a in advice
        )
        if not already_recommended:
            advice.append(Advice("heuristic", "low",
                                  f"Hold {best_flash.name} — has flash, cast on opp turn",
                                  confidence=0.5))

    # Non-flash spells only
    if non_flash_spells and not advice:
        # Prefer spells castable now
        spells_now = [c for c in non_flash_spells if c.name not in needs_land_first]
        spell_list = spells_now if spells_now else non_flash_spells
        spell_list.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        best = spell_list[0]
        wr = _get_card_wr()
        wr_note = f" [{wr[best.name]:.0f}% WR]" if best.name in wr else ""
        land_note = " (play land first)" if best.name in needs_land_first else ""
        conf = 0.4 if best.name in needs_land_first else 0.5
        advice.append(Advice("heuristic", "medium",
                              f"Cast {best.name} ({best.mana_cost}){wr_note}{land_note}",
                              confidence=conf,
                              recommended_cards=[best.name]))

    # Suggest activated abilities on battlefield creatures
    ability_advice = _suggest_activated_abilities(state, mana)
    for aa in ability_advice:
        # Don't duplicate if we already have a cast suggestion for same card
        if not any(aa.recommended_cards and rc in a.message
                   for a in advice for rc in aa.recommended_cards):
            advice.append(aa)

    # Remind about land drop
    if lands_in_hand and not any("land" in a.message.lower() for a in advice):
        advice.append(Advice("heuristic", "low", "Play a land", confidence=0.4))

    return advice[:4]


def _suggest_attacks(state: GameState) -> list[Advice]:
    """Suggest attack strategy."""
    if _combat_window(state) != "attack":
        return []

    attackers = [
        c for c in _creature_objects(state.my_battlefield())
        if not c.is_tapped and not c.has_summoning_sickness
        and not _has_keyword(c, "Defender")
    ]
    if not attackers:
        return []

    opp_blockers = [c for c in _creature_objects(state.opp_battlefield())
                    if not c.is_tapped and not _has_keyword(c, "Defender")]

    # Evasive creatures — only count GUARANTEED keywords as safe evasion
    evasive = []
    for a in attackers:
        if _is_unblockable(a):
            evasive.append(a)
        elif _has_guaranteed_keyword(a, "Flying") and not any(
                _has_keyword(b, "Flying") or _has_keyword(b, "Reach") for b in opp_blockers):
            evasive.append(a)
        elif _has_guaranteed_keyword(a, "Menace") and len(opp_blockers) < 2:
            evasive.append(a)

    # Vigilance creatures — free attacks, only count guaranteed vigilance
    vigilant = [a for a in attackers if _has_guaranteed_keyword(a, "Vigilance") and a not in evasive]

    safe_attackers = evasive + vigilant
    if safe_attackers:
        names = ", ".join(a.name for a in safe_attackers)
        dmg = sum(a.power for a in safe_attackers)
        label = "evasion/vigilance" if vigilant else "evasion"
        return [Advice("heuristic", "medium",
                        f"Attack with {names} ({dmg} {label} damage)",
                        confidence=0.7)]

    # If no blockers, attack with everything
    if not opp_blockers and attackers:
        names = ", ".join(a.name for a in attackers)
        return [Advice("heuristic", "medium",
                        f"Attack with all — no blockers: {names}",
                        confidence=0.7)]

    # Deathtouch attackers are good to send — opponent must trade or take damage
    deathtouchers = [a for a in attackers if _has_keyword(a, "Deathtouch") and a not in evasive]
    if deathtouchers and opp_blockers:
        names = ", ".join(a.name for a in deathtouchers)
        return [Advice("heuristic", "medium",
                        f"Attack with {names} — deathtouch forces bad trades",
                        confidence=0.6)]

    # Favorable trades: creatures that outclass all possible single blockers
    favorable = []
    for a in attackers:
        if a in evasive or a in vigilant:
            continue
        # Only suggest this line if no single blocker can kill the attacker.
        # The earlier version only checked whether some block was favorable,
        # which produced false positives like "survives any single block"
        # even when the opponent had a larger blocker.
        safe_into_all_single_blocks = all(
            b.power < a.toughness and not _has_keyword(b, "Deathtouch")
            for b in opp_blockers
        )
        kills_at_least_one_single_blocker = any(
            a.power >= b.toughness for b in opp_blockers
        )
        if safe_into_all_single_blocks and kills_at_least_one_single_blocker:
            favorable.append(a)
    if favorable:
        names = ", ".join(a.name for a in favorable)
        return [Advice("heuristic", "medium",
                        f"Attack with {names} — survives any single block",
                        confidence=0.6)]

    if opp_blockers:
        blocker_names = ", ".join(f"{b.name} ({b.power}/{b.toughness})"
                                  for b in opp_blockers[:3])
        return [Advice("heuristic", "medium",
                        f"Don't attack — no clean attacks into {blocker_names}",
                        confidence=0.65)]

    return []


def _opp_open_mana_colors(state: GameState) -> tuple[int, set[str]]:
    """Count opponent's untapped lands and determine available colors."""
    _BASIC_COLORS = {"plains": "W", "island": "U", "swamp": "B",
                     "mountain": "R", "forest": "G"}
    opp_lands = [o for o in state.opp_battlefield() if o.is_land and not o.is_tapped]
    colors: set[str] = set()
    for land in opp_lands:
        card = card_cache.get(land.grp_id)
        if card:
            colors.update(_land_produces_colors(card))
        else:
            # Fallback: recognize basic land names from object
            name_lower = land.name.lower()
            for basic, c in _BASIC_COLORS.items():
                if basic in name_lower:
                    colors.add(c)
    return len(opp_lands), colors


def _combat_trick_risk(state: GameState, opp_attackers: list[GameObject],
                       my_blockers: list[GameObject]) -> dict:
    """Assess risk of opponent having a combat trick.

    Factors: open mana, colors, suspicious attack patterns, meta deck info.
    Returns {risk: 0.0-0.95, types: [...], warning: str}.
    """
    opp_mana, opp_colors = _opp_open_mana_colors(state)

    if opp_mana == 0:
        return {"risk": 0.0, "types": [], "warning": ""}

    risk = 0.0
    trick_types: list[str] = []

    # Color-based trick likelihood
    if "R" in opp_colors:
        if opp_mana >= 1:
            risk += 0.25
            trick_types.append("pump")
        if opp_mana >= 3:
            risk += 0.15  # mass pump (Trumpet Blast etc.)
            trick_types.append("mass_pump")
    if "G" in opp_colors and opp_mana >= 1:
        risk += 0.25
        trick_types.append("pump")
    if "W" in opp_colors and opp_mana >= 1:
        risk += 0.15
        trick_types.append("protection")
    if "B" in opp_colors and opp_mana >= 2:
        risk += 0.10
        trick_types.append("removal")
    if "U" in opp_colors and opp_mana >= 2:
        risk += 0.10
        trick_types.append("bounce")

    # Suspicious attack pattern: sending creatures into obviously bad blocks
    # (e.g. 1/1s into a 4/3 — they'd all die for no gain without a trick)
    if my_blockers:
        would_die = 0
        for att in opp_attackers:
            for blk in my_blockers:
                if blk.power >= att.toughness and blk.toughness > att.power:
                    would_die += 1
                    break
        if len(opp_attackers) > 0:
            die_ratio = would_die / len(opp_attackers)
            if die_ratio >= 0.4 and would_die >= 2:
                risk += 0.20

    # Meta deck intel: hidden_reach or known combat tricks in key_threats
    opp_deck = _current_opp_deck
    if opp_deck:
        if opp_deck.hidden_reach > 0:
            risk += 0.10
        for threat in opp_deck.key_threats:
            card_name = threat.get("card", "") if isinstance(threat, dict) else str(threat)
            reason = threat.get("reason", "") if isinstance(threat, dict) else ""
            if any(kw in reason.lower() for kw in ["trick", "pump", "combat", "instant"]):
                risk += 0.15
                break

    # Reduce risk if opponent has already spent removal this game
    # (fewer cards in hand = lower trick probability)
    if _opp_spent_removal:
        spent = len(_opp_spent_removal)
        risk -= spent * 0.05  # each spent removal = -5% risk
        risk = max(risk, 0.0)

    risk = min(risk, 0.95)

    warning = ""
    if risk >= 0.3:
        color_names = {"R": "red", "G": "green", "W": "white", "B": "black", "U": "blue"}
        colors_str = "/".join(color_names.get(c, c) for c in sorted(opp_colors))
        warning = (f"Opp has {opp_mana} open mana ({colors_str})"
                   f" — combat trick risk {risk:.0%}")

    return {"risk": risk, "types": trick_types, "warning": warning}


def _blocker_value(obj: GameObject, state: GameState) -> float:
    """How valuable is a creature — higher score = more worth preserving.

    Considers: keywords (lifelink, ward, flying), buffed stats,
    board scarcity, card win rate.
    """
    card = card_cache.get(obj.grp_id)
    value = 0.0

    # Base from current P/T and CMC
    value += obj.power * 1.5 + obj.toughness * 0.5
    if card:
        value += card.cmc * 0.5

    # Buffed creature: current stats exceed card's base stats (has auras/counters)
    if card:
        try:
            base_p = int(card.power) if card.power else 0
        except (ValueError, TypeError):
            base_p = 0
        try:
            base_t = int(card.toughness) if card.toughness else 0
        except (ValueError, TypeError):
            base_t = 0
        if obj.power > base_p or obj.toughness > base_t:
            value += 3  # carrying enchantments or counters — high value

    # Key keywords that provide ongoing value
    if _has_keyword(obj, "Lifelink"):
        value += 5  # ongoing life swing every turn
    if _has_keyword(obj, "Flying"):
        value += 3  # evasion = primary win condition in flyer decks
    if _has_keyword(obj, "Ward"):
        value += 2  # hard to remove — opponent already invested to get past ward
    if _has_keyword(obj, "Deathtouch"):
        value += 2  # defensive deterrent
    if _has_keyword(obj, "Hexproof"):
        value += 3
    if _has_keyword(obj, "Indestructible"):
        value += 4
    if _has_keyword(obj, "Double strike"):
        value += 3

    # Board scarcity: losing your only/few creatures is devastating
    my_creatures = state.my_creatures()
    creature_count = len(my_creatures)
    if creature_count == 1:
        value += 6
    elif creature_count == 2:
        value += 4
    elif creature_count == 3:
        value += 2

    # Card win rate bonus
    wr = _get_card_wr().get(obj.name, 50)
    value += (wr - 50) * 0.1

    return value


def _check_combat_blocks(state: GameState) -> list[Advice]:
    if _combat_window(state) != "block":
        return []

    opp_attackers = [o for o in _creature_objects(state.opp_battlefield()) if o.attack_state]
    my_blockers = [c for c in _creature_objects(state.my_battlefield()) if not c.is_tapped]

    if not opp_attackers:
        return []

    incoming = sum(a.power for a in opp_attackers)
    me = state.my_player()
    life = me.life_total if me else 20
    hand = state.my_hand()
    mana = len(state.my_untapped_lands())

    # C3: check if hand has castable threats for next turn (worth trading)
    hand_has_threat = any(
        card_cache.get(o.grp_id) and not card_cache.get(o.grp_id).is_land
        and card_cache.get(o.grp_id).cmc <= mana + 1  # next turn we have +1 land
        for o in hand
    )

    # C3: check for combat tricks (instants castable now, with color check)
    untapped_lands = state.my_untapped_lands()
    combat_tricks = []
    for o in hand:
        c = card_cache.get(o.grp_id)
        if c and c.is_instant and c.cmc <= mana:
            if c.mana_cost and not _can_pay_mana_cost(c.mana_cost, untapped_lands):
                continue
            combat_tricks.append(c)

    # C4: Assess opponent combat trick risk
    trick = _combat_trick_risk(state, opp_attackers, my_blockers)
    trick_risk = trick["risk"]

    # Life cushion: how safe is it to take the hit and preserve board
    life_cushion = max(0.0, (life - incoming) / life) if life > 0 else 0.0

    advice = []
    lethal = incoming >= life
    near_lethal = incoming >= life * 0.6  # taking >60% of life

    if lethal:
        advice.append(Advice("heuristic", "critical",
                              f"Must block — {incoming} incoming vs {life} life",
                              confidence=0.95))
    elif near_lethal:
        advice.append(Advice("heuristic", "high",
                              f"Dangerous — {incoming} incoming vs {life} life, consider blocking",
                              confidence=0.8))

    # Prioritize blocking lifelink attackers — they heal opponent
    lifelink_attackers = [a for a in opp_attackers if _has_keyword(a, "Lifelink")]
    if lifelink_attackers:
        ll_dmg = sum(a.power for a in lifelink_attackers)
        advice.append(Advice("heuristic", "high",
                              f"Block lifelink attackers first — they heal {ll_dmg}",
                              confidence=0.75))

    # C3: Use board evaluator to sort attackers by threat priority
    scored_threats = evaluate_opponent_board(state)
    attacker_ids = {a.instance_id for a in opp_attackers}
    # Sort attackers by threat score (highest first)
    attacker_priority = []
    for obj, score, reason in scored_threats:
        if obj.instance_id in attacker_ids:
            attacker_priority.append((obj, score, reason))
    # Add any attackers not in evaluator (shouldn't happen, but safety)
    for a in opp_attackers:
        if a.instance_id not in {ap[0].instance_id for ap in attacker_priority}:
            attacker_priority.append((a, a.power, ""))

    used_blockers: set[int] = set()
    # C4: track blockers that were skipped due to trick risk
    trick_preserved: list[GameObject] = []

    for attacker, threat_score, _ in attacker_priority:
        att_deathtouch = _has_keyword(attacker, "Deathtouch")
        att_first_strike = _has_keyword(attacker, "First strike") or _has_keyword(attacker, "Double strike")
        att_trample = _has_keyword(attacker, "Trample")
        att_menace = _has_keyword(attacker, "Menace")

        available = [b for b in my_blockers if b.instance_id not in used_blockers]

        if att_menace:
            eligible = [b for b in available if b.toughness > 0]
            if len(eligible) >= 2 and (lethal or threat_score >= 6):
                names = f"{eligible[0].name} + {eligible[1].name}"
                advice.append(Advice("heuristic", "high",
                    f"Block {attacker.name} with 2 creatures (menace): {names}",
                    confidence=0.8))
                used_blockers.add(eligible[0].instance_id)
                used_blockers.add(eligible[1].instance_id)
            elif len(eligible) < 2:
                advice.append(Advice("heuristic", "medium",
                    f"Can't block {attacker.name} — menace requires 2 blockers",
                    confidence=0.85))
            continue

        # Flying check: only flying/reach creatures can block flyers
        att_flying = _has_keyword(attacker, "Flying")
        if att_flying:
            available = [b for b in available
                         if _has_keyword(b, "Flying") or _has_keyword(b, "Reach")]
            if not available:
                if lethal or near_lethal:
                    advice.append(Advice("heuristic", "medium",
                        f"Can't block {attacker.name} — flying (no flyers/reach)",
                        confidence=0.9))
                continue

        best_block = None
        best_type = ""  # "clean_kill", "trade", "chump"
        for blocker in available:
            blk_deathtouch = _has_keyword(blocker, "Deathtouch")
            blk_first_strike = _has_keyword(blocker, "First strike") or _has_keyword(blocker, "Double strike")
            blk_lifelink = _has_keyword(blocker, "Lifelink")

            blocker_kills = (blocker.power >= attacker.toughness) or blk_deathtouch
            blocker_survives = blocker.toughness > attacker.power and not att_deathtouch
            if blk_first_strike and not att_first_strike and blocker_kills:
                blocker_survives = True
            if att_first_strike and not blk_first_strike and attacker.power >= blocker.toughness:
                blocker_kills = False

            # C3: Lifelink blocker bonus — prevents damage AND gains life
            lifelink_bonus = 2 if blk_lifelink else 0

            if blocker_kills and blocker_survives:
                # Best possible — clean kill
                if not best_block or best_type != "clean_kill":
                    best_block = blocker
                    best_type = "clean_kill"
            elif blocker_kills and not blocker_survives:
                # Trade — worth it if attacker is high threat or we have follow-up
                if best_type not in ("clean_kill",):
                    if (threat_score >= 5 or lethal or hand_has_threat
                            or att_deathtouch or att_trample or lifelink_bonus):
                        best_block = blocker
                        best_type = "trade"
            elif not blocker_kills and lethal:
                # Chump block — only if lethal and no better option
                if best_type not in ("clean_kill", "trade"):
                    best_block = blocker
                    best_type = "chump"

        # C4: Evaluate trick risk vs blocker value before committing
        if best_block and trick_risk > 0.3 and not lethal:
            blk_value = _blocker_value(best_block, state)

            # High-value blocker + significant trick risk + comfortable life
            # → preserve the creature, don't risk losing it to a trick
            if (blk_value >= 10 and trick_risk >= 0.4
                    and life_cushion >= 0.3 and best_type != "chump"):
                trick_preserved.append(best_block)
                best_block = None
                best_type = ""

            # Medium-value blocker with trick risk: downgrade confidence
            elif blk_value >= 6 and trick_risk >= 0.3 and best_type == "clean_kill":
                best_type = "risky_clean"

        if best_block:
            used_blockers.add(best_block.instance_id)
            if best_type == "clean_kill":
                advice.append(Advice("heuristic", "high",
                    f"Block {attacker.name} with {best_block.name} — kills it, survives",
                    confidence=0.85))
            elif best_type == "risky_clean":
                opp_mana, _ = _opp_open_mana_colors(state)
                advice.append(Advice("heuristic", "medium",
                    f"Block {attacker.name} with {best_block.name} — kills it IF no trick"
                    f" (opp has {opp_mana} open mana)",
                    confidence=max(0.4, 0.85 - trick_risk)))
            elif best_type == "trade":
                extra = ""
                if hand_has_threat:
                    extra = " (you have follow-up in hand)"
                # C3: check if combat trick saves the blocker
                for trick_card in combat_tricks:
                    trick_text = " ".join(a.lower() for a in trick_card.abilities)
                    if "indestructible" in trick_text or "+1/+" in trick_text or "gets +" in trick_text:
                        extra = f" — cast {trick_card.name} to save it"
                        break
                advice.append(Advice("heuristic", "medium",
                    f"Trade: block {attacker.name} with {best_block.name}{extra}",
                    confidence=0.65))
            elif best_type == "chump":
                advice.append(Advice("heuristic", "high",
                    f"Chump block {attacker.name} with {best_block.name} to survive",
                    confidence=0.8))

    # C3: check remaining unblocked damage
    blocked_ids = used_blockers  # approximate
    unblocked_dmg = sum(a.power for a in opp_attackers
                        if a.instance_id not in {ap[0].instance_id for ap in attacker_priority
                                                  if any(b.instance_id in used_blockers for b in my_blockers)})
    # Simplified: just warn if still lethal after suggested blocks
    if lethal and len(advice) <= 1:
        advice.append(Advice("heuristic", "critical",
            f"Not enough blockers — need to block at least {incoming - life + 1} damage to survive",
            confidence=0.9))

    # C4: Trick risk — recommend preserving high-value blockers
    if trick_preserved and not lethal:
        names = ", ".join(b.name for b in trick_preserved)
        advice.insert(0, Advice("heuristic", "high",
            f"Don't block — preserve {names}"
            f" (trick risk {trick_risk:.0%}, life cushion {life_cushion:.0%})",
            confidence=0.7 + trick_risk * 0.2))

    # Explicit "don't block" when we have blockers but no good matchups
    if my_blockers and not used_blockers and not trick_preserved and not lethal:
        att_names = ", ".join(f"{a.name} ({a.power}/{a.toughness})"
                              for a in opp_attackers[:3])
        advice.append(Advice("heuristic", "medium",
            f"Don't block — no favorable trades vs {att_names}",
            confidence=0.75))

    # C4: Add trick warning when we still recommend blocking
    if (trick["warning"] and used_blockers
            and any(a.priority in ("high", "critical") for a in advice)):
        advice.append(Advice("heuristic", "high",
            trick["warning"], confidence=0.7))

    return advice[:3]


def _detect_cast_penalty(opp_battlefield: list[GameObject]) -> str | None:
    """Detect opponent permanents that penalize us for casting spells.

    Returns warning message or None.
    """
    for obj in opp_battlefield:
        card = card_cache.get(obj.grp_id)
        if not card:
            continue
        combined = " ".join(a.lower() for a in card.abilities)
        # Pattern: "whenever [opponent/player] casts a spell" + penalty
        if ("whenever" not in combined or "cast" not in combined
                or "spell" not in combined):
            continue
        # Must affect us (opponent = us from their perspective, or "a player")
        affects_us = ("opponent" in combined or "a player" in combined
                      or "each player" in combined)
        if not affects_us:
            continue
        # Detect penalty type
        if "loses" in combined and "life" in combined:
            import re
            m = re.search(r"loses\s+(\d+)\s+life", combined)
            amount = m.group(1) if m else "?"
            return (f"WARNING: {card.name} on board — "
                    f"each spell costs you {amount} life or a discard!")
        if "discard" in combined and "draw" not in combined:
            return (f"WARNING: {card.name} on board — "
                    f"each spell forces a discard!")
        if "pay" in combined and "unless" in combined:
            return (f"CAUTION: {card.name} on board — "
                    f"spells have extra cost")
    return None


def _is_aura(card) -> bool:
    """Check if a card is an Aura enchantment that needs a target."""
    if "Enchantment" not in card.card_types:
        return False
    first_ability = card.abilities[0].lower() if card.abilities else ""
    return first_ability.startswith("enchant ")


def _is_aura_removal(card) -> bool:
    """Check if a card is an aura with an ETB removal effect (exile/destroy).

    Examples: Sheltered by Ghosts, Ossification, Borrowed Time.
    These enchant a permanent but have an ETB that exiles/destroys opponent's stuff.
    """
    if not _is_aura(card):
        return False
    abilities_lower = " ".join(a.lower() for a in card.abilities)
    has_etb = "enters" in abilities_lower or "enter" in abilities_lower
    has_removal = any(kw in abilities_lower
                      for kw in ["exile", "destroy target", "destroy another"])
    return has_etb and has_removal


def _creature_objects(objects: list[GameObject]) -> list[GameObject]:
    """Resolve creatures from battlefield objects using card cache as fallback."""
    result = []
    for obj in objects:
        if obj.is_creature:
            result.append(obj)
            continue
        card = card_cache.get(obj.grp_id)
        if card and card.is_creature:
            result.append(obj)
    return result


def _needs_own_creature(card) -> bool:
    """Check if a spell requires a creature you control (buff/protection spells)."""
    oracle = (card.oracle_text or "").lower()
    abilities_text = " ".join(a.lower() for a in card.abilities)
    combined = oracle + " " + abilities_text
    return ("target creature you control" in combined
            or "creature you control gets" in combined)


def _is_reactive_instant(card) -> bool:
    """Check if a card is a reactive instant (combat trick, protection, counterspell).

    These should NOT be proactively suggested in Main phase —
    they only make sense in response to a threat (combat, removal on stack).
    """
    if "Instant" not in card.card_types:
        return False
    oracle = (card.oracle_text or "").lower()
    abilities_text = " ".join(a.lower() for a in card.abilities)
    combined = oracle + " " + abilities_text
    # Fight/bite instants ARE removal, not reactive
    if "fight" in combined or "deals damage equal" in combined:
        return False
    # Modal spells ("Choose one/two") with a removal mode are NOT reactive
    # e.g. Valorous Stance: indestructible OR destroy creature power 4+
    if "choose one" in combined or "choose two" in combined:
        _REMOVAL_IN_MODE = ["destroy", "exile", "damage", "return target",
                            "sacrifice", "fight"]
        if any(kw in combined for kw in _REMOVAL_IN_MODE):
            return False
    # Combat tricks / protection: buff own creature
    if _needs_own_creature(card):
        return True
    # Counterspells
    if "counter target" in combined or "counter spell" in combined:
        return True
    # Pure protection (hexproof, indestructible, phase out)
    if any(kw in combined for kw in ["hexproof", "phase out", "indestructible",
                                      "protection from"]):
        return True
    return False


def _is_battlefield_removal(card) -> bool:
    """True for spells/permanents that remove or bounce on-board objects.

    Excludes stack-only interaction like counterspells or creatures that exile a spell
    on ETB, which should not be suggested as permanent removal targets.
    """
    oracle = (card.oracle_text or "").lower()
    abilities_text = " ".join(a.lower() for a in card.abilities)
    combined = oracle + " " + abilities_text
    removal_kw = [
        "destroy", "exile", "damage", "sacrifice", "-",
        "fight", "deals damage equal", "return target", "return up to",
    ]
    if not any(kw in combined for kw in removal_kw):
        return False

    # Stack-only interaction is not board removal.
    stack_only = [
        "target spell",
        "counter target",
        "counter up to one target activated or triggered ability",
        "activated or triggered ability",
    ]
    battlefield_markers = [
        "target creature",
        "another target creature",
        "target attacking creature",
        "target blocking creature",
        "target creature an opponent controls",
        "target nonland permanent",
        "target permanent",
        "target enchantment",
        "target artifact",
        "target planeswalker",
        "target creature or planeswalker",
        "target attacking or blocking creature",
        "any target",
        "each creature",
        "all creatures",
        "each opponent",
        "target player",
    ]
    if any(token in combined for token in stack_only) and not any(
            marker in combined for marker in battlefield_markers):
        return False

    return any(marker in combined for marker in battlefield_markers)


def _has_flash(card) -> bool:
    """Check if a card has Flash keyword."""
    oracle = (card.oracle_text or "").lower()
    abilities_text = " ".join(a.lower() for a in card.abilities)
    combined = oracle + " " + abilities_text
    return "flash" in combined


def _main_phase_flash_hold_reason(card, state: GameState, opp_is_fast: bool) -> str | None:
    """Return a reason to avoid main-phase casting a flash creature.

    This is intentionally conservative for tempo shells: if we already have
    pressure on board, flash creatures often gain more by punishing the
    opponent's key turn than by adding another body now.
    """
    if not _has_flash(card):
        return None

    my_creatures = state.my_creatures()
    opp_creatures = state.opp_creatures()
    if not my_creatures:
        return None

    attackers = [c for c in my_creatures if _can_attack(c)]
    my_player = state.my_player()
    opp_player = state.opp_player()
    ahead_on_life = bool(my_player and opp_player and my_player.life_total >= opp_player.life_total)
    ahead_on_board = len(my_creatures) >= len(opp_creatures)
    under_pressure = opp_is_fast and (
        not attackers or len(my_creatures) + 1 < len(opp_creatures)
    )
    if under_pressure:
        return None

    if card.name == "Aven Interrupter":
        if len(attackers) >= 2 and (ahead_on_board or ahead_on_life):
            return "you already have pressure; keep flash up for their key spell"
        if len(my_creatures) >= 3 and ahead_on_board:
            return "board is already developed; flash punish is worth more than a main-phase body"

    if attackers and ahead_on_board and ahead_on_life:
        return "board is already ahead; flash is worth more on their turn"
    return None


def _has_evasion(card) -> bool:
    """Check if a card has evasion (flying, menace, unblockable, shadow, etc.)."""
    abilities_text = " ".join(a.lower() for a in card.abilities)
    return any(kw in abilities_text
               for kw in ["flying", "menace", "can't be blocked",
                           "shadow", "fear", "intimidate", "skulk"])


import re as _re

# Patterns for activated abilities: "{cost}: effect"
_ACTIVATED_RE = _re.compile(
    r'\{([^}]+)\}'   # mana symbols in braces
    r'(?:[,\s]*\{[^}]+\})*'  # additional cost symbols
    r'\s*:\s*'        # colon separator
    r'(.+)',          # effect text
)

# Mana cost pattern inside braces: oN or oW/oU/oB/oR/oG
_MANA_COST_RE = _re.compile(r'o(\d+|[WUBRG])')

# Effect keywords that are worth suggesting
_PUMP_KEYWORDS = ["get +", "+1/+1", "+2/+2", "+1/+0", "+2/+0", "+0/+1"]
_DRAW_KEYWORDS = ["draw a card", "draw cards", "look at the top"]
_OTHER_VALUABLE = ["create", "destroy", "exile", "return", "counter",
                   "gain", "lose", "deals", "tap target", "untap"]


def _parse_activated_abilities(card) -> list[dict]:
    """Parse activated abilities from card text. Returns list of
    {text, mana_cost, effect, category} dicts."""
    results = []
    for ab in (card.abilities or []):
        m = _ACTIVATED_RE.match(ab)
        if not m:
            continue
        cost_part = ab[:ab.index(":")]
        effect = m.group(2).strip().lower()

        # Skip pure mana abilities (tap for mana)
        if _re.match(r'^add \{?o?[WUBRGC\d]', effect):
            continue
        # Skip sacrifice-only costs without meaningful effect
        if "sacrifice cardname" in cost_part.lower() and "add" in effect:
            continue

        # Calculate total mana cost
        total_mana = 0
        for sym in _MANA_COST_RE.findall(cost_part):
            if sym.isdigit():
                total_mana += int(sym)
            else:
                total_mana += 1  # colored pip = 1 mana

        needs_tap = "oT" in cost_part

        # Categorize
        category = "utility"
        if any(kw in effect for kw in _PUMP_KEYWORDS):
            category = "pump"
        elif any(kw in effect for kw in _DRAW_KEYWORDS):
            category = "draw"
        elif any(kw in effect for kw in ["create", "token"]):
            category = "token"
        elif any(kw in effect for kw in ["destroy", "exile", "deals"]):
            category = "removal"

        results.append({
            "text": ab,
            "mana_cost": total_mana,
            "needs_tap": needs_tap,
            "effect": effect,
            "category": category,
        })
    return results


def _suggest_activated_abilities(state: GameState, mana: int) -> list[Advice]:
    """Suggest activated abilities on battlefield creatures worth using."""
    my_bf = state.my_battlefield()
    my_creatures = state.my_creatures()
    advice = []

    for obj in my_bf:
        if obj.is_land:
            continue
        # Skip tapped creatures for tap-abilities
        card = card_cache.get(obj.grp_id)
        if not card:
            continue

        abilities = _parse_activated_abilities(card)
        for ab in abilities:
            if ab["mana_cost"] > mana:
                continue
            if ab["needs_tap"] and obj.is_tapped:
                continue

            # Pump: only suggest before combat if we have creatures to attack
            if ab["category"] == "pump":
                attackers = [c for c in my_creatures if _can_attack(c)]
                if not attackers:
                    continue
                # Only in main phase or begin combat
                if "Main" not in state.turn_info.phase and "Combat" not in state.turn_info.phase:
                    continue
                n_attackers = len(attackers)
                # Is it a team pump or single target?
                is_team = "creatures you control" in ab["effect"]
                if is_team:
                    advice.append(Advice("heuristic", "high",
                                         f"Activate {card.name} — {ab['text'][:60]}... "
                                         f"(buffs {n_attackers} attacker{'s' if n_attackers > 1 else ''})",
                                         confidence=0.8,
                                         recommended_cards=[card.name]))
                else:
                    advice.append(Advice("heuristic", "medium",
                                         f"Activate {card.name} — {ab['text'][:60]}",
                                         confidence=0.6,
                                         recommended_cards=[card.name]))

            elif ab["category"] == "draw":
                # Draw abilities: suggest if low on cards
                hand_size = len(state.my_hand())
                if hand_size <= 2:
                    advice.append(Advice("heuristic", "medium",
                                         f"Activate {card.name} — draw (hand: {hand_size})",
                                         confidence=0.6,
                                         recommended_cards=[card.name]))

            elif ab["category"] == "removal":
                opp_threats = state.opp_battlefield()
                if opp_threats:
                    advice.append(Advice("heuristic", "high",
                                         f"Activate {card.name} — {ab['text'][:60]}",
                                         confidence=0.7,
                                         recommended_cards=[card.name]))

            elif ab["category"] == "token":
                advice.append(Advice("heuristic", "low",
                                     f"Activate {card.name} — {ab['text'][:60]}",
                                     confidence=0.5,
                                     recommended_cards=[card.name]))

    return advice


# Land subtype → color mapping (MTGA DB uses numeric subtype IDs)
_LAND_SUBTYPE_COLORS: dict[str, str] = {
    "54": "W",  # Plains
    "43": "U",  # Island
    "69": "B",  # Swamp
    "49": "R",  # Mountain
    "29": "G",  # Forest
}

# Mana symbol → color
_MANA_SYMBOL_COLORS = {"oW": "W", "oU": "U", "oB": "B", "oR": "R", "oG": "G"}


def _land_produces_colors(card) -> set[str]:
    """Determine what colors of mana a land can produce."""
    colors: set[str] = set()
    # Basic land subtypes (Plains, Island, etc.)
    for st in card.subtypes:
        c = _LAND_SUBTYPE_COLORS.get(st)
        if c:
            colors.add(c)
    # Parse abilities for mana production: "{oW}", "{oU}", etc.
    for ab in card.abilities:
        ab_lower = ab.lower()
        if "{ow}" in ab_lower:
            colors.add("W")
        if "{ou}" in ab_lower:
            colors.add("U")
        if "{ob}" in ab_lower:
            colors.add("B")
        if "{or}" in ab_lower:
            colors.add("R")
        if "{og}" in ab_lower:
            colors.add("G")
    # "any color" / "any type" mana abilities
    oracle = (card.oracle_text or "").lower()
    abilities_text = " ".join(a.lower() for a in card.abilities)
    combined = oracle + " " + abilities_text
    if "mana of any color" in combined or "mana of any type" in combined:
        colors.update("WUBRG")
    # Fallback: basic lands with no abilities still produce their color
    name_lower = card.name.lower()
    if not colors:
        basic_map = {"plains": "W", "island": "U", "swamp": "B",
                     "mountain": "R", "forest": "G"}
        for basic, c in basic_map.items():
            if basic in name_lower:
                colors.add(c)
    return colors


def _parse_mana_pips(mana_cost: str) -> tuple[list[str], int]:
    """Parse mana cost string into (colored_pips, generic_count).

    e.g. 'o1oWoU' → (['W', 'U'], 1)
         'o2oBoB' → (['B', 'B'], 2)
         'oW'     → (['W'], 0)
    """
    import re
    colored_pips: list[str] = []
    generic = 0
    for token in re.findall(r"o[WUBRGCX\d]+", mana_cost):
        if token in _MANA_SYMBOL_COLORS:
            colored_pips.append(_MANA_SYMBOL_COLORS[token])
        elif token == "oC":
            generic += 1  # colorless pip — any land pays it
        elif token == "oX":
            pass  # X cost — ignore for castability
        else:
            # Generic mana: o1, o2, etc.
            try:
                generic += int(token[1:])
            except ValueError:
                pass
    return colored_pips, generic


def _can_pay_mana_cost(mana_cost: str, untapped_lands: list[GameObject]) -> bool:
    """Check if untapped lands can pay a spell's mana cost (including colors).

    Uses greedy assignment: most constrained color pips first.
    """
    colored_pips, generic = _parse_mana_pips(mana_cost)
    total_needed = len(colored_pips) + generic
    total_available = len(untapped_lands)
    if total_available < total_needed:
        return False
    if not colored_pips:
        return True  # only generic cost, already checked total

    # Build mana sources: each land → set of colors it can produce
    sources: list[set[str]] = []
    for land_obj in untapped_lands:
        land_card = card_cache.get(land_obj.grp_id)
        if land_card:
            colors = _land_produces_colors(land_card)
            sources.append(colors if colors else {"C"})  # colorless-only land
        else:
            sources.append({"C"})

    # Greedy assignment: sort pips by how few sources can pay them (most constrained first)
    pip_options = []
    for pip in colored_pips:
        matching = [i for i, s in enumerate(sources) if pip in s]
        pip_options.append((pip, matching))
    pip_options.sort(key=lambda x: len(x[1]))

    used: set[int] = set()
    for pip, matching in pip_options:
        assigned = False
        for idx in matching:
            if idx not in used:
                used.add(idx)
                assigned = True
                break
        if not assigned:
            return False

    # Check remaining lands cover generic cost
    remaining = total_available - len(used)
    return remaining >= generic


def _has_aura_target(card, my_creatures: list, opp_battlefield: list) -> bool:
    """Check if there's a valid target for an aura on the battlefield."""
    first_ability = card.abilities[0].lower() if card.abilities else ""
    if "enchant creature you control" in first_ability:
        return len(my_creatures) > 0
    if "enchant creature an opponent controls" in first_ability:
        return any(obj.is_creature for obj in opp_battlefield)
    if "enchant creature" in first_ability:
        return len(my_creatures) > 0 or any(obj.is_creature for obj in opp_battlefield)
    if "enchant land" in first_ability:
        # Lands exist if we're casting spells
        return True
    if "enchant permanent" in first_ability:
        return len(my_creatures) > 0 or len(opp_battlefield) > 0
    # Unknown aura type — allow it
    return True


def _main_phase_land_options(
    untapped_lands: list[GameObject],
    lands_in_hand: list[GameObject],
) -> list[list[GameObject]]:
    """Possible untapped land sets after an optional main-phase land drop."""
    options = [untapped_lands]
    for land_obj in lands_in_hand:
        land_card = card_cache.get(land_obj.grp_id)
        if land_card:
            first_ab = land_card.abilities[0].lower() if land_card.abilities else ""
            if "enters tapped" in first_ab:
                continue
        options.append(untapped_lands + [land_obj])
    return options


def _attached_auras(
    creature: GameObject,
    state: GameState,
) -> list[tuple[GameObject, object, str]]:
    """Return opponent aura permanents currently attached to a creature."""
    opp_bf = state.opp_battlefield()
    attached: list[tuple[GameObject, object, str]] = []
    for obj in opp_bf:
        if obj.attached_to_id != creature.instance_id:
            continue
        card = card_cache.get(obj.grp_id)
        if not card:
            continue
        card_abs = " ".join(a.lower() for a in card.abilities)
        if "enchant" not in card_abs:
            continue
        attached.append((obj, card, card_abs))
    return attached


def _attached_aura_pressure(
    creature: GameObject,
    state: GameState,
) -> tuple[float, list[str]]:
    """Boost targets whose attached auras collapse multiple advantages at once."""
    bonus = 0.0
    reasons: list[str] = []
    for _, aura_card, aura_abs in _attached_auras(creature, state):
        aura_bonus = 0.0
        aura_reason: str | None = None
        if _is_aura_removal(aura_card):
            aura_bonus += 8
            aura_reason = f"carries {aura_card.name}"
            if "exile" in aura_abs:
                aura_bonus += 4
            if "lifelink" in aura_abs:
                aura_bonus += 2
            if "ward" in aura_abs or "hexproof" in aura_abs:
                aura_bonus += 2
        else:
            if "gets +" in aura_abs or "get +" in aura_abs:
                aura_bonus += 2
            if "flying" in aura_abs or "first strike" in aura_abs:
                aura_bonus += 1.5
            if "lifelink" in aura_abs:
                aura_bonus += 1.5
            if "draw a card" in aura_abs:
                aura_bonus += 1
            if "ward" in aura_abs or "hexproof" in aura_abs or "indestructible" in aura_abs:
                aura_bonus += 2
            if aura_bonus >= 3:
                aura_reason = f"buffed by {aura_card.name}"

        if aura_bonus:
            bonus += aura_bonus
        if aura_reason and aura_reason not in reasons:
            reasons.append(aura_reason)
    return bonus, reasons[:2]


def _attached_aura_collapse_note(creature: GameObject, state: GameState) -> str:
    """Short note for removal advice when killing a creature drops attached auras."""
    notes: list[str] = []
    for _, aura_card, aura_abs in _attached_auras(creature, state):
        if _is_aura_removal(aura_card):
            note = f"drops {aura_card.name}"
            if "exile" in aura_abs:
                note += " and should return your exiled permanent"
            notes.append(note)
            continue
        if any(token in aura_abs for token in ["gets +", "get +", "ward", "lifelink", "flying", "first strike"]):
            notes.append(f"drops {aura_card.name}")
    return "; ".join(notes[:2])


def _get_aura_abilities(creature: GameObject, state: GameState) -> str:
    """Return concatenated lowercase ability text of opponent's auras
    attached to this creature. Used to detect ward/hexproof/etc granted
    by auras rather than static card text."""
    parts = []
    for _, _, card_abs in _attached_auras(creature, state):
        parts.append(card_abs)
    return " ".join(parts)


def _suggest_aura_removal(
    threat: GameObject,
    state: GameState,
    removal_cards: list,
    mana: int,
    land_options: list[list[GameObject]],
    threat_name: str,
    threat_score: float,
    threat_reason: str,
) -> Advice | None:
    """When a creature has ward making it too expensive to target directly,
    check if we can target an opponent's aura attached to it instead.

    Removing the aura strips ward/lifelink/pump and may return exiled cards.
    """
    # Find opponent's auras attached to this creature
    auras_on_threat = _attached_auras(threat, state)

    if not auras_on_threat:
        return None

    # Find removal that can hit a nonland permanent (not just creatures)
    for aura_obj, aura_card, aura_abs in auras_on_threat:
        for removal in removal_cards:
            rem_abs = " ".join(a.lower() for a in removal.abilities)
            # Must target nonland permanent or enchantment (not creature-only)
            if "target creature" in rem_abs and "nonland permanent" not in rem_abs:
                continue
            if removal.cmc > mana:
                continue
            if removal.mana_cost and not any(
                    _can_pay_mana_cost(removal.mana_cost, option)
                    for option in land_options):
                continue

            # Describe what removing the aura achieves
            benefits = []
            if "ward" in aura_abs:
                benefits.append("strips ward")
            if "lifelink" in aura_abs:
                benefits.append("strips lifelink")
            if "exile" in aura_abs:
                benefits.append("returns your exiled card")
            if "gets +" in aura_abs or "get +" in aura_abs:
                benefits.append("removes buff")
            benefit_str = ", ".join(benefits) if benefits else "weakens it"

            return Advice(
                "heuristic", "high",
                f"Exile {aura_card.name} on {threat_name} with "
                f"{removal.name} — {benefit_str}"
                f"{_spree_bonus_note(removal, state, mana)}",
                confidence=0.8,
                recommended_cards=[removal.name],
            )

    return None


def _spree_bonus_note(card, state: GameState, available_mana: int) -> str:
    """Short note for spree cards when an extra paid mode is likely worth it."""
    if not card or card.name != "Requisition Raid":
        return ""
    my_creatures = state.my_creatures()
    if not my_creatures or available_mana < 3:
        return ""
    attackers = [c for c in my_creatures if _can_attack(c)]
    if attackers:
        return "; if you cast it here, spend the extra 1 mana for the +1/+1 team mode"
    return "; if that mana would go unused, add the extra 1 mana +1/+1 mode too"


def _matches_mana_value_restriction(text: str, target_cmc: int) -> bool:
    """Check simple 'mana value N or less/greater' targeting restrictions."""
    import re

    m = re.search(r"mana value (\d+) or less", text)
    if m and target_cmc > int(m.group(1)):
        return False

    m = re.search(r"mana value (\d+) or greater", text)
    if m and target_cmc < int(m.group(1)):
        return False

    return True


def _removal_can_target(removal_card, target: GameObject) -> bool:
    """Check if a removal spell can legally target a creature."""
    import re
    target_card = card_cache.get(target.grp_id)
    if target_card and not target_card.is_creature:
        return False

    text = " ".join(a.lower() for a in removal_card.abilities)
    oracle = (removal_card.oracle_text or "").lower()
    combined = f"{oracle} {text}"

    allowed_patterns = (
        r"\bdestroy target [^.;\n]*creature\b",
        r"\bexile target [^.;\n]*creature\b",
        r"\breturn target [^.;\n]*creature\b",
        r"\bdeal[s]? \d+ damage to target [^.;\n]*creature\b",
        r"\btarget attacking creature\b",
        r"\btarget blocking creature\b",
        r"\btarget attacking or blocking creature\b",
        r"\btarget creature an opponent controls\b",
        r"\btarget creature you control\b",
        r"\btarget creature gains\b",
        r"\btarget nonland permanent\b",
        r"\btarget permanent\b",
        r"\bany target\b",
    )
    if not any(re.search(pattern, combined) for pattern in allowed_patterns):
        return False

    if target_card and not _matches_mana_value_restriction(combined, target_card.cmc):
        return False

    # Check "toughness N or greater/less"
    m = re.search(r"target creature with toughness (\d+) or (greater|less)", combined)
    if m:
        threshold = int(m.group(1))
        direction = m.group(2)
        if direction == "greater" and target.toughness < threshold:
            return False
        if direction == "less" and target.toughness > threshold:
            return False
    # Check "power N or greater/less"
    m = re.search(r"target creature with power (\d+) or (greater|less)", combined)
    if m:
        threshold = int(m.group(1))
        direction = m.group(2)
        if direction == "greater" and target.power < threshold:
            return False
        if direction == "less" and target.power > threshold:
            return False
    return True


def _removal_kills_creature(removal_card, target: GameObject) -> bool:
    """Check if a removal spell can actually KILL a creature, not just target it.

    For destroy/exile removal, this is the same as can_target.
    For damage-based removal (burn), checks if damage >= toughness.
    """
    if not _removal_can_target(removal_card, target):
        return False
    # Check if this is damage-based removal — if so, verify it deals enough
    import re
    combined = " ".join(a.lower() for a in removal_card.abilities)
    oracle = (removal_card.oracle_text or "").lower()
    combined = f"{oracle} {combined}"
    dmg_match = re.search(r"deals? (\d+) damage", combined)
    if dmg_match:
        damage = int(dmg_match.group(1))
        if damage < target.toughness:
            return False
    return True


def _removal_can_hit_noncreature(removal_card, target: GameObject) -> bool:
    """Check whether a removal spell can hit a noncreature permanent."""
    target_card = card_cache.get(target.grp_id)
    if not target_card:
        return False

    text = " ".join(a.lower() for a in removal_card.abilities)
    oracle = (removal_card.oracle_text or "").lower()
    combined = oracle + " " + text

    if not _matches_mana_value_restriction(combined, target_card.cmc):
        return False

    if "target nonland permanent" in combined or "target permanent" in combined:
        return True

    card_types = set(target_card.card_types)
    if "Planeswalker" in card_types and (
            "target planeswalker" in combined
            or "creature or planeswalker" in combined):
        return True
    if "Enchantment" in card_types and (
            "target enchantment" in combined
            or "artifact or enchantment" in combined):
        return True
    if "Artifact" in card_types and (
            "target artifact" in combined
            or "artifact or enchantment" in combined):
        return True

    return False


def _parse_ward_cost(abilities_text: str) -> int | None:
    """Parse ward mana cost from ability text. Returns mana value or None."""
    import re
    # Ward {N} or Ward—pay N life (mana-based ward)
    m = re.search(r"ward\s*\{?\s*(\d+)\s*\}?", abilities_text)
    if m:
        return int(m.group(1))
    # Ward with mana symbols like {o1}{oU}
    m = re.search(r"ward[^.]*?o(\d+)", abilities_text)
    if m:
        return int(m.group(1))
    # Default ward cost estimate
    if "ward" in abilities_text:
        return 2  # conservative estimate
    return None


def _has_keyword(obj: GameObject, keyword: str) -> bool:
    """Check if creature has a keyword (includes conditional/granted keywords)."""
    card = card_cache.get(obj.grp_id)
    if not card:
        return False
    kw = keyword.lower()
    return any(kw in ab.lower() for ab in card.abilities)


def _protection_status(card, keyword: str) -> str:
    """Check if a protection keyword (hexproof/indestructible/shroud) is
    permanent, conditional, or absent.

    Returns:
        "permanent"   — always active (standalone keyword)
        "conditional" — gated by "as long as" / "if" / "until" condition
        "absent"      — keyword not present
    """
    kw = keyword.lower()
    abilities_lower = " ".join(a.lower() for a in card.abilities)
    if kw not in abilities_lower:
        return "absent"
    # Check if the keyword is conditional
    _COND_MARKERS = ["as long as", "if you", "if an ", "if a ", "if it ",
                     "if there", "while ", "unless ", kw + " until",
                     "gains " + kw + " until"]
    for ab in card.abilities:
        ab_lower = ab.lower()
        if kw not in ab_lower:
            continue
        if any(m in ab_lower for m in _COND_MARKERS):
            return "conditional"
    # If we get here, keyword is present but not conditional → permanent
    return "permanent"


def _aura_keyword_status(aura_abs: str, keyword: str) -> str:
    """Check if a keyword granted by an aura is permanent or conditional.

    aura_abs is the concatenated lowercase ability text of auras on a creature.
    Returns "permanent" or "conditional".
    """
    kw = keyword.lower()
    _COND_MARKERS = ["as long as", "if you", "if an ", "if a ", "if it ",
                     "if there", "while ", "unless ", kw + " until",
                     "gains " + kw + " until",
                     kw + " entered this turn",   # Shardmage's Rescue pattern
                     "entered this turn"]
    if any(m in aura_abs for m in _COND_MARKERS):
        return "conditional"
    return "permanent"


def _has_guaranteed_keyword(obj: GameObject, keyword: str) -> bool:
    """Check if creature has a keyword as a STATIC ability (not conditional).

    Returns True only if the keyword is a standalone ability entry (e.g. "Flying"),
    NOT if it appears inside a conditional clause like
    "As long as you've lost life this turn, CARDNAME has flying".
    """
    card = card_cache.get(obj.grp_id)
    if not card:
        return False
    kw = keyword.lower()
    for ab in card.abilities:
        ab_lower = ab.lower().strip()
        # Standalone keyword: ability text is just the keyword (possibly with reminder text)
        if ab_lower == kw or ab_lower.startswith(kw + " ("):
            return True
        # Equipment/aura grants: "Enchanted/Equipped creature has flying"
        if ab_lower.startswith("enchant") or ab_lower.startswith("equip"):
            continue
        # Conditional: "as long as", "if ", "whenever", "gains ... until"
        # These are NOT guaranteed — skip them
        if any(cond in ab_lower for cond in
               ["as long as", "if you", "if an", "if a ", "whenever",
                "gains " + kw, kw + " until", "you may have"]):
            continue
        # Keyword in a list at the start: "Flying, first strike" or "Flying\nVigilance"
        first_word = ab_lower.split(",")[0].split("\n")[0].strip()
        if first_word == kw:
            return True
    return False


def _can_attack(obj: GameObject) -> bool:
    """Check if creature can attack (includes Defender check)."""
    if not obj.can_attack:
        return False
    if _has_keyword(obj, "Defender"):
        return False
    return True


def _is_unblockable(obj: GameObject) -> bool:
    """Check if creature can't be blocked."""
    card = card_cache.get(obj.grp_id)
    if not card:
        return False
    text = " ".join(a.lower() for a in card.abilities)
    return "can't be blocked" in text or "unblockable" in text
