"""FastAPI server with WebSocket for real-time game state + advice."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .advisor_engine import AdvisorEngine, generate_match_summary
from .database import (
    backup_db, card_cache, clear_match_events, get_match_history,
    get_match_life_graph, get_observed_opp_decks, get_stats_compliance,
    get_stats_decks, get_stats_mana_curve, get_stats_matchups,
    get_stats_mulligan, get_stats_my_cards, get_stats_opp_cards,
    get_stats_color_matchups, get_stats_opponents, get_stats_overview,
    get_stats_recent_trend, get_stats_weaknesses, get_match_timeline,
    import_cards_from_mtga, init_db,
)
from .game_state import GameStateTracker
from .llm_advisor import get_backend, set_backend
from .log_watcher import LogWatcher
from .models import Advice, GameState

log = logging.getLogger(__name__)

app = FastAPI(title="MTGA Advisor")

# Static files
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Global state
tracker = GameStateTracker()
advisor = AdvisorEngine()
watcher = LogWatcher()
clients: list[WebSocket] = []


async def broadcast(msg: dict):
    """Send message to all connected WebSocket clients."""
    dead = []
    text = json.dumps(msg, default=str)
    for ws in clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


def state_to_dict(state: GameState) -> dict:
    """Convert game state to a JSON-serializable dict for the UI."""
    me = state.my_player()
    opp = state.opp_player()

    # Hand cards with resolved names
    hand = []
    for obj in state.my_hand():
        card = card_cache.get(obj.grp_id)
        hand.append({
            "instance_id": obj.instance_id,
            "grp_id": obj.grp_id,
            "name": card.name if card else obj.name,
            "mana_cost": card.mana_cost if card else "",
            "cmc": card.cmc if card else 0,
            "card_types": card.card_types if card else [],
            "power": card.power if card else "",
            "toughness": card.toughness if card else "",
            "colors": card.colors if card else [],
            "abilities": card.abilities if card else [],
        })

    # Battlefields
    def bf_cards(bf_list):
        result = []
        for obj in bf_list:
            card = card_cache.get(obj.grp_id)
            result.append({
                "instance_id": obj.instance_id,
                "grp_id": obj.grp_id,
                "name": card.name if card else obj.name,
                "mana_cost": card.mana_cost if card else "",
                "card_types": card.card_types if card else [],
                "power": obj.power,
                "toughness": obj.toughness,
                "colors": card.colors if card else [],
                "is_tapped": obj.is_tapped,
                "has_summoning_sickness": obj.has_summoning_sickness,
                "is_creature": obj.is_creature,
                "is_land": obj.is_land,
                "abilities": card.abilities if card else [],
            })
        return result

    return {
        "match_id": state.match_info.match_id,
        "game_number": state.match_info.game_number,
        "stage": state.match_info.stage,
        "opponent_name": state.match_info.opponent_name,
        "my_seat_id": state.my_seat_id,
        "turn": {
            "number": state.turn_info.turn_number,
            "phase": state.turn_info.phase,
            "step": state.turn_info.step,
            "phase_display": state.turn_info.phase_display,
            "active_player": state.turn_info.active_player,
            "priority_player": state.turn_info.priority_player,
            "is_my_turn": state.turn_info.active_player == state.my_seat_id,
        },
        "my_life": me.life_total if me else 0,
        "opp_life": opp.life_total if opp else 0,
        "hand": hand,
        "my_battlefield": bf_cards(state.my_battlefield()),
        "opp_battlefield": bf_cards(state.opp_battlefield()),
        "my_graveyard_count": len(state.my_graveyard()),
        "opp_graveyard_count": len(state.opp_graveyard()),
        "my_library_count": len(
            (state.zone_by_type("ZoneType_Library", state.my_seat_id) or type("Z", (), {"object_instance_ids": []})).object_instance_ids
        ),
        "stack_count": len(state.stack()),
        "pending_request": state.pending_request,
        "game_state_id": state.game_state_id,
    }


def advice_to_list(advice_list: list[Advice]) -> list[dict]:
    return [asdict(a) for a in advice_list]


def on_state_change(state: GameState):
    """Callback when game state changes — run advice immediately, broadcast async."""
    # Broadcast state update (fire and forget)
    asyncio.get_event_loop().create_task(
        broadcast({"type": "state_update", "data": state_to_dict(state)})
    )
    # Run advisor synchronously so advice is ready before next message
    asyncio.get_event_loop().create_task(_run_advice(state))


async def _run_advice(state: GameState):
    """Run advisor and broadcast advice immediately."""
    await advisor.on_state_change(state)


def on_decision_point(state: GameState, request_type: str):
    """Callback when a decision point is reached — advice runs inline."""
    asyncio.get_event_loop().create_task(
        _handle_decision(state, request_type)
    )


async def _handle_decision(state: GameState, request_type: str):
    # Run advisor FIRST, then broadcast both
    await advisor.on_decision_point(state, request_type)
    await broadcast({
        "type": "decision_point",
        "data": {
            "request_type": request_type,
            "state": state_to_dict(state),
        }
    })


def on_match_start():
    """Callback when a new match starts — reset advisor + clear UI."""
    advisor.on_match_start()
    asyncio.get_event_loop().create_task(
        broadcast({"type": "match_start"})
    )


def on_advice(advice_list: list[Advice]):
    """Callback when new advice is generated."""
    asyncio.get_event_loop().create_task(
        broadcast({"type": "advice", "data": advice_to_list(advice_list)})
    )


def on_strategy_info(info: dict):
    """Callback when strategy is loaded or opponent identified."""
    asyncio.get_event_loop().create_task(
        broadcast({"type": "strategy_info", "data": info})
    )


def on_threat_update(threats: list[dict]):
    """Callback when threat assessments change."""
    asyncio.get_event_loop().create_task(
        broadcast({"type": "threat_assessment", "data": threats})
    )


def _backfill_opp_decks():
    """Fill missing opp_deck_name from match event data."""
    from .database import get_connection
    from .strategy import load_meta_decks, OpponentTracker, MetaDeck
    import json as _json

    conn = get_connection()
    cur = conn.cursor()

    # Find matches missing opp_deck_name that have opp card events
    cur.execute("""
        SELECT DISTINCT e.match_id
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'opp_card_played'
          AND (m.opp_deck_name IS NULL OR m.opp_deck_name = '')
    """)
    missing = [r[0] for r in cur.fetchall()]
    if not missing:
        conn.close()
        return

    # Load global meta_decks + observed data
    all_meta: list[MetaDeck] = load_meta_decks()
    existing_names = {md.name for md in all_meta}
    for od in get_observed_opp_decks():
        if od["name"] not in existing_names:
            all_meta.append(MetaDeck(
                name=od["name"], archetype=od["archetype"],
                colors=od["colors"], signal_cards=od["signal_cards"]))

    if not all_meta:
        conn.close()
        return

    # Get opp cards for missing matches
    placeholders = ",".join("?" for _ in missing)
    cur.execute(f"""
        SELECT match_id, json_extract(data, '$.name'), json_extract(data, '$.colors')
        FROM match_events
        WHERE event_type = 'opp_card_played' AND match_id IN ({placeholders})
    """, missing)

    trackers: dict[str, OpponentTracker] = {}
    for mid, name, colors_str in cur.fetchall():
        if not name:
            continue
        if mid not in trackers:
            trackers[mid] = OpponentTracker()
        cols = _json.loads(colors_str) if colors_str else []
        trackers[mid].observe(name, cols)

    updates = 0
    for mid, tracker in trackers.items():
        result = tracker.identify(all_meta)
        if result:
            cur.execute(
                "UPDATE matches SET opp_deck_name = ? WHERE match_id = ?",
                (result.name, mid))
            updates += 1

    conn.commit()
    conn.close()
    if updates:
        log.info("Backfilled opp_deck_name for %d matches", updates)


@app.on_event("startup")
async def startup():
    """Initialize DB, load cards, start log watcher."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Init DB & backup
    init_db()
    backup_db()

    # Import cards if needed
    if card_cache.size == 0:
        log.info("Importing cards from MTGA database...")
        count = import_cards_from_mtga()
        log.info("Imported %d cards", count)

    card_cache.load()
    log.info("Card cache loaded: %d cards", card_cache.size)

    # Set up callbacks
    tracker.on_state_change = on_state_change
    tracker.on_decision_point = on_decision_point
    tracker.on_match_start = on_match_start
    tracker.on_match_end = advisor.on_match_end
    advisor.on_advice = on_advice
    advisor.on_strategy_info = on_strategy_info
    advisor.on_threat_update = on_threat_update
    tracker.on_my_card_played = advisor.check_card_played

    # Catch up on current log (clear events first to avoid duplicates on restart)
    log.info("Reading existing log...")
    clear_match_events()
    messages = watcher.read_from_beginning()
    for msg in messages:
        tracker.process_message(msg)
    log.info("Processed %d existing messages. Match active: %s",
             len(messages), tracker.match_active)

    # Backfill opponent deck names from match event data
    _backfill_opp_decks()

    # Start watching for new messages
    asyncio.create_task(watcher.watch(tracker.process_message))
    log.info("Log watcher started. Listening for new messages...")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    log.info("WebSocket client connected (%d total)", len(clients))

    # Send current state immediately
    await ws.send_text(json.dumps({
        "type": "state_update",
        "data": state_to_dict(tracker.state),
    }, default=str))

    # Send last advice
    if advisor.last_advice:
        await ws.send_text(json.dumps({
            "type": "advice",
            "data": advice_to_list(advisor.last_advice),
        }, default=str))

    # Send active threats
    if advisor.active_threats:
        await ws.send_text(json.dumps({
            "type": "threat_assessment",
            "data": advisor.active_threats,
        }, default=str))

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("action") == "ask_llm":
                advice = await advisor.ask_llm(tracker.state)
                if advice:
                    await ws.send_text(json.dumps({
                        "type": "advice",
                        "data": advice_to_list(advisor.last_advice),
                    }, default=str))

            elif msg.get("action") == "match_summary":
                summary = await advisor.match_summary(tracker.state)
                if summary:
                    await ws.send_text(json.dumps({
                        "type": "advice",
                        "data": advice_to_list([summary]),
                    }, default=str))

            elif msg.get("action") == "set_backend":
                set_backend(msg.get("backend", "claude_cli"))
                await ws.send_text(json.dumps({
                    "type": "backend_changed",
                    "data": {"backend": get_backend()},
                }, default=str))

    except WebSocketDisconnect:
        clients.remove(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(clients))


