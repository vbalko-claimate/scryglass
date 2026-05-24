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

from advisor.database import DB_PATH  # noqa: E402

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

SCHEMA_VERSION = 1


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


def make_battlefield_card(bf_entry: dict) -> dict:
    """Convert a turn_start battlefield entry to ReplayRecord
    BattlefieldCard. Counter information isn't currently tracked
    per-permanent by scryglass — we encode P/T deltas under
    plus_counters when both are positive and equal (a heuristic for
    +1/+1 counters; refined by permanent_stats_changed analysis in a
    follow-up)."""
    return {
        "name": bf_entry.get("name", "?"),
        "tapped": bool(bf_entry.get("tapped", False)),
        "plus_counters": 0,
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
    # Second pass: collect target_spec joins + life-paid mana counts.
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
        for evt in window_events:
            if evt["event_type"] == "annotation":
                # Annotations are pre-aggregated GAME-WIDE below so
                # joins survive turn-window timing skew (TargetSpec
                # often appears in a later gamestate than the
                # spell_cast event that triggered it).
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
                me_side["plays"].append(name)
                if d.get("is_land") and d.get("enters_tapped"):
                    me_side["lands_entered_tapped"].append(name)
                _attach_spell_target(me_side, name, iid, targets_by_iid)
                _attach_life_choice(me_side, name, iid, life_change_by_source)
            elif et in ("opp_card_played", "opp_spell_cast"):
                name = d.get("name", "?")
                if is_sac_token or not is_manual_play:
                    continue
                opp_side["plays"].append(name)
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
                })
                opp_side["activation_x"].append(
                    counter_amount_by_iid.get(iid, 0) if iid else 0
                )
            elif et == "creature_left_bf":
                owner = d.get("owner", "me")
                name = d.get("name", "?")
                (my_gy if owner == "me" else opp_gy).append(name)

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
