"""Persistent SQLite database with automatic backups."""
import glob
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .enums import DB_COLORS, DB_TYPES, RARITY_MAP
from .models import CardInfo

# Paths
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USER_DATA_DIR = Path(os.environ.get("SCRY_USER_DATA", Path.home() / "MTG" / "mtg-data"))
DB_PATH = DATA_DIR / "advisor.db"
BACKUP_DIR = DATA_DIR / "backups"
MTGA_DB_GLOB = str(Path.home() / "Library" / "Application Support" / "Steam" / "steamapps" / "common" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw" / "Raw_CardDatabase_*.mtga")

MAX_BACKUPS = 10


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def backup_db():
    """Create a timestamped backup of the database."""
    if not DB_PATH.exists():
        return
    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"advisor_{ts}.db"
    shutil.copy2(DB_PATH, backup_path)

    # Rotate old backups
    backups = sorted(BACKUP_DIR.glob("advisor_*.db"))
    while len(backups) > MAX_BACKUPS:
        backups[0].unlink()
        backups.pop(0)
    return backup_path


def get_connection() -> sqlite3.Connection:
    """Get a connection to the persistent database."""
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database schema."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cards (
            grp_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            mana_cost TEXT DEFAULT '',
            cmc INTEGER DEFAULT 0,
            colors TEXT DEFAULT '[]',
            card_types TEXT DEFAULT '[]',
            subtypes TEXT DEFAULT '[]',
            power TEXT DEFAULT '',
            toughness TEXT DEFAULT '',
            rarity TEXT DEFAULT '',
            expansion TEXT DEFAULT '',
            abilities TEXT DEFAULT '[]',
            oracle_text TEXT DEFAULT '',
            source TEXT DEFAULT 'mtga_db',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ended_at TEXT,
            opponent_name TEXT DEFAULT '',
            my_deck_grp_ids TEXT DEFAULT '[]',
            result TEXT DEFAULT '',
            game_count INTEGER DEFAULT 0,
            format TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS match_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT NOT NULL,
            game_number INTEGER DEFAULT 1,
            turn_number INTEGER DEFAULT 0,
            phase TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches(match_id)
        );

        CREATE TABLE IF NOT EXISTS advice_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT,
            game_number INTEGER DEFAULT 1,
            turn_number INTEGER DEFAULT 0,
            phase TEXT DEFAULT '',
            source TEXT NOT NULL,
            priority TEXT DEFAULT 'medium',
            message TEXT NOT NULL,
            details TEXT DEFAULT '',
            game_state_summary TEXT DEFAULT '{}',
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT DEFAULT '',
            game_number INTEGER DEFAULT 1,
            turn_number INTEGER DEFAULT 0,
            phase TEXT DEFAULT '',
            request_type TEXT DEFAULT '',
            source TEXT DEFAULT '',
            backend TEXT DEFAULT '',
            advice_mode TEXT DEFAULT '',
            llm_scope TEXT DEFAULT '',
            state_id INTEGER DEFAULT 0,
            accepted INTEGER DEFAULT 1,
            message TEXT DEFAULT '',
            duration_ms INTEGER,
            total_cost_usd REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_creation_input_tokens INTEGER,
            cache_read_input_tokens INTEGER,
            session_id TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_match_events_match ON match_events(match_id);
        CREATE INDEX IF NOT EXISTS idx_advice_log_match ON advice_log(match_id);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_match ON llm_calls(match_id);
        CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);
    """)
    # Add deck name columns (migration for existing DBs)
    try:
        conn.execute("ALTER TABLE matches ADD COLUMN my_deck_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE matches ADD COLUMN opp_deck_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _find_mtga_db() -> str | None:
    """Find the MTGA CardDatabase SQLite file."""
    files = glob.glob(MTGA_DB_GLOB)
    return files[0] if files else None


def _parse_abilities(mtga_conn: sqlite3.Connection, ability_ids_str: str) -> list[str]:
    """Parse ability IDs and resolve to text."""
    if not ability_ids_str:
        return []
    texts = []
    cur = mtga_conn.cursor()
    for ab in ability_ids_str.split(","):
        ab = ab.strip()
        if not ab:
            continue
        # Format: baseId:abilityGrpId — baseId is the ability ID
        ab_id = ab.split(":")[0] if ":" in ab else ab
        try:
            cur.execute("SELECT TextId FROM Abilities WHERE Id = ?", (int(ab_id),))
            row = cur.fetchone()
            if row and row[0]:
                cur.execute(
                    "SELECT Loc FROM Localizations_enUS WHERE LocId = ? AND Formatted = 1",
                    (row[0],),
                )
                loc_row = cur.fetchone()
                if loc_row and loc_row[0]:
                    text = re.sub(r"<[^>]+>", "", loc_row[0])
                    texts.append(text)
        except (ValueError, sqlite3.Error):
            pass
    return texts


def import_cards_from_mtga():
    """Import/refresh card data from MTGA CardDatabase into our persistent DB."""
    mtga_path = _find_mtga_db()
    if not mtga_path:
        print("MTGA CardDatabase not found")
        return 0

    mtga_conn = sqlite3.connect(mtga_path)
    mtga_cur = mtga_conn.cursor()

    # Get all primary cards
    mtga_cur.execute("""
        SELECT c.GrpId, l.Loc, c.OldSchoolManaText, c.Order_CMCWithXLast,
               c.Colors, c.Types, c.Subtypes, c.Power, c.Toughness,
               c.Rarity, c.ExpansionCode, c.AbilityIds
        FROM Cards c
        JOIN Localizations_enUS l ON c.TitleId = l.LocId AND l.Formatted = 1
        WHERE c.IsPrimaryCard = 1 AND c.IsToken = 0
    """)

    # Build subtype ID → name map from Enums table
    subtype_map: dict[str, str] = {}
    try:
        for val, loc in mtga_conn.execute("""
            SELECT e.Value, l.Loc FROM Enums e
            JOIN Localizations_enUS l ON e.LocId = l.LocId AND l.Formatted = 1
            WHERE e.Type = 'SubType'
        """):
            subtype_map[str(val)] = re.sub(r"<[^>]+>", "", loc)
    except sqlite3.Error:
        pass  # older DB versions might not have this

    conn = get_connection()
    count = 0
    batch = []

    for row in mtga_cur.fetchall():
        grp_id, name_raw, mana, cmc, colors_str, types_str, subtypes_str, power, toughness, rarity, exp, ability_ids = row

        name = re.sub(r"<[^>]+>", "", name_raw or "")
        colors = [DB_COLORS.get(c, c) for c in (colors_str or "").split(",") if c.strip() and c.strip() in DB_COLORS]
        card_types = [DB_TYPES.get(t, t) for t in (types_str or "").split(",") if t.strip() and t.strip() in DB_TYPES]
        subtypes_list = [subtype_map.get(s.strip(), s.strip())
                         for s in (subtypes_str or "").split(",") if s.strip()]
        abilities = _parse_abilities(mtga_conn, ability_ids or "")
        rarity_name = RARITY_MAP.get(rarity, "Unknown")

        batch.append((
            grp_id, name, mana or "", cmc or 0,
            json.dumps(colors), json.dumps(card_types), json.dumps(subtypes_list),
            power or "", toughness or "", rarity_name, exp or "",
            json.dumps(abilities), "", "mtga_db",
        ))
        count += 1

        if len(batch) >= 1000:
            conn.executemany("""
                INSERT OR REPLACE INTO cards
                (grp_id, name, mana_cost, cmc, colors, card_types, subtypes,
                 power, toughness, rarity, expansion, abilities, oracle_text, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)
            batch = []

    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO cards
            (grp_id, name, mana_cost, cmc, colors, card_types, subtypes,
             power, toughness, rarity, expansion, abilities, oracle_text, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)

    # Import tokens (lightweight — just name/types/P/T for display)
    token_batch = []
    mtga_cur.execute("""
        SELECT c.GrpId, l.Loc, c.Colors, c.Types, c.Subtypes,
               c.Power, c.Toughness, c.ExpansionCode
        FROM Cards c
        JOIN Localizations_enUS l ON c.TitleId = l.LocId AND l.Formatted = 1
        WHERE c.IsToken = 1
    """)
    for row in mtga_cur.fetchall():
        grp_id, name_raw, colors_str, types_str, subtypes_str, power, toughness, exp = row
        name = re.sub(r"<[^>]+>", "", name_raw or "")
        colors = [DB_COLORS.get(c, c) for c in (colors_str or "").split(",") if c.strip() and c.strip() in DB_COLORS]
        card_types = [DB_TYPES.get(t, t) for t in (types_str or "").split(",") if t.strip() and t.strip() in DB_TYPES]
        subtypes_list = [subtype_map.get(s.strip(), s.strip())
                         for s in (subtypes_str or "").split(",") if s.strip()]
        token_batch.append((
            grp_id, name, "", 0,
            json.dumps(colors), json.dumps(card_types), json.dumps(subtypes_list),
            power or "", toughness or "", "Token", exp or "",
            "[]", "", "mtga_token",
        ))
    if token_batch:
        conn.executemany("""
            INSERT OR REPLACE INTO cards
            (grp_id, name, mana_cost, cmc, colors, card_types, subtypes,
             power, toughness, rarity, expansion, abilities, oracle_text, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, token_batch)
    token_count = len(token_batch)

    # Store import metadata
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
        ("last_card_import", json.dumps({"count": count, "tokens": token_count, "mtga_db": mtga_path}),
         datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    mtga_conn.close()
    return count


def get_card(grp_id: int) -> CardInfo | None:
    """Look up a card by grp_id from persistent DB."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cards WHERE grp_id = ?", (grp_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return CardInfo(
        grp_id=row[0],
        name=row[1],
        mana_cost=row[2],
        cmc=row[3],
        colors=json.loads(row[4]),
        card_types=json.loads(row[5]),
        subtypes=json.loads(row[6]),
        power=row[7],
        toughness=row[8],
        rarity=row[9],
        expansion=row[10],
        abilities=json.loads(row[11]),
        oracle_text=row[12],
    )


class CardCache:
    """In-memory card cache backed by persistent DB."""

    def __init__(self):
        self._cache: dict[int, CardInfo] = {}
        self._loaded = False

    def load(self):
        """Load all cards into memory from persistent DB."""
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT grp_id, name, mana_cost, cmc, colors, card_types, "
                     "subtypes, power, toughness, rarity, expansion, abilities, oracle_text "
                     "FROM cards")
        for row in cur.fetchall():
            self._cache[row[0]] = CardInfo(
                grp_id=row[0], name=row[1], mana_cost=row[2], cmc=row[3],
                colors=json.loads(row[4]), card_types=json.loads(row[5]),
                subtypes=json.loads(row[6]), power=row[7], toughness=row[8],
                rarity=row[9], expansion=row[10], abilities=json.loads(row[11]),
                oracle_text=row[12],
            )
        self._loaded = True
        conn.close()

    def get(self, grp_id: int) -> CardInfo | None:
        if not self._loaded:
            self.load()
        return self._cache.get(grp_id)

    def get_name(self, grp_id: int) -> str:
        card = self.get(grp_id)
        return card.name if card else f"Unknown({grp_id})"

    @property
    def size(self) -> int:
        return len(self._cache)


# Module-level singleton
card_cache = CardCache()


def save_match(match_id: str, **kwargs):
    """Create or update a match record."""
    conn = get_connection()
    existing = conn.execute("SELECT match_id FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(f"UPDATE matches SET {sets} WHERE match_id = ?",
                     (*kwargs.values(), match_id))
    else:
        kwargs["match_id"] = match_id
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn.execute(f"INSERT INTO matches ({cols}) VALUES ({placeholders})",
                     tuple(kwargs.values()))
    conn.commit()
    conn.close()


def save_match_event(match_id: str, event_type: str, **kwargs):
    """Log a match event."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO match_events (match_id, event_type, game_number, turn_number, phase, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (match_id, event_type, kwargs.get("game_number", 1),
         kwargs.get("turn_number", 0), kwargs.get("phase", ""),
         json.dumps(kwargs.get("data", {}))),
    )
    conn.commit()
    conn.close()


def clear_match_events():
    """Clear log-derived events (rebuilt from log). Preserve real-time-only events."""
    conn = get_connection()
    # Keep advice_compliance — can't be regenerated from log replay
    conn.execute(
        "DELETE FROM match_events WHERE event_type != 'advice_compliance'")
    conn.execute("DELETE FROM advice_log")
    conn.execute("DELETE FROM meta WHERE key LIKE 'summary_%'")
    conn.commit()
    conn.close()


def save_advice(match_id: str, advice_data: dict):
    """Log an advice entry."""
    conn = get_connection()
    params = (
        match_id,
        advice_data.get("game_number", 1),
        advice_data.get("turn_number", 0),
        advice_data.get("phase", ""),
        advice_data["source"],
        advice_data.get("priority", "medium"),
        advice_data["message"],
    )
    existing = conn.execute(
        "SELECT 1 FROM advice_log WHERE match_id = ? AND game_number = ? "
        "AND turn_number = ? AND phase = ? AND source = ? AND priority = ? "
        "AND message = ? ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if existing:
        conn.close()
        return
    conn.execute(
        "INSERT INTO advice_log (match_id, game_number, turn_number, phase, "
        "source, priority, message, details, game_state_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (params[0], params[1], params[2], params[3],
         params[4], params[5], params[6], advice_data.get("details", ""),
         json.dumps(advice_data.get("game_state_summary", {}))),
    )
    conn.commit()
    conn.close()


def save_llm_call(call_data: dict):
    """Persist LLM latency/token usage independently of replayed advice logs."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO llm_calls (match_id, game_number, turn_number, phase, request_type, "
        "source, backend, advice_mode, llm_scope, state_id, accepted, message, duration_ms, "
        "total_cost_usd, input_tokens, output_tokens, cache_creation_input_tokens, "
        "cache_read_input_tokens, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_data.get("match_id", ""),
            call_data.get("game_number", 1),
            call_data.get("turn_number", 0),
            call_data.get("phase", ""),
            call_data.get("request_type", ""),
            call_data.get("source", ""),
            call_data.get("backend", ""),
            call_data.get("advice_mode", ""),
            call_data.get("llm_scope", ""),
            call_data.get("state_id", 0),
            1 if call_data.get("accepted", True) else 0,
            call_data.get("message", ""),
            call_data.get("duration_ms"),
            call_data.get("total_cost_usd"),
            call_data.get("input_tokens"),
            call_data.get("output_tokens"),
            call_data.get("cache_creation_input_tokens"),
            call_data.get("cache_read_input_tokens"),
            call_data.get("session_id", ""),
        ),
    )
    conn.commit()
    conn.close()