@app.get("/api/history")
async def match_history():
    return get_match_history()


@app.get("/api/state")
async def current_state():
    return state_to_dict(tracker.state)


@app.get("/api/backend")
async def llm_backend():
    return {"backend": get_backend(), "available": ["claude_cli", "ollama", "anthropic_api"]}


@app.get("/api/debug/test-threats")
async def test_threats():
    """Simulate threat broadcast for UI testing."""
    threats = [
        {
            "instance_id": 1, "name": "The One Ring",
            "type_line": "Legendary Artifact", "mana_cost": "o4",
            "danger": 5, "summary": "Protection + massive card draw, snowballs every turn",
            "priority": "must-remove", "source": "llm",
        },
        {
            "instance_id": 2, "name": "Agatha's Soul Cauldron",
            "type_line": "Legendary Artifact", "mana_cost": "o2",
            "danger": 4, "summary": "Exiles from graveyard; gives abilities to all your creatures",
            "priority": "should-remove", "source": "llm",
        },
        {
            "instance_id": 3, "name": "Insidious Roots",
            "type_line": "Enchantment", "mana_cost": "oBG",
            "danger": 3, "summary": "Creates tokens whenever creature leaves graveyard",
            "priority": "should-remove", "source": "heuristic",
        },
        {
            "instance_id": 4, "name": "Relic of Progenitus",
            "type_line": "Artifact", "mana_cost": "o1",
            "danger": 2, "summary": "Graveyard hate, minor nuisance",
            "priority": "monitor", "source": "llm",
        },
    ]
    await broadcast({"type": "threat_assessment", "data": threats})
    await broadcast({"type": "strategy_info", "data": {
        "strategy_name": "Boros Tokens v1", "archetype": "aggro",
        "rule_count": 49, "meta_deck_count": 7,
        "opp_deck": "Golgari Midrange", "opp_confidence": 72,
        "opp_archetype": "midrange", "opp_speed": "medium",
        "opp_kill_turn": 8, "opp_hidden_reach": 5,
        "opp_key_threats": ["Sheoldred", "Graveyard Trespasser"],
        "opp_cards_seen": ["Llanowar Wastes", "Cut Down", "Sheoldred"],
    }})
    return {"status": "ok", "threats_sent": len(threats)}


