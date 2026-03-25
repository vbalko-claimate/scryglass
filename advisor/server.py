"""FastAPI server with WebSocket for real-time game state + advice."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fastapi.responses import JSONResponse, PlainTextResponse

from .advisor_engine import AdvisorEngine, generate_match_summary
from .database import (
    backup_db, card_cache, clear_match_events, get_match_history,
    get_match_life_graph, get_observed_opp_decks, get_stats_compliance,
    get_stats_decks, get_stats_mana_curve, get_stats_matchups,
    get_stats_mulligan, get_stats_my_cards, get_stats_opp_cards,
    get_stats_color_matchups, get_stats_opponents, get_stats_overview,
    get_stats_recent_trend, get_stats_weaknesses, get_match_timeline,
    import_cards_from_mtga, init_db, USER_DATA_DIR,
)
from .game_state import GameStateTracker
from .llm_advisor import get_backend, set_backend
from .log_watcher import LogWatcher
from .models import Advice, GameState
from .reporting import build_match_report, get_latest_completed_match_id
from .deck_routes import router as deck_router
from .strategy import (
    RULES_DIR, DECKS_ROOT, META_DECKS_PATH,
    load_meta_decks, load_strategy, _load_strategy_file,
    _all_strategy_paths,
)
from . import deck_storage

log = logging.getLogger(__name__)

ARCHIVE_DIR = Path(__file__).parent.parent / "data" / "log_archive"


def _archive_player_log(log_path: Path):
    """Copy Player.log to archive dir so raw GRE data survives MTGA restarts."""
    if not log_path.exists():
        return
    import shutil
    from datetime import datetime
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / f"Player_{stamp}.log"
    # Skip if file is tiny (empty session) or already archived this minute
    size = log_path.stat().st_size
    if size < 1000:
        return
    # Avoid duplicate archives
    existing = sorted(ARCHIVE_DIR.glob("Player_*.log"))
    if existing:
        last = existing[-1]
        if last.stat().st_size == size:
            return  # same size = same log, already archived
    shutil.copy2(log_path, dest)
    log.info("Archived Player.log (%d KB) -> %s", size // 1024, dest.name)

    # Keep only last 50 archives
    archives = sorted(ARCHIVE_DIR.glob("Player_*.log"))
    for old in archives[:-50]:
        old.unlink()


app = FastAPI(title="MTGA Advisor")
app.include_router(deck_router)


@app.get("/health")
async def health():
    """Health check for sidecar readiness."""
    from .version import ENGINE_VERSION
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "card_count": card_cache.size,
        "match_active": tracker.match_active,
        "ws_clients": len(clients),
    }


@app.get("/match-status")
async def match_status():
    """Is a match currently active? Used by overlay to show/hide."""
    return {"active": tracker.match_active}


@app.get("/api/review/matches")
async def review_match_list():
    """List recent matches for review selection."""
    from .database import get_connection
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT match_id, my_deck_name, opp_deck_name, result, started_at
            FROM matches
            WHERE result != ''
            ORDER BY started_at DESC
            LIMIT 20
        """).fetchall()
        return [
            {"match_id": r[0], "my_deck": r[1], "opp_deck": r[2], "result": r[3], "started": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


@app.get("/api/review/latest")
async def latest_match_review():
    """Review the most recent completed match."""
    from .database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT match_id FROM matches WHERE result != '' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return JSONResponse({"error": "No completed matches"}, status_code=404)
        match_id = row[0]
    finally:
        conn.close()
    return await match_review_detail(match_id)


@app.get("/api/review/{match_id}")
async def match_review_detail(match_id: str):
    """Post-game review — key advice per turn for a match."""
    from .database import get_connection
    conn = get_connection()
    try:
        # Match info
        match_row = conn.execute(
            "SELECT match_id, my_deck_name, opp_deck_name, result, started_at, ended_at "
            "FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not match_row:
            return JSONResponse({"error": "Match not found"}, status_code=404)

        match_info = {
            "match_id": match_row[0],
            "my_deck": match_row[1],
            "opp_deck": match_row[2],
            "result": match_row[3],
            "started": match_row[4],
            "ended": match_row[5],
        }

        # Advice grouped by turn
        rows = conn.execute("""
            SELECT turn_number, phase, source, priority, message, details
            FROM advice_log
            WHERE match_id = ?
            AND source IN ('heuristic', 'strategy')
            AND priority IN ('critical', 'high', 'medium')
            ORDER BY turn_number, timestamp
        """, (match_id,)).fetchall()

        turns: dict[int, list] = {}
        for turn, phase, source, priority, message, details in rows:
            turns.setdefault(turn, []).append({
                "phase": phase,
                "source": source,
                "priority": priority,
                "message": message,
                "details": details or "",
            })

        # Key moments — turns with critical/high advice
        key_moments = []
        for turn_num, advices in sorted(turns.items()):
            critical = [a for a in advices if a["priority"] in ("critical", "high")]
            if critical:
                key_moments.append({
                    "turn": turn_num,
                    "advice": critical[:3],
                    "all_advice_count": len(advices),
                })

        return {
            "match": match_info,
            "turns": {str(k): v for k, v in sorted(turns.items())},
            "key_moments": key_moments,
            "total_turns": max(turns.keys()) if turns else 0,
            "total_advice": sum(len(v) for v in turns.values()),
        }
    finally:
        conn.close()


@app.get("/review")
async def review_page():
    return FileResponse(str(STATIC_DIR / "review.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

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
    state_snapshot = copy.deepcopy(state)
    # Broadcast state update (fire and forget)
    asyncio.get_event_loop().create_task(
        broadcast({"type": "state_update", "data": state_to_dict(state_snapshot)})
    )
    # Run advisor synchronously so advice is ready before next message
    asyncio.get_event_loop().create_task(_run_advice(state_snapshot))


async def _run_advice(state: GameState):
    """Run advisor and broadcast advice immediately."""
    await advisor.on_state_change(state)


def on_decision_point(state: GameState, request_type: str):
    """Callback when a decision point is reached — advice runs inline."""
    state_snapshot = copy.deepcopy(state)
    asyncio.get_event_loop().create_task(
        _handle_decision(state_snapshot, request_type)
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


def on_llm_status(status: dict):
    """Callback when LLM pending/ready state changes."""
    asyncio.get_event_loop().create_task(
        broadcast({"type": "llm_status", "data": status})
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

    # Export card cache to JSON for Forge/Docker (no MTGA DB there)
    if card_cache.size > 0:
        exported = card_cache.export_json()
        log.info("Card cache exported to JSON: %d cards", exported)

    # Migrate decks from SQLite to filesystem (one-time)
    from . import deck_storage
    db_path = Path(__file__).parent.parent / "data" / "advisor.db"
    migrated = deck_storage.migrate_from_db(db_path)
    if migrated:
        log.info("Migrated %d decks from SQLite to filesystem", migrated)

    # Set up callbacks
    tracker.on_state_change = on_state_change
    tracker.on_decision_point = on_decision_point
    tracker.on_match_start = on_match_start
    def on_match_end_handler(won: bool):
        advisor.on_match_end(won)
        result = "Win" if won else "Loss"
        asyncio.get_event_loop().create_task(
            broadcast({"type": "match_end", "data": {"result": result}})
        )
    tracker.on_match_end = on_match_end_handler
    advisor.on_advice = on_advice
    advisor.on_strategy_info = on_strategy_info
    advisor.on_threat_update = on_threat_update
    advisor.on_llm_status = on_llm_status
    tracker.on_my_card_played = advisor.check_card_played
    tracker.on_stack_observed = advisor.on_stack_observed

    # Archive Player.log (and Player-prev.log) before processing
    _archive_player_log(watcher.log_path)
    prev_log = watcher.log_path.parent / "Player-prev.log"
    _archive_player_log(prev_log)

    # Catch up on current log — resume from last known position
    # IMPORTANT: Disable auto-LLM during replay to prevent hundreds of Claude CLI calls
    prev_auto_llm = advisor.auto_llm_enabled
    advisor.set_auto_llm(False)

    # Get saved log position from DB (if any)
    from .database import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key = 'log_position'")
    row = cur.fetchone()
    resume_pos = int(row[0]) if row else 0
    conn.close()

    clear_match_events()
    log.info("Reading log from position %d (LLM disabled during replay)...", resume_pos)
    messages = watcher.read_from_beginning(resume_position=resume_pos)
    for msg in messages:
        tracker.process_message(msg)
    advisor.set_auto_llm(prev_auto_llm)
    log.info("Processed %d new messages. Match active: %s",
             len(messages), tracker.match_active)

    # Save current position for next startup
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('log_position', ?)",
        (str(watcher._position),))
    conn.commit()
    conn.close()

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

    await ws.send_text(json.dumps({
        "type": "backend_changed",
        "data": {"backend": get_backend()},
    }, default=str))
    await ws.send_text(json.dumps({
        "type": "llm_auto_changed",
        "data": {
            "enabled": advisor.auto_llm_enabled,
            "mode": advisor.advice_mode,
            "scope": advisor.llm_scope,
        },
    }, default=str))
    await ws.send_text(json.dumps({
        "type": "strategy_info",
        "data": advisor.current_strategy_info(),
    }, default=str))
    await ws.send_text(json.dumps({
        "type": "llm_status",
        "data": advisor.llm_status,
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

            elif msg.get("action") == "toggle_llm":
                advisor.set_auto_llm(bool(msg.get("enabled")))
                await broadcast({
                    "type": "llm_auto_changed",
                    "data": {
                        "enabled": advisor.auto_llm_enabled,
                        "mode": advisor.advice_mode,
                        "scope": advisor.llm_scope,
                    },
                })

            elif msg.get("action") == "set_backend":
                set_backend(msg.get("backend", "claude_cli"))
                await broadcast({
                    "type": "backend_changed",
                    "data": {"backend": get_backend()},
                })

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
    return {
        "backend": get_backend(),
        "available": ["claude_cli", "ollama", "anthropic_api"],
        "auto_llm": advisor.auto_llm_enabled,
        "advice_mode": advisor.advice_mode,
        "llm_scope": advisor.llm_scope,
    }


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


@app.get("/api/match-report/latest")
async def match_report_latest_endpoint():
    """Download a deterministic report for the latest completed match."""
    match_id = get_latest_completed_match_id()
    if not match_id:
        raise HTTPException(status_code=404, detail="No completed match found")
    filename, report = build_match_report(match_id)
    return PlainTextResponse(
        report,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Match-Id": match_id,
        },
    )


@app.get("/api/match-report/{match_id}")
async def match_report_endpoint(match_id: str):
    """Download a deterministic report for a specific match."""
    try:
        filename, report = build_match_report(match_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        report,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Match-Id": match_id,
        },
    )


@app.get("/api/match-timeline/{match_id}")
async def match_timeline_endpoint(match_id: str):
    """Get structured turn-by-turn timeline for a match."""
    return get_match_timeline(match_id)


# ─── Management API ─────────────────────────────────────────────

@app.get("/manage")
async def manage_page():
    return FileResponse(str(STATIC_DIR / "manage.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/decks")
async def decks_page():
    return FileResponse(str(STATIC_DIR / "decks.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/setup")
async def setup_page():
    return FileResponse(str(STATIC_DIR / "setup.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/overlay")
async def overlay_page():
    return FileResponse(str(STATIC_DIR / "overlay.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/manage/strategies")
async def manage_strategies():
    """List all strategies from deck dirs and built-in."""
    strategies = []
    seen_names = set()
    for path in _all_strategy_paths():
        try:
            data = json.loads(path.read_text())
            name = data.get("name", path.stem)
            if name in seen_names:
                continue
            seen_names.add(name)
            # deck dirs → user, RULES_DIR → builtin
            is_user = str(path).startswith(str(DECKS_ROOT))
            deck_id = path.parent.name if is_user else ""
            has_deck = (path.parent / "deck.json").exists() if is_user else False
            strategies.append({
                "name": name,
                "file": path.name,
                "deck_id": deck_id,
                "has_deck": has_deck,
                "source": "user" if is_user else "builtin",
                "archetype": data.get("archetype", "unknown"),
                "colors": data.get("colors", []),
                "deck_signature": data.get("deck_signature", []),
                "rule_count": len(data.get("rules", [])),
                "stats": data.get("stats", {}),
            })
        except Exception:
            continue
    return strategies


@app.get("/api/manage/strategy/{deck_id}")
async def manage_strategy_detail(deck_id: str):
    """Get full strategy JSON for a deck."""
    # Check deck dir first
    path = DECKS_ROOT / deck_id / "strategy.json"
    if path.exists():
        return json.loads(path.read_text())
    # Fallback to built-in
    path = RULES_DIR / f"{deck_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return JSONResponse({"error": "not found"}, status_code=404)


@app.put("/api/manage/strategy/{deck_id}")
async def manage_strategy_save(deck_id: str, request: dict):
    """Save strategy JSON to deck dir."""
    path = DECKS_ROOT / deck_id / "strategy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request, indent=2, ensure_ascii=False))
    log.info("Strategy saved via manage UI: %s", path)
    return {"status": "ok", "path": str(path)}


@app.delete("/api/manage/strategy/{deck_id}")
async def manage_strategy_delete(deck_id: str):
    """Delete a stub strategy (deck dir without deck.json)."""
    deck_dir = DECKS_ROOT / deck_id
    if not deck_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if (deck_dir / "deck.json").exists():
        return JSONResponse({"error": "use /api/decks/{id} to delete managed decks"}, status_code=400)
    import shutil
    shutil.rmtree(deck_dir)
    log.info("Deleted stub strategy: %s", deck_id)
    return {"status": "ok", "deleted": deck_id}


@app.get("/api/manage/ga-runs")
async def manage_ga_runs():
    """List all GA runs across all decks, with latest generation stats."""
    runs = []
    if not DECKS_ROOT.exists():
        return runs
    for deck_dir in sorted(DECKS_ROOT.iterdir()):
        ga_dir = deck_dir / "ga_logs"
        if not ga_dir.exists():
            continue
        for log_file in sorted(ga_dir.glob("*.ga_log.json")):
            try:
                data = json.loads(log_file.read_text())
                if not isinstance(data, list) or not data:
                    continue
                last = data[-1]
                first = data[0]
                runs.append({
                    "deck_id": deck_dir.name,
                    "file": log_file.name,
                    "generations": len(data),
                    "best_fitness": round(last.get("best_fitness", 0), 4),
                    "avg_fitness": round(last.get("avg_fitness", 0), 4),
                    "best_record": last.get("best_record", ""),
                    "elapsed_h": round(sum(e.get("elapsed_s", 0) for e in data) / 3600, 1),
                    "matchups": last.get("best_matchups", {}),
                    "started": first.get("timestamp", ""),
                    "status": "completed",
                })
            except Exception:
                continue

        # Check for live status.json (written by GA runner during execution)
        status_file = ga_dir / "status.json"
        if status_file.exists():
            try:
                status = json.loads(status_file.read_text())
                # If status is newer than last log entry, GA is running
                runs.append({
                    "deck_id": deck_dir.name,
                    "file": status.get("run_name", "live"),
                    "generations": status.get("generation", 0),
                    "best_fitness": round(status.get("best_fitness", 0), 4),
                    "avg_fitness": round(status.get("avg_fitness", 0), 4),
                    "best_record": status.get("best_record", ""),
                    "elapsed_h": round(status.get("elapsed_s", 0) / 3600, 1),
                    "matchups": status.get("best_matchups", {}),
                    "started": status.get("started", ""),
                    "status": "running",
                    "progress": status.get("progress", ""),
                })
            except Exception:
                pass

    runs.sort(key=lambda r: (r["status"] == "running", r["best_fitness"]), reverse=True)
    return runs


@app.get("/api/manage/ga-live")
async def manage_ga_live():
    """Check for live GA status from Studio via SSH.

    Reads status.json files written by simlab's forge_ga during execution.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "studio",
             "find ~/ga-workspace -name status.json -newer ~/ga-workspace/.ga_check_marker "
             "-exec cat {} \\; 2>/dev/null; touch ~/ga-workspace/.ga_check_marker"],
            capture_output=True, text=True, timeout=10,
        )
        statuses = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    statuses.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Also check if GA processes are running
        ps_result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", "studio",
             "pgrep -af 'ga-optimize|ga-staged' 2>/dev/null | head -3"],
            capture_output=True, text=True, timeout=8,
        )
        processes = [l.strip() for l in ps_result.stdout.strip().splitlines() if l.strip()]

        return {
            "reachable": True,
            "statuses": statuses,
            "processes": processes,
            "running": len(processes) > 0 or any(s.get("status") == "running" for s in statuses),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        return {
            "reachable": False,
            "statuses": [],
            "processes": [],
            "running": None,
            "error": str(e),
        }


@app.get("/api/manage/general-rules")
async def manage_general_rules():
    """Get general.json rules."""
    path = RULES_DIR / "general.json"
    if not path.exists():
        return JSONResponse({"error": "general.json not found"}, status_code=404)
    return json.loads(path.read_text())


@app.put("/api/manage/general-rules")
async def manage_general_rules_save(request: dict):
    """Save general.json."""
    path = RULES_DIR / "general.json"
    path.write_text(json.dumps(request, indent=2, ensure_ascii=False))
    log.info("General rules saved via manage UI")
    return {"status": "ok"}


@app.get("/api/manage/meta-decks")
async def manage_meta_decks():
    """Get meta_decks.json."""
    if not META_DECKS_PATH.exists():
        return {"meta_decks": []}
    return json.loads(META_DECKS_PATH.read_text())


@app.put("/api/manage/meta-decks")
async def manage_meta_decks_save(request: dict):
    """Save meta_decks.json."""
    META_DECKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_DECKS_PATH.write_text(json.dumps(request, indent=2, ensure_ascii=False))
    log.info("Meta decks saved via manage UI (%d decks)", len(request.get("meta_decks", [])))
    return {"status": "ok"}


@app.get("/api/manage/decks")
async def manage_decks():
    """List decks from filesystem (new deck dir structure)."""
    from .deck_lifecycle import DeckService
    svc = DeckService()
    return svc.list_decks()


@app.get("/api/manage/deck/{deck_id}")
async def manage_deck_detail(deck_id: str):
    """Get full deck detail."""
    from .deck_lifecycle import DeckService
    svc = DeckService()
    result = svc.get_deck(deck_id)
    if not result:
        return JSONResponse({"error": "not found"}, status_code=404)
    return result


@app.get("/api/manage/guides")
async def manage_guides():
    """List guide markdown files from deck dirs."""
    guides = []
    if DECKS_ROOT.exists():
        for deck_dir in sorted(DECKS_ROOT.iterdir()):
            if not deck_dir.is_dir():
                continue
            guides_dir = deck_dir / "guides"
            if not guides_dir.exists():
                continue
            for path in sorted(guides_dir.glob("*.md")):
                guides.append({
                    "file": path.name,
                    "deck_id": deck_dir.name,
                    "name": path.stem.replace("_", " ").title(),
                    "source": "user",
                    "size_bytes": path.stat().st_size,
                })
    return guides


@app.get("/api/manage/guide/{deck_id}/{filename}")
async def manage_guide_detail(deck_id: str, filename: str):
    """Get guide markdown content from deck dir."""
    path = DECKS_ROOT / deck_id / "guides" / filename
    if path.exists() and path.suffix == ".md":
        return {"file": filename, "deck_id": deck_id, "content": path.read_text()}
    return JSONResponse({"error": "not found"}, status_code=404)


_sync_lock = asyncio.Lock()


@app.post("/api/manage/sync-meta")
async def manage_sync_meta():
    """Run meta deck sync: scrape MTGGoldfish + merge + LLM enrichment."""
    if _sync_lock.locked():
        return JSONResponse({"error": "Sync already in progress"}, status_code=409)

    async with _sync_lock:
        script = Path(__file__).parent.parent / "tools" / "update_meta.py"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(script.parent.parent),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode("utf-8", errors="replace") if stdout else ""

        # Parse summary from output
        lines = output.split("\n")
        summary_lines = []
        in_summary = False
        for line in lines:
            if "SUMMARY" in line:
                in_summary = True
                continue
            if in_summary and line.strip().startswith(("From ", "Kept ", "Need ", "Written", "Backup")):
                summary_lines.append(line.strip())

        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "summary": summary_lines,
            "output": output[-3000:],  # last 3k chars
        }


_collection_refresh_lock = asyncio.Lock()


@app.post("/api/manage/refresh-collection")
async def manage_refresh_collection():
    """Refresh local collection snapshot via the sudo-approved memory reader wrapper."""
    if _collection_refresh_lock.locked():
        return JSONResponse({"error": "Collection refresh already in progress"}, status_code=409)

    async with _collection_refresh_lock:
        script = Path(__file__).parent.parent / "tools" / "refresh_collection_snapshot.py"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(script.parent.parent),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        out_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        err_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        payload: dict | None = None
        try:
            payload = json.loads(out_text) if out_text.strip() else None
        except json.JSONDecodeError:
            payload = None

        if proc.returncode != 0:
            return JSONResponse(
                {
                    "status": "error",
                    "returncode": proc.returncode,
                    "detail": payload or out_text[-2000:] or err_text[-2000:] or "Collection refresh failed",
                    "stdout": out_text[-2000:],
                    "stderr": err_text[-2000:],
                },
                status_code=500,
            )

        return {
            "status": "ok",
            "returncode": proc.returncode,
            "summary": payload or {},
            "stderr": err_text[-2000:],
        }


@app.get("/api/manage/collection-stats")
async def manage_collection_stats():
    """Return current collection stats from the raw snapshot."""
    raw_path = Path(__file__).parent.parent / "mtga_collection_raw.json"
    if not raw_path.exists():
        return {"status": "no_data", "message": "No collection snapshot found. Run Refresh first."}

    import json as _json
    data = _json.loads(raw_path.read_text())
    if not data or not isinstance(data, dict):
        return {"status": "no_data", "message": "Collection snapshot is empty."}

    total_unique = len(data)
    total_copies = sum(data.values())

    # Analyze by rarity using card cache
    rarities: dict[str, dict] = {}
    for grp_id_str, count in data.items():
        card = card_cache.get(int(grp_id_str))
        r = (card.rarity if card else "unknown") or "unknown"
        if r not in rarities:
            rarities[r] = {"unique": 0, "copies": 0}
        rarities[r]["unique"] += 1
        rarities[r]["copies"] += count

    # Read wildcards from Untapped inventory if available
    wildcards = {}
    inv_path = Path(__file__).parent.parent / "mtga_inventory.json"
    if inv_path.exists():
        try:
            inv = _json.loads(inv_path.read_text())
            wildcards = inv.get("wildcards", {})
        except Exception:
            pass

    return {
        "status": "ok",
        "total_unique": total_unique,
        "total_copies": total_copies,
        "rarities": rarities,
        "wildcards": wildcards,
        "snapshot_date": raw_path.stat().st_mtime,
    }
