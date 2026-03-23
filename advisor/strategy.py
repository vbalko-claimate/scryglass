"""Layered rule engine — deck strategy plugins that learn from match outcomes.

Rule Layers (higher = more specific, wins conflicts):
  0: GENERAL        — universal MTG fundamentals
  1: ARCHETYPE      — aggro/midrange/control patterns
  2: MULLIGAN       — hand evaluation per deck
  3: CARD_SYNERGY   — combos/sequences in your deck
  4: THREAT_RESPONSE— responses to opponent's cards on board
  5: SITUATION      — board state triggers (low life, flooding, racing)
  6: META_GAMEPLAN  — full matchup strategy once opponent deck identified
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .database import card_cache, USER_DATA_DIR
from .models import ActionFamily, ActionScore, Advice, GameState, GameObject, RuleHit
from .actions import infer_action_family, is_hold_rule, score_from_priority, render_advice
from .version import ENGINE_VERSION, SCHEMA_VERSION

log = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent.parent / "data" / "strategies"
DECKS_ROOT = USER_DATA_DIR / "decks"

# ─── Hot-reload: mtime-based cache invalidation ────────────────
_strategy_cache: dict[str, tuple[float, Strategy]] = {}  # path -> (mtime, strategy)

# Layer priority (higher = more specific)
LAYERS = {
    "general": 0,
    "archetype": 1,
    "mulligan": 2,
    "card_synergy": 3,
    "threat_response": 4,
    "situation": 5,
    "meta_gameplan": 6,
}

# Learning constants
WIN_BOOST = 0.05
LOSS_PENALTY = 0.03
MIN_WEIGHT = 0.1
MAX_WEIGHT = 2.0


# ─── Data Models ────────────────────────────────────────────────

@dataclass
class CardMatcher:
    """Matches a card by various criteria."""
    name: str | list[str] | None = None
    keyword: str | None = None
    card_type: str | None = None  # "Creature", "Instant", etc.
    cmc_min: int | None = None
    cmc_max: int | None = None
    power_min: int | None = None
    toughness_min: int | None = None
    toughness_max: int | None = None
    castable: bool = False  # must be castable with current mana
    color: str | None = None

    @staticmethod
    def from_dict(d: dict) -> CardMatcher:
        if isinstance(d, str):
            return CardMatcher(name=d)
        return CardMatcher(
            name=d.get("name"),
            keyword=d.get("keyword"),
            card_type=d.get("type"),
            cmc_min=d.get("cmc_min"),
            cmc_max=d.get("cmc_max"),
            power_min=d.get("power_min"),
            toughness_min=d.get("toughness_min"),
            toughness_max=d.get("toughness_max"),
            castable=d.get("castable", False),
            color=d.get("color"),
        )


@dataclass
class ZoneCondition:
    """A condition checking a game zone."""
    zone: str  # hand, battlefield, opp_battlefield, graveyard, stack
    match: CardMatcher = field(default_factory=CardMatcher)
    min_count: int = 1
    max_count: int | None = None
    absent: bool = False  # True = card must NOT be present
    tapped: bool | None = None  # None = don't care
    prefer: str | None = None  # C1: "synergy_first" = pick card with best hand synergy

    @staticmethod
    def from_dict(d: dict) -> ZoneCondition:
        return ZoneCondition(
            zone=d.get("zone", "hand"),
            match=CardMatcher.from_dict(d.get("match", {})),
            min_count=d.get("min_count", 1),
            max_count=d.get("max_count"),
            absent=d.get("absent", False),
            tapped=d.get("tapped"),
            prefer=d.get("prefer"),
        )


@dataclass
class Rule:
    """A single strategy rule."""
    id: str
    layer: str = "general"
    tags: list[str] = field(default_factory=list)

    # Trigger conditions
    phase: list[str] | None = None  # ["Main", "Combat"], None = any
    my_turn: bool | None = None  # None = either turn
    turn_min: int | None = None
    turn_max: int | None = None
    step: str | None = None

    # Zone conditions (the core matching)
    require: list[ZoneCondition] = field(default_factory=list)

    # Simple conditions (shortcuts)
    life_below: int | None = None
    life_above: int | None = None
    opp_life_below: int | None = None
    opp_life_above: int | None = None
    mana_min: int | None = None
    hand_lands_min: int | None = None
    hand_lands_max: int | None = None
    hand_size_min: int | None = None
    hand_size_max: int | None = None
    hand_castable_min: int | None = None
    hand_castable_max: int | None = None
    my_creatures_min: int | None = None
    opp_creatures_min: int | None = None

    # Opponent meta conditions (for meta_gameplan rules)
    opp_speed: str | None = None           # "fast", "medium", "slow"
    opp_has_must_answer: bool | None = None  # opponent has must_answer card on board
    opp_has_vulnerability: bool | None = None  # opponent played a card from our vulnerabilities

    # Output
    action: str = ""  # advice text, supports {card}, {threat} placeholders
    action_family: str | None = None  # canonical: cast_spell, play_land, attack, block, activate, pass
    priority: str = "medium"
    conflicts_with: list[str] = field(default_factory=list)

    # Learning
    weight: float = 1.0
    times_fired: int = 0
    times_correct: int = 0

    metrics: dict = field(default_factory=dict)  # serialized RuleMetrics

    # Provenance
    source: str = ""  # "mechanical" | "llm" | "manual" | "ga"

    @property
    def layer_priority(self) -> int:
        return LAYERS.get(self.layer, 0)


@dataclass
class MetaDeck:
    """A known meta deck for opponent recognition."""
    name: str
    archetype: str  # aggro, midrange, control, combo
    colors: list[str] = field(default_factory=list)
    signal_cards: dict[str, float] = field(default_factory=dict)  # card_name: confidence
    key_threats: list[dict] = field(default_factory=list)  # [{card, danger, reason}]
    speed: str = "medium"  # very_fast, fast, medium, slow
    typical_kill_turn: int = 10
    hidden_reach: int = 0  # damage from hand
    description: str = ""


@dataclass
class Strategy:
    """Complete deck strategy with all layers."""
    name: str
    deck_signature: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    archetype: str = "unknown"
    rules: list[Rule] = field(default_factory=list)
    general_overrides: list[str] = field(default_factory=list)  # general rule IDs replaced by deck rules
    vulnerabilities: list[dict] = field(default_factory=list)  # cards that specifically threaten THIS deck
    stats: dict = field(default_factory=lambda: {"games": 0, "wins": 0, "losses": 0})
    global_biases: dict[str, float] = field(default_factory=dict)

    def win_rate(self) -> float:
        return self.stats["wins"] / self.stats["games"] if self.stats["games"] else 0.0


# ─── Opponent Tracker ───────────────────────────────────────────

class OpponentTracker:
    """Tracks opponent's played cards to identify their deck."""

    def __init__(self):
        self.seen_cards: dict[str, int] = {}  # card_name: count
        self.seen_colors: set[str] = set()
        self.identified_deck: MetaDeck | None = None
        self.confidence: float = 0.0
        self.spent_removal: list[str] = []  # removal spells already used
        self.ability_triggers: dict[str, int] = {}  # source_card_name: trigger_count

    def observe(self, card_name: str, colors: list[str]):
        """Track a card the opponent played."""
        self.seen_cards[card_name] = self.seen_cards.get(card_name, 0) + 1
        self.seen_colors.update(colors)

    def observe_spell(self, card_name: str, colors: list[str],
                      card_types: list[str], oracle_text: str = ""):
        """Track a spell cast (instant/sorcery) — also detects removal."""
        self.observe(card_name, colors)
        # Detect removal spells
        text = oracle_text.lower()
        if any(kw in text for kw in ("destroy", "exile", "damage",
                                      "return target", "-x/-x", "gets -")):
            self.spent_removal.append(card_name)

    def observe_ability(self, source_card_name: str):
        """Track a triggered/activated ability firing."""
        self.ability_triggers[source_card_name] = (
            self.ability_triggers.get(source_card_name, 0) + 1)

    def identify(self, meta_decks: list[MetaDeck]) -> MetaDeck | None:
        """Try to identify opponent's deck from observed cards."""
        if self.identified_deck and self.confidence >= 0.8:
            return self.identified_deck

        best: MetaDeck | None = None
        best_conf = 0.0

        for deck in meta_decks:
            conf = 0.0
            matched = 0
            for card, weight in deck.signal_cards.items():
                if card in self.seen_cards:
                    conf += weight
                    matched += 1
            # Color match bonus — stronger when colors fully match
            if deck.colors and self.seen_colors:
                color_overlap = len(set(deck.colors) & self.seen_colors) / len(deck.colors)
                # Extra colors not in the deck reduce confidence
                extra_colors = self.seen_colors - set(deck.colors)
                penalty = len(extra_colors) * 0.05
                conf += color_overlap * 0.25 - penalty

            if conf > best_conf:
                best_conf = conf
                best = deck

        if best and best_conf >= 0.3:
            if best != self.identified_deck:
                log.info("Opponent deck identified: %s (%.0f%% confidence, %d cards seen)",
                         best.name, best_conf * 100, len(self.seen_cards))
            self.identified_deck = best
            self.confidence = min(best_conf, 1.0)
            return best

        # Fallback: color-based identification when no signal cards match
        # but we've seen enough cards to know the color identity.
        # Always create a generic deck — don't reuse specific archetypes
        # which would have wrong key_threats and gameplan info.
        if len(self.seen_cards) >= 4 and self.seen_colors:
            color_name = _color_identity_name(sorted(self.seen_colors))
            archetype = _guess_archetype_from_cards(self.seen_cards)
            fallback = MetaDeck(
                name=f"{color_name} (unknown)",
                archetype=archetype,
                colors=sorted(self.seen_colors),
                speed="medium",
            )
            if not self.identified_deck or self.identified_deck.name != fallback.name:
                log.info("Opponent deck fallback: %s (%s, %d cards seen)",
                         fallback.name, archetype, len(self.seen_cards))
            self.identified_deck = fallback
            self.confidence = 0.25
            return fallback

        return None

    def reset(self):
        self.seen_cards.clear()
        self.seen_colors.clear()
        self.identified_deck = None
        self.confidence = 0.0
        self.spent_removal.clear()
        self.ability_triggers.clear()