@app.get("/api/debug/test-compliance")
async def test_compliance():
    """Simulate advice + card play to verify compliance tracking."""
    from .database import save_match_event, get_stats_compliance
    # Simulate: advisor recommends "Ajani's Pridemate"
    advisor._pending_recs = ["Ajani's Pridemate", "Leonin Vanguard"]
    advisor._pending_turn = 3
    test_match = tracker.state.match_info.match_id or "test-compliance"

    # Case 1: Player follows advice
    advisor.check_card_played("Ajani's Pridemate", test_match, 3, 1)

    # Case 2: Player ignores advice
    advisor._pending_recs = ["Banishing Light"]
    advisor._pending_turn = 5
    advisor.check_card_played("Healer's Hawk", test_match, 5, 1)

    stats = get_stats_compliance()
    return {"status": "ok", "compliance_stats": stats}


@app.get("/api/debug/card/{grp_id}")
async def debug_card(grp_id: int):
    card = card_cache.get(grp_id)
    if not card:
        return {"error": f"Card {grp_id} not found"}
    return {
        "grp_id": card.grp_id,
        "name": card.name,
        "card_types": card.card_types,
        "mana_cost": card.mana_cost,
        "colors": card.colors,
        "abilities": card.abilities,
    }


# ─── Statistics ──────────────────────────────────────────────────

