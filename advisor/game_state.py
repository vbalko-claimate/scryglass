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
        self._match_active = False
        self._last_logged_turn = 0
        # B1: recent annotations for death-cause resolution
        self._recent_annotations: list[dict] = []
        # B5: auto-tap solutions keyed by instance_id
        self._last_auto_tap: dict[int, dict] = {}
        # Track instance IDs already seen on battlefield to avoid
        # duplicate card_played events when a Full state snapshot
        # re-sends all objects (clearing self.state.objects loses
        # the old_obj needed for entering-battlefield detection).
        self._seen_on_bf: set[int] = set()
        self._seen_on_stack: set[int] = set()

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

            # Find our seat and opponent
            my_seat = 0
            opp_seat = 0
            opp_name = ""
            for p in config.get("reservedPlayers", []):
                if p.get("userId") == self.state.match_info.match_id:
                    continue
                # We detect our seat from ConnectResp, but set opp info here
                for pl in players:
                    if pl.get("userId") != p.get("userId"):
                        continue

            # Store player names for later
            self._player_names = {
                p.get("systemSeatId", 0): p.get("playerName", "")
                for p in players
            }

            # Find our account name and opponent from reservedPlayers (has userId)
            reserved = config.get("reservedPlayers", [])
            self._all_reserved = reserved
            my_user_id = getattr(self, "_my_user_id", None)

            # Extract opponent name directly from reservedPlayers
            self._opp_name_from_room = ""
            self._my_account_name = ""
            for p in reserved:
                if my_user_id and p.get("userId") == my_user_id:
                    self._my_account_name = p.get("playerName", "")
                elif my_user_id and p.get("userId") != my_user_id:
                    self._opp_name_from_room = p.get("playerName", "")

            self.state.match_info.match_id = match_id
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
        elif msg_type == "GREMessageType_GameStateMessage":
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
        self.state.annotations = gsm.get("annotations", [])

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
                        who = "me" if seat == self.state.my_seat_id else "opp"
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

        # Log start of my turn for mana tracking
        if ti:
            new_turn = ti.get("turnNumber", 0)
            new_active = ti.get("activePlayer", 0)
            if (new_turn > 0 and new_active == self.state.my_seat_id
                    and new_turn != self._last_logged_turn
                    and self.state.match_info.match_id):
                self._last_logged_turn = new_turn
                lands = self.state.my_lands()
                me = self.state.my_player()
                opp = self.state.opp_player()
                # B4: board state snapshot
                my_creatures = []
                for o in self.state.my_creatures():
                    c = card_cache.get(o.grp_id)
                    my_creatures.append({
                        "name": c.name if c else o.name,
                        "power": o.power, "toughness": o.toughness,
                        "tapped": o.is_tapped,
                    })
                opp_creatures = []
                for o in self.state.opp_creatures():
                    c = card_cache.get(o.grp_id)
                    opp_creatures.append({
                        "name": c.name if c else o.name,
                        "power": o.power, "toughness": o.toughness,
                        "tapped": o.is_tapped,
                    })
                save_match_event(
                    self.state.match_info.match_id, "turn_start",
                    game_number=self.state.match_info.game_number,
                    turn_number=new_turn,
                    phase="turn_start",
                    data={"available_mana": len(lands),
                          "total_lands": len(lands),
                          "my_creatures": my_creatures,
                          "opp_creatures": opp_creatures,
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
            attached_to_id=None,  # B3: resolved via annotations, not object field
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

        # --- Stack zone detection for instants/sorceries ---
        on_stack = obj_zone and obj_zone.type == "ZoneType_Stack"
        was_on_stack = False
        if old_obj:
            old_zone_stack = self.state.zones.get(old_obj.zone_id)
            was_on_stack = old_zone_stack and old_zone_stack.type == "ZoneType_Stack"

        entering_stack = on_stack and not was_on_stack

        # Guard against duplicate events from Full state snapshots
        if entering_stack and iid in self._seen_on_stack:
            entering_stack = False
        if on_stack:
            self._seen_on_stack.add(iid)
        else:
            self._seen_on_stack.discard(iid)

        # Log instants/sorceries entering the stack (they never touch the battlefield)
        is_instant_or_sorcery = (card and
            ("Instant" in (card.card_types or []) or
             "Sorcery" in (card.card_types or [])))

        if (entering_stack and is_instant_or_sorcery and grp_id > 0
                and self.state.match_info.match_id and card):
            spell_data = {
                "name": card.name,
                "grp_id": grp_id,
                "card_types": card.card_types,
                "colors": card.colors,
                "cmc": card.cmc,
                "oracle_text": (card.oracle_text[:200]
                                if card.oracle_text else ""),
            }
            if opp_seat and owner == opp_seat:
                save_match_event(
                    self.state.match_info.match_id, "opp_spell_cast",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data=spell_data)
            elif my_seat and owner == my_seat:
                save_match_event(
                    self.state.match_info.match_id, "spell_cast",
                    game_number=self.state.match_info.game_number,
                    turn_number=self.state.turn_info.turn_number,
                    phase=self.state.turn_info.phase,
                    data=spell_data)

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
                      "colors": card.colors})

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
                         "is_land": card.is_land}
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

            who = "me" if owner == my_seat else "opp"
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
            return {"name": source_name, "type": cause_type}
        return None

    def reset(self):
        """Reset state for a new match."""
        self.state = GameState()
        self._match_active = False
        self._last_logged_turn = 0
        self._recent_annotations = []
        self._last_auto_tap = {}
        self._seen_on_bf = set()
        self._seen_on_stack = set()


def _safe_int(val) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