# ─── Color / Archetype Helpers ─────────────────────────────────

_GUILD_NAMES = {
    frozenset({"W", "U"}): "Azorius", frozenset({"U", "B"}): "Dimir",
    frozenset({"B", "R"}): "Rakdos", frozenset({"R", "G"}): "Gruul",
    frozenset({"G", "W"}): "Selesnya", frozenset({"W", "B"}): "Orzhov",
    frozenset({"U", "R"}): "Izzet", frozenset({"B", "G"}): "Golgari",
    frozenset({"R", "W"}): "Boros", frozenset({"G", "U"}): "Simic",
}
_MONO_NAMES = {"W": "Mono-White", "U": "Mono-Blue", "B": "Mono-Black",
               "R": "Mono-Red", "G": "Mono-Green"}
_SHARD_NAMES = {
    frozenset({"W", "U", "B"}): "Esper", frozenset({"U", "B", "R"}): "Grixis",
    frozenset({"B", "R", "G"}): "Jund", frozenset({"R", "G", "W"}): "Naya",
    frozenset({"G", "W", "U"}): "Bant", frozenset({"W", "B", "G"}): "Abzan",
    frozenset({"W", "U", "R"}): "Jeskai", frozenset({"U", "B", "G"}): "Sultai",
    frozenset({"B", "R", "W"}): "Mardu", frozenset({"R", "G", "U"}): "Temur",
}


def _color_identity_name(colors: list[str] | set[str]) -> str:
    """Convert color set to MTG name (e.g., {U, B} → 'Dimir')."""
    cs = frozenset(colors)
    if len(cs) == 1:
        return _MONO_NAMES.get(next(iter(cs)), "Unknown")
    if len(cs) == 2:
        return _GUILD_NAMES.get(cs, "/".join(sorted(cs)))
    if len(cs) == 3:
        return _SHARD_NAMES.get(cs, "/".join(sorted(cs)))
    return "/".join(sorted(cs))


def _guess_archetype_from_cards(seen_cards: dict[str, int]) -> str:
    """Rough archetype guess from observed card types."""
    # Build a name→card lookup from the cache
    creatures = 0
    noncreatures = 0
    name_lookup: dict[str, CardInfo] = {}
    if not card_cache._loaded:
        card_cache.load()
    for c in card_cache._cache.values():
        name_lookup[c.name] = c
    for name in seen_cards:
        card = name_lookup.get(name)
        if not card:
            continue
        if card.is_creature:
            creatures += 1
        else:
            noncreatures += 1
    total = creatures + noncreatures
    if total == 0:
        return "unknown"
    ratio = creatures / total
    if ratio >= 0.7:
        return "aggro"
    if ratio <= 0.3:
        return "control"
    return "midrange"


# ─── Global Meta Decks ─────────────────────────────────────────

META_DECKS_PATH = Path(__file__).parent.parent / "data" / "meta" / "meta_decks.json"


def load_meta_decks() -> list[MetaDeck]:
    """Load the global meta_decks database (shared across all decks)."""
    if not META_DECKS_PATH.exists():
        return []
    try:
        data = json.loads(META_DECKS_PATH.read_text())
        return [
            MetaDeck(
                name=md["name"], archetype=md.get("archetype", "unknown"),
                colors=md.get("colors", []), signal_cards=md.get("signal_cards", {}),
                key_threats=md.get("key_threats", []), speed=md.get("speed", "medium"),
                typical_kill_turn=md.get("typical_kill_turn", 10),
                hidden_reach=md.get("hidden_reach", 0), description=md.get("description", ""),
            )
            for md in data.get("meta_decks", [])
        ]
    except Exception as e:
        log.error("Failed to load meta_decks: %s", e)
        return []


def save_meta_decks(meta_decks: list[MetaDeck]):
    """Save the global meta_decks database."""
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "meta_decks": [
            {"name": md.name, "archetype": md.archetype, "colors": md.colors,
             "signal_cards": md.signal_cards, "key_threats": md.key_threats,
             "speed": md.speed, "typical_kill_turn": md.typical_kill_turn,
             "hidden_reach": md.hidden_reach, "description": md.description}
            for md in meta_decks
        ]
    }
    META_DECKS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log.info("Global meta_decks saved: %d entries", len(meta_decks))


# ─── Card Matching ──────────────────────────────────────────────

def _has_keyword(card_abilities: list[str], keyword: str) -> bool:
    """Check if any ability string contains the keyword (case-insensitive).

    Supports '|' as OR: "destroy|exile" matches if any ability contains
    either "destroy" or "exile".
    """
    keywords = [k.strip().lower() for k in keyword.split("|")]
    for ab in card_abilities:
        ab_l = ab.lower()
        if any(kw in ab_l for kw in keywords):
            return True
    return False


def _has_protection(card_abilities: list[str]) -> set[str]:
    """Return set of protection keywords found on a card."""
    found = set()
    for ab in card_abilities:
        ab_l = ab.lower()
        if "hexproof" in ab_l:
            found.add("hexproof")
        if "shroud" in ab_l:
            found.add("shroud")
        if "indestructible" in ab_l:
            found.add("indestructible")
        if "ward" in ab_l:
            found.add("ward")
        if "protection from" in ab_l:
            found.add("protection")
    return found


