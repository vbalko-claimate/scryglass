#!/usr/bin/env python3
"""Simulate what the current suggestion engine would advise for a historical match.

Usage: uv run python tools/simulate_match.py <match_id>
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from advisor.database import card_cache, init_db
from advisor.heuristics import (
    _suggest_plays, _suggest_attacks, _check_mulligan,
    _suggest_activated_abilities, evaluate_opponent_board,
    set_opp_deck,
)
from advisor.models import GameState, GameObject, PlayerState, TurnInfo, MatchInfo, Zone

from advisor.database import DB_PATH


def _to_gre_types(card_types: list[str]) -> list[str]:
    """Convert card cache types ('Land') to GRE format ('CardType_Land') for GameObjects."""
    return [f"CardType_{t}" if not t.startswith("CardType_") else t
            for t in card_types]


def build_game_state(turn_data: dict, hand_cards: list, my_seat: int = 1) -> GameState:
    """Build a minimal GameState from turn_start event data."""
    gs = GameState()
    gs.my_seat_id = my_seat

    # Players
    p_me = PlayerState(seat_id=my_seat, life_total=turn_data.get("my_life", 20))
    p_opp = PlayerState(seat_id=2, life_total=turn_data.get("opp_life", 20))
    gs.players = {my_seat: p_me, 2: p_opp}

    # Turn info
    gs.turn_info = TurnInfo()
    gs.turn_info.turn_number = turn_data.get("turn_number", 1)
    gs.turn_info.phase = "Phase_Main1"
    gs.turn_info.step = "Step_Main1"
    gs.turn_info.active_player = my_seat
    gs.turn_info.priority_player = my_seat

    # Match info
    gs.match_info = MatchInfo()
    gs.match_info.opponent_seat_id = 2

    # Zones
    bf_zone_id = 10
    hand_zone_id = 20
    opp_bf_zone_id = 30

    gs.zones = {
        bf_zone_id: Zone(zone_id=bf_zone_id, type="ZoneType_Battlefield",
                              owner_seat_id=my_seat, object_instance_ids=[]),
        hand_zone_id: Zone(zone_id=hand_zone_id, type="ZoneType_Hand",
                                owner_seat_id=my_seat, object_instance_ids=[]),
        opp_bf_zone_id: Zone(zone_id=opp_bf_zone_id, type="ZoneType_Battlefield",
                                  owner_seat_id=2, object_instance_ids=[]),
    }

    inst_id = 100

    # My battlefield
    for c in turn_data.get("my_creatures", []):
        obj = GameObject(
            instance_id=inst_id,
            grp_id=_find_grp_id(c["name"]),
            name=c["name"],
            owner_seat_id=my_seat,
            controller_seat_id=my_seat,
            zone_id=bf_zone_id,
            card_types=["CardType_Creature"],
            power=c.get("power", 0),
            toughness=c.get("toughness", 0),
            is_tapped=c.get("tapped", False),
        )
        gs.objects[inst_id] = obj
        gs.zones[bf_zone_id].object_instance_ids.append(inst_id)
        inst_id += 1

    # Opponent battlefield
    for c in turn_data.get("opp_creatures", []):
        obj = GameObject(
            instance_id=inst_id,
            grp_id=_find_grp_id(c["name"]),
            name=c["name"],
            owner_seat_id=2,
            controller_seat_id=2,
            zone_id=opp_bf_zone_id,
            card_types=["CardType_Creature"],
            power=c.get("power", 0),
            toughness=c.get("toughness", 0),
            is_tapped=c.get("tapped", False),
        )
        gs.objects[inst_id] = obj
        gs.zones[opp_bf_zone_id].object_instance_ids.append(inst_id)
        inst_id += 1

    # Hand
    for card_name in hand_cards:
        grp_id = _find_grp_id(card_name)
        card = card_cache.get(grp_id)
        ct = _to_gre_types(card.card_types) if card else []
        obj = GameObject(
            instance_id=inst_id,
            grp_id=grp_id,
            name=card_name,
            owner_seat_id=my_seat,
            controller_seat_id=my_seat,
            zone_id=hand_zone_id,
            card_types=ct,
        )
        gs.objects[inst_id] = obj
        gs.zones[hand_zone_id].object_instance_ids.append(inst_id)
        inst_id += 1

    # Add untapped lands to battlefield (based on available_mana)
    mana = turn_data.get("available_mana", 0)
    for i in range(mana):
        land_name = "Plains" if i % 2 == 0 else "Island"
        grp_id = _find_grp_id(land_name)
        obj = GameObject(
            instance_id=inst_id,
            grp_id=grp_id,
            name=land_name,
            owner_seat_id=my_seat,
            controller_seat_id=my_seat,
            zone_id=bf_zone_id,
            card_types=["CardType_Land"],
        )
        gs.objects[inst_id] = obj
        gs.zones[bf_zone_id].object_instance_ids.append(inst_id)
        inst_id += 1

    return gs


def _find_grp_id(name: str) -> int:
    for gid, card in card_cache._cache.items():
        if card.name == name:
            return gid
    return 0


def main():
    init_db()
    card_cache.load()

    if len(sys.argv) < 2:
        # Use latest match
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT match_id FROM matches ORDER BY started_at DESC LIMIT 1").fetchone()
        conn.close()
        if not row:
            print("No matches in DB")
            return
        match_id = row[0]
    else:
        match_id = sys.argv[1]

    conn = sqlite3.connect(str(DB_PATH))
    events = conn.execute(
        "SELECT event_type, turn_number, phase, data FROM match_events "
        "WHERE match_id = ? ORDER BY rowid",
        (match_id,)).fetchall()
    match_info = conn.execute(
        "SELECT opponent_name, my_deck_name, opp_deck_name FROM matches WHERE match_id = ?",
        (match_id,)).fetchone()
    conn.close()

    opp_name, my_deck, opp_deck = match_info or ("?", "?", "?")
    print(f"Match: {match_id[:12]}... | {my_deck} vs {opp_deck} ({opp_name})")
    print("=" * 70)

    # Track hand contents through the game
    hand = []
    mulligan_hand = []

    for etype, turn, phase, data_str in events:
        data = json.loads(data_str) if data_str else {}

        if etype == "mulligan":
            mulligan_hand = [c["name"] for c in data.get("hand", [])]
            hand = list(mulligan_hand)
            print(f"\n  Mulligan: {data.get('decision')} — {hand}")

            # Simulate mulligan advice
            turn_data = {"turn_number": 0, "available_mana": 0, "my_life": 20, "opp_life": 20,
                         "my_creatures": [], "opp_creatures": []}
            gs = build_game_state(turn_data, hand)
            gs.turn_info.phase = "Mulligan"
            advice = _check_mulligan(gs)
            if advice:
                for a in advice:
                    print(f"    >> [{a.priority}] {a.message}")

        elif etype == "turn_start":
            turn_num = data.get("turn_number", turn) if data else turn
            # Only show my turns (odd turns = mine typically, but use available_mana > 0 heuristic)
            mana = data.get("available_mana", 0)
            my_life = data.get("my_life", 20)
            opp_life = data.get("opp_life", 20)
            my_creatures = data.get("my_creatures", [])
            opp_creatures = data.get("opp_creatures", [])

            print(f"\n  === Turn {turn_num} === "
                  f"Life: {my_life}/{opp_life} | Mana: {mana} | "
                  f"Board: {len(my_creatures)} vs {len(opp_creatures)} | "
                  f"Hand: {len(hand)}")

            if my_creatures:
                my_str = ", ".join(f"{c['name']} {c.get('power',0)}/{c.get('toughness',0)}"
                                   for c in my_creatures)
                print(f"    My board: {my_str}")
            if opp_creatures:
                opp_str = ", ".join(f"{c['name']} {c.get('power',0)}/{c.get('toughness',0)}"
                                    for c in opp_creatures)
                print(f"    Opp board: {opp_str}")
            if hand:
                print(f"    Hand: {hand}")

            # Build game state and get suggestions
            # Account for land drop: if hand has a land, mana will be +1 after playing it
            has_land_in_hand = any(
                card_cache.get(_find_grp_id(h)) and
                any("Land" in t for t in card_cache.get(_find_grp_id(h)).card_types)
                for h in hand
            )
            post_land_mana = mana + 1 if has_land_in_hand else mana

            gs = build_game_state(data, hand)
            play_advice = _suggest_plays(gs)
            ability_advice = _suggest_activated_abilities(gs, post_land_mana)

            # Threat evaluation
            threats = evaluate_opponent_board(gs)

            if play_advice:
                print(f"    SUGGESTIONS:")
                for a in play_advice:
                    print(f"      [{a.priority:8s}] {a.message}")
            if ability_advice:
                for a in ability_advice:
                    if not any(a.message == pa.message for pa in play_advice):
                        print(f"      [{a.priority:8s}] {a.message}")
            if threats:
                top = threats[0]
                print(f"    TOP THREAT: {top[0].name} (score {top[1]:.0f}) — {top[2]}")

        elif etype == "card_played":
            card_name = data.get("name", "?")
            is_land = data.get("is_land", False)
            if not is_land and card_name in hand:
                hand.remove(card_name)
            elif is_land:
                # Remove a land from hand if present
                for i, h in enumerate(hand):
                    card = card_cache.get(_find_grp_id(h))
                    if card and any("Land" in t for t in card.card_types):
                        hand.pop(i)
                        break
            print(f"    > Played: {card_name}")

        elif etype == "game_end":
            my_life = data.get("my_life", 0)
            opp_life = data.get("opp_life", 0)
            print(f"\n  === GAME END === Life: {my_life}/{opp_life}")


if __name__ == "__main__":
    main()
