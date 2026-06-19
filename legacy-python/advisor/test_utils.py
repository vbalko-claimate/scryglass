"""Shared test utilities — synthetic GameState builder and constants."""
from __future__ import annotations

from pathlib import Path

from .database import card_cache
from .models import GameState, TurnInfo, PlayerState, MatchInfo, Zone, GameObject


CORPUS_DIR = Path(__file__).parent.parent / "data" / "replay_corpus"
REPLAY_PASS_THRESHOLD = 0.90


def build_synthetic_state(
    *,
    turn: int = 1,
    phase: str = "Phase_Main1",
    my_life: int = 20,
    opp_life: int = 20,
    hand: list[str] | None = None,
    my_battlefield: list[str] | None = None,
    opp_battlefield: list[str] | None = None,
) -> GameState:
    """Build a synthetic GameState from card name lists.

    Used by regression_tests, replay_diff, and replay_corpus.
    """
    card_cache.load()

    state = GameState()
    state.my_seat_id = 1
    state.match_info = MatchInfo()
    state.match_info.opponent_seat_id = 2

    state.players = {
        1: PlayerState(seat_id=1, life_total=my_life),
        2: PlayerState(seat_id=2, life_total=opp_life),
    }
    state.turn_info = TurnInfo(
        phase=phase, step=phase,
        turn_number=turn,
        active_player=1, priority_player=1, decision_player=1,
    )

    zone_id = 1
    objects: dict[int, GameObject] = {}
    instance_id = 1000

    def make_obj(name: str, owner: int) -> GameObject:
        nonlocal instance_id
        card = None
        for c in card_cache._cache.values():
            if c.name == name:
                card = c
                break
        if not card:
            is_land = name in ("Plains", "Mountain", "Island", "Swamp", "Forest")
            gre_types = ["CardType_Land"] if is_land else ["CardType_Creature"]
            obj = GameObject(
                instance_id=instance_id, grp_id=0, zone_id=0,
                owner_seat_id=owner, controller_seat_id=owner,
                card_types=gre_types, name=name,
                power=2 if not is_land else 0,
                toughness=2 if not is_land else 0,
            )
        else:
            gre_types = [f"CardType_{t}" for t in card.card_types]
            obj = GameObject(
                instance_id=instance_id, grp_id=card.grp_id, zone_id=0,
                owner_seat_id=owner, controller_seat_id=owner,
                card_types=gre_types, name=card.name,
                color=card.colors,
                power=int(card.power) if card.power and card.power.isdigit() else 0,
                toughness=int(card.toughness) if card.toughness and card.toughness.isdigit() else 0,
            )
        instance_id += 1
        return obj

    hand_zone = Zone(zone_id=zone_id, type="ZoneType_Hand", owner_seat_id=1)
    zone_id += 1
    bf_zone = Zone(zone_id=zone_id, type="ZoneType_Battlefield", owner_seat_id=0)
    zone_id += 1

    for name in (hand or []):
        obj = make_obj(name, 1)
        obj.zone_id = hand_zone.zone_id
        objects[obj.instance_id] = obj
        hand_zone.object_instance_ids.append(obj.instance_id)

    for name in (my_battlefield or []):
        obj = make_obj(name, 1)
        obj.zone_id = bf_zone.zone_id
        objects[obj.instance_id] = obj
        bf_zone.object_instance_ids.append(obj.instance_id)

    for name in (opp_battlefield or []):
        obj = make_obj(name, 2)
        obj.zone_id = bf_zone.zone_id
        objects[obj.instance_id] = obj
        bf_zone.object_instance_ids.append(obj.instance_id)

    state.zones = {hand_zone.zone_id: hand_zone, bf_zone.zone_id: bf_zone}
    state.objects = objects
    state.game_objects = objects
    return state
