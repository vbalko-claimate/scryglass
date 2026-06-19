#!/usr/bin/env python3
"""Bulk-export every match-with-events from the live advisor.db into the
replay-v3 corpus directory, in a SINGLE process so the card cache is loaded
once (per-match `export_replay.py` reloads it each time — far too slow for
hundreds of matches).

Usage:
    uv run python tools/export_corpus_all.py [--out DIR] [--fresh]

  --out    corpus directory (default: ~/MTG/scryglass/data/replay_corpus_v3)
  --fresh  delete existing *.json in the corpus dir first (full rebuild)

Reuses `tools/export_replay.py`'s load_match / load_events /
build_replay_record so the emitted schema stays identical to the per-match
exporter that the Glass Shard replay-v3 consumer expects.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from pathlib import Path

# tools/ is not a package; put it on the path so `import export_replay` works.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_replay as er  # noqa: E402
from advisor.database import DB_PATH, card_cache  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path.home() / "MTG" / "scryglass" / "data" / "replay_corpus_v3",
    )
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="remove existing corpus *.json before exporting (full rebuild)",
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=None,
        help="match/event source db (default: live DB_PATH). Use a backup "
        "db to recover matches whose events were pruned from the live db.",
    )
    ap.add_argument(
        "--no-overwrite",
        action="store_true",
        help="skip a match whose output file already exists (additive merge)",
    )
    args = ap.parse_args()

    if not card_cache._loaded:
        card_cache.load()  # cards table (stable across dbs) from the live db

    src_db = args.db if args.db else DB_PATH
    conn = sqlite3.connect(str(src_db))
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT match_id FROM match_events ORDER BY match_id")
    match_ids = [r[0] for r in cur.fetchall()]
    print(f"db={src_db}  matches-with-events={len(match_ids)}", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        removed = 0
        for f in args.out.glob("*.json"):
            f.unlink()
            removed += 1
        print(f"--fresh: removed {removed} existing files", file=sys.stderr)

    n_files = 0
    n_match = 0
    n_err = 0
    for i, mid in enumerate(match_ids):
        try:
            match = er.load_match(conn, mid)
            if not match:
                continue
            events = er.load_events(conn, mid)
            if not events:
                continue
            game_numbers = sorted({e["game_number"] for e in events})
            wrote = False
            for gn in game_numbers:
                out_path = args.out / f"{mid}_game{gn}.json"
                if args.no_overwrite and out_path.exists():
                    continue
                record = er.build_replay_record(match, events, gn)
                if not record or not record.get("turns"):
                    continue
                with out_path.open("w") as f:
                    json.dump(record, f, indent=2)
                n_files += 1
                wrote = True
            if wrote:
                n_match += 1
        except Exception:  # noqa: BLE001 — isolate one bad match, keep going
            n_err += 1
            print(f"  ERR match {mid}:", file=sys.stderr)
            traceback.print_exc()
        if (i + 1) % 100 == 0:
            print(
                f"  …{i + 1}/{len(match_ids)} processed "
                f"({n_files} files, {n_err} errors)",
                file=sys.stderr,
            )

    print(
        f"DONE: exported {n_files} game files from {n_match} matches "
        f"({n_err} matches errored, skipped) → {args.out}"
    )


if __name__ == "__main__":
    main()
