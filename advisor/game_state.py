"""Game state tracker — reconstructs and maintains game state from log messages."""
from __future__ import annotations

import json
import logging
from typing import Callable

from .database import card_cache, save_match, save_match_event
from .log_parser import extract_gre_messages
from .models import (
    Action, GameState, GameObject, MatchInfo, PlayerState, TurnInfo, Zone,
)

log = logging.getLogger(__name__)


class GameStateTracker:
    """Processes log messages and maintains current game state."""

    def __init__(self):
        self.state = GameState()
        self.on_state_change: Callable[[GameState], None] | None = None
        self.on_decision_point: Callable[[GameState, str], None] | None = None
        self.on_match_start: Callable[[], None] | None = None
        self.on_match_end: Callable[[bool], None] | None = None
        self.on_my_card_played: Callable[[str, str, int, int], None] | None = None
        # Spell/ability observation callback: (event_type, card_name, colors, card_types, oracle_text, source_card_name)
        self.on_stack_observed: Callable[[str, dict], None] | None = None
        self._match_active = False
        self._last_logged_turn = 0
        # B1: recent annotations for death-cause resolution
        self._recent_annotations: list[dict] = []
        # B3b: persistent aura→target attachment map (aura_iid → target_iid)
        self._attachment_map: dict[int, int] = {}
        # B5: auto-tap solutions keyed by instance_id
        self._last_auto_tap: dict[int, dict] = {}
        # Track instance IDs already seen on battlefield to avoid
        # duplicate card_played events when a Full state snapshot
        # re-sends all objects (clearing self.state.objects loses
        # the old_obj needed for entering-battlefield detection).
        self._seen_on_bf: set[int] = set()
        self._seen_on_stack: set[int] = set()
        self._pending_connect_context: dict[str, object] = {}
        self._last_decision_snapshot_key: tuple[int, str] = (-1, "")

    @property
    def match_active(self) -> bool:
        return self._match_active

    def process_message(self, msg: dict):
        """Process a raw parsed log message."""
        msg_type = msg["type"]

        # Capture our account userId from log message header
        if not hasattr(self, "_my_user_id") or not self._my_user_id:
            pid = msg.get("player_id", "")
            if pid and msg.get("direction") == "incoming":
                self._my_user_id = pid

        if msg_type == "MatchGameRoomStateChangedEvent":
            self._handle_room_state(msg["payload"])
        elif msg_type == "GreToClientEvent":
            for gre_msg in extract_gre_messages(msg):
                self._handle_gre_message(gre_msg)
        elif msg_type == "ClientToGremessage":
            self._handle_client_message(msg["payload"])

    def _handle_room_state(self, payload: dict):
        """Handle MatchGameRoomStateChangedEvent — match start/end."""
        event = payload.get("matchGameRoomStateChangedEvent", {})
        room_info = event.get("gameRoomInfo", {})
        state_type = room_info.get("stateType", "")

        if state_type == "MatchGameRoomStateType_Playing":
            config = room_info.get("gameRoomConfig", {})
            match_id = config.get("matchId", "")
            players = room_info.get("players", [])
            reserved = config.get("reservedPlayers", [])
            pending_connect = dict(self._pending_connect_context)

            if match_id and self.state.match_info.match_id and match_id != self.state.match_info.match_id:
                self.reset()

            # Store player names for later
            self._player_names = {
                p.get("systemSeatId", 0): p.get("playerName", "")
                for p in players
            }

            # Find our account name and opponent from reservedPlayers (has userId)
            self._all_reserved = reserved
            my_user_id = getattr(self, "_my_user_id", None)
            my_seat = 0
            opp_seat = 0

            # Extract opponent name directly from reservedPlayers
            self._opp_name_from_room = ""
            self._my_account_name = ""
            for p in reserved:
                seat = p.get("systemSeatId", 0)
                if my_user_id and p.get("userId") == my_user_id:
                    self._my_account_name = p.get("playerName", "")
                    my_seat = seat
                elif my_user_id and p.get("userId") != my_user_id:
                    self._opp_name_from_room = p.get("playerName", "")
                    opp_seat = seat

            if not my_seat:
                my_seat = int(pending_connect.get("my_seat_id", 0) or 0)
            if my_seat and not opp_seat:
                opp_seat = 1 if my_seat == 2 else 2

            self.state.match_info.match_id = match_id
            if my_seat:
                self.state.my_seat_id = my_seat
            if opp_seat:
                self.state.match_info.opponent_seat_id = opp_seat
            if pending_connect.get("my_deck"):
                self.state.my_deck = list(pending_connect.get("my_deck", []))
            self._match_active = True
            log.info("Match started: %s", match_id)

            if self.on_match_start:
                self.on_match_start()

            save_match(match_id, format="Constructed")

        elif state_type == "MatchGameRoomStateType_MatchCompleted":
            self._match_active = False
            results = room_info.get("finalMatchResult", {})
            result_list = results.get("resultList", [])
            if result_list and self.state.match_info.match_id:
                # resultList has MatchScope_Game + MatchScope_Match entries
                # Count actual games played (MatchScope_Game entries)
                game_results = [r for r in result_list
                                if r.get("scope") == "MatchScope_Game"]
                game_count = max(len(game_results), 1)

                # Determine win/loss from match-scope result
                match_result = next(
                    (r for r in result_list if r.get("scope") == "MatchScope_Match"),
                    result_list[0],
                )
                my_team = self.state.players.get(
                    self.state.my_seat_id, PlayerState(0)).team_id
                result_str = ("Win" if match_result.get("winningTeamId") == my_team
                              else "Loss")

                save_match(self.state.match_info.match_id,
                           result=result_str, game_count=game_count)
                log.info("Match ended: %s (%d games)", result_str, game_count)

                # Notify for strategy learning
                if self.on_match_end:
                    self.on_match_end(result_str == "Win")

    def _handle_gre_message(self, gre_msg: dict):
        """Handle an individual GRE message."""
        msg_type = gre_msg.get("type", "")
        gm = gre_msg.get("gre_msg", gre_msg)

        if msg_type == "GREMessageType_ConnectResp":
            self._handle_connect_resp(gm)
        elif msg_type in ("GREMessageType_GameStateMessage",
                           "GREMessageType_QueuedGameStateMessage"):
            self._handle_game_state_message(gm)
        elif msg_type == "GREMessageType_DieRollResultsResp":
            self._handle_die_roll(gm)
        elif msg_type in (
            "GREMessageType_MulliganReq",
            "GREMessageType_ActionsAvailableReq",
            "GREMessageType_DeclareAttackersReq",
            "GREMessageType_DeclareBlockersReq",
            "GREMessageType_SelectTargetsReq",
            "GREMessageType_ChooseStartingPlayerReq",
        ):
            self.state.pending_request = msg_type
            self._save_decision_context(msg_type)
            if self.on_decision_point:
                self.on_decision_point(self.state, msg_type)
        elif msg_type == "GREMessageType_IntermissionReq":
            self._handle_intermission(gm)

    def _handle_connect_resp(self, gm: dict):
        """Handle ConnectResp — our seat ID and deck."""
        resp = gm.get("connectResp", {})
        seat_ids = gm.get("systemSeatIds", [])
        if seat_ids:
            self.state.my_seat_id = seat_ids[0]

        deck_msg = resp.get("deckMessage", {})
        self.state.my_deck = deck_msg.get("deckCards", [])
        self._pending_connect_context = {
            "my_seat_id": self.state.my_seat_id,
            "my_deck": list(self.state.my_deck),
        }

        # Set opponent seat
        opp_seat = 1 if self.state.my_seat_id == 2 else 2
        self.state.match_info.opponent_seat_id = opp_seat

        # Set opponent name
        opp_name = getattr(self, "_opp_name_from_room", "")
        if not opp_name and hasattr(self, "_player_names"):
            # Fallback: pick name from seat that isn't ours
            my_name = getattr(self, "_my_account_name", "")
            for seat, name in self._player_names.items():
                if name and name != my_name and seat != self.state.my_seat_id:
                    opp_name = name
                    break
        self.state.match_info.opponent_name = opp_name

        # Save opponent name + deck to DB (only if we have a real opponent name)
        if self.state.match_info.match_id:
            kwargs = {}
            opp = self.state.match_info.opponent_name
            my_name = getattr(self, "_my_account_name", "")
            # Guard: never save our own name as opponent
            all_my_names = {my_name} | {
                p.get("playerName", "")
                for p in getattr(self, "_all_reserved", [])
                if p.get("userId") == getattr(self, "_my_user_id", None)
            } - {""}
            if opp and opp not in all_my_names:
                kwargs["opponent_name"] = opp
            if self.state.my_deck:
                kwargs["my_deck_grp_ids"] = json.dumps(self.state.my_deck)
            if kwargs:
                save_match(self.state.match_info.match_id, **kwargs)

        log.info("Connected: seat=%d, deck=%d cards, opponent=%s",
                 self.state.my_seat_id, len(self.state.my_deck),
                 self.state.match_info.opponent_name)

    def _handle_die_roll(self, gm: dict):
        """Handle die roll results."""
        resp = gm.get("dieRollResultsResp", {})
        rolls = resp.get("playerDieRolls", [])
        for r in rolls:
            log.info("Die roll: seat %d = %d", r.get("systemSeatId", 0), r.get("rollValue", 0))

    def _handle_intermission(self, gm: dict):
        """Handle game end within a match."""
        req = gm.get("intermissionReq", {})
        result = req.get("result", {})
        reason = result.get("reason", "unknown")
        log.info("Game ended: %s", reason)

        # Save game end event
        mid = self.state.match_info.match_id
        if mid:
            me = self.state.my_player()
            opp = self.state.opp_player()
            save_match_event(mid, "game_end",
                game_number=self.state.match_info.game_number,
                turn_number=self.state.turn_info.turn_number,
                phase="game_end",
                data={"reason": reason,
                      "my_life": me.life_total if me else 0,
                      "opp_life": opp.life_total if opp else 0})

    def _handle_game_state_message(self, gm: dict):
        """Handle GameStateMessage — the core state update."""
        gsm = gm.get("gameStateMessage", {})
        state_type = gsm.get("type", "")
        state_id = gsm.get("gameStateId", 0)

        if state_type == "GameStateType_Full":
            self._apply_full_state(gsm)
        elif state_type == "GameStateType_Diff":
            self._apply_diff_state(gsm)

        self.state.game_state_id = state_id
        # A new game state invalidates the previous explicit decision request.
        # Decision-point handlers already receive a frozen snapshot, so clearing
        # here prevents stale mulligan/attack prompts from leaking forward.
        self.state.pending_request = None

        # Notify listener
        if self.on_state_change:
            self.on_state_change(self.state)

    def _apply_full_state(self, gsm: dict):
        """Apply a full game state snapshot."""
        # Game info
        game_info = gsm.get("gameInfo", {})
        self.state.match_info.game_number = game_info.get("gameNumber", 1)
        self.state.match_info.stage = game_info.get("stage", "")

        # Players
        for p in gsm.get("players", []):
            seat = p.get("systemSeatNumber", 0)
            self.state.players[seat] = PlayerState(
                seat_id=seat,
                life_total=p.get("lifeTotal", 20),
                starting_life_total=p.get("startingLifeTotal", 20),
                max_hand_size=p.get("maxHandSize", 7),
                mulligan_count=p.get("mulliganCount", 0),
                team_id=p.get("teamId", 0),
                pending_message_type=p.get("pendingMessageType"),
                controller_type=p.get("controllerType", "ControllerType_Player"),
            )
            # Set name from room state
            if hasattr(self, "_player_names"):
                self.state.players[seat].name = self._player_names.get(seat, "")

        # Zones
        self.state.zones.clear()
        for z in gsm.get("zones", []):
            zone_id = z.get("zoneId", 0)
            self.state.zones[zone_id] = Zone(
                zone_id=zone_id,
                type=z.get("type", ""),
                owner_seat_id=z.get("ownerSeatId"),
                object_instance_ids=z.get("objectInstanceIds", []),
                visibility=z.get("visibility", "Visibility_Public"),
            )

        # Game objects
        self.state.objects.clear()
        for obj in gsm.get("gameObjects", []):
            self._add_or_update_object(obj)

        # Track stack spells
        self._detect_stack_spells(gsm)

        # Remove deleted
        for iid in gsm.get("diffDeletedInstanceIds", []):
            self.state.objects.pop(iid, None)

        # Turn info
        ti = gsm.get("turnInfo", {})
        self.state.turn_info = TurnInfo(
            phase=ti.get("phase", ""),
            step=ti.get("step", ""),
            turn_number=ti.get("turnNumber", 0),
            active_player=ti.get("activePlayer", 0),
            priority_player=ti.get("priorityPlayer", 0),
            decision_player=ti.get("decisionPlayer", 0),
            next_phase=ti.get("nextPhase", ""),
            next_step=ti.get("nextStep", ""),
        )

        # Actions
        self._parse_actions(gsm.get("actions", []))

        # Annotations
        new_annotations = gsm.get("annotations", [])
        self.state.annotations = new_annotations
        # SPRINT 1 — persistent annotations are a SEPARATE list from
        # `annotations`; this is where MTGA puts TargetSpec
        # (recording the chosen target object) and similar
        # cross-state choices. Combine both lists when persisting
        # for the replay exporter.
        persistent = gsm.get("persistentAnnotations", [])
        if new_annotations or persistent:
            self._save_user_choice_annotations(
                list(new_annotations) + list(persistent)
            )

    def _apply_diff_state(self, gsm: dict):
        """Apply a diff update to the current state."""
        # Game info (if present)
        game_info = gsm.get("gameInfo")
        if game_info:
            self.state.match_info.game_number = game_info.get("gameNumber", self.state.match_info.game_number)
            self.state.match_info.stage = game_info.get("stage", self.state.match_info.stage)

        # Players (merge)
        for p in gsm.get("players", []):
            seat = p.get("systemSeatNumber", 0)
            new_life = p.get("lifeTotal")
            if seat in self.state.players:
                ps = self.state.players[seat]
                old_life = ps.life_total
                ps.life_total = new_life if new_life is not None else ps.life_total
                ps.mulligan_count = p.get("mulliganCount", ps.mulligan_count)
                ps.pending_message_type = p.get("pendingMessageType", ps.pending_message_type)

                # Track significant life changes
                if new_life is not None and new_life != old_life:
                    mid = self.state.match_info.match_id
                    if mid:
                        who = self._seat_role(seat)
                        save_match_event(mid, "life_change",
                            game_number=self.state.match_info.game_number,
                            turn_number=self.state.turn_info.turn_number,
                            phase=self.state.turn_info.phase,
                            data={"player": who,
                                  "old": old_life,
                                  "new": new_life,
                                  "delta": new_life - old_life})
            else:
                self.state.players[seat] = PlayerState(
                    seat_id=seat,
                    life_total=new_life if new_life is not None else 20,
                    team_id=p.get("teamId", 0),
                )

        # Annotations — MUST be stored BEFORE game objects so that
        # _resolve_removal_cause() has current annotations when creatures die.
        new_annotations = gsm.get("annotations", [])
        if new_annotations:
            self.state.annotations = new_annotations
            self._recent_annotations = new_annotations
            # B3b: update persistent attachment map from AttachmentCreated annotations
            for ann in new_annotations:
                if "AnnotationType_AttachmentCreated" in ann.get("type", []):
                    aura_iid = ann.get("affectorId")
                    aids = ann.get("affectedIds", [])
                    if aura_iid and aids:
                        self._attachment_map[aura_iid] = aids[0]
        # SPRINT 1 — persist user-choice annotations so the
        # replay exporter can reconstruct per-action choices
        # (spell targets, X values, modal modes, life-cost mana
        # payments). These are the bits MTGA records but our
        # event log was dropping. The annotations join with
        # spell_cast / ability / card_played events later via
        # `affector_id` (the spell/ability's instanceId).
        # `persistentAnnotations` lives separately from the
        # standard `annotations` list — that's where TargetSpec
        # records the chosen target object during a cast.
        persistent = gsm.get("persistentAnnotations", [])
        if new_annotations or persistent:
            self._save_user_choice_annotations(
                list(new_annotations) + list(persistent)
            )

        # Zones (merge — replace changed zones)
        for z in gsm.get("zones", []):
            zone_id = z.get("zoneId", 0)
            self.state.zones[zone_id] = Zone(
                zone_id=zone_id,
                type=z.get("type", ""),
                owner_seat_id=z.get("ownerSeatId"),
                object_instance_ids=z.get("objectInstanceIds", []),
                visibility=z.get("visibility", "Visibility_Public"),
            )

        # Game objects (merge)
        for obj in gsm.get("gameObjects", []):
            self._add_or_update_object(obj)

        # Track instants/sorceries via stack zone membership
        # (objects may already have a different zoneId in their gameObject
        #  by the time the diff arrives, so we detect stack presence from
        #  the zone's objectInstanceIds list instead)
        self._detect_stack_spells(gsm)

        # Remove deleted objects
        for iid in gsm.get("diffDeletedInstanceIds", []):
            self.state.objects.pop(iid, None)

        # Turn info (replace if present)
        ti = gsm.get("turnInfo")
        if ti:
            self.state.turn_info = TurnInfo(
                phase=ti.get("phase", ""),
                step=ti.get("step", ""),
                turn_number=ti.get("turnNumber", 0),
                active_player=ti.get("activePlayer", 0),
                priority_player=ti.get("priorityPlayer", 0),
                decision_player=ti.get("decisionPlayer", 0),
                next_phase=ti.get("nextPhase", ""),
                next_step=ti.get("nextStep", ""),
            )

        # Actions
        if "actions" in gsm:
            self._parse_actions(gsm["actions"])

        # Log turn boundaries for replay reconstruction. We emit
        # turn_start for BOTH players' turns (the original version
        # gated on `new_active == self.state.my_seat_id`, which lost
        # half the turn boundaries and made downstream replay
        # validation impossible). The `active_player` field in the
        # payload tells consumers whose turn just started ("me" or
        # "opp"), so the replay exporter doesn't need to alternate
        # from a starting-player heuristic.
        if ti:
            new_turn = ti.get("turnNumber", 0)
            new_active = ti.get("activePlayer", 0)
            if (new_turn > 0
                    and new_turn != self._last_logged_turn
                    and self.state.match_info.match_id):
                self._last_logged_turn = new_turn
                lands = self.state.my_lands()
                me = self.state.my_player()
                opp = self.state.opp_player()
                snapshot = self._build_state_snapshot(include_actions=False)
                active_label = (
                    "me" if new_active == self.state.my_seat_id
                    else ("opp" if new_active else "unknown")
                )
                save_match_event(
                    self.state.match_info.match_id, "turn_start",
                    game_number=self.state.match_info.game_number,
                    turn_number=new_turn,
                    phase="turn_start",
                    data={"available_mana": len(lands),
                          "total_lands": len(lands),
                          "active_player": active_label,
                          "active_seat_id": new_active,
                          "my_seat_id": self.state.my_seat_id,
                          "my_creatures": snapshot["my_creatures"],
                          "opp_creatures": snapshot["opp_creatures"],
                          "my_battlefield": snapshot["my_battlefield"],
                          "opp_battlefield": snapshot["opp_battlefield"],
                          "my_hand": snapshot["my_hand"],
                          "stack": snapshot["stack"],
                          "my_hand_size": len(self.state.my_hand()),
                          "my_life": me.life_total if me else 0,
                          "opp_life": opp.life_total if opp else 0})

    def _add_or_update_object(self, obj: dict):
        """Add or update a game object."""
        iid = obj.get("instanceId", 0)
        grp_id = obj.get("grpId", 0)

        # Resolve card name
        card = card_cache.get(grp_id)
        name = card.name if card else f"Unknown({grp_id})"

        game_obj = GameObject(
            instance_id=iid,
            grp_id=grp_id,
            zone_id=obj.get("zoneId", 0),
            owner_seat_id=obj.get("ownerSeatId", 0),
            controller_seat_id=obj.get("controllerSeatId", obj.get("ownerSeatId", 0)),
            card_types=obj.get("cardTypes", []),
            subtypes=obj.get("subtypes", []),
            color=obj.get("color", []),
            power=_safe_int(obj.get("power", {}).get("value", 0)) if isinstance(obj.get("power"), dict) else _safe_int(obj.get("power", 0)),
            toughness=_safe_int(obj.get("toughness", {}).get("value", 0)) if isinstance(obj.get("toughness"), dict) else _safe_int(obj.get("toughness", 0)),
            name=name,
            is_tapped=obj.get("isTapped", False),
            has_summoning_sickness=obj.get("hasSummoningSickness", False),
            object_type=obj.get("type", "GameObjectType_Card").replace("GameObjectType_", ""),
            attached_to_id=self._attachment_map.get(iid),  # B3: from AttachmentCreated annotations
            source_grp_id=obj.get("objectSourceGrpId", 0),
            parent_id=obj.get("parentId", 0)
        )

        # Check if object is entering battlefield (zone-type based, not owner-based)
        old_obj = self.state.objects.get(iid)
        owner = game_obj.owner_seat_id
        opp_seat = self.state.match_info.opponent_seat_id
        my_seat = self.state.my_seat_id

        obj_zone = self.state.zones.get(game_obj.zone_id)
        on_battlefield = obj_zone and obj_zone.type == "ZoneType_Battlefield"

        was_on_bf = False
        if old_obj:
            old_zone = self.state.zones.get(old_obj.zone_id)
            was_on_bf = old_zone and old_zone.type == "ZoneType_Battlefield"

        entering_bf = on_battlefield and not was_on_bf

        # Guard against duplicate events from Full state snapshots:
        # when _apply_full_state clears self.state.objects, every
        # battlefield object looks "new".  _seen_on_bf survives the
        # clear, so we can tell if we already emitted events for it.
        if entering_bf and iid in self._seen_on_bf:
            entering_bf = False          # already tracked — skip events
        if on_battlefield:
            self._seen_on_bf.add(iid)
        else:
            self._seen_on_bf.discard(iid)

        # Log opponent cards entering battlefield
        if (entering_bf and opp_seat and owner == opp_seat and card
                and not card.is_land and grp_id > 0
                and self.state.match_info.match_id):
            save_match_event(
                self.state.match_info.match_id, "opp_card_played",
                game_number=self.state.match_info.game_number,
                turn_number=self.state.turn_info.turn_number,
                phase=self.state.turn_info.phase,
                data={"name": card.name, "grp_id": grp_id,
                      "card_types": card.card_types,
                      "colors": card.colors,
                      "instance_id": iid})

        if (on_battlefield and was_on_bf and old_obj and card
                and self.state.match_info.match_id):
            stats_changed = (
                old_obj.power != game_obj.power
                or old_obj.toughness != game_obj.toughness
            )
            if stats_changed:
                save_match_event(
                    self.state.match_info.match_id, "permanent_stats_changed",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data={
                        "name": card.name,
                        "grp_id": grp_id,
                        "controller": self._seat_role(game_obj.controller_seat_id),
                        "owner": self._seat_role(owner),
                        "old_power": old_obj.power,
                        "new_power": game_obj.power,
                        "old_toughness": old_obj.toughness,
                        "new_toughness": game_obj.toughness,
                    })

        # Set attack/block state from game object data
        game_obj.attack_state = obj.get("attackState")
        game_obj.block_state = obj.get("blockState")

        # Log my card entering battlefield
        if (entering_bf and my_seat and owner == my_seat and card
                and grp_id > 0 and self.state.match_info.match_id):
            play_data = {"name": card.name, "grp_id": grp_id,
                         "card_types": card.card_types,
                         "colors": card.colors,
                         "cmc": card.cmc,
                         "is_land": card.is_land,
                         "instance_id": iid}
            # B5: attach mana spent info
            auto_tap = self._last_auto_tap.pop(iid, None)
            if auto_tap:
                play_data["auto_tap"] = auto_tap
            save_match_event(
                self.state.match_info.match_id, "card_played",
                game_number=self.state.match_info.game_number,
                turn_number=self.state.turn_info.turn_number,
                phase=self.state.turn_info.phase,
                data=play_data)
            # Notify advisor for compliance tracking (non-land only)
            if not card.is_land and self.on_my_card_played:
                self.on_my_card_played(
                    card.name,
                    self.state.match_info.match_id,
                    self.state.turn_info.turn_number,
                    self.state.match_info.game_number)

        # B3: Log enchantment/aura attachments via AttachmentCreated annotations
        # GRE uses annotations (affectorId=aura, affectedIds=[target]), not object fields
        if (entering_bf and card and self.state.match_info.match_id
                and "Enchantment" in (card.card_types or [])):
            target_iid = None
            for ann in self._recent_annotations:
                ann_types = ann.get("type", [])
                if "AnnotationType_AttachmentCreated" in ann_types:
                    if ann.get("affectorId") == iid:
                        aids = ann.get("affectedIds", [])
                        if aids:
                            target_iid = aids[0]
                            break
            if target_iid:
                # Wire attached_to_id on the aura object so _get_aura_abilities works
                game_obj.attached_to_id = target_iid
                target_obj = self.state.objects.get(target_iid)
                target_card = card_cache.get(target_obj.grp_id) if target_obj else None
                target_owner = ("me" if target_obj and target_obj.owner_seat_id == my_seat
                                else "opp") if target_obj else "unknown"
                save_match_event(
                    self.state.match_info.match_id, "enchantment_attached",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data={"aura": card.name, "aura_id": iid,
                          "target": target_card.name if target_card else (
                              target_obj.name if target_obj else "unknown"),
                          "target_id": target_iid,
                          "target_owner": target_owner})

        # Log my creatures attacking
        if (my_seat and game_obj.controller_seat_id == my_seat
                and game_obj.attack_state and "Attacking" in str(game_obj.attack_state)
                and card and self.state.match_info.match_id):
            was_attacking = (old_obj and old_obj.attack_state
                             and "Attacking" in str(old_obj.attack_state)) if old_obj else False
            if not was_attacking:
                save_match_event(
                    self.state.match_info.match_id, "attack_declared",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data={"name": card.name, "grp_id": grp_id,
                          "power": game_obj.power,
                          "toughness": game_obj.toughness})

        # Log opponent creatures attacking
        if (opp_seat and game_obj.controller_seat_id == opp_seat
                and game_obj.attack_state and "Attacking" in str(game_obj.attack_state)
                and card and self.state.match_info.match_id):
            was_attacking = (old_obj and old_obj.attack_state
                             and "Attacking" in str(old_obj.attack_state)) if old_obj else False
            if not was_attacking:
                save_match_event(
                    self.state.match_info.match_id, "opp_attack_declared",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data={"name": card.name, "grp_id": grp_id,
                          "power": game_obj.power,
                          "toughness": game_obj.toughness})

        # Track cards leaving the hand (hand disruption: exile/discard by opponent)
        in_hand = obj_zone and obj_zone.type == "ZoneType_Hand"
        was_in_hand = False
        if old_obj:
            old_zone_for_hand = self.state.zones.get(old_obj.zone_id)
            was_in_hand = old_zone_for_hand and old_zone_for_hand.type == "ZoneType_Hand"

        leaving_hand = was_in_hand and not in_hand and not on_battlefield
        if (leaving_hand and my_seat and owner == my_seat and card
                and grp_id > 0 and self.state.match_info.match_id):
            dest_zone_h = self.state.zones.get(game_obj.zone_id)
            dest_type_h = dest_zone_h.type if dest_zone_h else "unknown"
            dest_label_h = {
                "ZoneType_Exile": "exiled",
                "ZoneType_Graveyard": "discarded",
                "ZoneType_Stack": None,      # being cast — ignore
                "ZoneType_Battlefield": None, # played — ignore
            }.get(dest_type_h)
            if dest_label_h:
                caused_by_h = self._resolve_removal_cause(iid)
                if not caused_by_h or caused_by_h.get("seat") != opp_seat:
                    caused_by_h = None
            if dest_label_h and caused_by_h:
                event_data_h: dict = {
                    "name": card.name, "grp_id": grp_id,
                    "destination": dest_label_h,
                    "card_types": card.card_types,
                }
                event_data_h["caused_by"] = caused_by_h["name"]
                event_data_h["caused_by_type"] = caused_by_h["type"]
                save_match_event(
                    self.state.match_info.match_id, "hand_disrupted",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data=event_data_h)
                self.state.hand_disrupted_count += 1

        # Track creatures leaving the battlefield (death/exile/bounce)
        leaving_bf = was_on_bf and not on_battlefield
        if (leaving_bf and old_obj and card and grp_id > 0
                and self.state.match_info.match_id):
            # Determine destination zone
            dest_zone = self.state.zones.get(game_obj.zone_id)
            dest_type = dest_zone.type if dest_zone else "unknown"
            dest_label = {
                "ZoneType_Graveyard": "died",
                "ZoneType_Exile": "exiled",
                "ZoneType_Hand": "bounced",
                "ZoneType_Library": "tucked",
            }.get(dest_type, "removed")

            who = self._seat_role(owner)
            event_data = {
                "name": card.name, "grp_id": grp_id,
                "owner": who,
                "destination": dest_label,
                "card_types": card.card_types,
                "power": old_obj.power,
                "toughness": old_obj.toughness,
            }

            # B1: determine cause of removal from annotations
            caused_by = self._resolve_removal_cause(iid)
            if caused_by:
                event_data["caused_by"] = caused_by["name"]
                event_data["caused_by_type"] = caused_by["type"]

            # Check if this is a temporary exile (e.g. Sheltered by Ghosts)
            # by looking for the exiling source still on the battlefield
            if dest_label == "exiled" and who == "opp":
                event_data["temporary_exile"] = True

            save_match_event(
                self.state.match_info.match_id, "creature_left_bf",
                game_number=self.state.match_info.game_number,
                turn_number=self.state.turn_info.turn_number,
                phase=self.state.turn_info.phase,
                data=event_data)

        self.state.objects[iid] = game_obj

    def _detect_stack_spells(self, gsm: dict):
        """Detect instants/sorceries entering the stack via zone membership.

        GRE often sends instant/sorcery objects with their *final* zoneId
        (graveyard/limbo) even though they appear in the stack zone's
        objectInstanceIds list.  So we track them through zones, not objects.
        """
        if not self.state.match_info.match_id:
            return
        opp_seat = self.state.match_info.opponent_seat_id
        my_seat = self.state.my_seat_id

        for z in gsm.get("zones", []):
            if z.get("type") != "ZoneType_Stack":
                continue
            for iid in z.get("objectInstanceIds", []):
                if iid in self._seen_on_stack:
                    continue  # already logged
                self._seen_on_stack.add(iid)

                obj = self.state.objects.get(iid)
                if not obj:
                    continue
                owner = obj.owner_seat_id

                # --- Ability on stack ---
                if obj.object_type == "Ability":
                    source_card = card_cache.get(obj.source_grp_id)
                    if not source_card:
                        continue
                    ability_data = {
                        "name": source_card.name,
                        "source_grp_id": obj.source_grp_id,
                        "ability_grp_id": obj.grp_id,
                        "parent_id": obj.parent_id,
                        "instance_id": iid,
                    }
                    event_type = None
                    if opp_seat and owner == opp_seat:
                        event_type = "opp_ability"
                        log.info("Opponent ability: %s", source_card.name)
                    elif my_seat and owner == my_seat:
                        event_type = "ability"
                        log.info("Player ability: %s", source_card.name)
                    if event_type:
                        save_match_event(
                            self.state.match_info.match_id, event_type,
                            game_number=self.state.match_info.game_number,
                            turn_number=self.state.turn_info.turn_number,
                            phase=self.state.turn_info.phase,
                            data=ability_data)
                        if self.on_stack_observed:
                            self.on_stack_observed(event_type, {
                                "name": source_card.name,
                                "colors": source_card.colors,
                            })
                    continue

                # --- Instant/Sorcery spell on stack ---
                card = card_cache.get(obj.grp_id)
                if not card or obj.grp_id <= 0:
                    continue
                if "Instant" not in (card.card_types or []) and \
                   "Sorcery" not in (card.card_types or []):
                    continue

                spell_data = {
                    "name": card.name,
                    "grp_id": obj.grp_id,
                    "card_types": card.card_types,
                    "colors": card.colors,
                    "cmc": card.cmc,
                    "oracle_text": (card.oracle_text[:200]
                                    if card.oracle_text else ""),
                    "instance_id": iid,
                }
                event_type = None
                if opp_seat and owner == opp_seat:
                    event_type = "opp_spell_cast"
                    log.info("Opponent spell cast: %s", card.name)
                elif my_seat and owner == my_seat:
                    event_type = "spell_cast"
                    log.info("Player spell cast: %s", card.name)
                if event_type:
                    save_match_event(
                        self.state.match_info.match_id, event_type,
                        game_number=self.state.match_info.game_number,
                        turn_number=self.state.turn_info.turn_number,
                        phase=self.state.turn_info.phase,
                        data=spell_data)
                    if self.on_stack_observed:
                        self.on_stack_observed(event_type, spell_data)

    def _parse_actions(self, actions_data: list[dict]):
        """Parse available actions."""
        self.state.available_actions.clear()
        self._last_auto_tap.clear()  # B5: reset per action batch
        for a in actions_data:
            seat = a.get("seatId", 0)
            action = a.get("action", {})
            auto_tap = action.get("autoTapActions")
            iid = action.get("instanceId")
            self.state.available_actions.append(Action(
                seat_id=seat,
                action_type=action.get("actionType", ""),
                instance_id=iid,
                grp_id=action.get("grpId"),
                mana_cost=action.get("manaCost"),
                ability_grp_id=action.get("abilityGrpId"),
                auto_tap_solution=auto_tap,
            ))
            # B5: store auto-tap for mana-spent tracking on card_played
            if iid and auto_tap:
                self._last_auto_tap[iid] = auto_tap

    def _handle_client_message(self, payload: dict):
        """Handle our own outgoing messages (for tracking)."""
        inner = payload.get("payload", {})
        msg_type = inner.get("type", "")
        if msg_type == "ClientMessageType_MulliganResp":
            decision = inner.get("mulliganResp", {}).get("decision", "")
            log.info("Mulligan decision: %s", decision)
            # Save mulligan event with hand contents
            mid = self.state.match_info.match_id
            if mid:
                hand = self.state.my_hand()
                hand_info = []
                for h_obj in hand:
                    h_card = card_cache.get(h_obj.grp_id)
                    hand_info.append({
                        "name": h_card.name if h_card else h_obj.name,
                        "cmc": h_card.cmc if h_card else 0,
                    })
                save_match_event(
                    mid, "mulligan",
                    game_number=self.state.match_info.game_number,
                    turn_number=0,
                    phase="mulligan",
                    data={"decision": decision,
                          "hand_size": len(hand),
                          "hand": hand_info})
        elif msg_type == "ClientMessageType_ChooseStartingPlayerResp":
            choice = inner.get("chooseStartingPlayerResp", {}).get("teamType", "")
            log.info("Starting player choice: %s", choice)
        # B2: block declarations
        elif msg_type == "ClientMessageType_DeclareBlockersResp":
            resp = inner.get("declareBlockersResp", {})
            blockers = resp.get("selectedBlockers", resp.get("blockers", []))
            mid = self.state.match_info.match_id
            if mid and blockers:
                for b in blockers:
                    blocker_iid = b.get("blockerInstanceId", 0)
                    attacker_iids = b.get("attackerInstanceIds", [])
                    blocker_obj = self.state.objects.get(blocker_iid)
                    blocker_card = card_cache.get(blocker_obj.grp_id) if blocker_obj else None
                    attacker_names = []
                    for a_iid in attacker_iids:
                        a_obj = self.state.objects.get(a_iid)
                        a_card = card_cache.get(a_obj.grp_id) if a_obj else None
                        attacker_names.append({
                            "name": a_card.name if a_card else (a_obj.name if a_obj else "?"),
                            "id": a_iid,
                            "power": a_obj.power if a_obj else 0,
                            "toughness": a_obj.toughness if a_obj else 0,
                        })
                    save_match_event(
                        mid, "block_declared",
                        game_number=self.state.match_info.game_number,
                        turn_number=self.state.turn_info.turn_number,
                        phase=self.state.turn_info.phase,
                        data={
                            "blocker": blocker_card.name if blocker_card else (
                                blocker_obj.name if blocker_obj else "?"),
                            "blocker_id": blocker_iid,
                            "blocker_power": blocker_obj.power if blocker_obj else 0,
                            "blocker_toughness": blocker_obj.toughness if blocker_obj else 0,
                            "attackers": attacker_names,
                        })
                log.info("Block declarations: %d blockers", len(blockers))
            elif mid:
                # No blockers declared — log that we chose not to block
                save_match_event(
                    mid, "block_declared",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data={"blocker": None, "no_blocks": True})

    def _resolve_removal_cause(self, removed_iid: int) -> dict | None:
        """B1: Try to determine what caused a creature to leave the battlefield.

        GRE annotations use 'affectorId' at top level (not in details).
        Relevant types: ZoneTransfer, DamageDealt, ResolutionComplete.

        For SBA_Damage deaths the ZoneTransfer has no affectorId — the
        creature's instance ID also changes (ObjectIdChanged) before the
        transfer.  We trace back: new_id → orig_id via ObjectIdChanged,
        then find DamageDealt on the original ID.
        """
        # Build ObjectIdChanged mappings: new_id → orig_id
        id_map: dict[int, int] = {}
        for ann in self._recent_annotations:
            ann_types = ann.get("type", [])
            if not isinstance(ann_types, list):
                ann_types = [ann_types]
            if any("ObjectIdChanged" in t for t in ann_types):
                for d in ann.get("details", []):
                    if d.get("key") == "orig_id":
                        orig = (d.get("valueInt32") or [0])[0]
                    elif d.get("key") == "new_id":
                        new = (d.get("valueInt32") or [0])[0]
                if orig and new:
                    id_map[new] = orig

        # IDs to search: the removed instance itself + its original ID
        search_ids = {removed_iid}
        if removed_iid in id_map:
            search_ids.add(id_map[removed_iid])

        for ann in self._recent_annotations:
            affected = ann.get("affectedIds", [])
            if not search_ids.intersection(affected):
                continue
            ann_types = ann.get("type", [])
            if not isinstance(ann_types, list):
                ann_types = [ann_types]

            # affectorId is the source instance at top level
            source_iid = ann.get("affectorId")
            if not source_iid:
                continue

            # Only care about zone transfers, damage, or resolution
            relevant = any(t in at for at in ann_types
                           for t in ("ZoneTransfer", "DamageDealt",
                                     "ResolutionComplete"))
            if not relevant:
                continue

            source_obj = self.state.objects.get(source_iid)
            source_card = card_cache.get(source_obj.grp_id) if source_obj else None
            source_name = (source_card.name if source_card
                           else source_obj.name if source_obj else "unknown")
            # Determine type: combat if source was attacking/blocking
            cause_type = "spell"
            if source_obj:
                if (source_obj.attack_state
                        and "Attacking" in str(source_obj.attack_state)):
                    cause_type = "combat"
                elif (source_obj.block_state
                      and "Blocking" in str(source_obj.block_state)):
                    cause_type = "combat"
                elif source_obj.object_type == "Ability":
                    cause_type = "ability"
            # SBA_Damage with a DamageDealt source → combat damage
            if (any("DamageDealt" in t for t in ann_types)
                    and cause_type == "spell"):
                cause_type = "combat"
            return {
                "name": source_name,
                "type": cause_type,
                "seat": source_obj.controller_seat_id if source_obj else 0,
            }
        return None

    def _seat_role(self, seat: int) -> str:
        if seat and seat == self.state.my_seat_id:
            return "me"
        if seat and seat == self.state.match_info.opponent_seat_id:
            return "opp"
        return "unknown"

    def _resolve_iid_to_card_name(self, iid: int) -> str | None:
        """Best-effort instanceId → card name. Used by annotation
        capture below. Returns None if unknown."""
        obj = self.state.objects.get(iid)
        if not obj:
            return None
        if obj.grp_id and obj.grp_id > 0:
            card = card_cache.get(obj.grp_id)
            if card:
                return card.name
        return obj.name or None

    def _ann_details_map(self, ann: dict) -> dict:
        """Flatten an annotation's `details` list of key/value pairs
        into a `{key: value}` dict (single-value scalars only)."""
        out = {}
        for d in ann.get("details") or []:
            key = d.get("key")
            if not key:
                continue
            for vk in ("valueInt32", "valueString", "valueDouble"):
                if vk in d:
                    arr = d[vk]
                    if isinstance(arr, list) and arr:
                        out[key] = arr[0]
                    break
        return out

    def _save_user_choice_annotations(self, annotations: list[dict]) -> None:
        """Persist annotations needed by the replay exporter to
        reconstruct per-action user choices: spell targets, X values,
        modal modes, life-cost mana payments. Saved as match_events
        with type=`annotation` and `data.kind` identifying the
        annotation subtype. Off by default (`save_match_event` is a
        no-op when match_id is missing).

        Resolved card names are attached when the affector/affected
        ids point to known game objects so the exporter doesn't need
        to re-walk the game-state DB.
        """
        mid = self.state.match_info.match_id
        if not mid:
            return
        for ann in annotations:
            ann_types = ann.get("type") or []
            kind = None
            if "AnnotationType_TargetSpec" in ann_types:
                kind = "target_spec"
            elif "AnnotationType_PlayerSubmittedTargets" in ann_types:
                kind = "submitted_targets"
            elif "AnnotationType_ManaPaid" in ann_types:
                kind = "mana_paid"
            elif "AnnotationType_UserActionTaken" in ann_types:
                kind = "user_action_taken"
            else:
                continue

            affector_id = ann.get("affectorId")
            affected_ids = ann.get("affectedIds") or []
            data = {
                "kind": kind,
                "ann_id": ann.get("id"),
                "affector_id": affector_id,
                "affected_ids": list(affected_ids),
                "details": self._ann_details_map(ann),
            }
            # Resolve names for downstream joining. Affector is
            # typically the spell/ability on stack; affected are
            # targets or the source of a payment. For
            # PlayerSubmittedTargets, affectorId is a player seat —
            # we don't resolve seats, but the affected_ids list
            # references the spell whose targets were just submitted.
            if kind != "submitted_targets" and affector_id:
                name = self._resolve_iid_to_card_name(affector_id)
                if name:
                    data["affector_name"] = name
            names = []
            for iid in affected_ids:
                n = self._resolve_iid_to_card_name(iid)
                if n:
                    names.append(n)
            if names:
                data["affected_names"] = names

            save_match_event(
                mid, "annotation",
                game_number=self.state.match_info.game_number,
                turn_number=self.state.turn_info.turn_number,
                phase=self.state.turn_info.phase,
                data=data)

    def _snapshot_battlefield(self, seat: int) -> list[dict]:
        cards: list[dict] = []
        for obj in self.state.objects_in_zone("ZoneType_Battlefield"):
            if obj.controller_seat_id != seat:
                continue
            card = card_cache.get(obj.grp_id)
            card_types = card.card_types if card else obj.card_types
            cards.append({
                "name": card.name if card else obj.name,
                "grp_id": obj.grp_id,
                "types": card_types,
                "subtypes": card.subtypes if card else obj.subtypes,
                "power": obj.power,
                "toughness": obj.toughness,
                "tapped": obj.is_tapped,
                "summoning_sick": obj.has_summoning_sickness,
            })
        cards.sort(key=lambda c: (0 if "Creature" in c["types"] else 1, c["name"]))
        return cards

    def _snapshot_hand(self, seat: int) -> list[dict]:
        cards: list[dict] = []
        for obj in self.state.objects_in_zone("ZoneType_Hand", seat):
            card = card_cache.get(obj.grp_id)
            cards.append({
                "name": card.name if card else obj.name,
                "grp_id": obj.grp_id,
                "cmc": card.cmc if card else 0,
                "types": card.card_types if card else obj.card_types,
                "mana_cost": card.mana_cost if card else "",
            })
        return cards

    def _snapshot_actions(self, seat: int) -> list[dict]:
        actions: list[dict] = []
        for action in self.state.available_actions:
            if action.seat_id != seat:
                continue
            obj = self.state.objects.get(action.instance_id or 0)
            card = card_cache.get(obj.grp_id) if obj else (
                card_cache.get(action.grp_id) if action.grp_id else None
            )
            actions.append({
                "action_type": action.action_type,
                "name": card.name if card else (obj.name if obj else ""),
                "grp_id": action.grp_id or (obj.grp_id if obj else 0),
                "instance_id": action.instance_id or 0,
                "ability_grp_id": action.ability_grp_id or 0,
            })
        return actions

    def _build_state_snapshot(self, include_actions: bool = True) -> dict:
        my_seat = self.state.my_seat_id
        opp_seat = self.state.match_info.opponent_seat_id
        me = self.state.my_player()
        opp = self.state.opp_player()
        my_battlefield = self._snapshot_battlefield(my_seat) if my_seat else []
        opp_battlefield = self._snapshot_battlefield(opp_seat) if opp_seat else []
        stack_cards = []
        for obj in self.state.stack():
            card = card_cache.get(obj.grp_id)
            stack_cards.append({
                "name": card.name if card else obj.name,
                "grp_id": obj.grp_id,
                "controller": self._seat_role(obj.controller_seat_id),
                "types": card.card_types if card else obj.card_types,
            })

        snapshot = {
            "my_life": me.life_total if me else None,
            "opp_life": opp.life_total if opp else None,
            "my_hand_size": len(self.state.my_hand()),
            "opp_hand_size": len(self.state.objects_in_zone("ZoneType_Hand", opp_seat)),
            "my_battlefield": my_battlefield,
            "opp_battlefield": opp_battlefield,
            "my_creatures": [c for c in my_battlefield if "Creature" in c["types"]],
            "opp_creatures": [c for c in opp_battlefield if "Creature" in c["types"]],
            "my_hand": self._snapshot_hand(my_seat) if my_seat else [],
            "stack": stack_cards,
        }
        if include_actions:
            snapshot["legal_actions"] = self._snapshot_actions(my_seat) if my_seat else []
        return snapshot

    def _save_decision_context(self, request_type: str):
        mid = self.state.match_info.match_id
        if not mid:
            return
        key = (self.state.game_state_id, request_type)
        if key == self._last_decision_snapshot_key:
            return
        self._last_decision_snapshot_key = key
        save_match_event(
            mid, "decision_context",
            game_number=self.state.match_info.game_number,
            turn_number=self.state.turn_info.turn_number,
            phase=self.state.turn_info.phase,
            data={
                "decision_id": f"{mid}_{self.state.match_info.game_number}_{self.state.game_state_id}",
                "request_type": request_type,
                "phase_display": self.state.turn_info.phase_display,
                "game_state_id": self.state.game_state_id,
                "my_seat_id": self.state.my_seat_id,
                "opp_seat_id": self.state.match_info.opponent_seat_id,
                "active_player": self.state.turn_info.active_player,
                "priority_player": self.state.turn_info.priority_player,
                "decision_player": self.state.turn_info.decision_player,
                "available_action_count": len(self.state.available_actions),
                "my_action_count": len([
                    a for a in self.state.available_actions
                    if a.seat_id == self.state.my_seat_id
                ]),
                **self._build_state_snapshot(include_actions=True),
            })

    def reset(self):
        """Reset state for a new match."""
        self.state = GameState()
        self._match_active = False
        self._last_logged_turn = 0
        self._recent_annotations = []
        self._attachment_map = {}
        self._last_auto_tap = {}
        self._seen_on_bf = set()
        self._seen_on_stack = set()
        self._pending_connect_context = {}
        self._last_decision_snapshot_key = (-1, "")


def _safe_int(val) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
