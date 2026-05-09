"""Persistent layout cache learned across runs.

Keeps the reader resilient to MTGA patches: after each successful scan
we persist the offsets / class name / field layout that worked, so the
next run finds them immediately instead of falling back to expensive
brute heap scans.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

CACHE_PATH = Path(
    os.environ.get(
        "MTGA_READER_CACHE",
        str(Path.home() / "MTG" / ".mtga_reader_cache.json"),
    )
)

DEFAULT_TYPE_INFO_OFFSETS: list[int] = [
    0x24C10, 0x24360, 0x24350, 0x24370, 0x24340, 0x24380, 0x243A0,
]
DEFAULT_ISW_CLASS_NAMES: list[str] = ["AwsInventoryServiceWrapper"]
DEFAULT_ISW_FIELD_LAYOUT: dict[str, int] = {"cards": 72, "inventory": 64}
DEFAULT_INVENTORY_FIELD_LAYOUT: dict[str, int] = {
    "wc_common": 16,
    "wc_uncommon": 20,
    "wc_rare": 24,
    "wc_mythic": 28,
    "gold": 32,
    "gems": 36,
    "vault_progress": 48,
}


def _defaults() -> dict[str, Any]:
    return {
        "type_info_offsets": list(DEFAULT_TYPE_INFO_OFFSETS),
        "isw_class_names": list(DEFAULT_ISW_CLASS_NAMES),
        "isw_field_layouts": {"AwsInventoryServiceWrapper": dict(DEFAULT_ISW_FIELD_LAYOUT)},
        "inventory_field_layout": dict(DEFAULT_INVENTORY_FIELD_LAYOUT),
        "last_cards_ptr": 0,
        "last_cards_count": 0,
        "field_signature_useful": True,
        "history": [],
    }


def load() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return _defaults()
    try:
        with open(CACHE_PATH) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _defaults()
    base = _defaults()
    for key, default in base.items():
        if key not in data:
            data[key] = default
    return data


def save(data: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[!] Cache save failed: {exc}")


def remember_int_list(data: dict[str, Any], key: str, value: int, cap: int = 16) -> None:
    if not isinstance(value, int) or value < 0:
        return
    bucket = data.setdefault(key, [])
    if not isinstance(bucket, list):
        bucket = []
        data[key] = bucket
    if value in bucket:
        bucket.remove(value)
    bucket.insert(0, value)
    del bucket[cap:]


def remember_class_name(data: dict[str, Any], name: str, cap: int = 12) -> None:
    if not name or not isinstance(name, str):
        return
    bucket = data.setdefault("isw_class_names", [])
    if name in bucket:
        bucket.remove(name)
    bucket.insert(0, name)
    del bucket[cap:]


def remember_isw_layout(data: dict[str, Any], class_name: str, cards: int, inventory: int) -> None:
    if not class_name:
        return
    layouts = data.setdefault("isw_field_layouts", {})
    layouts[class_name] = {"cards": int(cards), "inventory": int(inventory)}


def record_run(
    data: dict[str, Any],
    status: str,
    detail: str = "",
    cap: int = 24,
) -> None:
    history = data.setdefault("history", [])
    history.insert(
        0,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": status,
            "detail": detail[:240],
        },
    )
    del history[cap:]
