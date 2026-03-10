#!/usr/bin/env python3
"""
Read MTGA card collection from process memory.

Usage:
    sudo python -m tools.mtga_reader              # default: JSON to stdout
    sudo python -m tools.mtga_reader -o cards.json # save to file
    sudo python -m tools.mtga_reader --inventory   # also show inventory

Works on macOS (ARM64/x86), Windows, and Linux.
Requires elevated privileges (sudo / Administrator).
"""

import argparse
import json
import sys

from .memory import create_reader, find_pid
from .il2cpp import find_type_info_table, find_class_by_name, list_fields
from .mtga import find_inventory_service, read_cards, read_inventory


def main():
    parser = argparse.ArgumentParser(
        description="Read MTGA card collection from game memory"
    )
    parser.add_argument("-o", "--output", help="Save collection JSON to file")
    parser.add_argument("--inventory", action="store_true", help="Show player inventory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--pid", type=int, help="MTGA process ID (auto-detect if omitted)")
    args = parser.parse_args()

    # 1. Find and attach to MTGA
    pid = args.pid or find_pid()
    print(f"[+] MTGA PID: {pid}", file=sys.stderr)

    reader = create_reader(pid)

    # 2. Find GameAssembly data segment
    data_base = reader.find_game_assembly_data_base()

    # 3. Find IL2CPP type table
    table = find_type_info_table(reader, data_base)

    # 4. Discover PAPA class (for verbose field dump)
    if args.verbose:
        try:
            papa = find_class_by_name(reader, table, "PAPA")
            print(f"\n[*] PAPA fields:", file=sys.stderr)
            for name, offset in list_fields(reader, papa):
                if offset < 1000:  # skip garbage
                    print(f"    {name} (offset={offset})", file=sys.stderr)
        except RuntimeError:
            pass

    # 5. Find inventory service on heap
    isw_addr, cards_ptr, count = find_inventory_service(reader, table)
    print(f"\n[+] Found {count} cards in memory", file=sys.stderr)

    # 6. Read inventory
    if args.inventory:
        inv = read_inventory(reader, isw_addr)
        print(f"\n=== Player Inventory ===", file=sys.stderr)
        print(f"  Wildcards: {inv.wc_common}C / {inv.wc_uncommon}U / {inv.wc_rare}R / {inv.wc_mythic}M", file=sys.stderr)
        print(f"  Gold: {inv.gold}, Gems: {inv.gems}", file=sys.stderr)

    # 7. Read card collection
    cards = read_cards(reader, cards_ptr)
    total = sum(cards.values())
    print(f"[+] Read {len(cards)} unique cards ({total} total)", file=sys.stderr)

    # 8. Output
    result = {str(k): v for k, v in sorted(cards.items())}

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[+] Saved to {args.output}", file=sys.stderr)
    else:
        json.dump(result, sys.stdout, indent=2)
        print()  # trailing newline


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[!] Error: {e}", file=sys.stderr)
        if "--verbose" in sys.argv or "-v" in sys.argv:
            import traceback
            traceback.print_exc()
        sys.exit(1)