def _card_matches(obj: GameObject, matcher: CardMatcher, mana: int = 99,
                   untapped_lands: list[GameObject] | None = None) -> bool:
    """Check if a game object matches a CardMatcher."""
    card = card_cache.get(obj.grp_id)

    if not card:
        # Fallback for objects not in card cache (e.g. Forge-only cards with grp_id=0).
        # Use data directly from the GameObject for basic matching.
        if matcher.name:
            names = matcher.name if isinstance(matcher.name, list) else [matcher.name]
            if obj.name not in names:
                return False
        if matcher.card_type:
            # Check against GRE-format types on the object (e.g. "CardType_Creature")
            gre_type = f"CardType_{matcher.card_type}"
            if gre_type not in obj.card_types:
                return False
        if matcher.keyword:
            return False  # can't check keywords without card data
        if matcher.castable:
            return False  # can't verify castability without cmc
        if matcher.power_min is not None and obj.power < matcher.power_min:
            return False
        if matcher.toughness_min is not None and obj.toughness < matcher.toughness_min:
            return False
        return True

    if matcher.name:
        names = matcher.name if isinstance(matcher.name, list) else [matcher.name]
        if card.name not in names:
            return False
    if matcher.keyword:
        # Check both abilities and oracle_text for keyword match
        oracle = [card.oracle_text] if card.oracle_text else []
        if not _has_keyword(card.abilities + oracle, matcher.keyword):
            return False
    if matcher.card_type and matcher.card_type not in card.card_types:
        return False
    if matcher.cmc_min is not None and card.cmc < matcher.cmc_min:
        return False
    if matcher.cmc_max is not None and card.cmc > matcher.cmc_max:
        return False
    if matcher.power_min is not None and obj.power < matcher.power_min:
        return False
    if matcher.toughness_min is not None and obj.toughness < matcher.toughness_min:
        return False
    if matcher.toughness_max is not None and obj.toughness > matcher.toughness_max:
        return False
    if matcher.castable and card.cmc > mana:
        return False
    # Color-aware castability: check if untapped lands can pay colored pips
    if matcher.castable and untapped_lands is not None and card.mana_cost:
        from .heuristics import _can_pay_mana_cost
        if not _can_pay_mana_cost(card.mana_cost, untapped_lands):
            return False
    if matcher.color:
        if matcher.color not in card.colors:
            return False
    return True


def _get_zone_objects(state: GameState, zone: str) -> list[GameObject]:
    """Get objects from a named zone."""
    if zone == "hand":
        return state.my_hand()
    elif zone == "battlefield":
        return state.my_battlefield()
    elif zone == "opp_battlefield":
        return state.opp_battlefield()
    elif zone == "graveyard":
        return state.my_graveyard()
    elif zone == "opp_graveyard":
        return state.opp_graveyard()
    elif zone == "stack":
        return state.stack()
    return []


def _check_zone_condition(cond: ZoneCondition, state: GameState, mana: int,
                          untapped_lands: list[GameObject] | None = None) -> tuple[bool, GameObject | None]:
    """Check a zone condition. Returns (passed, matched_card)."""
    objects = _get_zone_objects(state, cond.zone)

    # Filter by tapped state
    if cond.tapped is not None:
        objects = [o for o in objects if o.is_tapped == cond.tapped]

    matching = [o for o in objects if _card_matches(o, cond.match, mana, untapped_lands)]
    count = len(matching)

    if cond.absent:
        return (count == 0, None)

    if count < cond.min_count:
        return (False, None)
    if cond.max_count is not None and count > cond.max_count:
        return (False, None)

    # C1: prefer — re-sort matching by synergy with hand
    if cond.prefer == "synergy_first" and len(matching) > 1 and cond.zone == "hand":
        from .heuristics import hand_synergy_score
        hand_objs = _get_zone_objects(state, "hand")
        matching.sort(key=lambda o: hand_synergy_score(o.grp_id, hand_objs), reverse=True)

    return (True, matching[0] if matching else None)


# ─── Rule Evaluation ────────────────────────────────────────────

def _phase_matches(phase: str, patterns: list[str]) -> bool:
    """Check if current phase matches any pattern."""
    phase_lower = phase.lower()
    return any(p.lower() in phase_lower for p in patterns)


def _speed_category(speed: str) -> str:
    """Normalize speed to fast/medium/slow for rule matching."""
    if speed in ("very_fast", "fast"):
        return "fast"
    if speed in ("medium_fast", "medium"):
        return "medium"
    return "slow"  # medium_slow, slow