def get_match_history(limit: int = 20) -> list[dict]:
    """Get recent match history."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT match_id, started_at, ended_at, opponent_name, result, game_count, format, "
        "my_deck_name, opp_deck_name "
        "FROM matches ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return []
    # Check which matches have cached summaries
    match_ids = [r[0] for r in rows]
    placeholders = ",".join("?" for _ in match_ids)
    cur.execute(
        f"SELECT key FROM meta WHERE key IN ({placeholders})",
        [f"summary_{mid}" for mid in match_ids],
    )
    has_summary = {r[0].removeprefix("summary_") for r in cur.fetchall()}
    # Count games per opponent
    cur.execute("""
        SELECT opponent_name, COUNT(*) as cnt
        FROM matches
        WHERE result IN ('Win', 'Loss') AND opponent_name <> ''
        GROUP BY opponent_name
        HAVING cnt >= 2
    """)
    opp_counts = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return [
        {"match_id": r[0], "started_at": r[1], "ended_at": r[2],
         "opponent_name": r[3], "result": r[4], "game_count": r[5], "format": r[6],
         "my_deck_name": r[7] or "", "opp_deck_name": r[8] or "",
         "has_summary": r[0] in has_summary,
         "opp_match_count": opp_counts.get(r[3], 0)}
        for r in rows
    ]


# ─── Statistics Queries ──────────────────────────────────────────

def get_stats_overview() -> dict:
    """Overall win/loss stats."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result='Loss' THEN 1 ELSE 0 END) as losses
        FROM matches WHERE result IN ('Win', 'Loss')
    """)
    row = cur.fetchone()
    total, wins, losses = row[0] or 0, row[1] or 0, row[2] or 0

    # Average game length (turns)
    cur.execute("""
        SELECT AVG(max_turn) FROM (
            SELECT match_id, MAX(turn_number) as max_turn
            FROM match_events WHERE event_type = 'life_change'
            GROUP BY match_id
        )
    """)
    avg_turns = cur.fetchone()[0] or 0

    # Streak
    cur.execute("""
        SELECT result FROM matches WHERE result IN ('Win', 'Loss')
        ORDER BY started_at DESC LIMIT 20
    """)
    results = [r[0] for r in cur.fetchall()]
    streak = 0
    if results:
        first = results[0]
        for r in results:
            if r == first:
                streak += 1
            else:
                break
        streak = streak if first == "Win" else -streak

    conn.close()
    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "avg_turns": round(avg_turns, 1),
        "streak": streak,
    }


def get_stats_decks() -> list[dict]:
    """Win rate per deck."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT my_deck_name,
            COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result='Loss' THEN 1 ELSE 0 END) as losses
        FROM matches
        WHERE result IN ('Win', 'Loss') AND my_deck_name IS NOT NULL AND my_deck_name <> ''
        GROUP BY my_deck_name
        ORDER BY total DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"deck": r[0], "total": r[1], "wins": r[2], "losses": r[3],
         "win_rate": round(r[2] / r[1] * 100, 1) if r[1] else 0}
        for r in rows
    ]


