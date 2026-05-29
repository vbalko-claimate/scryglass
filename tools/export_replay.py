#!/usr/bin/env python3
"""Export one match from advisor.db as a Glass Shard ReplayRecord JSON.

The output schema mirrors `glass_engine::replay::ReplayRecord` and is
consumed by `glass-engine/examples/replay_match.rs`. The exporter
groups `match_events` rows by `(game_number, turn_number)`, derives
per-turn plays/attacks/blocks/activations + an end-of-turn
Checkpoint, and emits one JSON file per game.

Usage:
    uv run python tools/export_replay.py <match_id> [--out DIR]
    uv run python tools/export_replay.py --latest-deck "Mono-Green Landfall"

The `--latest-deck` shortcut picks the most recently started match
whose `my_deck_name` (case-insensitive substring) matches the
argument; useful for "export the game I just finished."

Schema versions:
    1 — initial. `plays` was `list[str]`. `activations` had only
        `{source, target}`. No top-level `counter_events`.
    2 — adds three fields so the Glass Shard replay-v2 consumer can
        disambiguate ambiguous MTGA log shapes:
          • each entry in `me.plays` / `opp.plays` is now
            `{name, cause_object_id}` (was bare string). When
            `cause_object_id` is non-null the card was put onto the
            battlefield by another effect (search-fetched basic,
            Cascade reveal, Fabled Passage sac trigger, …) rather
            than manually cast/played from hand. The Rust consumer
            keeps a back-compat untagged deserializer that still
            accepts the v1 bare-string shape.
          • `counter_events: list[CounterEvent]` per turn. Each
            element is `{turn_idx_in_turn, target_iid, counter_type,
            amount}` and is emitted in MTGA `AnnotationType_CounterAdded`
            order so downstream code can project per-permanent
            counter trajectories (previously every
            `checkpoint.my_battlefield[].plus_counters` was zero).
          • each entry in `me.activations` / `opp.activations` now
            carries `kind`: `"triggered"`, `"activated"`,
            `"spell_cast"`, or `null` (ambiguous). Discriminated by
            the presence/absence of a nearby
            `AnnotationType_UserActionTaken` whose `affected_ids`
            references the ability's iid.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Add project root to sys.path so `from advisor.database ...` works
sys.path.insert(0, str(Path(__file__).parent.parent))

from advisor.database import DB_PATH, card_cache  # noqa: E402

# The PyInstaller-bundled scryglass.app uses SCRY_USER_DATA →
# ~/MTG/mtg-data/app_data/advisor.db. When this script is run from
# the source repo, the advisor.database default is the same; if
# SCRY_USER_DATA is unset we still want to read the live DB the
# bundled app is writing to.
import os  # noqa: E402

if not os.environ.get("SCRY_USER_DATA") and not DB_PATH.exists():
    fallback = Path.home() / "MTG" / "mtg-data" / "app_data" / "advisor.db"
    if fallback.exists():
        DB_PATH = fallback

SCHEMA_VERSION = 2


# Subset of MTGA's CounterType enum we currently translate to named
# strings. Unknown values fall through to `f"Counter({n})"` so the
# raw enum is still recoverable on the consumer side without needing
# an exhaustive table here. Extend as more counter types are
# observed in the corpus.
COUNTER_TYPE_NAMES: dict[int, str] = {
    1: "PlusOnePlusOne",
    2: "MinusOneMinusOne",
    7: "Loyalty",
}


def _counter_type_name(raw: int | None) -> str:
    """Translate an MTGA CounterType int to a stable string name.

    Returns `f"Counter({raw})"` for values we haven't catalogued so
    the consumer can still distinguish them downstream without a
    silent enum collision."""
    if raw is None:
        return "Unknown"
    return COUNTER_TYPE_NAMES.get(int(raw), f"Counter({raw})")


def load_match(conn: sqlite3.Connection, match_id: str) -> dict | None:
    """Return the matches-table row as a dict or None."""
    row = conn.execute(
        "SELECT match_id, started_at, opponent_name, my_deck_name, "
        "opp_deck_name, result, game_count "
        "FROM matches WHERE match_id = ?",
        (match_id,),
    ).fetchone()
    if not row:
        return None
    keys = [
        "match_id", "started_at", "opponent_name", "my_deck_name",
        "opp_deck_name", "result", "game_count",
    ]
    return dict(zip(keys, row))


def load_events(conn: sqlite3.Connection, match_id: str) -> list[dict]:
    """Return ordered events for a match, parsed JSON `data`."""
    rows = conn.execute(
        "SELECT id, game_number, turn_number, phase, event_type, data "
        "FROM match_events WHERE match_id = ? ORDER BY id",
        (match_id,),
    ).fetchall()
    events = []
    for r in rows:
        try:
            data = json.loads(r[5]) if r[5] else {}
        except json.JSONDecodeError:
            data = {"raw": r[5]}
        events.append({
            "id": r[0],
            "game_number": r[1],
            "turn_number": r[2],
            "phase": r[3],
            "event_type": r[4],
            "data": data,
        })
    return events


def latest_match_for_deck(conn: sqlite3.Connection, deck_substring: str) -> str | None:
    row = conn.execute(
        "SELECT match_id FROM matches "
        "WHERE LOWER(my_deck_name) LIKE ? "
        "ORDER BY started_at DESC LIMIT 1",
        (f"%{deck_substring.lower()}%",),
    ).fetchone()
    return row[0] if row else None


def make_side_play() -> dict:
    return {
        "plays": [],
        "attacks": [],
        "blocks": [],
        "activations": [],
        "spell_targets": {},
        # Sprint 1 — per-cast / per-activation user choices joined
        # from MTGA annotation events. See `_attach_user_choices`.
        "spell_x": {},          # cast spell name → X value paid
        "spell_modes": {},      # modal cast spell name → 0-based mode
        "activation_x": [],     # parallel to `activations`, X per entry
        # Sprint 2 — life paid per turn for life-cost mana abilities
        # ("{T}, Pay N life: Add ...", e.g. Starting Town). Counted
        # from ManaPaid annotations where the affector is a known
        # life-cost-mana land and the color enum is 1-5 (= a colored
        # mana, only producible via the pay-life side of that source).
        "life_paid_for_mana": 0,
        # Sprint 4 — per-spell life-cost choice. ScriptedPlayer reads
        # this and forces choice index 1 (PayLife) for matching spell
        # names; otherwise picks index 0 (the cheap branch). Empty
        # when no Choice spells were cast this turn.
        "spell_paid_life": {},
        # Land names that entered tapped this turn (from MTGA's
        # `isTapped` flag at ETB time). Lets the engine know whether
        # the player paid life for a shock land or accepted the
        # tapped enter.
        "lands_entered_tapped": [],
        # Creature/permanent names that LEFT this side's battlefield
        # this turn (died, bounced, exiled, sacrificed) — derived from
        # `creature_left_bf` events. The replay harness applies these
        # as recorded departures so opponent permanents the engine
        # injected but can't see removed (opp removal / sac / combat
        # death we don't simulate) are taken off the board BEFORE the
        # end-of-turn checkpoint diff, instead of lingering as phantoms
        # until the post-turn prune.
        "creatures_left": [],
        # Net life change for THIS side during the turn, summed from
        # `life_change` events. The replay harness applies the OPP
        # side's value only on the opponent's OWN turn — where the
        # engine never simulates opp life changes — so opponent
        # self-inflicted losses (fetch / pay-life / Dark Confidant)
        # and lifegain land on the engine naturally instead of via the
        # post-turn opp_life sync. Our-turn combat damage is left to
        # the engine (not double-counted).
        "life_delta": 0,
    }


# MTGA ManaPaid color enum (empirically decoded from corpus annotations):
#   1 = White, 2 = Blue, 3 = Black, 4 = Red, 5 = Green, 12 = Colorless
# Higher values (6-11, 13+) appear on hybrid/snow/other sources we
# don't need to disambiguate for life-payment detection.
MANA_PAID_COLOR_W = 1
MANA_PAID_COLOR_U = 2
MANA_PAID_COLOR_B = 3
MANA_PAID_COLOR_R = 4
MANA_PAID_COLOR_G = 5
MANA_PAID_COLOR_C = 12

# Mana sources whose ONLY way to produce a coloured ({W}/{U}/{B}/{R}/{G})
# tap is through their "{T}, Pay N life: Add one mana of any color"
# ability — the free side adds {C}. When such a source emits a
# ManaPaid annotation with color 1-5 (not 12), the player must have
# paid life to activate it. The life cost per use is encoded
# alongside (1 for Starting Town; future cards with different costs
# can override the default 1 below). This is the empirical wiring
# decoded from c93ef724's 14-turn life-drift gap.
LIFE_COST_MANA_LANDS: dict[str, int] = {
    "Starting Town": 1,
}


# MTGA ManaPaid color enum values that represent a life payment as
# the "color" of mana produced. Empirically 5 maps to the
# Starting-Town-style "{T}, Pay N life: Add one mana of any color"
# ability (the player chooses the color at activation time, and the
# log records the choice via the affector / affected pair while the
# `color: 5` flag signals "life was paid"). Generic colorless is a
# DIFFERENT bucket. Used by `_attach_user_choices` to count life
# paid per spell cast.
MANA_PAID_LIFE_COLOR = 5


def _attach_life_choice(
    side: dict, name: str, iid: int | None, life_change_by_source: dict
) -> None:
    """Detect "pay X life" additional-cost choice for the cast.
    Scoped to known cards with `Choice(DiscardCard, PayLife(N))` (or
    similar) additional costs — the broader "any negative ModifiedLife
    on cast iid" filter was too noisy, conflating combat damage
    attributed to attacking creatures, lifelink lifegain on cast
    sources, etc. When MTGA logs a ModifiedLife with the cast's iid
    and a negative delta inside the expected cost range, we emit
    `spell_paid_life[name] = abs(delta)`; ScriptedPlayer forces the
    PayLife branch for that spell.

    First-cast-wins by spell name (matches engine's HashMap shape).
    """
    if iid is None:
        return
    if name not in ADDITIONAL_COST_LIFE_CARDS:
        return
    delta = life_change_by_source.get(iid, 0)
    expected_cost = ADDITIONAL_COST_LIFE_CARDS[name]
    # Tight window — only treat a delta of exactly -expected_cost as
    # PayLife. Anything else is downstream damage / lifegain.
    if delta == -expected_cost:
        side["spell_paid_life"].setdefault(name, expected_cost)


# Cards whose mana cost includes a `Choice(DiscardCard, PayLife(N))`
# additional cost. Empirically curated from the Standard corpus —
# the value is the PayLife cost in life points. Extend when wiring
# more cards.
ADDITIONAL_COST_LIFE_CARDS: dict[str, int] = {
    "Bitter Triumph": 3,
}


def _classify_ability_kind(
    ability_iid: int | None, activated_iids: set[int]
) -> str | None:
    """Schema v2 — return `"triggered"`, `"activated"`, or `None`
    (ambiguous) for a stack `ability` event.

    Discrimination is by membership in `activated_iids`, which was
    populated from `AnnotationType_UserActionTaken` annotations whose
    `affected_ids` referenced the ability's iid. Triggered abilities
    fire from event resolution (no UserActionTaken) so their iids
    never enter the set. When the ability's iid is missing entirely
    (legacy events without instance_id) we fall back to `None` so the
    consumer keeps treating it as ambiguous.

    `"spell_cast"` is reserved for a future migration where spell
    casts share the `activations` list; today they flow through
    `spell_cast` events on the side struct and are not routed here.
    """
    if ability_iid is None:
        return None
    return "activated" if ability_iid in activated_iids else "triggered"


def _attach_spell_target(
    side: dict, name: str, iid: int | None, targets_by_iid: dict
) -> None:
    """Set `spell_targets[name]` from the global TargetSpec map if a
    target was recorded for this spell's instance_id. ReplayRecord's
    `spell_targets` is a `HashMap<String, String>` (single value per
    spell name), so when multiple casts of the same spell hit, the
    FIRST recorded target wins. That's fine in practice — repeated
    casts in the same game usually pick the same target type
    (creature removal) or the engine's heuristic fills the gap.
    """
    if iid is None:
        return
    tgts = targets_by_iid.get(iid)
    if tgts:
        side["spell_targets"].setdefault(name, tgts[0])


def _parse_int_pt(value) -> int | None:
    """Parse a card's power/toughness field, handling MTGA's quirks.
    Returns None for variable / non-numeric P/T (e.g. '*', 'X',
    '1+*', '') so the caller can skip the +1/+1 delta inference."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def make_battlefield_card(bf_entry: dict) -> dict:
    """Convert a turn_start battlefield entry to ReplayRecord
    BattlefieldCard.

    +1/+1 counters are inferred from the difference between the
    snapshot's effective power (`bf_entry['power']`, which MTGA
    reports POST-modifications) and the card's printed/base power
    looked up via card_cache. Net positive delta on BOTH power and
    toughness is a strong signal for +1/+1 counters; we encode the
    minimum of the two so an additive +1/+1 effect (e.g. landfall
    on Sazh's Chocobo, Bristly Bill counter dumps, Mossborn Hydra
    landfall doubling) reaches the engine's checkpoint compare.

    The heuristic is intentionally conservative — it misses
    static-pump-only buffs (Glorious Anthem +1/+1 to all) because
    those add to base P/T without being counters in MTG terms. The
    engine's static-ability pipeline computes those independently,
    so reporting them as counters here would double-count.

    -1/-1 net deltas are NOT emitted (the schema is positive-only;
    -1/-1 counters cancel against +1/+1 via SBA and the resulting
    net is observable as a smaller positive delta or a deficit that
    propagates through effective P/T directly).
    """
    name = bf_entry.get("name", "?")
    eff_p = _parse_int_pt(bf_entry.get("power"))
    eff_t = _parse_int_pt(bf_entry.get("toughness"))
    grp_id = bf_entry.get("grp_id")
    plus_counters = 0
    if eff_p is not None and eff_t is not None and grp_id:
        base = card_cache.get(grp_id) if card_cache._loaded else None
        if base is not None:
            base_p = _parse_int_pt(base.power)
            base_t = _parse_int_pt(base.toughness)
            if base_p is not None and base_t is not None:
                dp = eff_p - base_p
                dt = eff_t - base_t
                if dp > 0 and dt > 0:
                    plus_counters = min(dp, dt)
    return {
        "name": name,
        "tapped": bool(bf_entry.get("tapped", False)),
        "plus_counters": plus_counters,
        "loyalty": 0,
        "summoning_sick": bool(bf_entry.get("summoning_sick", False)),
    }


def make_checkpoint(turn_start: dict | None, my_gy: list[str], opp_gy: list[str]) -> dict:
    """Build a Checkpoint from a `turn_start` event payload."""
    if not turn_start:
        return {
            "my_life": 20,
            "opp_life": 20,
            "my_battlefield": [],
            "opp_battlefield": [],
            "my_hand_size": 0,
            "opp_hand_size": 0,
            "my_graveyard": list(my_gy),
            "opp_graveyard": list(opp_gy),
        }
    ts = turn_start.get("data", {})
    return {
        "my_life": int(ts.get("my_life", 20)),
        "opp_life": int(ts.get("opp_life", 20)),
        "my_battlefield": [make_battlefield_card(c) for c in ts.get("my_battlefield", [])],
        "opp_battlefield": [make_battlefield_card(c) for c in ts.get("opp_battlefield", [])],
        "my_hand_size": int(ts.get("my_hand_size", 0)),
        "opp_hand_size": 0,  # MTGA only reveals opponent hand size on broadcast events; not in turn_start
        "my_graveyard": list(my_gy),
        "opp_graveyard": list(opp_gy),
    }


def build_replay_record(match: dict, events: list[dict], game_number: int) -> dict:
    """Build a ReplayRecord for one game of a match.

    Turn numbering note: scryglass tags turn_start events with the
    MTGA-internal turn_number, which can be irregular (we observed
    2/4/6/8/10 in some matches where the opposite side's turns
    weren't recorded). This builder groups events between
    consecutive turn_start events using the INDEX of the turn_start
    in chronological order (1-based: first turn_start = "turn 1",
    second = "turn 2", etc.) rather than the raw MTGA turn_number,
    so downstream consumers see a clean sequential turn timeline.
    """
    # Filter to this game's events (keep them id-ordered).
    game_events = [e for e in events if e["game_number"] == game_number]
    if not game_events:
        return {}

    # Mulligan + opening hand.
    mulligan_evt = next(
        (e for e in game_events if e["event_type"] == "mulligan"), None
    )
    if mulligan_evt:
        my_opening_hand = [c.get("name", "?") for c in mulligan_evt["data"].get("hand", [])]
        my_starting_hand_size = int(mulligan_evt["data"].get("hand_size", len(my_opening_hand)))
    else:
        first_ts = next((e for e in game_events if e["event_type"] == "turn_start"), None)
        if first_ts:
            my_opening_hand = [
                c.get("name", "?") for c in first_ts["data"].get("my_hand", [])
            ]
            my_starting_hand_size = int(first_ts["data"].get("my_hand_size", len(my_opening_hand)))
        else:
            my_opening_hand = []
            my_starting_hand_size = 0

    # Index every turn_start by its position in the event timeline.
    # Each turn_start marks the START of a new player's turn (the
    # transition AT the boundary). The pre-first-turn_start events
    # belong to logical turn 1 (the starting player's first turn).
    turn_start_events = [
        (i, e) for i, e in enumerate(game_events) if e["event_type"] == "turn_start"
    ]

    # Starting player: prefer the explicit `active_player` field on
    # the first turn_start event (added in Sprint #REPLAY-BOTH-SIDES
    # — scryglass tracker now emits turn_start for both players'
    # turns AND tags each with active_player). When the first
    # turn_start lacks that field (legacy logs), fall back to
    # inferring from the first card_played-style event observed
    # before any turn_start.
    starting_player = "me"  # default
    if turn_start_events:
        first_ts = turn_start_events[0][1]
        candidate = first_ts.get("data", {}).get("active_player")
        if candidate in ("me", "opp"):
            starting_player = candidate
        else:
            first_ts_idx = turn_start_events[0][0]
            for evt in game_events[:first_ts_idx]:
                et = evt["event_type"]
                if et in ("card_played", "spell_cast", "ability", "attack_declared"):
                    starting_player = "me"
                    break
                if et in ("opp_card_played", "opp_spell_cast", "opp_ability", "opp_attack_declared"):
                    starting_player = "opp"
                    break

    my_gy: list[str] = []
    opp_gy: list[str] = []

    turns_out: list[dict] = []
    final_result = "Unknown"

    game_end_evt = next(
        (e for e in game_events if e["event_type"] == "game_end"), None
    )
    if game_end_evt:
        final_result = game_end_evt["data"].get("result", final_result)

    # Build the window list. Each scryglass turn_start event fires
    # at the BEGINNING of an MTGA turn (Sprint #REPLAY-BOTH-SIDES
    # added emission for both players). So window K = the events
    # that happen DURING the MTGA turn whose start was announced by
    # turn_start_events[K] (exclusive end at turn_start_events[K+1]).
    #
    # The active player for window K is turn_start_events[K]'s
    # active_player field (the player whose turn this is). The
    # checkpoint for window K is turn_start_events[K+1]'s snapshot,
    # i.e. the state AT THE END of this turn (= start of next turn).
    #
    # Pre-game events (game start, mulligans) before turn_start[0]
    # don't belong to any in-game turn and are dropped. The tail
    # after the last turn_start is also dropped — it's typically the
    # untap step of a turn that ended via game_end with no usable
    # snapshot.
    windows: list[tuple[list[dict], dict, dict]] = []
    if turn_start_events:
        for k in range(len(turn_start_events) - 1):
            cur_idx, cur_ts = turn_start_events[k]
            next_idx, next_ts = turn_start_events[k + 1]
            windows.append((
                game_events[cur_idx + 1 : next_idx],
                cur_ts,    # turn_start that STARTED this window
                next_ts,   # turn_start that ENDED this window (checkpoint)
            ))

    # SPRINT 1 — game-wide annotation join maps, keyed by the
    # spell/ability instance_id that the annotation describes. Built
    # ONCE per game so per-window joining survives the timing skew
    # between a spell_cast event and the TargetSpec that records its
    # target (the TargetSpec persists across many subsequent
    # gamestate diffs, so it often arrives in a later window than
    # the cast itself). Dedup by `ann_id` because persistent
    # annotations get rebroadcast on every diff.
    targets_by_iid: dict[int, list[str]] = {}
    # Schema v2 — cause_object_id for cards entering the battlefield
    # via an effect (ETB-fetch basic, Cascade reveal, Fabled Passage
    # sac trigger). Keyed by the moving card's iid → the affector
    # source iid recorded on the ZoneTransfer annotation. iids without
    # an entry are treated as manual hand-played / cast-resolution
    # transfers (cause_object_id = null in the exported JSON).
    zone_transfer_cause: dict[int, int] = {}
    # MTGA refreshes an object's instance_id on every zone transition
    # (CR 400.7). Captured from AnnotationType_ObjectIdChanged
    # (kind="object_id_changed"). Maps the OLD iid → the NEW iid so
    # downstream lookups by old iid resolve to the live one. Chains
    # of renames are normalised below via repeated `resolve_alias`.
    iid_aliases: dict[int, int] = {}
    # Permanent / spell → stack-ability-instance mapping. Built from
    # AnnotationType_AbilityInstanceCreated (kind="ability_instance_
    # created"). affector_id is the source permanent / spell;
    # affected_ids[0] is the freshly minted ability iid that lives on
    # the stack. When a later ZoneTransfer attributes a move to that
    # ability iid (rather than the source object), we chase back
    # through this map to recover the underlying source iid that the
    # consumer can dereference to a card name.
    ability_to_source: dict[int, int] = {}

    def resolve_alias(iid: int | None) -> int | None:
        """Walk `iid_aliases` transitively to the current iid for
        `iid`. Returns the input unchanged when no rename exists.
        Capped to a small depth to defend against pathological cycles
        in the wire format (none observed; defensive only)."""
        if iid is None:
            return None
        seen: set[int] = set()
        cur = iid
        while cur in iid_aliases and cur not in seen:
            seen.add(cur)
            cur = iid_aliases[cur]
        return cur
    # Schema v2 — set of ability iids that the user explicitly
    # activated. Built from UserActionTaken annotations whose
    # affected_ids references the ability's instance_id. The Sprint 1
    # capture path already stores these as `kind=user_action_taken`;
    # we only consult the actionType to skip non-activation entries
    # (priority pass, mulligan, etc.).
    activated_ability_iids: set[int] = set()
    # Sprint 4 — per-source life delta captured from ModifiedLife.
    # Lets the exporter detect Bitter Triumph style additional-cost
    # life payments (Choice(DiscardCard, PayLife(N)) — if MTGA logs
    # a negative life delta tied to the cast's iid, the choice was
    # PayLife). The map is iid → signed life delta (cumulative).
    life_change_by_source: dict[int, int] = {}
    # Sprint 1 — X-values for activated abilities, decoded from
    # AnnotationType_CounterAdded `transaction_amount`. For Mossborn
    # Hydra's `{X}{G}: gets X +1/+1 counters`, the transaction_amount
    # equals X chosen at activation. Some X-pump activations get
    # MULTIPLIED by a doubler effect (Doubling Season, Branching
    # Evolution) — in those cases transaction_amount is the FINAL
    # counter count, not the X paid. The harness can divide by the
    # known doubler stack at the moment of activation if needed.
    counter_amount_by_iid: dict[int, int] = {}
    # iid → "me" / "opp" map built from spell/ability/play events so
    # life-payment attribution (ManaPaid affecting a spell) can
    # credit the correct side without re-walking events.
    side_by_iid: dict[int, str] = {}
    # window index for each event (so per-window life-pay aggregation
    # knows which side-play dict to update).
    window_of_iid: dict[int, int] = {}
    # Pre-compute (event_id → window_idx) for fast lookups of which
    # window an annotation's affected spell was cast in.
    event_window: dict[int, int] = {}
    for w_idx, (window_events, _, _) in enumerate(windows):
        for evt in window_events:
            event_window[evt["id"]] = w_idx
    seen_ann_ids: set[int] = set()
    # First pass over GAME events: build the iid → side map from
    # spell_cast / ability / card_played events (and their opp
    # mirrors). This is order-stable: each iid's first appearance
    # wins, matching how MTGA assigns instanceIds at creation.
    for evt in game_events:
        et = evt["event_type"]
        d = evt["data"]
        iid = d.get("instance_id")
        if iid is None:
            continue
        if et in ("spell_cast", "card_played", "ability"):
            side_by_iid.setdefault(iid, "me")
            window_of_iid.setdefault(iid, event_window.get(evt["id"], 0))
        elif et in ("opp_spell_cast", "opp_card_played", "opp_ability"):
            side_by_iid.setdefault(iid, "opp")
            window_of_iid.setdefault(iid, event_window.get(evt["id"], 0))
    # Second pass — pre-build the iid_aliases and ability_to_source
    # maps BEFORE walking zone_transfer joins. Both maps need to be
    # populated game-wide so a ZoneTransfer that references a stack
    # ability iid (created earlier the same window) or a renamed iid
    # (CR 400.7) can resolve through them. Without this pre-pass the
    # annotations could arrive in any order within a window and we'd
    # under-attribute.
    for evt in game_events:
        if evt["event_type"] != "annotation":
            continue
        d = evt["data"]
        kind = d.get("kind")
        if kind == "object_id_changed":
            details = d.get("details") or {}
            orig = details.get("orig_id")
            new = details.get("new_id")
            if isinstance(orig, int) and isinstance(new, int) and orig != new:
                iid_aliases.setdefault(orig, new)
        elif kind == "ability_instance_created":
            affected = d.get("affected_ids") or []
            aff_id_local = d.get("affector_id")
            if aff_id_local and affected:
                ability_to_source.setdefault(affected[0], aff_id_local)

    # Third pass: collect target_spec joins + life-paid mana counts +
    # zone_transfer cause attribution.
    # life_pay_by_window[(side, w_idx)] = total life paid in that
    # window by that side via "{T}, Pay N life: Add ..." sources.
    life_pay_by_window: dict[tuple[str, int], int] = {}
    for evt in game_events:
        if evt["event_type"] != "annotation":
            continue
        d = evt["data"]
        ann_id = d.get("ann_id")
        if ann_id is not None and ann_id in seen_ann_ids:
            continue
        if ann_id is not None:
            seen_ann_ids.add(ann_id)
        kind = d.get("kind")
        aff_id = d.get("affector_id")
        aff_names = d.get("affected_names") or []
        if kind == "target_spec" and aff_id and aff_names:
            targets_by_iid.setdefault(aff_id, []).extend(aff_names)
        elif kind == "modified_life" and aff_id:
            # ModifiedLife tied to a spell/effect. affector_id is
            # the source iid; affected_ids[0] is the player seat
            # whose life changed; details.life is the signed delta.
            # We accumulate per affector_id so a spell with multiple
            # ModifiedLife (e.g. drain) gets the full picture.
            life_delta = (d.get("details") or {}).get("life", 0)
            if life_delta:
                life_change_by_source.setdefault(aff_id, 0)
                life_change_by_source[aff_id] = (
                    life_change_by_source[aff_id] + int(life_delta)
                )
        elif kind == "counter_added" and aff_id:
            # affector_id = the ability/source that put the counter.
            # transaction_amount = total counters added by THIS
            # activation (so for "X +1/+1 counters", amount == X).
            amt = (d.get("details") or {}).get("transaction_amount", 1)
            counter_amount_by_iid[aff_id] = (
                counter_amount_by_iid.get(aff_id, 0) + int(amt)
            )
        elif kind == "zone_transfer":
            # Schema v2 — affector_id (the cause source) is recorded
            # whenever an effect (search-fetch, Cascade, Fabled
            # Passage sac trigger) moves a card; affected_ids[0] is
            # the moving card's iid. First write wins per iid
            # (subsequent transfers of the SAME instance — e.g. BF →
            # GY on death — shouldn't overwrite the ETB cause).
            #
            # Filter out seat-id "affectors": MTGA reuses affectorId
            # to hold the priority seat (1 / 2) on standard cast
            # resolutions, which is not a cause object the consumer
            # can dereference. We gate on `affector_name` (set by
            # _save_user_choice_annotations only when the iid
            # resolves to a known card object); seat-id affectors
            # never resolve to a name and are skipped. We also skip
            # category=Resolve (normal spell resolution from the
            # stack — not an effect-driven put).
            #
            # When `affector_name` is absent (the iid resolves to a
            # stack ability instance rather than a card), chase
            # `ability_to_source` to recover the underlying permanent
            # that owns the ability. Then run the result through
            # `resolve_alias` to land on the live iid after any
            # zone-transition rename. This is the path that fills
            # cause_object_id for ETB-fetch chains where the
            # ZoneTransfer attributes the put to the ability stack
            # object, not the source permanent.
            details = d.get("details") or {}
            affected = d.get("affected_ids") or []
            category = details.get("category")
            if aff_id and affected and category != "Resolve":
                moving_iid = resolve_alias(affected[0])
                cause = aff_id
                if not d.get("affector_name"):
                    chased = ability_to_source.get(aff_id)
                    if chased is None:
                        # Seat-id / unknown source — drop the
                        # attribution rather than emit a bogus link.
                        cause = None
                    else:
                        cause = chased
                if cause is not None and moving_iid is not None:
                    cause = resolve_alias(cause)
                    zone_transfer_cause.setdefault(moving_iid, cause)
        # NOTE: object_id_changed + ability_instance_created are
        # pre-aggregated in the dedicated second pass above so the
        # maps are populated game-wide before any zone_transfer
        # resolves through them.
        elif kind == "user_action_taken":
            # Schema v2 — discriminate triggered vs activated ability
            # entries on the consumer side. MTGA emits one
            # UserActionTaken per voluntary action; for ability
            # activations (actionType in {2,4}) affected_ids[0] is the
            # ability's iid on the stack. Triggered abilities never
            # get a UserActionTaken (they fire from event resolution),
            # so their iids never enter this set.
            details = d.get("details") or {}
            at = details.get("actionType")
            if at in (2, 4):
                for ab_iid in d.get("affected_ids") or []:
                    activated_ability_iids.add(ab_iid)
        elif kind == "mana_paid":
            affector_name = d.get("affector_name")
            color = (d.get("details") or {}).get("color")
            life_cost = LIFE_COST_MANA_LANDS.get(affector_name or "")
            # Color values 1-5 (W/U/B/R/G) from a life-cost-only land
            # mean the player used the pay-life-for-any-color side.
            # Color 12 (colorless) means the free `{T}: Add {C}` was
            # used — no life paid.
            if (
                life_cost
                and color is not None
                and color in (
                    MANA_PAID_COLOR_W,
                    MANA_PAID_COLOR_U,
                    MANA_PAID_COLOR_B,
                    MANA_PAID_COLOR_R,
                    MANA_PAID_COLOR_G,
                )
            ):
                affected_ids = d.get("affected_ids") or []
                if affected_ids:
                    payee = affected_ids[0]
                    side = side_by_iid.get(payee)
                    w_idx = window_of_iid.get(payee)
                    if side and w_idx is not None:
                        key = (side, w_idx)
                        life_pay_by_window[key] = life_pay_by_window.get(key, 0) + life_cost

    for k, (window_events, start_ts, checkpoint_src) in enumerate(windows):
        # Active player for this window = the active_player field on
        # the turn_start that BEGAN the window. Fall back to
        # alternation from starting_player when the field is missing
        # (legacy logs without explicit active_player).
        explicit = start_ts.get("data", {}).get("active_player")
        if explicit in ("me", "opp"):
            active = explicit
        else:
            active = starting_player if k % 2 == 0 else (
                "opp" if starting_player == "me" else "me"
            )

        me_side = make_side_play()
        opp_side = make_side_play()
        # Schema v2 — per-turn counter_events list, in observed
        # CounterAdded annotation order. Populated below from the
        # window's annotation events; each entry tags its position
        # within the turn so consumers can re-interleave with other
        # events (plays / attacks / blocks) when needed.
        counter_events: list[dict] = []
        for evt in window_events:
            if evt["event_type"] == "annotation":
                # Most annotation joins are pre-aggregated GAME-WIDE
                # above so they survive turn-window timing skew
                # (TargetSpec often appears in a later gamestate than
                # the spell_cast event that triggered it). The one
                # exception is CounterAdded: those are surfaced
                # per-turn as `counter_events` so the consumer can
                # project per-permanent counter trajectories without
                # losing emit order.
                d = evt["data"]
                if d.get("kind") == "counter_added":
                    affected = d.get("affected_ids") or []
                    if affected:
                        details = d.get("details") or {}
                        counter_events.append({
                            "turn_idx_in_turn": len(counter_events),
                            "target_iid": affected[0],
                            "counter_type": _counter_type_name(
                                details.get("counter_type")
                            ),
                            "amount": int(
                                details.get("transaction_amount", 1)
                            ),
                        })
                continue

            et = evt["event_type"]
            d = evt["data"]
            iid = d.get("instance_id")
            # Artifact tokens that the engine creates implicitly from
            # their parent's ETB ability (Map from Spyglass Siren,
            # Food from Witch's Cottage, Treasure from Brokers,
            # Clue from Glass Casket) should NOT also be put into
            # the player's `plays` list — the engine would treat the
            # re-emit as a fresh cast and double the token count on
            # battlefield. Creature tokens (Soldier, Rabbit, Spirit)
            # ARE kept because most of their creator abilities
            # aren't implemented in the engine; the re-cast path is
            # the only way they appear on opp's BF for damage math.
            _SAC_TOKEN_NAMES = {"Map", "Food", "Treasure", "Clue", "Blood",
                                "Powerstone", "Gold"}
            is_sac_token = (
                d.get("is_token") and d.get("name") in _SAC_TOKEN_NAMES
            )
            # Cards entering BF from Library / Graveyard / Exile are
            # not "manual plays" — they're consequences of an effect
            # already on the stack (search-fetched basic, reanimated
            # creature, cast-from-exile). ScriptedPlayer should NOT
            # try to cast them; the engine path that resolved the
            # source effect handles their BF entry.
            #
            # ACCEPT: None (legacy events without zone tracking),
            # "Hand" (manually-played lands, which skip the stack per
            # CR 305.1), "Stack" (cast spells resolving normally).
            # REJECT: "Library" (fetch), "Graveyard" (reanimate),
            # "Exile" (cast-from-exile reveal).
            #
            # Filtering Library-origin lands also prevents engine
            # from picking the WRONG slot under CR 305.2 (1 land per
            # turn): when MTGA shows both a manual land AND a fetched
            # basic in me.plays, engine plays the first land in order
            # which may differ from MTGA's manual choice. With the
            # fetched basic filtered, engine plays the manual one and
            # the engine's own search effect handles the fetch.
            from_zone = d.get("from_zone")
            is_manual_play = from_zone in (None, "Hand", "Stack")
            if et in ("card_played", "spell_cast"):
                name = d.get("name", "?")
                if is_sac_token or not is_manual_play:
                    continue
                me_side["plays"].append({
                    "name": name,
                    "cause_object_id": zone_transfer_cause.get(iid)
                    if iid else None,
                })
                if d.get("is_land") and d.get("enters_tapped"):
                    me_side["lands_entered_tapped"].append(name)
                _attach_spell_target(me_side, name, iid, targets_by_iid)
                _attach_life_choice(me_side, name, iid, life_change_by_source)
            elif et in ("opp_card_played", "opp_spell_cast"):
                name = d.get("name", "?")
                if is_sac_token or not is_manual_play:
                    continue
                opp_side["plays"].append({
                    "name": name,
                    "cause_object_id": zone_transfer_cause.get(iid)
                    if iid else None,
                })
                if d.get("is_land") and d.get("enters_tapped"):
                    opp_side["lands_entered_tapped"].append(name)
                _attach_spell_target(opp_side, name, iid, targets_by_iid)
                _attach_life_choice(opp_side, name, iid, life_change_by_source)
            elif et == "attack_declared":
                _append_attackers(me_side, d)
            elif et == "opp_attack_declared":
                _append_attackers(opp_side, d)
            elif et == "block_declared":
                # scryglass emits one block_declared per BLOCKER with
                # shape: {blocker, blocker_id, attackers: [{name,id,..}]}.
                # Blocker is always on the non-active player's side
                # (the defender). Determine side: if active=me, blocks
                # go on opp_side; if active=opp, blocks go on me_side.
                # Pick the FIRST attacker only — normally a creature
                # blocks exactly one attacker; multi-attacker shapes
                # appear only for menace-style mandatory multi-block
                # which we treat as the first assignment.
                blocker = d.get("blocker")
                if not blocker or d.get("no_blocks"):
                    continue
                attackers = d.get("attackers") or []
                target_side = opp_side if active == "me" else me_side
                if attackers and isinstance(attackers[0], dict):
                    block_entry = {
                        "blocker": blocker,
                        "attacker": attackers[0].get("name", "?"),
                    }
                    # Dedup against duplicate scryglass log entries
                    # (same block declared in multiple phases).
                    if block_entry not in target_side["blocks"]:
                        target_side["blocks"].append(block_entry)
            elif et == "ability":
                name = d.get("name", "?")
                target_names = targets_by_iid.get(iid) if iid else None
                me_side["activations"].append({
                    "source": name,
                    "target": (target_names[0] if target_names else d.get("target")),
                    "kind": _classify_ability_kind(iid, activated_ability_iids),
                })
                me_side["activation_x"].append(
                    counter_amount_by_iid.get(iid, 0) if iid else 0
                )
            elif et == "opp_ability":
                name = d.get("name", "?")
                target_names = targets_by_iid.get(iid) if iid else None
                opp_side["activations"].append({
                    "source": name,
                    "target": (target_names[0] if target_names else d.get("target")),
                    "kind": _classify_ability_kind(iid, activated_ability_iids),
                })
                opp_side["activation_x"].append(
                    counter_amount_by_iid.get(iid, 0) if iid else 0
                )
            elif et == "creature_left_bf":
                owner = d.get("owner", "me")
                name = d.get("name", "?")
                (my_gy if owner == "me" else opp_gy).append(name)
                # Also expose as a per-turn departure signal so the
                # harness can remove the permanent from the engine BF
                # during this turn (not just reflect it in the GY).
                (me_side if owner == "me" else opp_side)["creatures_left"].append(name)
            elif et == "life_change":
                # Accumulate net per-side life change for the turn.
                # `player` is already 'me'/'opp'. Harness applies the
                # opp value only on opp's own turn (no combat double-
                # count). See make_side_play life_delta comment.
                who = d.get("player", "me")
                delta = int(d.get("delta", 0) or 0)
                (me_side if who == "me" else opp_side)["life_delta"] += delta

        checkpoint = make_checkpoint(checkpoint_src, my_gy, opp_gy)
        raw_mtga = int(checkpoint_src["turn_number"] or 0) if checkpoint_src else 0
        # Sprint 2 — attach per-side life-payment counts derived from
        # ManaPaid annotations on life-cost mana lands (Starting Town
        # etc.). The total accumulates across all casts in this
        # window, so the engine harness can deduct it once before the
        # next turn's checkpoint diff runs.
        me_side["life_paid_for_mana"] = life_pay_by_window.get(("me", k), 0)
        opp_side["life_paid_for_mana"] = life_pay_by_window.get(("opp", k), 0)
        turns_out.append({
            "turn": k + 1,  # logical 1-based timeline index
            "raw_mtga_turn": raw_mtga,
            "active_player": active,
            "me": me_side,
            "opp": opp_side,
            "me_draws": [],
            "opp_draws": [],
            "counter_events": counter_events,
            "checkpoint": checkpoint,
        })

    # Decklist — best effort.
    my_deck_names = derive_deck_names(match, game_events)

    # Opponent revealed names: every distinct card name we observed
    # the opponent control or cast. Filter out generic token names
    # that look like creature subtypes (no grp_id present in the
    # source event implies a token).
    opp_revealed: set[str] = set()
    for evt in game_events:
        et = evt["event_type"]
        d = evt["data"]
        if et in ("opp_card_played", "opp_spell_cast", "opp_ability"):
            name = d.get("name")
            if name and d.get("grp_id"):
                opp_revealed.add(name)
        elif et == "turn_start":
            for bf in d.get("opp_battlefield", []) or []:
                name = bf.get("name")
                if name and bf.get("grp_id"):
                    opp_revealed.add(name)

    return {
        "schema_version": SCHEMA_VERSION,
        "match_id": match["match_id"],
        "game_number": game_number,
        "my_deck_names": sorted(my_deck_names),
        "opp_revealed_names": sorted(opp_revealed),
        "starting_player": starting_player,
        "my_starting_hand_size": my_starting_hand_size,
        "opp_starting_hand_size": 7,
        "my_opening_hand": my_opening_hand,
        "turns": turns_out,
        "final_result": final_result,
    }


def _append_attackers(side: dict, data: dict) -> None:
    """Pull attacker card names out of an attack_declared payload.

    Scryglass emits attack_declared in two shapes — a single-card
    `{"name": "..."}` per attacker, or a batched
    `{"attackers": [...]}` list. Accept both."""
    attackers = data.get("attackers")
    if isinstance(attackers, list) and attackers:
        for a in attackers:
            if isinstance(a, dict):
                n = a.get("name")
                if n:
                    side["attacks"].append(n)
            elif isinstance(a, str):
                side["attacks"].append(a)
    elif data.get("name"):
        side["attacks"].append(data["name"])


def derive_deck_names(match: dict, events: list[dict]) -> list[str]:
    """Best-effort: read my_deck_grp_ids from matches and resolve to
    names via the `cards` table. Falls back to accumulating names
    from card_played events if the mapping is missing."""
    grp_ids_json = match.get("my_deck_grp_ids") or "[]"
    try:
        grp_ids = json.loads(grp_ids_json)
    except json.JSONDecodeError:
        grp_ids = []

    deck_names: list[str] = []
    if grp_ids:
        # Resolve via cards table.
        conn = sqlite3.connect(str(DB_PATH))
        try:
            for entry in grp_ids:
                if isinstance(entry, dict):
                    grp_id = entry.get("grpId") or entry.get("grp_id")
                    qty = int(entry.get("quantity", entry.get("qty", 1)))
                else:
                    grp_id = entry
                    qty = 1
                if not grp_id:
                    continue
                row = conn.execute(
                    "SELECT name FROM cards WHERE grp_id = ?",
                    (grp_id,),
                ).fetchone()
                if row:
                    for _ in range(qty):
                        deck_names.append(row[0])
        finally:
            conn.close()

    if not deck_names:
        # Fall back to observed names.
        observed = set()
        for evt in events:
            d = evt["data"]
            if evt["event_type"] == "card_played":
                n = d.get("name")
                if n:
                    observed.add(n)
            elif evt["event_type"] == "mulligan":
                for c in d.get("hand", []):
                    n = c.get("name")
                    if n:
                        observed.add(n)
            elif evt["event_type"] == "turn_start":
                for c in d.get("my_hand", []):
                    n = c.get("name")
                    if n:
                        observed.add(n)
                for c in d.get("my_battlefield", []):
                    n = c.get("name")
                    if n:
                        observed.add(n)
        deck_names = sorted(observed)
    return deck_names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "match_id",
        nargs="?",
        help="full match_id (UUID) to export; omit when using --latest-deck",
    )
    parser.add_argument(
        "--latest-deck",
        help="export the most recently started match whose my_deck_name "
        "matches this substring (case-insensitive)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("./replays"),
        help="output directory (one JSON per game)",
    )
    args = parser.parse_args()

    # Pre-load card_cache so make_battlefield_card can compute
    # +1/+1 counter deltas from base vs effective P/T. Idempotent.
    if not card_cache._loaded:
        card_cache.load()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = None

    match_id = args.match_id
    if args.latest_deck and not match_id:
        match_id = latest_match_for_deck(conn, args.latest_deck)
        if not match_id:
            print(f"No match found with deck matching '{args.latest_deck}'", file=sys.stderr)
            sys.exit(1)
        print(f"Resolved --latest-deck '{args.latest_deck}' → {match_id}")

    if not match_id:
        parser.error("either match_id or --latest-deck is required")

    match = load_match(conn, match_id)
    if not match:
        print(f"Match {match_id} not found in DB", file=sys.stderr)
        sys.exit(1)

    events = load_events(conn, match_id)
    if not events:
        print(f"Match {match_id} has no events in DB", file=sys.stderr)
        sys.exit(1)

    game_numbers = sorted(set(e["game_number"] for e in events))
    print(f"Match {match_id}: {len(events)} events across games {game_numbers}")

    args.out.mkdir(parents=True, exist_ok=True)
    for gn in game_numbers:
        record = build_replay_record(match, events, gn)
        if not record:
            continue
        out_path = args.out / f"{match_id}_game{gn}.json"
        with out_path.open("w") as f:
            json.dump(record, f, indent=2)
        print(
            f"  game {gn}: {len(record['turns'])} turns, "
            f"deck={len(record['my_deck_names'])} cards → {out_path}"
        )


if __name__ == "__main__":
    main()
