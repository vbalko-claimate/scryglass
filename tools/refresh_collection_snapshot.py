#!/usr/bin/env python3
"""Refresh the local MTGA collection snapshot via a sudo-approved wrapper.

Expected flow:
1. A root-owned wrapper runs `python -m tools.mtga_reader ...` with sudo rights.
2. That wrapper writes a fresh memory snapshot to `~/MTG/my_collection_memory.json`.
3. This helper validates the snapshot and copies it into repo-local files used by tools.

Default command:
    sudo /usr/local/sbin/mtga-read-collection
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path.home() / "MTG" / "my_collection_memory.json"
DEFAULT_TARGETS = [
    ROOT / "mtga_collection_raw.json",
]
DEFAULT_CMD = "sudo /usr/local/sbin/mtga-read-collection"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cmd",
        default=DEFAULT_CMD,
        help="Command used to refresh the root-owned collection snapshot",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Collection snapshot written by the sudo wrapper",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Extra target file to overwrite with the normalized snapshot",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for the sudo wrapper",
    )
    parser.add_argument(
        "--skip-command",
        action="store_true",
        help="Do not run the refresh command; just validate/copy an existing snapshot",
    )
    return parser.parse_args()


def parse_snapshot(path: Path) -> dict[str, int]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        if "cards" in raw and isinstance(raw["cards"], list):
            return {
                str(entry.get("grpid") or entry.get("grpId")): int(entry.get("quantity", 1))
                for entry in raw["cards"]
                if entry.get("grpid") or entry.get("grpId")
            }
        return {str(key): int(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return {
            str(entry.get("grpid") or entry.get("grpId")): int(entry.get("quantity", 1))
            for entry in raw
            if isinstance(entry, dict) and (entry.get("grpid") or entry.get("grpId"))
        }
    raise ValueError(f"Unsupported collection payload in {path}")


def parse_inventory(stderr: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    wildcards = re.search(
        r"Wildcards:\s*(\d+)C\s*/\s*(\d+)U\s*/\s*(\d+)R\s*/\s*(\d+)M",
        stderr,
    )
    if wildcards:
        result["wildcards"] = {
            "common": int(wildcards.group(1)),
            "uncommon": int(wildcards.group(2)),
            "rare": int(wildcards.group(3)),
            "mythic": int(wildcards.group(4)),
        }
    money = re.search(r"Gold:\s*(\d+),\s*Gems:\s*(\d+)", stderr)
    if money:
        result["gold"] = int(money.group(1))
        result["gems"] = int(money.group(2))
    cards = re.search(r"Read\s+(\d+)\s+unique cards\s+\((\d+)\s+total\)", stderr)
    if cards:
        result["reader_unique_cards"] = int(cards.group(1))
        result["reader_total_cards"] = int(cards.group(2))
    return result


def write_targets(snapshot: dict[str, int], source: Path, extra_targets: list[Path]) -> list[str]:
    targets: list[Path] = [*DEFAULT_TARGETS, *extra_targets]
    written: list[str] = []
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        written.append(str(path))
    if source not in targets:
        written.insert(0, str(source))
    return written


def main() -> int:
    args = parse_args()
    stdout = ""
    stderr = ""
    returncode = 0

    if not args.skip_command:
        proc = subprocess.run(
            shlex.split(args.cmd),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
        if proc.returncode != 0:
            print(json.dumps({
                "status": "error",
                "returncode": proc.returncode,
                "command": args.cmd,
                "stdout": stdout[-2000:],
                "stderr": stderr[-2000:],
            }, ensure_ascii=False, indent=2))
            return proc.returncode

    if not args.source.exists():
        print(json.dumps({
            "status": "error",
            "returncode": returncode,
            "command": args.cmd,
            "detail": f"Snapshot not found: {args.source}",
            "stdout": stdout[-2000:],
            "stderr": stderr[-2000:],
        }, ensure_ascii=False, indent=2))
        return 1

    snapshot = parse_snapshot(args.source)
    extra_targets = [Path(path) for path in args.target]
    written = write_targets(snapshot, args.source, extra_targets)
    summary = {
        "status": "ok",
        "returncode": returncode,
        "command": args.cmd,
        "source": str(args.source),
        "written": written,
        "unique_cards": len(snapshot),
        "snapshot_mtime": datetime.fromtimestamp(args.source.stat().st_mtime).isoformat(),
        "inventory": parse_inventory(stderr),
        "stdout": stdout[-1000:],
        "stderr": stderr[-2000:],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
