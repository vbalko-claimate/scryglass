#!/usr/bin/env python3
"""
Read MTGA card collection from process memory.

Usage:
    sudo python -m tools.mtga_reader              # default: JSON to stdout
    sudo python -m tools.mtga_reader -o cards.json # save to file
    sudo python -m tools.mtga_reader --inventory   # also show inventory

Works on macOS (ARM64/x86), Windows, and Linux.
Requires elevated privileges (sudo / Administrator).

Layout discovery is cached to ``~/MTG/.mtga_reader_cache.json`` so the
reader stays robust across MTGA patches that rename classes or shift
struct offsets — once a layout is learned, the next run skips the slow
brute-scan path. Override the cache location with ``$MTGA_READER_CACHE``.
"""

import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

from . import _cache as cache_mod
from .memory import create_reader, find_pid
from .il2cpp import find_type_info_table, find_class_by_name, list_fields
from .mtga import find_inventory_service, read_cards, read_inventory


def _atomic_write_with_backup(path: Path, payload: dict, *, force: bool = False) -> bool:
    """Write ``payload`` atomically, taking a ``.prev`` backup of the prior file.

    If the new snapshot is dramatically smaller than the existing one
    (less than 50% of unique-card entries), the write is rejected unless
    ``force`` is set — this protects against a bad heap-scan candidate
    overwriting a known-good collection. Returns True if written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    new_count = len(payload) if isinstance(payload, dict) else 0

    if path.exists() and not force:
        try:
            with open(path) as fh:
                existing = json.load(fh)
            old_count = len(existing) if isinstance(existing, dict) else 0
        except (OSError, json.JSONDecodeError):
            old_count = 0
        if old_count > 200 and new_count < old_count * 0.5:
            print(
                f"[!] Refusing to overwrite snapshot — new={new_count} unique cards is "
                f"<50% of existing {old_count}. Re-run with --force-write to override.",
                file=sys.stderr,
            )
            return False

    if path.exists():
        backup = path.with_suffix(path.suffix + ".prev")
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            print(f"[!] Backup copy failed ({exc}); proceeding without rollback file", file=sys.stderr)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    return True


_LIVE_CACHE: dict = {}


def main():
    global _LIVE_CACHE
    parser = argparse.ArgumentParser(
        description="Read MTGA card collection from game memory"
    )
    parser.add_argument("-o", "--output", help="Save collection JSON to file")
    parser.add_argument("--inventory", action="store_true", help="Show player inventory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--pid", type=int, help="MTGA process ID (auto-detect if omitted)")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the persistent layout cache (useful when debugging a stale layout)",
    )
    parser.add_argument(
        "--force-write",
        action="store_true",
        help="Bypass the shrink-guard and overwrite snapshot even if much smaller than prior",
    )
    args = parser.parse_args()

    cache = {} if args.no_cache else cache_mod.load()
    _LIVE_CACHE = cache

    pid = args.pid or find_pid()
    print(f"[+] MTGA PID: {pid}", file=sys.stderr)

    reader = create_reader(pid)

    segments = reader.find_game_assembly_data_segments()
    data_base = segments[0][0]

    table = find_type_info_table(reader, data_base, cache=cache)

    if args.verbose:
        try:
            papa = find_class_by_name(reader, table, "PAPA")
            print(f"\n[*] PAPA fields:", file=sys.stderr)
            for name, offset in list_fields(reader, papa):
                if offset < 1000:
                    print(f"    {name} (offset={offset})", file=sys.stderr)
        except RuntimeError:
            pass

    isw_addr, cards_ptr, count = find_inventory_service(reader, table, cache=cache)
    print(f"\n[+] Found {count} cards in memory", file=sys.stderr)

    if args.inventory:
        print(f"\n=== Player Inventory ===", file=sys.stderr)
        if isw_addr:
            inv = read_inventory(reader, isw_addr)
            print(f"  Wildcards: {inv.wc_common}C / {inv.wc_uncommon}U / {inv.wc_rare}R / {inv.wc_mythic}M", file=sys.stderr)
            print(f"  Gold: {inv.gold}, Gems: {inv.gems}", file=sys.stderr)
        else:
            print("  Inventory unavailable (collection-only fallback)", file=sys.stderr)

    cards = read_cards(reader, cards_ptr)
    total = sum(cards.values())
    print(f"[+] Read {len(cards)} unique cards ({total} total)", file=sys.stderr)

    result = {str(k): v for k, v in sorted(cards.items())}

    if args.output:
        out_path = Path(args.output)
        wrote = _atomic_write_with_backup(out_path, result, force=args.force_write)
        if wrote:
            print(f"[+] Saved to {out_path}", file=sys.stderr)
        else:
            print(f"[!] Snapshot NOT written; keeping previous file at {out_path}", file=sys.stderr)
    else:
        json.dump(result, sys.stdout, indent=2)
        print()

    if not args.no_cache:
        cache_mod.record_run(
            cache,
            status="ok",
            detail=f"unique={len(cards)} total={total} isw={'yes' if isw_addr else 'fallback'}",
        )
        cache_mod.save(cache)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[!] Error: {e}", file=sys.stderr)
        if "--verbose" in sys.argv or "-v" in sys.argv:
            traceback.print_exc()
        try:
            persisted = _LIVE_CACHE if _LIVE_CACHE else cache_mod.load()
            cache_mod.record_run(persisted, status="error", detail=str(e))
            cache_mod.save(persisted)
        except Exception:
            pass
        sys.exit(1)