def get_stats_matchups() -> list[dict]:
    """Win rate per opponent deck archetype."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT my_deck_name, opp_deck_name,
            COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins
        FROM matches
        WHERE result IN ('Win', 'Loss')
          AND my_deck_name IS NOT NULL AND my_deck_name <> ''
          AND opp_deck_name IS NOT NULL AND opp_deck_name <> ''
        GROUP BY my_deck_name, opp_deck_name
        ORDER BY total DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"my_deck": r[0], "opp_deck": r[1], "total": r[2], "wins": r[3],
         "win_rate": round(r[3] / r[2] * 100, 1) if r[2] else 0}
        for r in rows
    ]


def get_stats_color_matchups() -> list[dict]:
    """Win rate vs opponent color combinations (e.g. vs Black, vs Boros)."""
    conn = get_connection()
    cur = conn.cursor()
    # Get distinct colors per match from opp_card_played events
    cur.execute("""
        SELECT e.match_id, e.data
        FROM match_events e
        WHERE e.event_type = 'opp_card_played'
    """)
    match_colors: dict[str, set[str]] = {}
    for mid, data_str in cur.fetchall():
        try:
            colors = json.loads(data_str).get("colors", [])
            match_colors.setdefault(mid, set()).update(colors)
        except (json.JSONDecodeError, TypeError):
            pass

    # Get match results
    cur.execute("""
        SELECT match_id, result FROM matches WHERE result IN ('Win', 'Loss')
    """)
    results = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    color_names = {
        "B": "Black", "G": "Green", "R": "Red", "U": "Blue", "W": "White",
    }
    guild_names = {
        "BG": "Golgari", "BR": "Rakdos", "BU": "Dimir", "BW": "Orzhov",
        "GR": "Gruul", "GU": "Simic", "GW": "Selesnya",
        "RU": "Izzet", "RW": "Boros", "UW": "Azorius",
    }

    # Aggregate by color combo
    stats: dict[str, dict] = {}
    for mid, colors in match_colors.items():
        if mid not in results or not colors:
            continue
        color_key = "".join(sorted(colors))
        if color_key not in stats:
            stats[color_key] = {"wins": 0, "losses": 0, "total": 0}
        stats[color_key]["total"] += 1
        if results[mid] == "Win":
            stats[color_key]["wins"] += 1
        else:
            stats[color_key]["losses"] += 1

    result = []
    for color_key, s in sorted(stats.items(), key=lambda x: -x[1]["total"]):
        # Pretty name
        if color_key in guild_names:
            name = f"{guild_names[color_key]} ({color_key})"
        elif len(color_key) == 1 and color_key in color_names:
            name = f"Mono {color_names[color_key]}"
        else:
            name = "/".join(color_names.get(c, c) for c in color_key)
        result.append({
            "colors": color_key,
            "name": name,
            "total": s["total"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0,
        })
    return result


def get_opponent_history(opponent_name: str) -> dict:
    """Get history vs a specific opponent."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins
        FROM matches
        WHERE result IN ('Win', 'Loss') AND opponent_name = ?
    """, (opponent_name,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return {"total": 0, "wins": 0, "win_rate": 0}
    return {
        "total": row[0], "wins": row[1],
        "win_rate": round(row[1] / row[0] * 100, 1),
    }


def get_stats_opponents() -> list[dict]:
    """Win rate per opponent name."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT opponent_name,
            COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins
        FROM matches
        WHERE result IN ('Win', 'Loss') AND opponent_name <> ''
        GROUP BY opponent_name
        ORDER BY total DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"opponent": r[0], "total": r[1], "wins": r[2],
         "win_rate": round(r[2] / r[1] * 100, 1) if r[1] else 0}
        for r in rows
    ]


