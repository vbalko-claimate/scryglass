"""Heuristic-based play advisor — short, actionable suggestions."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .database import card_cache
from .models import Advice, GameState, GameObject

if TYPE_CHECKING:
    from .strategy import MetaDeck

# Current opponent deck (set by advisor_engine when opponent identified)
_current_opp_deck: "MetaDeck | None" = None


def set_opp_deck(deck: "MetaDeck | None"):
    """Set identified opponent deck for threat scoring."""
    global _current_opp_deck
    _current_opp_deck = deck


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
    global _card_wr, _player_prefs, _current_opp_deck
    _card_wr = None
    _player_prefs = None
    _current_opp_deck = None


def hand_synergy_score(candidate_grp_id: int, hand: list[GameObject]) -> int:
    """C1: Score how well a card synergizes with the rest of the hand.

    Checks if playing the candidate enables triggers on other hand cards.
    """
    candidate = card_cache.get(candidate_grp_id)
    if not candidate:
        return 0

    cand_text = " ".join(a.lower() for a in candidate.abilities)
    cand_oracle = (candidate.oracle_text or "").lower()
    cand_combined = cand_text + " " + cand_oracle

    # What does this candidate provide?
    has_lifelink = "lifelink" in cand_combined
    has_passive_lifegain = ("gain" in cand_combined and "life" in cand_combined
                            and "whenever" in cand_combined)
    # ETB lifegain = triggers from creatures entering (Sanctifier pattern)
    has_etb_lifegain = (has_passive_lifegain
                        and ("enters" in cand_combined or "creature" in cand_combined))
    provides_lifegain = has_lifelink or has_passive_lifegain
    is_creature = candidate.is_creature
    cand_subtypes = set(s.lower() for s in candidate.subtypes)

    score = 0
    for obj in hand:
        if obj.grp_id == candidate_grp_id:
            continue  # don't score self
        other = card_cache.get(obj.grp_id)
        if not other:
            continue
        other_text = " ".join(a.lower() for a in other.abilities)
        other_oracle = (other.oracle_text or "").lower()
        other_combined = other_text + " " + other_oracle

        # Does candidate enable other's "whenever you gain life" trigger?
        if "whenever you gain life" in other_combined:
            if has_etb_lifegain:
                # ETB lifegain triggers automatically from playing creatures — superior
                score += 3
            elif has_lifelink:
                # Lifelink requires attacking and not being blocked — conditional
                score += 1
            elif has_passive_lifegain:
                score += 2

        # Does candidate enable "whenever a creature enters" / "another creature"?
        if "whenever" in other_combined and "enters" in other_combined and is_creature:
            score += 1

        # Does candidate enable "whenever another [subtype]"?
        for st in cand_subtypes:
            if "whenever" in other_combined and st in other_combined:
                score += 1

        # Does candidate provide counters/buffs for other payoffs?
        if "+1/+1 counter" in other_combined and provides_lifegain:
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

    scored: list[tuple[GameObject, float, str]] = []
    for obj in opp_creatures:
        card = card_cache.get(obj.grp_id)
        if not card:
            scored.append((obj, obj.power * 1.5, "unknown card"))
            continue

        score = obj.power * 1.5 + obj.toughness * 0.5
        reasons = []
        text = " ".join(a.lower() for a in card.abilities)
        oracle = (card.oracle_text or "").lower()
        combined = text + " " + oracle

        # Keyword bonuses
        kw_bonuses = [
            ("flying", 3, "flying"),
            ("deathtouch", 4, "deathtouch"),
            ("lifelink", 3, "lifelink"),
            ("trample", 2, "trample"),
            ("menace", 2, "menace"),
            ("hexproof", 5, "hexproof"),
            ("indestructible", 6, "indestructible"),
            ("double strike", 4, "double strike"),
        ]
        for kw, bonus, label in kw_bonuses:
            if kw in combined:
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

        reason_str = ", ".join(reasons[:4]) if reasons else f"{obj.power}/{obj.toughness} body"
        scored.append((obj, score, reason_str))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def analyze(state: GameState) -> list[Advice]:
    """Run all checks. Returns short actionable advice — max 2-3 items."""
    if not state.my_seat_id or not state.players:
        return []

    ti = state.turn_info
    is_my_turn = ti.active_player == state.my_seat_id

    advice = []

    # Mulligan
    advice.extend(_check_mulligan(state))

    if is_my_turn:
        # Only suggest attacks/lethal on our turn
        advice.extend(_check_lethal(state))
        advice.extend(_suggest_plays(state))
        advice.extend(_suggest_attacks(state))
    else:
        # Opponent's turn — check their lethal and suggest blocks
        advice.extend(_check_opponent_lethal(state))
        advice.extend(_check_combat_blocks(state))

    # Cap at 3 most important
    advice.sort(key=lambda a: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(a.priority, 4))
    return advice[:3]


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

    opp_attackers = [c for c in state.opp_creatures()
                     if c.can_attack and not _has_keyword(c, "Defender")]
    opp_power = sum(c.power for c in opp_attackers)

    if opp_power >= me.life_total:
        my_blockers = [c for c in state.my_creatures() if not c.is_tapped]
        if len(my_blockers) < len(opp_attackers):
            return [Advice("heuristic", "critical",
                            f"DANGER — opponent has {opp_power} power, you have {me.life_total} life",
                            confidence=0.8)]
    return []


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

    # Count castable spells (assuming we play one land per turn)
    castable_t1 = 0
    castable_t2 = 0
    for obj in nonlands:
        card = card_cache.get(obj.grp_id)
        if card:
            if card.cmc <= 1:
                castable_t1 += 1
            if card.cmc <= 2:
                castable_t2 += 1

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
        # All lands ETB tapped — no play until T2+ (devastating for aggro)
        if tapped_lands > 0 and untapped_lands == 0 and lands <= 3:
            return [Advice("heuristic", "high",
                           f"Risky keep — all {lands} lands enter tapped, "
                           f"no play until T{lands + 1}: {hand_str}",
                           confidence=0.7)]
        if lands >= 2 and castable_t2 >= 1:
            # Warn if most lands are tapped
            tapped_warn = ""
            if tapped_lands >= lands - 1 and tapped_lands > 0:
                tapped_warn = f" ({tapped_lands} tapped!)"
            return [Advice("heuristic", "medium",
                           f"Keep — {lands} lands{tapped_warn}, "
                           f"{castable_t2} early play(s): {hand_str}",
                           confidence=0.75 if not tapped_warn else 0.55)]
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
    mana = len(untapped_lands)
    lands_in_hand = [c for c in hand if c.is_land]

    advice = []

    # Check for opponent cast-penalty enchantments (Painful Quandary etc.)
    opp_bf = state.opp_battlefield()
    cast_penalty = _detect_cast_penalty(opp_bf)
    if cast_penalty:
        advice.append(Advice("heuristic", "high",
                              cast_penalty, confidence=0.85))

    # Suggest castable spells (biggest first)
    my_creatures = state.my_creatures()
    castable = []
    for obj in hand:
        if obj.is_land:
            continue
        card = card_cache.get(obj.grp_id)
        if not card or card.cmc > mana:
            continue
        # Color check: verify untapped lands can actually pay the colored pips
        if card.mana_cost and not _can_pay_mana_cost(card.mana_cost, untapped_lands):
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
        castable.append(card)

    # Check if we have removal and opponent has threats
    removal_cards = []
    for card in castable:
        abilities_lower = " ".join(a.lower() for a in card.abilities)
        if any(kw in abilities_lower for kw in ["destroy", "exile", "damage", "sacrifice", "-"]):
            removal_cards.append(card)

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
            if "hexproof" in combined_abs or "shroud" in combined_abs:
                continue
            threat_name = threat_card.name
            warn = ""
            available_removal = removal_cards[:]
            if "indestructible" in combined_abs:
                exile_removal = [r for r in available_removal
                                 if "exile" in " ".join(a.lower() for a in r.abilities)]
                if exile_removal:
                    available_removal = exile_removal
                    warn = " (indestructible — exile!)"
                else:
                    continue
            valid_removal = [r for r in available_removal
                             if _removal_can_target(r, top_threat)]
            if not valid_removal:
                continue
            if "ward" in combined_abs:
                ward_cost = _parse_ward_cost(combined_abs)
                if ward_cost and ward_cost + valid_removal[0].cmc > mana:
                    # Ward too expensive — check if we can target an aura on it instead
                    aura_advice = _suggest_aura_removal(
                        top_threat, state, removal_cards, mana, untapped_lands,
                        threat_name, threat_score, threat_reason)
                    if aura_advice:
                        advice.append(aura_advice)
                        break
                    continue
                warn += " (has ward)"
            # Warn about aura-based exile returning if aura is destroyed
            best_removal = valid_removal[0]
            removal_abs = " ".join(a.lower() for a in best_removal.abilities)
            if ("enchant" in removal_abs and "exile" in removal_abs
                    and "when" in (threat_card.oracle_text or "").lower()
                    and "enters" in (threat_card.oracle_text or "").lower()):
                warn += " (returns if aura destroyed — has ETB!)"
            # Flash removal: hold for opp turn unless threat is urgent
            if _has_flash(best_removal) and threat_score < 10:
                advice.append(Advice("heuristic", "medium",
                                      f"Hold {best_removal.name} — flash removal, "
                                      f"use on opp turn vs {threat_name}",
                                      confidence=0.6,
                                      recommended_cards=[best_removal.name]))
            else:
                advice.append(Advice("heuristic", "high",
                                      f"Remove {threat_name} ({top_threat.power}/{top_threat.toughness}) "
                                      f"with {best_removal.name} — {threat_reason}{warn}",
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

    # Use non-flash creatures first; only suggest flash if nothing else to play
    active_creatures = non_flash_creatures if non_flash_creatures else creatures

    if active_creatures:
        turn = state.turn_info.turn_number
        if turn <= 4:
            # C1: hand-aware priority — synergy score breaks ties
            active_creatures.sort(key=lambda c: (
                -c.cmc if c.cmc <= mana else 99,
                -hand_synergy_score(c.grp_id, hand),
                -_card_score(c.name),
                c.name,
            ))
        else:
            active_creatures.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        best = active_creatures[0]
        wr = _get_card_wr()
        wr_note = f" [{wr[best.name]:.0f}% WR]" if best.name in wr else ""
        advice.append(Advice("heuristic", "medium",
                              f"Cast {best.name} ({best.mana_cost}){wr_note}",
                              confidence=0.6,
                              recommended_cards=[best.name]))

    # Suggest holding flash cards for opponent's turn (only if we have other plays)
    flash_holdable = flash_creatures + flash_spells
    if flash_holdable and (non_flash_creatures or non_flash_spells or removal_cards):
        flash_holdable.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        best_flash = flash_holdable[0]
        # Skip if we already have a "hold" advice from strategy rules
        if not any("hold" in a.message.lower() and best_flash.name.lower() in a.message.lower()
                   for a in advice):
            advice.append(Advice("heuristic", "low",
                                  f"Hold {best_flash.name} — has flash, cast on opp turn",
                                  confidence=0.5))

    # Non-flash spells only
    if non_flash_spells and not advice:
        non_flash_spells.sort(key=lambda c: (-c.cmc, -_card_score(c.name)))
        best = non_flash_spells[0]
        wr = _get_card_wr()
        wr_note = f" [{wr[best.name]:.0f}% WR]" if best.name in wr else ""
        advice.append(Advice("heuristic", "medium",
                              f"Cast {best.name} ({best.mana_cost}){wr_note}",
                              confidence=0.5,
                              recommended_cards=[best.name]))

    # Remind about land drop
    if lands_in_hand and not any("land" in a.message.lower() for a in advice):
        advice.append(Advice("heuristic", "low", "Play a land", confidence=0.4))

    return advice[:2]


def _suggest_attacks(state: GameState) -> list[Advice]:
    """Suggest attack strategy."""
    ti = state.turn_info
    if ti.phase != "Phase_Combat" or ti.step not in ("Step_DeclareAttack", "Step_BeginCombat", ""):
        return []

    attackers = [c for c in state.my_creatures() if _can_attack(c)]
    if not attackers:
        return []

    opp_blockers = [c for c in state.opp_creatures()
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

    return []


def _check_combat_blocks(state: GameState) -> list[Advice]:
    if state.pending_request != "GREMessageType_DeclareBlockersReq":
        return []

    opp_attackers = [o for o in state.opp_battlefield() if o.is_creature and o.attack_state]
    my_blockers = [c for c in state.my_creatures() if not c.is_tapped]

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

        if best_block:
            used_blockers.add(best_block.instance_id)
            if best_type == "clean_kill":
                advice.append(Advice("heuristic", "high",
                    f"Block {attacker.name} with {best_block.name} — kills it, survives",
                    confidence=0.85))
            elif best_type == "trade":
                extra = ""
                if hand_has_threat:
                    extra = " (you have follow-up in hand)"
                # C3: check if combat trick saves the blocker
                for trick in combat_tricks:
                    trick_text = " ".join(a.lower() for a in trick.abilities)
                    if "indestructible" in trick_text or "+1/+" in trick_text or "gets +" in trick_text:
                        extra = f" — cast {trick.name} to save it"
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

    # Explicit "don't block" when we have blockers but no good matchups
    if my_blockers and not used_blockers and not lethal:
        att_names = ", ".join(f"{a.name} ({a.power}/{a.toughness})"
                              for a in opp_attackers[:3])
        advice.append(Advice("heuristic", "medium",
            f"Don't block — no favorable trades vs {att_names}",
            confidence=0.75))

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
    # Combat tricks / protection: buff own creature
    if _needs_own_creature(card):
        return True
    # Counterspells
    if "counter target" in combined:
        return True
    # Pure protection (hexproof, indestructible, phase out)
    if any(kw in combined for kw in ["hexproof", "phase out", "indestructible",
                                      "protection from"]):
        return True
    return False


def _has_flash(card) -> bool:
    """Check if a card has Flash keyword."""
    oracle = (card.oracle_text or "").lower()
    abilities_text = " ".join(a.lower() for a in card.abilities)
    combined = oracle + " " + abilities_text
    return "flash" in combined


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


def _get_aura_abilities(creature: GameObject, state: GameState) -> str:
    """Return concatenated lowercase ability text of opponent's auras
    attached to this creature. Used to detect ward/hexproof/etc granted
    by auras rather than static card text."""
    opp_bf = state.opp_battlefield()
    parts = []
    for obj in opp_bf:
        if obj.attached_to_id != creature.instance_id:
            continue
        card = card_cache.get(obj.grp_id)
        if not card:
            continue
        card_abs = " ".join(a.lower() for a in card.abilities)
        if "enchant" in card_abs:
            parts.append(card_abs)
    return " ".join(parts)


def _suggest_aura_removal(
    threat: GameObject,
    state: GameState,
    removal_cards: list,
    mana: int,
    untapped_lands: list[GameObject],
    threat_name: str,
    threat_score: float,
    threat_reason: str,
) -> Advice | None:
    """When a creature has ward making it too expensive to target directly,
    check if we can target an opponent's aura attached to it instead.

    Removing the aura strips ward/lifelink/pump and may return exiled cards.
    """
    # Find opponent's auras attached to this creature
    opp_bf = state.opp_battlefield()
    auras_on_threat = []
    for obj in opp_bf:
        if obj.attached_to_id != threat.instance_id:
            continue
        card = card_cache.get(obj.grp_id)
        if not card:
            continue
        card_abs = " ".join(a.lower() for a in card.abilities)
        if "enchant" in card_abs:
            auras_on_threat.append((obj, card, card_abs))

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
            if removal.mana_cost and not _can_pay_mana_cost(
                    removal.mana_cost, untapped_lands):
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
                f"{removal.name} — {benefit_str}",
                confidence=0.8,
                recommended_cards=[removal.name],
            )

    return None


def _removal_can_target(removal_card, target: GameObject) -> bool:
    """Check if a removal spell can legally target a creature (toughness/power restrictions)."""
    import re
    text = " ".join(a.lower() for a in removal_card.abilities)
    # Check "toughness N or greater/less"
    m = re.search(r"target creature with toughness (\d+) or (greater|less)", text)
    if m:
        threshold = int(m.group(1))
        direction = m.group(2)
        if direction == "greater" and target.toughness < threshold:
            return False
        if direction == "less" and target.toughness > threshold:
            return False
    # Check "power N or greater/less"
    m = re.search(r"target creature with power (\d+) or (greater|less)", text)
    if m:
        threshold = int(m.group(1))
        direction = m.group(2)
        if direction == "greater" and target.power < threshold:
            return False
        if direction == "less" and target.power > threshold:
            return False
    return True


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