@app.get("/stats")
async def stats_page():
    return FileResponse(str(STATIC_DIR / "stats.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/stats/overview")
async def stats_overview():
    return get_stats_overview()


@app.get("/api/stats/decks")
async def stats_decks():
    return get_stats_decks()


@app.get("/api/stats/matchups")
async def stats_matchups():
    return get_stats_matchups()


@app.get("/api/stats/color-matchups")
async def stats_color_matchups():
    return get_stats_color_matchups()


@app.get("/api/stats/opponents")
async def stats_opponents():
    return get_stats_opponents()


@app.get("/api/stats/trend")
async def stats_trend():
    return get_stats_recent_trend()


@app.get("/api/stats/life/{match_id}")
async def stats_life_graph(match_id: str):
    return get_match_life_graph(match_id)


@app.get("/api/stats/opp-cards")
async def stats_opp_cards():
    return get_stats_opp_cards()


@app.get("/api/stats/mulligan")
async def stats_mulligan():
    return get_stats_mulligan()


@app.get("/api/stats/my-cards")
async def stats_my_cards():
    return get_stats_my_cards()


@app.get("/api/stats/mana-curve")
async def stats_mana_curve():
    return get_stats_mana_curve()


@app.get("/api/stats/compliance")
async def stats_compliance():
    return get_stats_compliance()


@app.get("/api/stats/weaknesses")
async def stats_weaknesses():
    return get_stats_weaknesses()


@app.get("/api/match-summary/{match_id}")
async def match_summary_endpoint(match_id: str):
    """Generate or return cached LLM summary for a historical match."""
    summary = await generate_match_summary(match_id)
    return {"match_id": match_id, "summary": summary}


@app.get("/api/match-timeline/{match_id}")
async def match_timeline_endpoint(match_id: str):
    """Get structured turn-by-turn timeline for a match."""
    return get_match_timeline(match_id)