def get_match_life_graph(match_id: str) -> dict:
    """Get life totals per turn for a specific match."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT game_number, turn_number, data
        FROM match_events
        WHERE match_id = ? AND event_type = 'life_change'
        ORDER BY id
    """, (match_id,))
    rows = cur.fetchall()

    # Get match result
    cur.execute("SELECT result, opponent_name, my_deck_name, opp_deck_name "
                "FROM matches WHERE match_id = ?", (match_id,))
    match_row = cur.fetchone()
    conn.close()

    # Build per-game life progression
    games: dict[int, dict] = {}
    for game_num, turn, data_str in rows:
        try:
            d = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue
        if game_num not in games:
            games[game_num] = {"me": {0: 20}, "opp": {0: 20}}
        player = d.get("player", "me")
        new_life = d.get("new", 20)
        games[game_num][player][turn] = new_life

    # Convert to arrays for charting
    result = []
    for gn in sorted(games.keys()):
        g = games[gn]
        max_turn = max(
            max(g["me"].keys(), default=0),
            max(g["opp"].keys(), default=0),
        )
        me_life = []
        opp_life = []
        last_me, last_opp = 20, 20
        for t in range(max_turn + 1):
            if t in g["me"]:
                last_me = g["me"][t]
            if t in g["opp"]:
                last_opp = g["opp"][t]
            me_life.append(last_me)
            opp_life.append(last_opp)
        result.append({"game": gn, "me": me_life, "opp": opp_life})

    return {
        "match_id": match_id,
        "result": match_row[0] if match_row else "",
        "opponent": match_row[1] if match_row else "",
        "my_deck": match_row[2] if match_row else "",
        "opp_deck": match_row[3] if match_row else "",
        "games": result,
    }