def evaluate_rules_v2(rules: list[Rule], state: GameState,
                      opp_tracker: OpponentTracker | None = None,
                      vulnerabilities: list[dict] | None = None,
                      max_results: int = 5) -> tuple[list[RuleHit], list[Advice]]:
    """Evaluate all rules against current game state — structured output.

    Returns (rule_hits, advice) where each RuleHit carries ActionScore objects
    and advice is the rendered Advice list (backward-compatible).
    max_results: maximum number of items to return (0 = unlimited).
    """
    if not rules:
        return [], []

    ti = state.turn_info
    is_my_turn = ti.active_player == state.my_seat_id
    is_mulligan = state.pending_request == "GREMessageType_MulliganReq"
    me = state.my_player()
    opp = state.opp_player()
    hand = state.my_hand()
    my_bf = state.my_battlefield()
    opp_bf = state.opp_battlefield()
    untapped_lands = state.my_untapped_lands()
    mana = len(untapped_lands)

    results: list[tuple[Rule, RuleHit]] = []
    fired_ids: set[str] = set()

    for rule in rules:
        if rule.weight < MIN_WEIGHT:
            continue

        # Initialize effective priority — branches below may downgrade it
        prio = rule.priority

        # --- Phase / timing ---
        is_mull_rule = rule.phase and "Mulligan" in rule.phase
        if is_mull_rule and not is_mulligan:
            continue
        if not is_mull_rule and is_mulligan:
            continue

        if not is_mull_rule:
            if rule.my_turn is not None and rule.my_turn != is_my_turn:
                continue
            if rule.phase and not _phase_matches(ti.phase, rule.phase):
                continue
        if rule.turn_min is not None and ti.turn_number < rule.turn_min:
            continue
        if rule.turn_max is not None and ti.turn_number > rule.turn_max:
            continue
        if rule.step and rule.step.lower() not in ti.step.lower():
            continue

        # --- Simple conditions ---
        if rule.life_below is not None:
            if not me or me.life_total >= rule.life_below:
                continue
        if rule.life_above is not None:
            if not me or me.life_total <= rule.life_above:
                continue
        if rule.opp_life_below is not None:
            if not opp or opp.life_total >= rule.opp_life_below:
                continue
        if rule.opp_life_above is not None:
            if not opp or opp.life_total <= rule.opp_life_above:
                continue
        if rule.mana_min is not None and mana < rule.mana_min:
            continue
        if rule.hand_lands_min is not None:
            lands = sum(1 for o in hand if o.is_land)
            if lands < rule.hand_lands_min:
                continue
        if rule.hand_lands_max is not None:
            lands = sum(1 for o in hand if o.is_land)
            if lands > rule.hand_lands_max:
                continue
        if rule.hand_size_min is not None and len(hand) < rule.hand_size_min:
            continue
        if rule.hand_size_max is not None and len(hand) > rule.hand_size_max:
            continue
        if rule.hand_castable_min is not None or rule.hand_castable_max is not None:
            land_count = sum(1 for o in hand if o.is_land)
            castable = sum(1 for o in hand if not o.is_land and
                           (card_cache.get(o.grp_id) or type("C", (), {"cmc": 99})).cmc <= land_count)
            if rule.hand_castable_min is not None and castable < rule.hand_castable_min:
                continue
            if rule.hand_castable_max is not None and castable > rule.hand_castable_max:
                continue
        if rule.my_creatures_min is not None:
            if sum(1 for o in my_bf if o.is_creature) < rule.my_creatures_min:
                continue
        if rule.opp_creatures_min is not None:
            if sum(1 for o in opp_bf if o.is_creature) < rule.opp_creatures_min:
                continue

        # --- Opponent meta conditions ---
        if rule.opp_speed is not None or rule.opp_has_must_answer is not None or rule.opp_has_vulnerability is not None:
            opp_deck_obj = opp_tracker.identified_deck if opp_tracker else None
            if not opp_deck_obj:
                continue  # can't evaluate meta rules without identified opponent

            if rule.opp_speed is not None:
                if _speed_category(opp_deck_obj.speed) != _speed_category(rule.opp_speed):
                    continue

            if rule.opp_has_must_answer is True:
                opp_bf_names = {card_cache.get(o.grp_id).name
                                for o in opp_bf if card_cache.get(o.grp_id)}
                has_ma = any(kt.get("must_answer") and kt.get("card") in opp_bf_names
                             for kt in opp_deck_obj.key_threats)
                if not has_ma:
                    continue

            if rule.opp_has_vulnerability is True:
                if not vulnerabilities:
                    continue
                opp_bf_names = {card_cache.get(o.grp_id).name
                                for o in opp_bf if card_cache.get(o.grp_id)}
                vuln_match = next((v for v in vulnerabilities
                                   if v.get("card") in opp_bf_names), None)
                if not vuln_match:
                    continue

        # --- Zone conditions (the powerful part) ---
        matched_card = None
        matched_threat = None
        zone_ok = True
        for zc in rule.require:
            passed, card_obj = _check_zone_condition(zc, state, mana, untapped_lands)
            if not passed:
                zone_ok = False
                break
            if card_obj:
                if zc.zone.startswith("opp"):
                    if not matched_threat:  # keep first opp match for {threat}
                        matched_threat = card_obj
                else:
                    if not matched_card:  # keep first own match for {card}
                        matched_card = card_obj
        if not zone_ok:
            continue

        # --- All conditions passed ---
        msg = rule.action
        matched_card_name = ""
        matched_threat_name = ""
        if matched_card:
            c = card_cache.get(matched_card.grp_id)
            matched_card_name = c.name if c else matched_card.name
            msg = msg.replace("{card}", matched_card_name)
            # If message doesn't mention the matched card, append it for specificity
            if matched_card_name and matched_card_name not in msg and "{card}" not in rule.action:
                mana_cost = c.mana_cost if c else ""
                msg = f"{msg}: {matched_card_name}" + (f" ({mana_cost})" if mana_cost else "")
        if matched_threat:
            c = card_cache.get(matched_threat.grp_id)
            matched_threat_name = c.name if c else matched_threat.name
            msg = msg.replace("{threat}", matched_threat_name)

        # Enrich meta_gameplan messages with specific card info
        if rule.opp_has_must_answer is True and opp_tracker and opp_tracker.identified_deck:
            opp_bf_names = {card_cache.get(o.grp_id).name
                            for o in opp_bf if card_cache.get(o.grp_id)}
            ma_card_name = None
            ma_reason = "remove now"
            for kt in opp_tracker.identified_deck.key_threats:
                if kt.get("must_answer") and kt.get("card") in opp_bf_names:
                    ma_card_name = kt["card"]
                    ma_reason = kt.get("reason", "remove now")
                    break
            if ma_card_name:
                # Check if player has removal that can actually answer it
                has_answer = False
                for h_obj in hand:
                    hc = card_cache.get(h_obj.grp_id)
                    if not hc or h_obj.is_land:
                        continue
                    hc_abs = " ".join(a.lower() for a in hc.abilities)
                    is_removal = any(w in hc_abs for w in
                                     ("exile", "destroy target", "deals", "return target"))
                    if is_removal and hc.cmc <= mana:
                        has_answer = True
                        break
                if has_answer:
                    msg = f"MUST ANSWER: {ma_card_name} — {ma_reason}"
                else:
                    msg = f"Threat: {ma_card_name} — {ma_reason} (need removal)"
                    prio = "high"  # downgrade from critical
        if rule.opp_has_vulnerability is True and vulnerabilities:
            opp_bf_names = {card_cache.get(o.grp_id).name
                            for o in opp_bf if card_cache.get(o.grp_id)}
            for v in vulnerabilities:
                if v.get("card") in opp_bf_names:
                    msg = f"WARNING: {v['card']} — {v.get('reason', 'counters your strategy')}"
                    break

        # --- Safety checks: suppress bad advice ---
        if not is_mulligan:
            action_lower = rule.action.lower()

            # If rule suggests casting a matched card, verify we have mana
            if matched_card:
                mc = card_cache.get(matched_card.grp_id)
                if mc and not matched_card.is_land:
                    in_hand = any(o.instance_id == matched_card.instance_id for o in hand)
                    if in_hand and mc.cmc > mana:
                        continue
                    in_bf = any(o.instance_id == matched_card.instance_id for o in my_bf)
                    if in_bf and matched_card.is_creature:
                        if "attack" in action_lower and not matched_card.can_attack:
                            continue

            # If rendered message names a specific card, verify it's in hand and castable.
            # This catches rules that match by keyword/type but mention a specific card.
            if not matched_card and matched_card_name:
                # Message names a card from {card} placeholder — check it's actually available
                found_in_hand = False
                for h_obj in hand:
                    hc = card_cache.get(h_obj.grp_id)
                    if hc and hc.name == matched_card_name:
                        found_in_hand = True
                        if hc.cmc > mana:
                            found_in_hand = False  # can't afford it
                        break
                if not found_in_hand:
                    continue  # suppress: card not in hand or not castable

            # If rule targets an opponent's creature, check for hexproof/indestructible
            if matched_threat and rule.layer in ("threat_response", "meta_gameplan"):
                tc = card_cache.get(matched_threat.grp_id)
                if tc:
                    prot = _has_protection(tc.abilities)
                    is_removal = any(w in action_lower for w in
                                     ["remove", "destroy", "exile", "kill", "bounce", "target"])
                    if is_removal:
                        if "hexproof" in prot or "shroud" in prot:
                            # Can't target — append warning instead of suppressing
                            msg += " (WARNING: has hexproof!)"
                        if "indestructible" in prot and "destroy" in action_lower:
                            msg += " (WARNING: indestructible — exile instead!)"
                        if "ward" in prot:
                            msg += " (has ward — extra mana needed)"

        # Effective priority — prio was set to rule.priority at the top of this
        # loop iteration and may have been downgraded by the must-answer branch.
        # Apply weight boost on top of whatever prio currently is.
        if rule.weight > 1.5:
            prio_order = ["low", "medium", "high", "critical"]
            idx = prio_order.index(prio) if prio in prio_order else 1
            prio = prio_order[min(idx + 1, 3)]

        # --- Build ActionScore ---
        # Mulligan rules don't produce game actions — skip ActionScore
        if rule.layer == "mulligan":
            action_score = None
        else:
            # Schema-first: use declared action_family when available, infer as fallback
            if rule.action_family:
                try:
                    family = ActionFamily(rule.action_family)
                except ValueError:
                    log.warning("Rule %s has invalid action_family=%r, falling back to inference",
                                rule.id, rule.action_family)
                    family = infer_action_family(msg, phase=ti.phase, rule_tags=rule.tags)
            else:
                family = infer_action_family(msg, phase=ti.phase, rule_tags=rule.tags)
            target = matched_card_name or matched_threat_name
            score = score_from_priority(prio, rule.weight)
            action_score = ActionScore(
                family=family,
                score=score,
                target=target,
                source="strategy",
                rule_id=rule.id,
                rule_layer=rule.layer,
                rule_weight=rule.weight,
            )

        hit = RuleHit(
            rule_id=rule.id,
            layer=rule.layer,
            weight=rule.weight,
            priority=prio,
            action_scores=[action_score] if action_score else [],
            matched_card=matched_card_name,
            matched_threat=matched_threat_name,
            raw_message=msg,
            tags=list(rule.tags),
        )
        results.append((rule, hit))
        fired_ids.add(rule.id)
        rule.times_fired += 1

    # --- Conflict resolution ---
    suppressed: set[str] = set()
    # Explicit conflicts — only for mulligan layer (legacy)
    for rule, _ in results:
        if rule.layer == "mulligan":
            for cid in rule.conflicts_with:
                suppressed.add(cid)

    # Auto-detect hold/use conflicts: if a "use" rule and a "hold" rule both
    # reference the same card, the more specific one (use) suppresses hold.
    hold_rules: dict[str, list[str]] = {}  # card_name → [rule_ids]
    use_rules: dict[str, list[str]] = {}   # card_name → [rule_ids]
    for rule, hit in results:
        # Use action_family from RuleHit (already resolved with fallback)
        family_value = hit.action_scores[0].family.value if hit.action_scores else ""
        is_hold = is_hold_rule(family_value, rule.action)
        for zc in rule.require:
            if zc.match.name:
                names = zc.match.name if isinstance(zc.match.name, list) else [zc.match.name]
                for n in names:
                    if is_hold:
                        hold_rules.setdefault(n, []).append(rule.id)
                    else:
                        use_rules.setdefault(n, []).append(rule.id)
    # When both hold and use fire for same card, suppress the lower-priority one.
    rule_by_id = {r.id: (r, h) for r, h in results}
    prio_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for card, hold_ids in hold_rules.items():
        if card not in use_rules:
            continue
        fired_use_ids = [uid for uid in use_rules[card] if uid in fired_ids]
        if not fired_use_ids:
            continue
        for hold_id in hold_ids:
            if hold_id not in fired_ids:
                continue
            hold_rule, hold_hit = rule_by_id[hold_id]
            hold_score = (hold_rule.layer_priority, -prio_rank.get(hold_hit.priority, 4), hold_rule.weight)
            for uid in fired_use_ids:
                use_rule, use_hit = rule_by_id[uid]
                use_score = (use_rule.layer_priority, -prio_rank.get(use_hit.priority, 4), use_rule.weight)
                if use_score >= hold_score:
                    suppressed.add(hold_id)
                else:
                    suppressed.add(uid)

    # Remove suppressed and low-weight rules, sort by layer → priority → weight
    DISPLAY_WEIGHT_THRESHOLD = 0.3
    prio_map = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    filtered = [(r, h) for r, h in results
                if r.id not in suppressed and r.weight >= DISPLAY_WEIGHT_THRESHOLD]
    filtered.sort(key=lambda x: (
        -x[0].layer_priority,
        prio_map.get(x[1].priority, 4),
        -x[0].weight,
    ))

    if max_results:
        filtered = filtered[:max_results]

    hits = [h for _, h in filtered]
    advice = render_advice(hits)
    return hits, advice


