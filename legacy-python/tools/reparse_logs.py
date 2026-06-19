#!/usr/bin/env python3
"""Re-parse archived Player.log files to backfill spell/ability events.

Usage:
    uv run python tools/reparse_logs.py              # parse all archives
    uv run python tools/reparse_logs.py Player_*.log  # parse specific files
"""
import json
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from advisor.database import card_cache, init_db, save_match_event
from advisor.game_state import GameStateTracker
from advisor.log_parser import iter_messages_from_lines

from advisor.database import DB_PATH, LOG_ARCHIVE_DIR as ARCHIVE_DIR


def get_existing_events(match_id: str) -> set[str]:
    """Get set of event types already logged for a match."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT DISTINCT event_type FROM match_events WHERE match_id = ?",
        (match_id,)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def reparse_log(log_path: Path, dry_run: bool = False) -> dict:
    """Parse a log file and extract spell/ability events.

    Returns {match_id: {added: int, skipped: int}}.
    """
    if not log_path.exists():
        print(f"  File not found: {log_path}")
        return {}

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if not lines:
        return {}

    # Ensure card cache is loaded
    if card_cache.size == 0:
        card_cache.load()

    tracker = GameStateTracker()
    results: dict[str, dict] = {}

    # Collect events via callback
    collected_events: list[tuple[str, dict]] = []

    def on_stack(event_type: str, data: dict):
        collected_events.append((event_type, data))

    tracker.on_stack_observed = on_stack

    # Process all messages
    messages = list(iter_messages_from_lines(lines))
    for msg in messages:
        tracker.process_message(msg)

    # Check what matches were found
    conn = sqlite3.connect(str(DB_PATH))

    # Find match IDs from events in DB
    match_events = conn.execute(
        "SELECT match_id, event_type, count(*) FROM match_events "
        "GROUP BY match_id, event_type").fetchall()
    existing_spells: dict[str, set[str]] = {}
    for mid, etype, cnt in match_events:
        if mid not in existing_spells:
            existing_spells[mid] = set()
        existing_spells[mid].add(etype)

    # Count what we found vs what's already there
    spell_types = {'opp_spell_cast', 'spell_cast', 'opp_ability', 'ability'}
    new_spell_events = conn.execute(
        "SELECT match_id, event_type, turn_number, data FROM match_events "
        "WHERE event_type IN ('opp_spell_cast', 'spell_cast', 'opp_ability', 'ability') "
        "ORDER BY match_id, rowid").fetchall()

    for mid in set(r[0] for r in new_spell_events):
        events = [r for r in new_spell_events if r[0] == mid]
        results[mid] = {"added": len(events), "events": events}

    conn.close()
    return results


def main():
    init_db()

    # Determine which files to parse
    if len(sys.argv) > 1:
        log_files = [Path(f) for f in sys.argv[1:]]
    else:
        # Parse all archives + current Player.log
        log_files = sorted(ARCHIVE_DIR.glob("Player_*.log")) if ARCHIVE_DIR.exists() else []
        current = Path.home() / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log"
        if current.exists():
            log_files.append(current)

    if not log_files:
        print("No log files found.")
        print(f"Archive dir: {ARCHIVE_DIR}")
        return

    print(f"Found {len(log_files)} log file(s)")

    # Show current DB state
    conn = sqlite3.connect(str(DB_PATH))
    matches = conn.execute(
        "SELECT match_id, opponent_name FROM matches ORDER BY rowid").fetchall()
    spell_counts = {}
    for mid, _ in matches:
        cnt = conn.execute(
            "SELECT count(*) FROM match_events WHERE match_id=? "
            "AND event_type IN ('opp_spell_cast','spell_cast','opp_ability','ability')",
            (mid,)).fetchone()[0]
        spell_counts[mid] = cnt
    conn.close()

    matches_without_spells = [(mid, name) for mid, name in matches
                              if spell_counts.get(mid, 0) == 0]
    print(f"Matches in DB: {len(matches)} ({len(matches_without_spells)} without spell data)")

    # Parse each log file
    total_added = 0
    for log_file in log_files:
        size_kb = log_file.stat().st_size // 1024
        print(f"\nParsing: {log_file.name} ({size_kb} KB)")

        # Delete existing spell events for matches we'll reparse
        # (the tracker will re-generate them)
        tracker = GameStateTracker()
        events_found: list[tuple] = []

        def collect(event_type, data):
            events_found.append((event_type, data,
                                 tracker.state.match_info.match_id,
                                 tracker.state.match_info.game_number,
                                 tracker.state.turn_info.turn_number,
                                 tracker.state.turn_info.phase))

        tracker.on_stack_observed = collect

        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for msg in iter_messages_from_lines(lines):
            tracker.process_message(msg)

        if not events_found:
            print("  No spell/ability events found")
            continue

        # Group by match
        by_match: dict[str, list] = {}
        for ev in events_found:
            mid = ev[2]
            if mid:
                by_match.setdefault(mid, []).append(ev)

        for mid, evs in by_match.items():
            # Check if this match already has spell data
            existing = spell_counts.get(mid, 0)
            if existing > 0:
                print(f"  Match {mid[:12]}... already has {existing} spell events, skipping")
                continue

            # Check if match exists in DB
            conn = sqlite3.connect(str(DB_PATH))
            match_exists = conn.execute(
                "SELECT 1 FROM matches WHERE match_id=?", (mid,)).fetchone()
            conn.close()

            if not match_exists:
                print(f"  Match {mid[:12]}... not in DB, skipping")
                continue

            # Insert events
            for event_type, data, _, game_num, turn_num, phase in evs:
                save_match_event(mid, event_type,
                                 game_number=game_num,
                                 turn_number=turn_num,
                                 phase=phase,
                                 data=data)

            added = len(evs)
            total_added += added
            opp_name = next((name for m, name in matches if m == mid), "?")
            spell_names = [e[1].get("name", "?") for e in evs
                           if e[0] in ("opp_spell_cast", "spell_cast")]
            ability_names = [e[1].get("name", "?") for e in evs
                             if e[0] in ("opp_ability", "ability")]
            print(f"  Match {mid[:12]}... vs {opp_name}: +{added} events"
                  f" (spells: {spell_names}, abilities: {ability_names})")

    print(f"\nDone. Added {total_added} events total.")


if __name__ == "__main__":
    main()