def get_stats_opp_cards(limit: int = 30) -> list[dict]:
    """Most commonly seen opponent cards across all matches."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT json_extract(data, '$.name') as card_name,
               COUNT(*) as times_seen,
               COUNT(DISTINCT match_id) as matches_seen
        FROM match_events
        WHERE event_type = 'opp_card_played'
        GROUP BY card_name
        ORDER BY matches_seen DESC, times_seen DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {"card": r[0], "times": r[1], "matches": r[2]}
        for r in rows if r[0]
    ]


def get_stats_mulligan() -> dict:
    """Mulligan stats: keep rate and win rate when keeping vs mulliganing."""
    conn = get_connection()
    cur = conn.cursor()
    # Get mulligan decisions with match results
    cur.execute("""
        SELECT
            json_extract(e.data, '$.decision') as decision,
            m.result,
            COUNT(*) as cnt
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'mulligan' AND m.result IN ('Win', 'Loss')
        GROUP BY decision, m.result
    """)
    rows = cur.fetchall()
    conn.close()

    stats = {}  # decision -> {wins, losses, total}
    for decision, result, cnt in rows:
        if decision not in stats:
            stats[decision] = {"wins": 0, "losses": 0, "total": 0}
        stats[decision]["total"] += cnt
        if result == "Win":
            stats[decision]["wins"] += cnt
        else:
            stats[decision]["losses"] += cnt

    result = []
    for dec, s in sorted(stats.items()):
        result.append({
            "decision": dec,
            "total": s["total"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0,
        })
    total_decisions = sum(s["total"] for s in stats.values())
    keep_count = stats.get("MulliganOption_AcceptHand", {}).get("total", 0)
    return {
        "decisions": result,
        "keep_rate": round(keep_count / total_decisions * 100, 1) if total_decisions else 0,
        "total": total_decisions,
    }


def get_stats_my_cards(limit: int = 30) -> list[dict]:
    """Most played cards with win rate correlation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            json_extract(e.data, '$.name') as card_name,
            COUNT(DISTINCT e.match_id) as matches,
            COUNT(DISTINCT CASE WHEN m.result='Win' THEN e.match_id END) as wins,
            COUNT(DISTINCT CASE WHEN m.result='Loss' THEN e.match_id END) as losses,
            COUNT(*) as times_played
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'card_played' AND m.result IN ('Win', 'Loss')
            AND json_extract(e.data, '$.is_land') != 1
        GROUP BY card_name
        HAVING matches >= 2
        ORDER BY matches DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {"card": r[0], "matches": r[1], "wins": r[2], "losses": r[3],
         "times": r[4],
         "win_rate": round(r[2] / (r[2] + r[3]) * 100, 1) if (r[2] + r[3]) else 0}
        for r in rows if r[0]
    ]


def get_stats_mana_curve() -> list[dict]:
    """Average mana efficiency per turn."""
    conn = get_connection()
    cur = conn.cursor()
    # Get turn_start events with available mana
    cur.execute("""
        SELECT match_id, game_number, turn_number,
               json_extract(data, '$.available_mana') as mana
        FROM match_events WHERE event_type = 'turn_start'
    """)
    turn_starts = cur.fetchall()
    # Get card_played events with CMC (non-land)
    cur.execute("""
        SELECT match_id, game_number, turn_number,
               json_extract(data, '$.cmc') as cmc
        FROM match_events WHERE event_type = 'card_played'
            AND json_extract(data, '$.is_land') != 1
    """)
    plays = cur.fetchall()
    conn.close()

    # Aggregate available mana by turn
    available_by_turn: dict[int, list[int]] = {}
    for mid, gn, turn, mana in turn_starts:
        available_by_turn.setdefault(turn, []).append(mana or 0)

    # Aggregate mana spent by (match, game, turn), then by turn
    spent_by_key: dict[tuple, int] = {}
    for mid, gn, turn, cmc in plays:
        key = (mid, gn, turn)
        spent_by_key[key] = spent_by_key.get(key, 0) + (cmc or 0)
    spent_by_turn: dict[int, list[int]] = {}
    for (mid, gn, turn), total in spent_by_key.items():
        spent_by_turn.setdefault(turn, []).append(total)

    result = []
    for turn in sorted(set(available_by_turn.keys()) | set(spent_by_turn.keys())):
        if turn <= 0 or turn > 15:
            continue
        avail = available_by_turn.get(turn, [0])
        spent = spent_by_turn.get(turn, [0])
        avg_avail = sum(avail) / len(avail) if avail else 0
        avg_spent = sum(spent) / len(spent) if spent else 0
        result.append({
            "turn": turn,
            "avg_available": round(avg_avail, 1),
            "avg_spent": round(avg_spent, 1),
            "efficiency": round(avg_spent / avg_avail * 100, 1) if avg_avail > 0 else 0,
            "samples": len(avail),
        })
    return result


def get_card_win_rates(min_matches: int = 3) -> dict[str, float]:
    """Card win rates for heuristic weighting. Returns {card_name: win_rate}."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            json_extract(e.data, '$.name') as card_name,
            COUNT(DISTINCT CASE WHEN m.result='Win' THEN e.match_id END) as wins,
            COUNT(DISTINCT CASE WHEN m.result='Loss' THEN e.match_id END) as losses
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'card_played' AND m.result IN ('Win', 'Loss')
            AND json_extract(e.data, '$.is_land') != 1
        GROUP BY card_name
        HAVING (wins + losses) >= ?
    """, (min_matches,))
    result = {}
    for name, wins, losses in cur.fetchall():
        total = wins + losses
        if total > 0 and name:
            result[name] = round(wins / total * 100, 1)
    conn.close()
    return result