def evaluate_rules(rules: list[Rule], state: GameState,
                   opp_tracker: OpponentTracker | None = None,
                   vulnerabilities: list[dict] | None = None,
                   max_results: int = 5) -> list[Advice]:
    """Evaluate all rules against current game state (backward-compatible wrapper).

    max_results: maximum number of advice items to return (0 = unlimited).
    """
    _, advice = evaluate_rules_v2(rules, state, opp_tracker=opp_tracker,
                                  vulnerabilities=vulnerabilities,
                                  max_results=max_results)
    return advice


# ─── Opponent Card Tracking ─────────────────────────────────────

def update_opponent_tracking(tracker: OpponentTracker, state: GameState,
                             prev_opp_cards: set[int]) -> set[int]:
    """Track new cards the opponent plays. Returns updated set of seen instance_ids."""
    current_opp = set()
    for obj in state.opp_battlefield():
        current_opp.add(obj.instance_id)
        if obj.instance_id not in prev_opp_cards:
            card = card_cache.get(obj.grp_id)
            if card:
                tracker.observe(card.name, card.colors)

    # Also track opponent's graveyard (reveals cards even if removed)
    for obj in state.opp_graveyard():
        if obj.instance_id not in prev_opp_cards:
            card = card_cache.get(obj.grp_id)
            if card:
                tracker.observe(card.name, card.colors)
            current_opp.add(obj.instance_id)

    return current_opp


# ─── Deck Analysis & Auto-Generation ───────────────────────────

def detect_deck(state: GameState) -> dict:
    """Analyze deck composition from grp_ids."""
    deck_ids = state.my_deck
    if not deck_ids:
        return {}

    colors: set[str] = set()
    card_names: list[str] = []
    type_counts = {"Creature": 0, "Instant": 0, "Sorcery": 0,
                   "Enchantment": 0, "Artifact": 0, "Planeswalker": 0, "Land": 0}
    keywords: dict[str, int] = {}
    cmc_sum = 0
    nonland_count = 0

    for grp_id in deck_ids:
        card = card_cache.get(grp_id)
        if not card:
            continue
        card_names.append(card.name)
        colors.update(card.colors)
        for ct in card.card_types:
            if ct in type_counts:
                type_counts[ct] += 1
        if "Land" not in card.card_types:
            cmc_sum += card.cmc
            nonland_count += 1
        for ab in card.abilities:
            keywords[ab] = keywords.get(ab, 0) + 1

    avg_cmc = cmc_sum / nonland_count if nonland_count else 0
    creature_ratio = type_counts["Creature"] / max(nonland_count, 1)

    if avg_cmc <= 2.5 and creature_ratio > 0.5:
        archetype = "aggro"
    elif avg_cmc >= 3.5 or type_counts["Creature"] < 10:
        archetype = "control"
    elif creature_ratio > 0.35:
        archetype = "midrange"
    else:
        archetype = "combo"

    # Signature cards (3-4 copies, non-land)
    name_counts: dict[str, int] = {}
    for n in card_names:
        name_counts[n] = name_counts.get(n, 0) + 1
    sig_cards = []
    for name, count in name_counts.items():
        if count < 3:
            continue
        for _, c in card_cache._cache.items():
            if c.name == name and "Land" not in c.card_types:
                sig_cards.append(name)
                break

    return {
        "colors": sorted(colors),
        "card_names": card_names,
        "name_counts": name_counts,
        "type_counts": type_counts,
        "keywords": keywords,
        "avg_cmc": round(avg_cmc, 2),
        "archetype": archetype,
        "sig_cards": sig_cards[:5],
    }


