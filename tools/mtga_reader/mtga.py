"""MTGA-specific: find inventory objects, read card collection."""

import struct
from dataclasses import dataclass

from .memory import BaseMemoryReader
from .il2cpp import (
    find_class_by_name, list_fields,
    CLASS_STATIC_FIELDS, CLASS_INSTANCE_SIZE, ARRAY_DATA,
)


@dataclass
class PlayerInventory:
    wc_common: int = 0
    wc_uncommon: int = 0
    wc_rare: int = 0
    wc_mythic: int = 0
    gold: int = 0
    gems: int = 0
    vault_progress: int = 0


# ── Instance discovery ────────────────────────────────────────────

def find_singleton_on_heap(reader: BaseMemoryReader, class_ptr: int,
                           label: str = "object") -> list[int]:
    """Scan heap for objects whose class pointer matches class_ptr."""
    CHUNK = 0x100000  # 1MB
    target = struct.pack("<Q", class_ptr)
    found = []

    for start, end in reader.get_heap_ranges():
        for base in range(start, end, CHUNK):
            data = reader.read_bytes(base, CHUNK)
            if data == b"\x00" * CHUNK:
                continue
            pos = 0
            while True:
                idx = data.find(target, pos)
                if idx < 0:
                    break
                found.append(base + idx)
                if len(found) >= 10:
                    break
                pos = idx + 8
            if len(found) >= 10:
                break
        if len(found) >= 10:
            break

    if found:
        print(f"[+] Found {len(found)} {label} candidate(s) on heap")
    return found


# ── Inventory service discovery ───────────────────────────────────

def find_inventory_service(reader: BaseMemoryReader, table_addr: int
                           ) -> tuple[int, int, int]:
    """Find AwsInventoryServiceWrapper on heap.
    Returns (isw_addr, cards_ptr, card_count)."""

    isw_class = find_class_by_name(reader, table_addr, "AwsInventoryServiceWrapper")

    print(f"\n[*] Scanning heap for AwsInventoryServiceWrapper (class={hex(isw_class)})...")
    candidates = []

    CHUNK = 0x100000
    target = struct.pack("<Q", isw_class)

    for start, end in reader.get_heap_ranges():
        for base in range(start, end, CHUNK):
            data = reader.read_bytes(base, CHUNK)
            if data == b"\x00" * CHUNK:
                continue
            pos = 0
            while True:
                idx = data.find(target, pos)
                if idx < 0:
                    break
                addr = base + idx
                # Validate: Cards at +72 should be a dict with reasonable count
                cards_ptr = reader.read_ptr(addr + 72)
                if reader.is_valid_ptr(cards_ptr):
                    entries_ptr = reader.read_ptr(cards_ptr + 0x18)
                    count = reader.read_i32(cards_ptr + 0x20)
                    if reader.is_valid_ptr(entries_ptr) and 100 < count < 30000:
                        candidates.append((addr, cards_ptr, count))
                        print(f"[+] Found ISW at {hex(addr)}, Cards dict count={count}")
                pos = idx + 8
            if candidates:
                break
        if candidates:
            break

    if not candidates:
        # Fallback: try InventoryManager -> _inventoryServiceWrapper
        print("[!] ISW not found directly, trying via InventoryManager...")
        try:
            inv_class = find_class_by_name(reader, table_addr, "InventoryManager")
            for addr in find_singleton_on_heap(reader, inv_class, "InventoryManager"):
                isw_ptr = reader.read_ptr(addr + 56)  # _inventoryServiceWrapper
                if not reader.is_valid_ptr(isw_ptr):
                    continue
                cards_ptr = reader.read_ptr(isw_ptr + 72)
                if not reader.is_valid_ptr(cards_ptr):
                    continue
                entries_ptr = reader.read_ptr(cards_ptr + 0x18)
                count = reader.read_i32(cards_ptr + 0x20)
                if reader.is_valid_ptr(entries_ptr) and 100 < count < 30000:
                    candidates.append((isw_ptr, cards_ptr, count))
                    print(f"[+] Found via InventoryManager: ISW at {hex(isw_ptr)}, count={count}")
                    break
        except RuntimeError:
            pass

    if not candidates:
        raise RuntimeError("Could not find inventory data in MTGA memory")

    return max(candidates, key=lambda x: x[2])


# ── Data readers ──────────────────────────────────────────────────

def read_cards(reader: BaseMemoryReader, cards_ptr: int) -> dict[int, int]:
    """Read CardsAndQuantity dictionary. Returns {GrpId: quantity}."""
    entries_arr_ptr = reader.read_ptr(cards_ptr + 0x18)
    count = reader.read_i32(cards_ptr + 0x20)

    if count <= 0 or count > 50000 or not reader.is_valid_ptr(entries_arr_ptr):
        raise RuntimeError(f"Invalid cards dict: count={count}, entries={hex(entries_arr_ptr)}")

    entries_data = entries_arr_ptr + ARRAY_DATA
    cards: dict[int, int] = {}
    chunk_size = min(count, 1000)
    entry_stride = 16

    for chunk_start in range(0, count, chunk_size):
        n = min(chunk_size, count - chunk_start)
        data = reader.read_bytes(entries_data + chunk_start * entry_stride, n * entry_stride)
        for i in range(n):
            off = i * entry_stride
            hash_code = struct.unpack_from("<i", data, off)[0]
            card_id = struct.unpack_from("<i", data, off + 8)[0]
            quantity = struct.unpack_from("<i", data, off + 12)[0]
            if hash_code >= 0 and card_id > 0 and 0 < quantity <= 99:
                cards[card_id] = quantity

    return cards


def read_inventory(reader: BaseMemoryReader, isw_addr: int) -> PlayerInventory:
    """Read ClientPlayerInventory from AwsInventoryServiceWrapper.m_inventory (+64)."""
    inv_ptr = reader.read_ptr(isw_addr + 64)
    if not reader.is_valid_ptr(inv_ptr):
        return PlayerInventory()
    return PlayerInventory(
        wc_common=reader.read_i32(inv_ptr + 16),
        wc_uncommon=reader.read_i32(inv_ptr + 20),
        wc_rare=reader.read_i32(inv_ptr + 24),
        wc_mythic=reader.read_i32(inv_ptr + 28),
        gold=reader.read_i32(inv_ptr + 32),
        gems=reader.read_i32(inv_ptr + 36),
        vault_progress=reader.read_i32(inv_ptr + 48),
    )