def get_matchup_wr(my_deck: str, opp_deck: str) -> dict | None:
    """Get historical WR for a specific matchup."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins
        FROM matches
        WHERE result IN ('Win', 'Loss')
          AND my_deck_name = ? AND opp_deck_name = ?
    """, (my_deck, opp_deck))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    return {
        "total": row[0],
        "wins": row[1],
        "win_rate": round(row[1] / row[0] * 100, 1),
    }


def get_stats_compliance() -> dict:
    """Advice compliance stats: followed vs ignored with WR correlation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            json_extract(e.data, '$.followed') as followed,
            m.result,
            COUNT(*) as cnt
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'advice_compliance' AND m.result IN ('Win', 'Loss')
        GROUP BY followed, m.result
    """)
    rows = cur.fetchall()
    conn.close()

    stats = {}
    for followed, result, cnt in rows:
        key = "followed" if followed else "ignored"
        if key not in stats:
            stats[key] = {"total": 0, "wins": 0, "losses": 0}
        stats[key]["total"] += cnt
        if result == "Win":
            stats[key]["wins"] += cnt
        else:
            stats[key]["losses"] += cnt

    result = []
    for key in ("followed", "ignored"):
        s = stats.get(key, {"total": 0, "wins": 0, "losses": 0})
        result.append({
            "type": key,
            "total": s["total"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0,
        })
    total = sum(s["total"] for s in stats.values())
    followed_count = stats.get("followed", {}).get("total", 0)
    return {
        "decisions": result,
        "follow_rate": round(followed_count / total * 100, 1) if total else 0,
        "total": total,
    }


def get_stats_weaknesses() -> list[dict]:
    """Identify weak cards and matchups for improvement alerts."""
    conn = get_connection()
    cur = conn.cursor()
    # Weak cards: low WR with enough data
    cur.execute("""
        SELECT
            json_extract(e.data, '$.name') as card_name,
            COUNT(DISTINCT e.match_id) as matches,
            COUNT(DISTINCT CASE WHEN m.result='Win' THEN e.match_id END) as wins,
            COUNT(DISTINCT CASE WHEN m.result='Loss' THEN e.match_id END) as losses
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'card_played' AND m.result IN ('Win', 'Loss')
            AND json_extract(e.data, '$.is_land') != 1
        GROUP BY card_name
        HAVING matches >= 3 AND (wins * 1.0 / matches) < 0.25
        ORDER BY matches DESC
    """)
    weak_cards = [
        {"type": "weak_card", "name": r[0], "matches": r[1],
         "wins": r[2], "losses": r[3],
         "win_rate": round(r[2] / r[1] * 100, 1) if r[1] else 0,
         "tip": f"Consider replacing — {r[2]}W/{r[3]}L in {r[1]} matches"}
        for r in cur.fetchall() if r[0]
    ]
    # Weak decks
    cur.execute("""
        SELECT my_deck_name,
            COUNT(*) as total,
            SUM(CASE WHEN result='Win' THEN 1 ELSE 0 END) as wins
        FROM matches
        WHERE result IN ('Win', 'Loss') AND my_deck_name <> ''
        GROUP BY my_deck_name
        HAVING total >= 5 AND (wins * 1.0 / total) < 0.35
    """)
    weak_decks = [
        {"type": "weak_deck", "name": r[0], "matches": r[1],
         "wins": r[2], "losses": r[1] - r[2],
         "win_rate": round(r[2] / r[1] * 100, 1),
         "tip": f"Deck needs work — {round(r[2]/r[1]*100)}% WR over {r[1]} matches"}
        for r in cur.fetchall()
    ]
    conn.close()
    return weak_cards + weak_decks


def get_player_preferences() -> dict[str, float]:
    """Preference adjustments from advice compliance outcomes.

    Positive = player successfully overrides to play this card.
    Negative = player ignores this recommended card and wins anyway.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.data, m.result
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'advice_compliance' AND m.result IN ('Win', 'Loss')
    """)
    rows = cur.fetchall()
    conn.close()

    scores: dict[str, float] = {}
    for data_str, result in rows:
        try:
            d = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            continue
        played = d.get("played", "")
        recommended = d.get("recommended", [])
        followed = d.get("followed", False)
        won = result == "Win"

        if not followed and played:
            if won:
                # Player was right to ignore — boost their pick, reduce rec
                scores[played] = scores.get(played, 0) + 3
                for rec in recommended:
                    scores[rec] = scores.get(rec, 0) - 1
            else:
                # Player was wrong — should have listened
                scores[played] = scores.get(played, 0) - 2
                for rec in recommended:
                    scores[rec] = scores.get(rec, 0) + 1
        elif followed and played:
            if won:
                scores[played] = scores.get(played, 0) + 1
            else:
                scores[played] = scores.get(played, 0) - 1
    return scores


