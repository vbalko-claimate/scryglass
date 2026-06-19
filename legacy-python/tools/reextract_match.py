#!/usr/bin/env python3
"""Re-extract a single match's game-state events from archived logs.

The companion `tools/reparse_logs.py` only backfills spell/ability
events and skips matches that already have any. This script is
broader: it wipes EVERY log-derived event for a match (preserving
the advisor's training records — decision_eval, decision_outcome,
advice_compliance, reranker_shadow, decision_context) and then
re-walks the archived log files via the current GameStateTracker so
the latest tracker logic (e.g. emitting `turn_start` for BOTH
players) takes effect.

Usage:
    uv run python tools/reextract_match.py <match_id>
                                            [--archive DIR]
                                            [--log FILE...]

If `--archive` is omitted, defaults to
`~/MTG/mtg-data/app_data/log_archive`. If `--log` is given, only
those files are scanned; otherwise every Player_*.log under the
archive is scanned plus the current ~/Library Player.log.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from advisor.database import DB_PATH, LOG_ARCHIVE_DIR, save_match_event  # noqa: E402
from advisor.game_state import GameStateTracker  # noqa: E402
from advisor.log_parser import iter_messages_from_lines  # noqa: E402

# Event types that are LIVE-ONLY (heuristic advisor / training
# signals). We preserve these on re-extraction so the data flywheel
# isn't disturbed.
PRESERVE_TYPES = (
    "advice_compliance",
    "decision_eval",
    "decision_outcome",
    "reranker_shadow",
    "decision_context",
)


def delete_log_derived_events(match_id: str) -> int:
    """Delete every match_event for `match_id` whose event_type is NOT
    in PRESERVE_TYPES. Returns the number of rows deleted."""
    conn = sqlite3.connect(str(DB_PATH))
    placeholders = ",".join("?" for _ in PRESERVE_TYPES)
    cur = conn.execute(
        f"DELETE FROM match_events WHERE match_id = ? "
        f"AND event_type NOT IN ({placeholders})",
        (match_id, *PRESERVE_TYPES),
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def discover_log_files(archive: Path | None, explicit: list[Path]) -> list[Path]:
    if explicit:
        return [p for p in explicit if p.exists()]
    files: list[Path] = []
    if archive and archive.exists():
        files.extend(sorted(archive.glob("Player_*.log")))
    current = Path.home() / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log"
    if current.exists():
        files.append(current)
    return files


def reextract(match_id: str, log_files: list[Path], commit: bool = True) -> dict:
    """Walk every log file with a fresh tracker and save every event
    the tracker emits for `match_id`. When `commit=False`, no events
    are persisted — the tracker still runs but inserts are suppressed
    so the function can be used as a dry probe to count what WOULD be
    saved (used by main() to decide whether deleting existing events
    is safe). Returns a small summary dict."""
    total_saved = 0
    seen_in_log: list[str] = []

    for log_file in log_files:
        try:
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            print(f"  ! could not read {log_file.name}: {e}", file=sys.stderr)
            continue

        tracker = GameStateTracker()
        saved_for_this_file = 0

        # Hook: intercept save_match_event so we only persist events
        # that belong to OUR target match_id. Other matches' events
        # are skipped (they're handled by their own reextract runs).
        # We monkey-patch the tracker's database.save_match_event
        # binding for the duration of this run.
        from advisor import game_state as game_state_module

        orig_save = game_state_module.save_match_event

        def filtered_save(mid: str, event_type: str, **kwargs):
            nonlocal saved_for_this_file
            if mid != match_id:
                return
            saved_for_this_file += 1
            if commit:
                orig_save(mid, event_type, **kwargs)

        game_state_module.save_match_event = filtered_save
        try:
            for msg in iter_messages_from_lines(lines):
                tracker.process_message(msg)
        finally:
            game_state_module.save_match_event = orig_save

        if saved_for_this_file > 0:
            total_saved += saved_for_this_file
            seen_in_log.append(f"{log_file.name}: +{saved_for_this_file}")

    return {"events_saved": total_saved, "log_files_with_match": seen_in_log}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("match_id", help="match_id (UUID) to re-extract")
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="log_archive directory (default: ~/MTG/mtg-data/app_data/log_archive)",
    )
    parser.add_argument(
        "--log",
        action="append",
        type=Path,
        default=[],
        help="explicit log file to scan (repeat for multiple); when "
        "absent, every Player_*.log under --archive plus the current "
        "Player.log is scanned",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't delete or insert; just report what would happen",
    )
    args = parser.parse_args()

    archive = args.archive or LOG_ARCHIVE_DIR
    log_files = discover_log_files(archive, args.log)

    if not log_files:
        print("error: no log files to scan; pass --log or set up archive", file=sys.stderr)
        sys.exit(1)

    print(f"Match ID: {args.match_id}")
    print(f"Archive:  {archive}")
    print(f"Logs:     {len(log_files)} files")
    print()

    if args.dry_run:
        summary = reextract(args.match_id, log_files, commit=False)
        print(f"Would re-extract {summary['events_saved']} events")
        for line in summary["log_files_with_match"]:
            print(f"  - {line}")
        return

    # Two-pass safety: probe FIRST without committing so we know how
    # many events the current logs hold for this match. If the probe
    # finds nothing, leave the existing events intact — wiping them
    # would be a destructive no-op (the source log is gone and we'd
    # end up with 0 events for a match that previously had data).
    probe = reextract(args.match_id, log_files, commit=False)
    if probe["events_saved"] == 0:
        print("No events found in any log file for this match; "
              "leaving existing events untouched.")
        return

    deleted = delete_log_derived_events(args.match_id)
    print(f"Deleted {deleted} pre-existing log-derived events")
    print()

    summary = reextract(args.match_id, log_files, commit=True)
    print(f"Re-extracted {summary['events_saved']} events")
    for line in summary["log_files_with_match"]:
        print(f"  - {line}")


if __name__ == "__main__":
    main()
