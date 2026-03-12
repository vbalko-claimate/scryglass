"""Data models for MTGA game state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CardInfo:
    """Resolved card information from DB/Scryfall."""
    grp_id: int
    name: str
    mana_cost: str = ""
    cmc: int = 0
    colors: list[str] = field(default_factory=list)
    card_types: list[str] = field(default_factory=list)
    subtypes: list[str] = field(default_factory=list)
    power: str = ""
    toughness: str = ""
    rarity: str = ""
    expansion: str = ""
    abilities: list[str] = field(default_factory=list)
    oracle_text: str = ""

    @property
    def is_creature(self) -> bool:
        return "Creature" in self.card_types

    @property
    def is_land(self) -> bool:
        return "Land" in self.card_types

    @property
    def is_instant(self) -> bool:
        return "Instant" in self.card_types

    @property
    def type_line(self) -> str:
        parts = self.card_types[:]
        if self.subtypes:
            parts.append("—")
            parts.extend(self.subtypes)
        return " ".join(parts)

    def short_str(self) -> str:
        if self.is_creature:
            return f"{self.name} ({self.mana_cost}) {self.power}/{self.toughness}"
        return f"{self.name} ({self.mana_cost})"


@dataclass
class GameObject:
    """A card/permanent/ability instance in the game."""
    instance_id: int
    grp_id: int
    zone_id: int
    owner_seat_id: int
    controller_seat_id: int = 0
    card_types: list[str] = field(default_factory=list)
    subtypes: list[str] = field(default_factory=list)
    color: list[str] = field(default_factory=list)
    power: int = 0
    toughness: int = 0
    name: str = ""
    is_tapped: bool = False
    has_summoning_sickness: bool = False
    attack_state: str | None = None
    block_state: str | None = None
    attached_to_id: int | None = None
    counters: dict[str, int] = field(default_factory=dict)
    abilities: list[dict] = field(default_factory=list)
    object_type: str = "Card"  # Card, Ability, Token
    source_grp_id: int = 0  # for Ability: grp_id of the source card
    parent_id: int = 0  # for Ability: instance_id of the parent object

    @property
    def is_creature(self) -> bool:
        return "CardType_Creature" in self.card_types

    @property
    def is_land(self) -> bool:
        return "CardType_Land" in self.card_types

    @property
    def can_attack(self) -> bool:
        return (self.is_creature and not self.is_tapped
                and not self.has_summoning_sickness)


@dataclass
class Zone:
    """A game zone (hand, battlefield, etc.)."""
    zone_id: int
    type: str
    owner_seat_id: int | None = None
    object_instance_ids: list[int] = field(default_factory=list)
    visibility: str = "Visibility_Public"


@dataclass
class PlayerState:
    """A player's state in the game."""
    seat_id: int
    life_total: int = 20
    starting_life_total: int = 20
    max_hand_size: int = 7
    mulligan_count: int = 0
    team_id: int = 0
    pending_message_type: str | None = None
    controller_type: str = "ControllerType_Player"
    name: str = ""


@dataclass
class TurnInfo:
    """Current turn/phase/step information."""
    phase: str = ""
    step: str = ""
    turn_number: int = 0
    active_player: int = 0
    priority_player: int = 0
    decision_player: int = 0
    next_phase: str = ""
    next_step: str = ""

    @property
    def phase_display(self) -> str:
        from .enums import PHASES, STEPS
        p = PHASES.get(self.phase, self.phase)
        s = STEPS.get(self.step, self.step)
        if s:
            return f"{p} - {s}"
        return p


@dataclass
class Action:
    """An available action for the player."""
    seat_id: int = 0
    action_type: str = ""
    instance_id: int | None = None
    grp_id: int | None = None
    mana_cost: list[dict] | None = None
    ability_grp_id: int | None = None
    auto_tap_solution: dict | None = None

    @property
    def type_display(self) -> str:
        from .enums import ACTION_TYPES
        return ACTION_TYPES.get(self.action_type, self.action_type)


@dataclass
class MatchInfo:
    """Top-level match metadata."""
    match_id: str = ""
    game_number: int = 0
    stage: str = ""
    opponent_name: str = ""
    opponent_seat_id: int = 0


@dataclass
class GameState:
    """Complete reconstructed game state."""
    match_info: MatchInfo = field(default_factory=MatchInfo)
    my_seat_id: int = 0
    my_deck: list[int] = field(default_factory=list)

    players: dict[int, PlayerState] = field(default_factory=dict)
    zones: dict[int, Zone] = field(default_factory=dict)
    objects: dict[int, GameObject] = field(default_factory=dict)

    turn_info: TurnInfo = field(default_factory=TurnInfo)
    available_actions: list[Action] = field(default_factory=list)
    pending_request: str | None = None

    game_state_id: int = 0
    annotations: list[dict] = field(default_factory=list)

    # Hand disruption counter — incremented when opponent exiles/discards from our hand
    hand_disrupted_count: int = 0

    # Helpers
    def my_player(self) -> PlayerState | None:
        return self.players.get(self.my_seat_id)

    def opp_player(self) -> PlayerState | None:
        return self.players.get(self.match_info.opponent_seat_id)

    def zone_by_type(self, zone_type: str, owner: int | None = None) -> Zone | None:
        for z in self.zones.values():
            if z.type == zone_type and (owner is None or z.owner_seat_id == owner):
                return z
        return None

    def objects_in_zone(self, zone_type: str, owner: int | None = None) -> list[GameObject]:
        result = []
        for z in self.zones.values():
            if z.type == zone_type and (owner is None or z.owner_seat_id == owner):
                result.extend(
                    self.objects[iid] for iid in z.object_instance_ids
                    if iid in self.objects
                )
        return result

    def my_hand(self) -> list[GameObject]:
        return self.objects_in_zone("ZoneType_Hand", self.my_seat_id)

    def my_battlefield(self) -> list[GameObject]:
        return [o for o in self.objects_in_zone("ZoneType_Battlefield")
                if o.controller_seat_id == self.my_seat_id]

    def opp_battlefield(self) -> list[GameObject]:
        return [o for o in self.objects_in_zone("ZoneType_Battlefield")
                if o.controller_seat_id == self.match_info.opponent_seat_id]

    def my_creatures(self) -> list[GameObject]:
        return [o for o in self.my_battlefield() if o.is_creature]

    def opp_creatures(self) -> list[GameObject]:
        return [o for o in self.opp_battlefield() if o.is_creature]

    def my_lands(self) -> list[GameObject]:
        return [o for o in self.my_battlefield() if o.is_land]

    def my_untapped_lands(self) -> list[GameObject]:
        return [o for o in self.my_lands() if not o.is_tapped]

    def my_graveyard(self) -> list[GameObject]:
        return self.objects_in_zone("ZoneType_Graveyard", self.my_seat_id)

    def opp_graveyard(self) -> list[GameObject]:
        return self.objects_in_zone("ZoneType_Graveyard", self.match_info.opponent_seat_id)

    def stack(self) -> list[GameObject]:
        return self.objects_in_zone("ZoneType_Stack")


@dataclass
class Advice:
    """Play advice from the advisor."""
    source: str  # "heuristic" or "llm"
    priority: str  # "critical", "high", "medium", "low"
    message: str
    details: str = ""
    confidence: float = 0.0
    recommended_cards: list[str] = field(default_factory=list)