def get_match_data_for_summary(match_id: str) -> dict:
    """Get all match data needed for LLM summary generation."""
    conn = get_connection()
    cur = conn.cursor()

    # Match info
    cur.execute(
        "SELECT match_id, started_at, ended_at, opponent_name, result, game_count, "
        "format, my_deck_name, opp_deck_name FROM matches WHERE match_id = ?",
        (match_id,))
    match_row = cur.fetchone()
    if not match_row:
        conn.close()
        return {}

    match_info = {
        "match_id": match_row[0], "started_at": match_row[1],
        "ended_at": match_row[2], "opponent_name": match_row[3] or "Unknown",
        "result": match_row[4] or "Unknown", "game_count": match_row[5] or 0,
        "format": match_row[6] or "", "my_deck_name": match_row[7] or "",
        "opp_deck_name": match_row[8] or "",
    }

    # All events
    cur.execute(
        "SELECT event_type, game_number, turn_number, phase, data "
        "FROM match_events WHERE match_id = ? ORDER BY id",
        (match_id,))
    events = []
    for etype, gn, turn, phase, data_str in cur.fetchall():
        try:
            data = json.loads(data_str) if data_str else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        events.append({
            "type": etype, "game": gn, "turn": turn,
            "phase": phase, "data": data,
        })

    # Advice log
    cur.execute(
        "SELECT turn_number, phase, source, priority, message "
        "FROM advice_log WHERE match_id = ? AND source != 'llm_summary' ORDER BY id",
        (match_id,))
    advice = [
        {"turn": r[0], "phase": r[1], "source": r[2],
         "priority": r[3], "message": r[4]}
        for r in cur.fetchall()
    ]

    conn.close()
    return {"match": match_info, "events": events, "advice": advice}