def generate_rules(archetype: str, info: dict) -> list[Rule]:
    """Auto-generate rules based on deck analysis."""
    rules: list[Rule] = []
    sig_cards = info.get("sig_cards", [])
    kw = info.get("keywords", {})

    # ── Layer 0: General ──
    rules.extend([
        Rule(id="gen_play_land", layer="general",
             phase=["Main"], my_turn=True,
             require=[ZoneCondition("hand", CardMatcher(card_type="Land"))],
             action="Play a land", action_family="play_land", priority="low", weight=0.5),
        Rule(id="gen_attack_before_main2", layer="general", tags=["tempo"],
             phase=["Main"], my_turn=True, step="Phase_Main1",
             action="Attack before casting in Main 2 — don't reveal info",
             action_family="attack", priority="low", weight=0.6),
    ])

    # ── Layer 1: Archetype ──
    if archetype == "aggro":
        rules.extend([
            Rule(id="arch_curve_out", layer="archetype", tags=["tempo"],
                 phase=["Main"], my_turn=True, turn_max=4,
                 require=[ZoneCondition("hand", CardMatcher(card_type="Creature", castable=True))],
                 action="Cast {card} — curve out early", action_family="cast_spell",
                 priority="high", weight=1.2),
            Rule(id="arch_go_wide", layer="archetype", tags=["aggro"],
                 phase=["Combat"], my_turn=True, my_creatures_min=3,
                 action="Attack with everything — go wide", action_family="attack",
                 priority="medium"),
            Rule(id="arch_push_lethal", layer="archetype",
                 phase=["Main"], my_turn=True, opp_life_below=8,
                 action="Opponent is low — push for lethal", action_family="attack",
                 priority="high", weight=1.3),
        ])
    elif archetype == "midrange":
        rules.extend([
            Rule(id="arch_biggest_threat", layer="archetype",
                 phase=["Main"], my_turn=True,
                 require=[ZoneCondition("hand", CardMatcher(card_type="Creature", castable=True, cmc_min=3))],
                 action="Cast {card} — biggest threat", action_family="cast_spell",
                 priority="medium"),
            Rule(id="arch_trade_up", layer="archetype", tags=["defensive"],
                 phase=["Combat"], my_turn=False,
                 require=[ZoneCondition("opp_battlefield", CardMatcher(card_type="Creature"))],
                 my_creatures_min=1,
                 action="Look for favorable blocks — trade up", action_family="block",
                 priority="medium"),
        ])
    elif archetype == "control":
        rules.extend([
            Rule(id="arch_hold_mana", layer="archetype", tags=["reactive"],
                 phase=["Main"], my_turn=True, mana_min=2,
                 require=[ZoneCondition("hand", CardMatcher(card_type="Instant"))],
                 action="Hold mana for {card} — don't tap out", action_family="pass",
                 priority="high", weight=1.2),
            Rule(id="arch_boardwipe", layer="archetype",
                 phase=["Main"], my_turn=True, opp_creatures_min=3,
                 require=[ZoneCondition("hand", CardMatcher(card_type="Sorcery", castable=True))],
                 action="Board wipe time — cast {card}", action_family="cast_spell",
                 priority="high", weight=1.3),
            Rule(id="arch_dont_overextend", layer="archetype",
                 phase=["Main"], my_turn=True, my_creatures_min=2,
                 action="Don't overextend — opponent may have a wipe", action_family="pass",
                 priority="low"),
        ])

    # ── Layer 2: Mulligan ──
    rules.extend([
        Rule(id="mull_no_lands", layer="mulligan",
             phase=["Mulligan"], hand_lands_max=0, hand_size_min=6,
             action="Mulligan — no lands", priority="critical", weight=1.5),
        Rule(id="mull_one_land", layer="mulligan",
             phase=["Mulligan"], hand_lands_max=1, hand_size_min=6,
             action="Mulligan — only 1 land", priority="high", weight=1.3),
        Rule(id="mull_flooded", layer="mulligan",
             phase=["Mulligan"], hand_lands_min=5, hand_size_min=6,
             action="Mulligan — too many lands", priority="high", weight=1.2),
        Rule(id="mull_keep_small", layer="mulligan",
             phase=["Mulligan"], hand_size_max=5, hand_lands_min=1,
             action="Keep — can't afford another mulligan", priority="medium"),
    ])

    if archetype == "aggro":
        rules.extend([
            Rule(id="mull_aggro_good", layer="mulligan",
                 phase=["Mulligan"], hand_lands_min=2, hand_lands_max=3,
                 hand_castable_min=1, hand_size_min=6,
                 action="Keep — good aggro hand with early plays", priority="high", weight=1.3),
            Rule(id="mull_aggro_no_plays", layer="mulligan",
                 phase=["Mulligan"], hand_lands_min=2, hand_lands_max=4,
                 hand_castable_max=0, hand_size_min=6,
                 action="Mulligan — lands OK but nothing to cast early", priority="high", weight=1.1),
        ])
    elif archetype == "control":
        rules.append(
            Rule(id="mull_ctrl_good", layer="mulligan",
                 phase=["Mulligan"], hand_lands_min=3, hand_lands_max=4, hand_size_min=6,
                 action="Keep — solid control hand", priority="high", weight=1.2))
    elif archetype == "midrange":
        rules.append(
            Rule(id="mull_mid_good", layer="mulligan",
                 phase=["Mulligan"], hand_lands_min=2, hand_lands_max=4,
                 hand_castable_min=1, hand_size_min=6,
                 action="Keep — good curve", priority="high", weight=1.2))

    if sig_cards:
        rules.append(
            Rule(id="mull_key_card", layer="mulligan",
                 phase=["Mulligan"], hand_lands_min=2, hand_size_min=6,
                 require=[ZoneCondition("hand", CardMatcher(name=sig_cards[:3]))],
                 action="Keep — has {card} (key card)", priority="high", weight=1.2))

    # ── Layer 3: Card Synergies ──
    # Detect common synergy patterns from deck contents
    card_names_set = set(info.get("card_names", []))

    # Lifelink + Pridemate engine
    if "Ajani's Pridemate" in card_names_set:
        lifelinkers = [n for n in card_names_set
                       if any(card_cache.get(gid) and "Lifelink" in card_cache.get(gid).abilities
                              for gid, c in card_cache._cache.items() if c.name == n)]
        if lifelinkers:
            rules.extend([
                Rule(id="syn_pridemate_first", layer="card_synergy", tags=["sequence"],
                     phase=["Main"], my_turn=True,
                     require=[
                         ZoneCondition("hand", CardMatcher(name="Ajani's Pridemate"), min_count=1),
                         ZoneCondition("hand", CardMatcher(keyword="Lifelink"), min_count=1),
                         ZoneCondition("battlefield", CardMatcher(name="Ajani's Pridemate"), absent=True),
                     ],
                     action="Cast Ajani's Pridemate FIRST — lifelink triggers will grow it",
                     priority="high", weight=1.3),
                Rule(id="syn_pridemate_protect", layer="card_synergy", tags=["protect"],
                     phase=["Main", "Combat"], my_turn=True,
                     require=[ZoneCondition("battlefield", CardMatcher(name="Ajani's Pridemate"))],
                     action="Protect Pridemate — don't attack into removal mana",
                     priority="medium", weight=1.0),
            ])

    # Flying theme
    if kw.get("Flying", 0) >= 4:
        rules.append(
            Rule(id="syn_flyers_pressure", layer="card_synergy",
                 phase=["Main"], my_turn=True,
                 require=[ZoneCondition("hand", CardMatcher(keyword="Flying", castable=True))],
                 action="Cast {card} — build air pressure", priority="high", weight=1.1))

    # Lifelink value
    if kw.get("Lifelink", 0) >= 3:
        rules.append(
            Rule(id="syn_lifelink_stabilize", layer="card_synergy",
                 my_turn=True, life_below=10,
                 require=[ZoneCondition("hand", CardMatcher(keyword="Lifelink", castable=True))],
                 action="Cast {card} — lifelink to stabilize", priority="high", weight=1.2))

    # Flash value
    if kw.get("Flash", 0) >= 2:
        rules.append(
            Rule(id="syn_flash_hold", layer="card_synergy", tags=["reactive"],
                 phase=["Main"], my_turn=True,
                 require=[ZoneCondition("hand", CardMatcher(keyword="Flash"))],
                 action="Hold {card} — cast on opponent's turn for tempo",
                 priority="medium"))

    # Lords / anthem effects
    for name in card_names_set:
        for gid, c in card_cache._cache.items():
            if c.name == name and c.oracle_text and "other" in c.oracle_text.lower() and "+1/+1" in c.oracle_text:
                rules.append(
                    Rule(id=f"syn_lord_{name[:10].lower().replace(' ','_')}", layer="card_synergy",
                         tags=["sequence"],
                         phase=["Main"], my_turn=True,
                         require=[
                             ZoneCondition("hand", CardMatcher(name=name, castable=True)),
                             ZoneCondition("battlefield", CardMatcher(card_type="Creature"), min_count=2),
                         ],
                         action=f"Cast {name} — buffs your team", priority="high", weight=1.2))
                break

    # ── Layer 4: Threat Response ──
    # These fire when specific opponent threats are on the battlefield
    rules.extend([
        Rule(id="threat_big_creature", layer="threat_response",
             require=[
                 ZoneCondition("opp_battlefield", CardMatcher(card_type="Creature", power_min=4)),
                 ZoneCondition("hand", CardMatcher(castable=True)),
             ],
             action="Remove {threat} — big threat on board", priority="high"),
        Rule(id="threat_growing", layer="threat_response",
             require=[ZoneCondition("opp_battlefield",
                                     CardMatcher(card_type="Creature", power_min=5))],
             action="REMOVE {threat} immediately — it's taking over", priority="critical", weight=1.3),
    ])

    # ── Layer 5: Situation ──
    rules.extend([
        Rule(id="sit_racing", layer="situation",
             life_below=8, opp_life_below=8,
             action="Race — both low, count damage clocks", priority="high"),
        Rule(id="sit_behind", layer="situation",
             my_turn=False, my_creatures_min=0, opp_creatures_min=4,
             action="Behind on board — play defensive, look for a wipe or removal",
             priority="high"),
        Rule(id="sit_empty_hand", layer="situation",
             phase=["Main"], my_turn=True, hand_size_max=1,
             action="Topdeck mode — play whatever you draw", priority="low"),
    ])

    return rules


