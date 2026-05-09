"""Shared helpers for syncing MTGA collection refresh artifacts."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import PERSISTENT_DIR


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path.home() / "MTG" / "my_collection_memory.json"
DEFAULT_TARGETS = [
    PERSISTENT_DIR / "mtga_collection_raw.json",
]
DEFAULT_INVENTORY_TARGET = PERSISTENT_DIR / "mtga_inventory.json"


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


def write_targets(snapshot: dict[str, int], source: Path, extra_targets: list[Path] | None = None) -> list[str]:
    targets: list[Path] = [*DEFAULT_TARGETS, *(extra_targets or [])]
    written: list[str] = []
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        written.append(str(path))
    if source not in targets:
        written.insert(0, str(source))
    return written


def write_inventory(inventory: dict[str, Any], target: Path = DEFAULT_INVENTORY_TARGET) -> str | None:
    if not inventory:
        target.unlink(missing_ok=True)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(inventory, indent=2, sort_keys=True), encoding="utf-8")
    return str(target)


def sync_collection_snapshot(
    source: Path = DEFAULT_SOURCE,
    *,
    stderr_text: str = "",
    extra_targets: list[Path] | None = None,
    command: str = "",
    returncode: int = 0,
    stdout_text: str = "",
) -> dict[str, Any]:
    if not source.exists():
        raise FileNotFoundError(f"Snapshot not found: {source}")

    snapshot = parse_snapshot(source)
    written = write_targets(snapshot, source, extra_targets)
    inventory = parse_inventory(stderr_text)
    inventory_written = write_inventory(inventory)
    if inventory_written:
        written.append(inventory_written)

    return {
        "status": "ok",
        "returncode": returncode,
        "command": command,
        "source": str(source),
        "written": written,
        "unique_cards": len(snapshot),
        "snapshot_mtime": datetime.fromtimestamp(source.stat().st_mtime).isoformat(),
        "inventory": inventory,
        "stdout": stdout_text[-1000:],
        "stderr": stderr_text[-2000:],
    }