def get_cached_summary(match_id: str) -> str | None:
    """Get cached LLM summary for a match."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key = ?",
                (f"summary_{match_id}",))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def save_cached_summary(match_id: str, summary: str):
    """Cache LLM summary for a match."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
        (f"summary_{match_id}", summary, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_match_timeline(match_id: str) -> dict:
    """Get structured turn-by-turn timeline for a match."""
    conn = get_connection()
    cur = conn.cursor()

    # Match info
    cur.execute(
        "SELECT match_id, opponent_name, result, game_count, my_deck_name, opp_deck_name "
        "FROM matches WHERE match_id = ?", (match_id,))
    match_row = cur.fetchone()
    if not match_row:
        conn.close()
        return {}

    # All events ordered
    cur.execute(
        "SELECT event_type, game_number, turn_number, phase, data "
        "FROM match_events WHERE match_id = ? ORDER BY id",
        (match_id,))
    raw_events = cur.fetchall()
    conn.close()

    # Build per-game, per-turn structure
    games: dict[int, dict[int, dict]] = {}
    mulligans: list[dict] = []
    game_ends: list[dict] = []

    for etype, gn, turn, phase, data_str in raw_events:
        try:
            data = json.loads(data_str) if data_str else {}
        except (json.JSONDecodeError, TypeError):
            data = {}

        if etype == "mulligan":
            dec = data.get("decision", "")
            mulligans.append({
                "game": gn,
                "decision": "keep" if "Accept" in dec else "mulligan",
                "hand_size": data.get("hand_size", 0),
                "hand": [c.get("name", "?") for c in data.get("hand", [])],
            })
            continue

        if etype == "game_end":
            game_ends.append({
                "game": gn,
                "my_life": data.get("my_life", 0),
                "opp_life": data.get("opp_life", 0),
                "reason": data.get("reason", ""),
            })
            continue

        if gn not in games:
            games[gn] = {}
        if turn not in games[gn]:
            games[gn][turn] = {
                "turn": turn, "mana": None, "lands": None,
                "my_plays": [], "opp_plays": [],
                "attacks": [], "opp_attacks": [],
                "removals": [], "life_changes": [],
                "blocks": [], "enchantments": [],
                "permanent_changes": [],
                "decision_points": [],
                "board_snapshot": None,
                "compliance": None,
            }
        t = games[gn][turn]

        if etype == "turn_start":
            t["mana"] = data.get("available_mana", 0)
            t["lands"] = data.get("total_lands", 0)
            # B4: board snapshot
            if data.get("my_creatures") is not None:
                t["board_snapshot"] = {
                    "my_creatures": data.get("my_creatures", []),
                    "opp_creatures": data.get("opp_creatures", []),
                    "my_battlefield": data.get("my_battlefield", []),
                    "opp_battlefield": data.get("opp_battlefield", []),
                    "my_hand": data.get("my_hand", []),
                    "stack": data.get("stack", []),
                    "my_hand_size": data.get("my_hand_size", 0),
                    "my_life": data.get("my_life", 0),
                    "opp_life": data.get("opp_life", 0),
                }
        elif etype == "card_played":
            t["my_plays"].append({
                "name": data.get("name", "?"),
                "cmc": data.get("cmc", 0),
                "is_land": data.get("is_land", False),
                "types": data.get("card_types", []),
            })
        elif etype == "opp_card_played":
            t["opp_plays"].append({
                "name": data.get("name", "?"),
                "types": data.get("card_types", []),
            })
        elif etype == "attack_declared":
            t["attacks"].append({
                "name": data.get("name", "?"),
                "power": data.get("power", 0),
                "toughness": data.get("toughness", 0),
            })
        elif etype == "life_change":
            t["life_changes"].append({
                "player": data.get("player", "?"),
                "old": data.get("old", 0),
                "new": data.get("new", 0),
                "delta": data.get("delta", 0),
            })
        elif etype == "opp_attack_declared":
            t["opp_attacks"].append({
                "name": data.get("name", "?"),
                "power": data.get("power", 0),
                "toughness": data.get("toughness", 0),
            })
        elif etype == "creature_left_bf":
            removal = {
                "name": data.get("name", "?"),
                "owner": data.get("owner", "?"),
                "destination": data.get("destination", "removed"),
                "power": data.get("power", 0),
                "toughness": data.get("toughness", 0),
                "temporary_exile": data.get("temporary_exile", False),
            }
            # B1: include cause if available
            if data.get("caused_by"):
                removal["caused_by"] = data["caused_by"]
                removal["caused_by_type"] = data.get("caused_by_type", "unknown")
            t["removals"].append(removal)
        elif etype == "block_declared":
            # B2: block declarations
            t["blocks"].append({
                "blocker": data.get("blocker"),
                "blocker_power": data.get("blocker_power", 0),
                "blocker_toughness": data.get("blocker_toughness", 0),
                "attackers": data.get("attackers", []),
                "no_blocks": data.get("no_blocks", False),
            })
        elif etype == "enchantment_attached":
            # B3: aura/enchantment targets
            t["enchantments"].append({
                "aura": data.get("aura", "?"),
                "target": data.get("target", "?"),
                "target_owner": data.get("target_owner", "?"),
            })
        elif etype == "permanent_stats_changed":
            t["permanent_changes"].append({
                "name": data.get("name", "?"),
                "controller": data.get("controller", "?"),
                "owner": data.get("owner", "?"),
                "old_power": data.get("old_power", 0),
                "new_power": data.get("new_power", 0),
                "old_toughness": data.get("old_toughness", 0),
                "new_toughness": data.get("new_toughness", 0),
            })
        elif etype == "decision_context":
            t["decision_points"].append({
                "request_type": data.get("request_type", ""),
                "phase_display": data.get("phase_display", ""),
                "my_life": data.get("my_life"),
                "opp_life": data.get("opp_life"),
                "my_hand": data.get("my_hand", []),
                "my_battlefield": data.get("my_battlefield", []),
                "opp_battlefield": data.get("opp_battlefield", []),
                "stack": data.get("stack", []),
                "legal_actions": data.get("legal_actions", []),
            })
        elif etype == "advice_compliance":
            t["compliance"] = {
                "followed": data.get("followed", False),
                "played": data.get("played", ""),
                "recommended": data.get("recommended", []),
            }

    # Convert to sorted arrays
    result_games = []
    for gn in sorted(games.keys()):
        turns = [games[gn][t] for t in sorted(games[gn].keys())]
        result_games.append({"game": gn, "turns": turns})

    return {
        "match_id": match_id,
        "opponent": match_row[1] or "Unknown",
        "result": match_row[2] or "",
        "game_count": match_row[3] or 0,
        "my_deck": match_row[4] or "",
        "opp_deck": match_row[5] or "",
        "mulligans": mulligans,
        "game_ends": game_ends,
        "games": result_games,
    }


def get_observed_opp_decks(min_cards: int = 3) -> list[dict]:
    """Build opponent deck profiles from actual match data.

    Groups opponent cards by match, clusters by color identity,
    and returns deck profiles with signal cards weighted by frequency.
    """
    conn = get_connection()
    cur = conn.cursor()

    # Get opponent cards grouped by match with colors
    cur.execute("""
        SELECT e.match_id, m.opponent_name,
               json_extract(e.data, '$.name') as name,
               json_extract(e.data, '$.colors') as colors,
               json_extract(e.data, '$.card_types') as types
        FROM match_events e
        JOIN matches m ON e.match_id = m.match_id
        WHERE e.event_type = 'opp_card_played'
        ORDER BY e.match_id
    """)
    rows = cur.fetchall()
    conn.close()

    # Group by match
    matches: dict[str, dict] = {}
    for mid, opp, name, colors_str, types_str in rows:
        if not name:
            continue
        if mid not in matches:
            matches[mid] = {"opponent": opp, "cards": {}, "colors": set()}
        matches[mid]["cards"][name] = matches[mid]["cards"].get(name, 0) + 1
        try:
            cols = json.loads(colors_str) if colors_str else []
            matches[mid]["colors"].update(cols)
        except (json.JSONDecodeError, TypeError):
            pass

    # Cluster matches by color identity
    clusters: dict[str, list[dict]] = {}  # color_key -> [match_data]
    for mid, mdata in matches.items():
        if len(mdata["cards"]) < 2:
            continue  # too few cards to identify
        color_key = "".join(sorted(mdata["colors"])) or "C"
        clusters.setdefault(color_key, []).append(mdata)

    # Build deck profiles from clusters with 2+ matches
    decks = []
    color_names = {
        "B": "Black", "G": "Green", "R": "Red", "U": "Blue", "W": "White",
        "BG": "Golgari", "BR": "Rakdos", "BU": "Dimir", "GR": "Gruul",
        "GU": "Simic", "RU": "Izzet", "RW": "Boros", "GW": "Selesnya",
        "BW": "Orzhov", "UW": "Azorius",
    }
    for color_key, cluster_matches in clusters.items():
        if len(cluster_matches) < 2:
            continue
        # Count card frequency across matches in this cluster
        card_freq: dict[str, int] = {}
        for m in cluster_matches:
            for card in m["cards"]:
                card_freq[card] = card_freq.get(card, 0) + 1
        # Signal cards: appear in 50%+ of matches in this cluster
        threshold = len(cluster_matches) * 0.4
        signals = {card: round(min(0.3, freq / len(cluster_matches) * 0.3), 2)
                   for card, freq in card_freq.items()
                   if freq >= threshold}
        if len(signals) < 2:
            continue
        guild = color_names.get(color_key, color_key)
        colors = sorted(set(color_key))
        decks.append({
            "name": f"{guild} (observed)",
            "archetype": "unknown",
            "colors": colors,
            "signal_cards": dict(sorted(signals.items(),
                                        key=lambda x: -x[1])[:10]),
            "matches": len(cluster_matches),
        })

    return decks


def get_stats_recent_trend(limit: int = 30) -> list[dict]:
    """Recent match results for trend chart."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT match_id, started_at, result, my_deck_name, opp_deck_name, opponent_name
        FROM matches
        WHERE result IN ('Win', 'Loss')
        ORDER BY started_at DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {"match_id": r[0], "date": r[1], "result": r[2],
         "my_deck": r[3] or "", "opp_deck": r[4] or "", "opponent": r[5] or ""}
        for r in reversed(rows)  # chronological order
    ]