# ─── Strategy Generation & Persistence ──────────────────────────

def generate_strategy(state: GameState) -> Strategy:
    """Auto-generate a strategy based on deck analysis."""
    info = detect_deck(state)
    if not info:
        return Strategy(name="Unknown")

    archetype = info["archetype"]
    sig_cards = info["sig_cards"]
    color_str = "".join(info["colors"])

    name = f"{sig_cards[0]} {archetype.title()}" if sig_cards else f"{color_str} {archetype.title()}"
    rules = generate_rules(archetype, info)

    return Strategy(
        name=name,
        deck_signature=sig_cards,
        colors=info["colors"],
        archetype=archetype,
        rules=rules,
    )


def _strategy_path(name: str) -> Path:
    safe_name = name.replace(" ", "_").replace("/", "_").replace("'", "").lower()
    # Check deck dirs first (new format)
    deck_path = DECKS_ROOT / safe_name / "strategy.json"
    if deck_path.exists():
        return deck_path
    # Then built-in strategies
    builtin_path = RULES_DIR / f"{safe_name}.json"
    if builtin_path.exists():
        return builtin_path
    # New strategies go to deck dir
    return deck_path


def _rule_to_dict(r: Rule) -> dict:
    d: dict = {"id": r.id, "layer": r.layer}
    if r.tags:
        d["tags"] = r.tags
    if r.phase:
        d["phase"] = r.phase
    if r.my_turn is not None:
        d["my_turn"] = r.my_turn
    if r.turn_min is not None:
        d["turn_min"] = r.turn_min
    if r.turn_max is not None:
        d["turn_max"] = r.turn_max
    if r.step:
        d["step"] = r.step
    if r.require:
        d["require"] = [
            {"zone": zc.zone,
             "match": {k: v for k, v in {
                 "name": zc.match.name, "keyword": zc.match.keyword,
                 "type": zc.match.card_type, "cmc_min": zc.match.cmc_min,
                 "cmc_max": zc.match.cmc_max, "power_min": zc.match.power_min,
                 "toughness_min": zc.match.toughness_min,
                 "toughness_max": zc.match.toughness_max,
                 "castable": zc.match.castable or None,
                 "color": zc.match.color,
             }.items() if v is not None and v is not False},
             **({"min_count": zc.min_count} if zc.min_count != 1 else {}),
             **({"max_count": zc.max_count} if zc.max_count is not None else {}),
             **({"absent": True} if zc.absent else {}),
             **({"tapped": zc.tapped} if zc.tapped is not None else {}),
             }
            for zc in r.require
        ]
    for attr in ["life_below", "life_above", "opp_life_below", "opp_life_above", "mana_min",
                 "hand_lands_min", "hand_lands_max", "hand_size_min", "hand_size_max",
                 "hand_castable_min", "hand_castable_max", "my_creatures_min", "opp_creatures_min",
                 "opp_speed", "opp_has_must_answer", "opp_has_vulnerability"]:
        v = getattr(r, attr)
        if v is not None:
            d[attr] = v
    d["action"] = r.action
    if r.action_family:
        d["action_family"] = r.action_family
    d["priority"] = r.priority
    if r.conflicts_with:
        d["conflicts_with"] = r.conflicts_with
    d["weight"] = r.weight
    d["stats"] = {"fired": r.times_fired, "correct": r.times_correct}
    d["metrics"] = r.metrics
    if r.source:
        d["_source"] = r.source
    return d


def _rule_from_dict(d: dict) -> Rule:
    require = []
    for zc_d in d.get("require", []):
        require.append(ZoneCondition(
            zone=zc_d.get("zone", "hand"),
            match=CardMatcher.from_dict(zc_d.get("match", {})),
            min_count=zc_d.get("min_count", 1),
            max_count=zc_d.get("max_count"),
            absent=zc_d.get("absent", False),
            tapped=zc_d.get("tapped"),
        ))
    stats = d.get("stats", {})
    return Rule(
        id=d["id"],
        layer=d.get("layer", "general"),
        tags=d.get("tags", []),
        phase=d.get("phase"),
        my_turn=d.get("my_turn"),
        turn_min=d.get("turn_min"),
        turn_max=d.get("turn_max"),
        step=d.get("step"),
        require=require,
        life_below=d.get("life_below"),
        life_above=d.get("life_above"),
        opp_life_below=d.get("opp_life_below"),
        opp_life_above=d.get("opp_life_above"),
        mana_min=d.get("mana_min"),
        hand_lands_min=d.get("hand_lands_min"),
        hand_lands_max=d.get("hand_lands_max"),
        hand_size_min=d.get("hand_size_min"),
        hand_size_max=d.get("hand_size_max"),
        hand_castable_min=d.get("hand_castable_min"),
        hand_castable_max=d.get("hand_castable_max"),
        my_creatures_min=d.get("my_creatures_min"),
        opp_creatures_min=d.get("opp_creatures_min"),
        opp_speed=d.get("opp_speed"),
        opp_has_must_answer=d.get("opp_has_must_answer"),
        opp_has_vulnerability=d.get("opp_has_vulnerability"),
        action=d.get("action", ""),
        action_family=d.get("action_family"),
        priority=d.get("priority", "medium"),
        conflicts_with=d.get("conflicts_with", []),
        weight=d.get("weight", 1.0),
        times_fired=stats.get("fired", 0),
        times_correct=stats.get("correct", 0),
        metrics=d.get("metrics", {}),
        source=d.get("_source", ""),
    )


def save_strategy(strategy: Strategy):
    """Save strategy to deck dir (decks/{deck_id}/strategy.json)."""
    # Don't save merged general rules — only deck-specific ones
    deck_rules = [r for r in strategy.rules if r.id not in
                  {gr.id for gr in _load_general_rules()}]
    data = {
        "name": strategy.name,
        "deck_signature": strategy.deck_signature,
        "colors": strategy.colors,
        "archetype": strategy.archetype,
        "rules": [_rule_to_dict(r) for r in deck_rules],
        "general_overrides": strategy.general_overrides,
        "vulnerabilities": strategy.vulnerabilities,
        "stats": strategy.stats,
    }
    if strategy.global_biases:
        data["global_biases"] = strategy.global_biases
    data["_engine_version"] = ENGINE_VERSION
    data["_schema_version"] = SCHEMA_VERSION
    path = _strategy_path(strategy.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    log.info("Strategy saved: %s → %s", strategy.name, path)


def load_strategy(name: str) -> Strategy | None:
    path = _strategy_path(name)
    if not path.exists():
        return None
    return _load_strategy_file(path)


def load_raw_strategy(name: str) -> dict | None:
    """Load raw JSON dict for a strategy file (no parsing into Strategy objects)."""
    path = _strategy_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _load_strategy_file(path: Path, *, use_cache: bool = True) -> Strategy | None:
    # Hot-reload: return cached if mtime unchanged
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0

    if use_cache:
        cached = _strategy_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]

    try:
        data = json.loads(path.read_text())
        saved_version = data.get("_engine_version", "")
        if saved_version and saved_version != ENGINE_VERSION:
            log.warning("Strategy %s was saved with engine %s (current: %s) — re-optimize recommended",
                        path.stem, saved_version, ENGINE_VERSION)
        saved_schema = data.get("_schema_version", "")
        if saved_schema and saved_schema != SCHEMA_VERSION:
            log.warning("Strategy %s uses schema %s (current: %s)", path.stem, saved_schema, SCHEMA_VERSION)
        rules = [_rule_from_dict(r) for r in data.get("rules", [])]
        strat = Strategy(
            name=data["name"],
            deck_signature=data.get("deck_signature", []),
            colors=data.get("colors", []),
            archetype=data.get("archetype", "unknown"),
            rules=rules,
            general_overrides=data.get("general_overrides", []),
            vulnerabilities=data.get("vulnerabilities", []),
            stats=data.get("stats", {"games": 0, "wins": 0, "losses": 0}),
            global_biases=data.get("global_biases", {}),
        )
        # Cache with mtime captured at start (single stat, no race)
        if use_cache:
            _strategy_cache[str(path)] = (mtime, strat)
        return strat
    except Exception as e:
        log.error("Failed to load strategy %s: %s", path, e)
        return None


def _all_strategy_paths() -> list[Path]:
    """Return all strategy file paths to search.
    User deck strategies (decks/*/strategy.json) first, then built-in."""
    paths = []
    # User deck strategies
    if DECKS_ROOT.exists():
        for deck_dir in DECKS_ROOT.iterdir():
            if deck_dir.is_dir():
                strat = deck_dir / "strategy.json"
                if strat.exists():
                    paths.append(strat)
    # Built-in strategies
    if RULES_DIR.exists():
        for p in RULES_DIR.glob("*.json"):
            if p.name not in ("meta_decks.json", "general.json"):
                paths.append(p)
    return paths


def invalidate_strategy_cache() -> int:
    """Clear the strategy mtime cache. Returns number of entries cleared."""
    n = len(_strategy_cache)
    _strategy_cache.clear()
    if n:
        log.info("Invalidated strategy cache (%d entries)", n)
    return n


def find_matching_strategy(state: GameState) -> Strategy | None:
    deck_ids = state.my_deck
    if not deck_ids:
        return None
    deck_names = {card_cache.get(gid).name for gid in deck_ids
                  if card_cache.get(gid)} - {""}

    best_match = None
    best_score = 0.0
    best_is_managed = False

    for path in _all_strategy_paths():
        try:
            data = json.loads(path.read_text())
            sig = data.get("deck_signature", [])
            if not sig:
                continue
            matched = [s for s in sig if s in deck_names]
            missing = [s for s in sig if s not in deck_names]
            score = len(matched) / len(sig)
            # Managed decks (with deck.json) get priority over stubs at same score
            is_managed = (path.parent / "deck.json").exists()
            log.info("Strategy match: %s — %.0f%% (%d/%d) managed=%s matched=%s missing=%s",
                     path.parent.name if path.name == "strategy.json" else path.stem,
                     score * 100, len(matched), len(sig), is_managed, matched, missing)
            if score >= 0.5 and (
                score > best_score
                or (score == best_score and is_managed and not best_is_managed)
            ):
                best_score = score
                best_match = path
                best_is_managed = is_managed
        except Exception:
            continue

    if best_match:
        strat = _load_strategy_file(best_match)
        if strat:
            log.info("Selected strategy: %s (%.0f%%)", strat.name, best_score * 100)
            return strat

    log.info("No strategy matched (best was %.0f%%). Deck cards: %s",
             best_score * 100, sorted(deck_names)[:20])
    return None


def _load_general_rules() -> list[Rule]:
    """Load general.json rules for merging into deck-specific strategies."""
    path = RULES_DIR / "general.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return [_rule_from_dict(r) for r in data.get("rules", [])]
    except Exception as e:
        log.error("Failed to load general rules: %s", e)
        return []


def _merge_general_rules(strategy: Strategy) -> Strategy:
    """Merge general.json rules into a deck-specific strategy.

    General rules are added unless:
    - The deck strategy has a rule with the same ID, OR
    - The general rule ID is listed in the deck's general_overrides
    """
    general_rules = _load_general_rules()
    if not general_rules:
        return strategy

    deck_ids = {r.id for r in strategy.rules}
    overrides = set(strategy.general_overrides)
    excluded = deck_ids | overrides
    added = []
    skipped = []
    for rule in general_rules:
        if rule.id in excluded:
            skipped.append(rule.id)
        else:
            added.append(rule)

    if added:
        strategy.rules.extend(added)
        log.info("Merged %d general rules into %s (skipped %d overrides, total: %d)",
                 len(added), strategy.name, len(skipped), len(strategy.rules))
    return strategy


def _load_fallback() -> Strategy | None:
    """Load general.json as fallback strategy."""
    path = RULES_DIR / "general.json"
    if path.exists():
        strat = _load_strategy_file(path)
        if strat:
            log.info("Using fallback strategy: %s (%d rules)",
                     strat.name, len(strat.rules))
            return strat
    return None


def get_or_create_strategy(state: GameState) -> Strategy | None:
    strategy = find_matching_strategy(state)
    if strategy:
        return _merge_general_rules(strategy)
    if not state.my_deck:
        return _load_fallback()
    strategy = generate_strategy(state)
    if strategy.name == "Unknown":
        return _load_fallback()
    save_strategy(strategy)
    log.info("Generated new strategy: %s (%s, %d rules)",
             strategy.name, strategy.archetype, len(strategy.rules))
    return _merge_general_rules(strategy)


# ─── Learning ───────────────────────────────────────────────────

def learn_from_match(strategy: Strategy, won: bool):
    """Adjust rule weights based on match outcome."""
    strategy.stats["games"] += 1
    if won:
        strategy.stats["wins"] += 1
    else:
        strategy.stats["losses"] += 1

    for rule in strategy.rules:
        if rule.times_fired == 0:
            continue
        if won:
            rule.weight = min(MAX_WEIGHT, rule.weight + WIN_BOOST)
            rule.times_correct += 1
        else:
            rule.weight = max(MIN_WEIGHT, rule.weight - LOSS_PENALTY)

    save_strategy(strategy)
    log.info("Strategy learning: %s — %s (W/L: %d/%d, rate: %.0f%%)",
             strategy.name, "WIN" if won else "LOSS",
             strategy.stats["wins"], strategy.stats["losses"],
             strategy.win_rate() * 100)
