"""MTGA-specific: find inventory objects, read card collection."""

import os
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

from .memory import BaseMemoryReader
from .il2cpp import (
    find_class_by_name, find_classes_by_substring, find_classes_with_fields, list_fields,
    CLASS_NAME, CLASS_NAMESPACE, CLASS_STATIC_FIELDS, CLASS_INSTANCE_SIZE, ARRAY_DATA,
)
from . import _cache as _cache_mod


# ── Card-DB ground-truth validation ──────────────────────────────
#
# The brute heap scan can otherwise pick up any int->int Dictionary that
# happens to be in MTGA memory (asset IDs, localization handles, quest
# states …). The only way to be sure a candidate is the *card* collection
# is to check its keys against the real card database that MTGA itself
# loads from disk. ``Raw_CardDatabase_*.mtga`` is just a SQLite file under
# the user's MTGA install — we open it read-only and use it as an oracle.

_DB_GLOB_CANDIDATES = (
    Path.home() / "Library/Application Support/Steam/steamapps/common/MTGA/MTGA_Data/Downloads/Raw",
    Path.home() / "Library/Application Support/com.wizards.mtga/Downloads/Raw",
    Path("/Applications/MTGA.app/Contents/Resources/Data/StreamingAssets/MTGData/Downloads/Raw"),
)

_DB_GRPIDS_CACHE: set[int] | None = None


def _load_known_grpids() -> set[int]:
    """Load the GrpId column from MTGA's Raw_CardDatabase_*.mtga (SQLite).

    Returns an empty set if the DB can't be located — callers fall back
    to range-based heuristics in that case.
    """
    global _DB_GRPIDS_CACHE
    if _DB_GRPIDS_CACHE is not None:
        return _DB_GRPIDS_CACHE

    db_path: Path | None = None
    override = os.environ.get("MTGA_CARD_DB")
    if override and Path(override).exists():
        db_path = Path(override)
    else:
        for raw_dir in _DB_GLOB_CANDIDATES:
            if not raw_dir.exists():
                continue
            matches = sorted(
                raw_dir.glob("Raw_CardDatabase_*.mtga"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                db_path = matches[0]
                break

    if not db_path:
        print("[!] MTGA card database not found; skipping GrpId oracle validation")
        _DB_GRPIDS_CACHE = set()
        return _DB_GRPIDS_CACHE

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT GrpId FROM Cards")
        ids = {int(row[0]) for row in cur.fetchall()}
        con.close()
    except sqlite3.Error as exc:
        print(f"[!] MTGA card DB read failed ({exc}); skipping oracle validation")
        ids = set()

    print(f"[+] Loaded {len(ids)} GrpIds from {db_path.name} for candidate validation")
    _DB_GRPIDS_CACHE = ids
    return ids


def _candidate_card_id_overlap(reader: BaseMemoryReader, cards_ptr: int, count: int,
                               sample_size: int = 200) -> tuple[int, int]:
    """Return ``(matches, sampled)`` — how many sampled keys are real GrpIds."""
    known = _load_known_grpids()
    if not known:
        return 0, 0
    entries_arr_ptr = reader.read_ptr(cards_ptr + 0x18)
    if not reader.is_valid_ptr(entries_arr_ptr):
        return 0, 0
    sampled = min(count, sample_size)
    data = reader.read_bytes(entries_arr_ptr + ARRAY_DATA, sampled * 16)
    matches = 0
    seen = 0
    for off in range(0, len(data), 16):
        hash_code = struct.unpack_from("<i", data, off)[0]
        if hash_code < 0:
            continue
        card_id = struct.unpack_from("<i", data, off + 8)[0]
        seen += 1
        if card_id in known:
            matches += 1
    return matches, max(seen, 1)


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


def _sample_cards_dict_full(
    reader: BaseMemoryReader, cards_ptr: int, count: int, sample_size: int = 64
) -> tuple[int, int, int]:
    """Probe a candidate cards-dict and return ``(valid, distinct, sampled)``.

    ``valid`` is how many of the first ``sample_size`` entries pass MTGA's
    cardId / quantity / hash-code shape check; ``distinct`` is how many
    unique card IDs were seen; ``sampled`` is how many entries we actually
    examined. Real MTGA Cards dictionaries have nearly 100% valid + nearly
    100% distinct, while incidental same-shape dicts (booster history,
    etc.) score much lower.
    """
    if count <= 0 or count > 50000:
        return 0, 0, 0
    entries_arr_ptr = reader.read_ptr(cards_ptr + 0x18)
    if not reader.is_valid_ptr(entries_arr_ptr):
        return 0, 0, 0
    sampled = min(count, sample_size)
    data = reader.read_bytes(entries_arr_ptr + ARRAY_DATA, sampled * 16)
    valid = 0
    distinct: set[int] = set()
    for off in range(0, len(data), 16):
        hash_code = struct.unpack_from("<i", data, off)[0]
        card_id = struct.unpack_from("<i", data, off + 8)[0]
        quantity = struct.unpack_from("<i", data, off + 12)[0]
        if (
            hash_code >= 0
            and 1000 <= card_id <= 2_000_000
            and 1 <= quantity <= 20
        ):
            valid += 1
            distinct.add(card_id)
    return valid, len(distinct), sampled


def _sample_cards_dict(reader: BaseMemoryReader, cards_ptr: int, count: int) -> int:
    """Legacy scalar sampler — kept for callers that just need a yes/no signal."""
    valid, distinct, sampled = _sample_cards_dict_full(reader, cards_ptr, count, sample_size=32)
    if distinct < 8 or sampled == 0:
        return 0
    if valid / sampled < 0.5:
        return 0
    return valid + distinct


def _scan_for_cards_dict(reader: BaseMemoryReader) -> tuple[int, int]:
    """Brute-force heap scan for the collection dictionary object.

    Two-stage filter:
      1. Cheap pass with a 32-entry sample to enumerate candidates.
      2. Re-probe survivors with a 200-entry sample so the rank reflects
         actual content quality (real MTGA Cards dict has ~100% valid +
         ~100% distinct entries; opportunistic same-shape dicts don't).
    """
    print("[*] Falling back to heap scan for collection dictionary...")
    CHUNK = 0x100000
    raw_candidates: list[tuple[int, int]] = []  # (count, cards_ptr)

    for start, end in reader.get_heap_ranges():
        for base in range(start, end, CHUNK):
            data = reader.read_bytes(base, CHUNK)
            if data == b"\x00" * CHUNK:
                continue
            for pos in range(0, CHUNK - 0x28, 8):
                count = struct.unpack_from("<i", data, pos + 0x20)[0]
                if count < 1500 or count > 10000:
                    continue
                cards_ptr = base + pos
                sample_valid = _sample_cards_dict(reader, cards_ptr, count)
                if sample_valid >= 20:
                    raw_candidates.append((count, cards_ptr))
        if raw_candidates:
            print(
                f"[+] Heap range {hex(start)}-{hex(end)} produced "
                f"{len(raw_candidates)} candidate(s) so far"
            )

    if not raw_candidates:
        raise RuntimeError("Could not find collection dictionary in MTGA memory")

    print(f"[*] Re-probing {len(raw_candidates)} candidate(s) with deeper sample...")
    known_grpids = _load_known_grpids()
    scored: list[tuple[tuple, int, int, int, int, int]] = []
    for count, cards_ptr in raw_candidates:
        valid, distinct, sampled = _sample_cards_dict_full(
            reader, cards_ptr, count, sample_size=200
        )
        if sampled == 0:
            continue
        valid_ratio = valid / sampled
        distinct_ratio = (distinct / valid) if valid else 0.0
        if valid_ratio < 0.6 or distinct_ratio < 0.85:
            continue

        oracle_score = 0.0
        oracle_matches = oracle_seen = 0
        if known_grpids:
            oracle_matches, oracle_seen = _candidate_card_id_overlap(
                reader, cards_ptr, count, sample_size=200
            )
            oracle_score = oracle_matches / oracle_seen if oracle_seen else 0.0
            if oracle_score < 0.5:
                # The keys don't look like real card GrpIds — ignore even if
                # the dict shape passed the loose validity check.
                print(
                    f"    rejected {hex(cards_ptr)} count={count}: "
                    f"GrpId-overlap {oracle_matches}/{oracle_seen} = {oracle_score:.0%}"
                )
                continue

        # Rank: oracle hit-rate first (real card collections have ~100%),
        # then valid ratio, then count.
        rank_key = (
            round(oracle_score, 2),
            round(valid_ratio, 2),
            round(distinct_ratio, 2),
            count,
        )
        scored.append((rank_key, valid, distinct, sampled, count, cards_ptr))

    if not scored:
        if known_grpids:
            raise RuntimeError(
                "No memory dict had keys matching real GrpIds — MTGA's collection "
                "is likely not loaded in memory right now (open Decks or Collection screen)."
            )
        # No DB oracle available — degrade to permissive rank so user still
        # gets *some* snapshot, and warn loudly.
        print("[!] No candidate passed strict ratio; using best-effort rank without DB oracle")
        for count, cards_ptr in raw_candidates:
            scored.append(((0.0, 0.0, 0.0, count), 0, 0, 0, count, cards_ptr))

    scored.sort(reverse=True, key=lambda item: item[0])
    rank_key, valid, distinct, sampled, count, cards_ptr = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    print(
        f"[+] Selected candidate at {hex(cards_ptr)}: count={count} "
        f"sample valid={valid}/{sampled} distinct={distinct} "
        f"(rank_key={rank_key})"
    )
    if runner_up:
        ru_key, _v, _d, _s, ru_count, ru_ptr = runner_up
        print(f"    runner-up: count={ru_count} {hex(ru_ptr)} (rank_key={ru_key})")
    return cards_ptr, count


def _find_parent_wrapper(reader: BaseMemoryReader, cards_ptr: int) -> int | None:
    """Find an object that points to cards_ptr at offset +72."""
    target = struct.pack("<Q", cards_ptr)
    CHUNK = 0x100000

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
                wrapper_addr = base + idx - 72
                if wrapper_addr > start:
                    inv_ptr = reader.read_ptr(wrapper_addr + 64)
                    if reader.is_valid_ptr(inv_ptr):
                        gold = reader.read_i32(inv_ptr + 32)
                        gems = reader.read_i32(inv_ptr + 36)
                        wc_common = reader.read_i32(inv_ptr + 16)
                        wc_uncommon = reader.read_i32(inv_ptr + 20)
                        wc_rare = reader.read_i32(inv_ptr + 24)
                        wc_mythic = reader.read_i32(inv_ptr + 28)
                        if (
                            0 <= gold <= 1_000_000
                            and 0 <= gems <= 100_000
                            and 0 <= wc_common <= 10_000
                            and 0 <= wc_uncommon <= 10_000
                            and 0 <= wc_rare <= 10_000
                            and 0 <= wc_mythic <= 10_000
                        ):
                            print(
                                "[+] Inventory candidate "
                                f"gold={gold} gems={gems} wc={wc_common}/{wc_uncommon}/{wc_rare}/{wc_mythic}"
                            )
                            return wrapper_addr
                pos = idx + 8
    return None


def _looks_like_inventory(reader: BaseMemoryReader, inv_addr: int) -> bool:
    """Validate a plausible ClientPlayerInventory-like struct."""
    wc_common = reader.read_i32(inv_addr + 16)
    wc_uncommon = reader.read_i32(inv_addr + 20)
    wc_rare = reader.read_i32(inv_addr + 24)
    wc_mythic = reader.read_i32(inv_addr + 28)
    gold = reader.read_i32(inv_addr + 32)
    gems = reader.read_i32(inv_addr + 36)
    vault_progress = reader.read_i32(inv_addr + 48)

    values = (wc_common, wc_uncommon, wc_rare, wc_mythic, gold, gems)

    return (
        0 <= wc_common <= 10_000
        and 0 <= wc_uncommon <= 10_000
        and 0 <= wc_rare <= 10_000
        and 0 <= wc_mythic <= 10_000
        and 0 <= gold <= 1_000_000
        and 0 <= gems <= 100_000
        and 0 <= vault_progress <= 10_000
        and (gold or gems or wc_common or wc_uncommon or wc_rare or wc_mythic)
        and len(set(values)) > 1
    )


def _find_inventory_candidates(reader: BaseMemoryReader, limit: int = 24) -> list[int]:
    """Find plausible inventory structs without relying on account-specific values."""
    print("[*] Searching heap for plausible inventory structs...")
    CHUNK = 0x100000
    candidates: list[int] = []
    seen: set[int] = set()

    for start, end in reader.get_heap_ranges():
        for base in range(start, end, CHUNK):
            data = reader.read_bytes(base, CHUNK)
            if data == b"\x00" * CHUNK:
                continue
            for pos in range(0, CHUNK - 64, 8):
                inv_addr = base + pos
                if inv_addr in seen or inv_addr <= start or inv_addr < start + 0x1000:
                    continue
                if not _looks_like_inventory(reader, inv_addr):
                    continue
                wc_common = reader.read_i32(inv_addr + 16)
                wc_uncommon = reader.read_i32(inv_addr + 20)
                wc_rare = reader.read_i32(inv_addr + 24)
                wc_mythic = reader.read_i32(inv_addr + 28)
                gold = reader.read_i32(inv_addr + 32)
                gems = reader.read_i32(inv_addr + 36)
                print(
                    "[+] Inventory struct candidate at "
                    f"{hex(inv_addr)} gold={gold} gems={gems} "
                    f"wc={wc_common}/{wc_uncommon}/{wc_rare}/{wc_mythic}"
                )
                seen.add(inv_addr)
                candidates.append(inv_addr)
                if len(candidates) >= limit:
                    return candidates
    return candidates


def _find_cards_from_inventory(reader: BaseMemoryReader, inv_ptr: int) -> tuple[int, int] | None:
    """Find wrapper object(s) pointing to inv_ptr, then locate a cards dictionary field."""
    print(f"[*] Searching wrapper objects referencing inventory {hex(inv_ptr)}...")
    target = struct.pack("<Q", inv_ptr)
    CHUNK = 0x100000
    best: tuple[int, int, int] | None = None

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
                ptr_addr = base + idx
                for inv_off in range(16, 160, 8):
                    wrapper_addr = ptr_addr - inv_off
                    if wrapper_addr <= start:
                        continue
                    for field_off in range(16, 176, 8):
                        if field_off == inv_off:
                            continue
                        maybe_cards = reader.read_ptr(wrapper_addr + field_off)
                        if not reader.is_valid_ptr(maybe_cards):
                            continue
                        count = reader.read_i32(maybe_cards + 0x20)
                        sample_valid = _sample_cards_dict(reader, maybe_cards, count)
                        if sample_valid >= 24:
                            cand = (sample_valid, count, maybe_cards)
                            if best is None or cand > best:
                                best = cand
                pos = idx + 8

    if not best:
        return None
    sample_valid, count, cards_ptr = best
    print(f"[+] Selected cards dictionary {hex(cards_ptr)} count={count} sample_valid={sample_valid}")
    return cards_ptr, count


# ── Inventory service discovery ───────────────────────────────────

_CARDS_FIELD_HINTS = ("Cards", "_cards", "cards", "m_cards", "CardsAndQuantities")
_INVENTORY_FIELD_HINTS = (
    "m_inventory",
    "_inventory",
    "Inventory",
    "PlayerInventory",
    "ClientPlayerInventory",
)
_DISCRIMINATOR_FIELDS = (
    "gold",
    "gems",
    "wc_common",
    "wc_uncommon",
    "wc_rare",
    "wc_mythic",
    "Cards",
    "m_inventory",
)


def _read_class_full_name(reader: BaseMemoryReader, class_ptr: int) -> str:
    name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
    if not reader.is_valid_ptr(name_ptr):
        return ""
    name = reader.read_cstring(name_ptr, 128)
    if not name:
        return ""
    ns_ptr = reader.read_ptr(class_ptr + CLASS_NAMESPACE)
    ns = reader.read_cstring(ns_ptr, 128) if reader.is_valid_ptr(ns_ptr) else ""
    return f"{ns}.{name}" if ns else name


def _scan_class_instances(
    reader: BaseMemoryReader,
    class_ptr: int,
    *,
    cards_offset: int,
    max_candidates: int = 4,
) -> list[tuple[int, int, int]]:
    """Find heap objects whose class pointer matches class_ptr and whose
    cards-field at ``cards_offset`` looks like a CardsAndQuantity dict."""
    target = struct.pack("<Q", class_ptr)
    CHUNK = 0x100000
    found: list[tuple[int, int, int]] = []

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
                cards_ptr = reader.read_ptr(addr + cards_offset)
                if reader.is_valid_ptr(cards_ptr):
                    count = reader.read_i32(cards_ptr + 0x20)
                    if 100 < count < 30000:
                        sample = _sample_cards_dict(reader, cards_ptr, count)
                        if sample >= 16:
                            found.append((addr, cards_ptr, count))
                            if len(found) >= max_candidates:
                                return found
                pos = idx + 8
    return found


def _try_class_for_inventory(
    reader: BaseMemoryReader,
    class_ptr: int,
    *,
    cards_offset_hints: list[int],
    inventory_offset_hint: int | None,
) -> tuple[int, int, int, int, int] | None:
    """Locate instances of ``class_ptr`` and pick the best cards-dict candidate.

    Returns (isw_addr, cards_ptr, count, cards_offset, inventory_offset) or
    ``None`` if nothing plausible is found.
    """
    fields = list_fields(reader, class_ptr)
    field_offsets = {name: offset for name, offset in fields}

    cards_offsets: list[int] = []
    for hint in _CARDS_FIELD_HINTS:
        if hint in field_offsets:
            cards_offsets.append(field_offsets[hint])
    for off in cards_offset_hints:
        if off not in cards_offsets:
            cards_offsets.append(off)

    inventory_offsets: list[int] = []
    for hint in _INVENTORY_FIELD_HINTS:
        if hint in field_offsets:
            inventory_offsets.append(field_offsets[hint])
    if inventory_offset_hint is not None and inventory_offset_hint not in inventory_offsets:
        inventory_offsets.append(inventory_offset_hint)

    for cards_offset in cards_offsets:
        candidates = _scan_class_instances(reader, class_ptr, cards_offset=cards_offset)
        if not candidates:
            continue
        addr, cards_ptr, count = max(candidates, key=lambda item: item[2])
        inventory_offset = inventory_offsets[0] if inventory_offsets else 64
        return addr, cards_ptr, count, cards_offset, inventory_offset
    return None


def _resolve_via_class_names(
    reader: BaseMemoryReader,
    table_addr: int,
    cache: dict,
) -> tuple[int, int, int, str, int, int] | None:
    cached_layouts: dict = cache.get("isw_field_layouts", {}) if cache else {}
    missing: list[str] = list(cache.get("isw_class_names_missing", [])) if cache else []
    for class_name in cache.get("isw_class_names", []) if cache else []:
        if class_name in missing:
            print(f"[*] Skipping class '{class_name}' — cached as missing on this build")
            continue
        try:
            class_ptr = find_class_by_name(reader, table_addr, class_name)
        except RuntimeError:
            print(f"[*] Class '{class_name}' not in this table; recording as missing")
            cache.setdefault("isw_class_names_missing", []).append(class_name)
            continue
        layout = cached_layouts.get(class_name, {})
        result = _try_class_for_inventory(
            reader,
            class_ptr,
            cards_offset_hints=[layout.get("cards", 72)],
            inventory_offset_hint=layout.get("inventory", 64),
        )
        if result:
            addr, cards_ptr, count, cards_off, inv_off = result
            print(
                f"[+] Resolved inventory via cached class '{class_name}' "
                f"at {hex(addr)} (cards@+{cards_off}, count={count})"
            )
            return addr, cards_ptr, count, class_name, cards_off, inv_off
    return None


_NAME_NEEDLES = (
    "InventoryServiceWrapper",
    "InventoryService",
    "InventoryWrapper",
    "PlayerInventory",
    "ClientInventory",
    "Inventory",
    "Collection",
)
_REQUIRED_FIELDS_STRONG = {"Cards", "m_inventory"}
_REQUIRED_FIELDS_WEAK = {"Cards", "gold", "gems"}


def _resolve_via_field_signature(
    reader: BaseMemoryReader,
    table_addr: int,
) -> tuple[int, int, int, str, int, int] | None:
    """Narrow class candidates by substring first (cheap), then inspect fields.

    Listing fields for every class in a 300k-entry type table costs minutes;
    substring filtering on names alone takes seconds, and the inventory
    wrapper consistently lives under a class whose name contains one of
    ``_NAME_NEEDLES`` even after MTGA renames it.
    """
    print("[*] Field-signature class discovery (substring-narrowed)...")

    seen: dict[int, str] = {}
    for needle in _NAME_NEEDLES:
        matches = find_classes_by_substring(reader, table_addr, needle)
        for full_name, class_ptr in matches:
            if class_ptr not in seen:
                seen[class_ptr] = full_name
        if seen:
            print(f"    '{needle}' → {len(matches)} match(es); pool size now {len(seen)}")

    if not seen:
        print("[!] No substring matches for inventory-wrapper class")
        return None

    scored: list[tuple[int, str, int, list[tuple[str, int]]]] = []
    for class_ptr, full_name in seen.items():
        fields = list_fields(reader, class_ptr)
        names = {name for name, _ in fields}
        score = 0
        if _REQUIRED_FIELDS_STRONG.issubset(names):
            score += 10
        if _REQUIRED_FIELDS_WEAK.issubset(names):
            score += 6
        if "Cards" in names:
            score += 4
        if "m_inventory" in names or "_inventoryServiceWrapper" in names:
            score += 3
        score += sum(1 for n in ("gold", "gems", "wc_common", "wc_rare") if n in names)
        if any(tag in full_name for tag in ("Inventory", "Wrapper", "Collection")):
            score += 1
        if score > 0:
            scored.append((score, full_name, class_ptr, fields))

    scored.sort(reverse=True, key=lambda item: item[0])
    print(f"[*] Scored {len(scored)} candidates from {len(seen)} substring matches")
    for score, full_name, class_ptr, fields in scored[:5]:
        present = [(n, o) for n, o in fields if n in {"Cards", "m_inventory", "gold", "gems"}]
        rendered = ", ".join(f"{n}@{o}" for n, o in present)
        print(f"    score={score} {full_name} -> {hex(class_ptr)} [{rendered}]")

    for score, full_name, class_ptr, _fields in scored[:12]:
        result = _try_class_for_inventory(
            reader,
            class_ptr,
            cards_offset_hints=[72, 64, 80, 96, 56, 88],
            inventory_offset_hint=None,
        )
        if result:
            addr, cards_ptr, count, cards_off, inv_off = result
            print(
                f"[+] Resolved inventory via field-signature class "
                f"'{full_name}' at {hex(addr)} (cards@+{cards_off}, count={count})"
            )
            return addr, cards_ptr, count, full_name, cards_off, inv_off
    return None


def _try_cached_cards_ptr(reader: BaseMemoryReader, cache: dict) -> tuple[int, int] | None:
    """Validate the previously-successful Cards dict pointer is still alive.

    Two-pronged check: shape + DB-oracle GrpId overlap. A bare shape match
    is no good — many heap dicts pass shape validation but contain
    unrelated IDs (we previously cached such a phantom for hours).
    """
    cards_ptr = cache.get("last_cards_ptr", 0)
    if not cards_ptr:
        return None
    if not reader.is_valid_ptr(cards_ptr):
        return None
    count = reader.read_i32(cards_ptr + 0x20)
    if not (1500 <= count <= 10000):
        return None
    valid, distinct, sampled = _sample_cards_dict_full(reader, cards_ptr, count, sample_size=64)
    if sampled == 0 or valid / sampled < 0.7 or (distinct and distinct / valid < 0.85):
        return None

    known_grpids = _load_known_grpids()
    if known_grpids:
        oracle_matches, oracle_seen = _candidate_card_id_overlap(
            reader, cards_ptr, count, sample_size=64
        )
        oracle_score = oracle_matches / oracle_seen if oracle_seen else 0.0
        if oracle_score < 0.7:
            print(
                f"[*] Cached cards_ptr {hex(cards_ptr)} fails GrpId oracle "
                f"({oracle_matches}/{oracle_seen} = {oracle_score:.0%}); will rediscover"
            )
            return None
        print(
            f"[+] Cached cards_ptr {hex(cards_ptr)} still valid: count={count} "
            f"GrpId-overlap {oracle_matches}/{oracle_seen} = {oracle_score:.0%}"
        )
    else:
        print(
            f"[+] Cached cards_ptr {hex(cards_ptr)} still valid: count={count} "
            f"sample valid={valid}/{sampled} distinct={distinct} (no DB oracle)"
        )
    return cards_ptr, count


def _substring_pool_is_stdlib_only(matches: list[tuple[str, int]]) -> bool:
    """If every substring match is a System.* / Unity / generic type, the
    type table we found doesn't contain MTGA application classes — and
    field-signature discovery can't possibly succeed."""
    if not matches:
        return True
    for full_name, _class_ptr in matches[:32]:
        if not any(
            full_name.startswith(prefix) or prefix in full_name
            for prefix in ("System.", "Unity", "Microsoft.", "mscorlib", "Mono.")
        ):
            return False
    return True


def find_inventory_service(
    reader: BaseMemoryReader,
    table_addr: int,
    cache: dict | None = None,
) -> tuple[int, int, int]:
    """Locate the inventory wrapper object on the heap.

    Resolution order (each step writes back to ``cache`` on success):
      0. Cached cards_ptr direct hit — millisecond cache validation.
      1. Cached class names — try every class name that worked previously.
      2. Field-signature discovery — find any class whose field set matches
         the inventory wrapper shape (works even after MTGA renames it).
         Skipped if the type table only contains stdlib types.
      3. Brute heap scan for a CardsAndQuantity-shaped dictionary.

    Returns ``(isw_addr, cards_ptr, card_count)``. ``isw_addr`` may be 0 if
    only the cards dictionary could be recovered (collection-only mode).
    """
    cache = cache if cache is not None else {}

    cached_direct = _try_cached_cards_ptr(reader, cache)
    if cached_direct:
        cards_ptr, count = cached_direct
        return 0, cards_ptr, count

    resolved = _resolve_via_class_names(reader, table_addr, cache)

    field_sig_useful = cache.get("field_signature_useful", True)
    if not resolved and field_sig_useful:
        # Cheap probe: look at any non-stdlib substring hits. If the table
        # only has System.* generics, field-signature discovery is pointless.
        probe = find_classes_by_substring(reader, table_addr, "Inventory")
        if _substring_pool_is_stdlib_only(probe):
            print(
                "[*] Skipping field-signature discovery — type table contains "
                "only stdlib types (no MTGA application classes)"
            )
            cache["field_signature_useful"] = False
        else:
            resolved = _resolve_via_field_signature(reader, table_addr)
    elif not resolved and not field_sig_useful:
        print("[*] Skipping field-signature discovery (cached: not useful on this build)")

    if resolved:
        addr, cards_ptr, count, class_name, cards_off, inv_off = resolved
        _cache_mod.remember_class_name(cache, class_name)
        _cache_mod.remember_isw_layout(cache, class_name, cards_off, inv_off)
        cache["last_cards_ptr"] = int(cards_ptr)
        cache["last_cards_count"] = int(count)
        return addr, cards_ptr, count

    print("[!] Class-based discovery failed; falling back to brute heap scan")
    cards_ptr, count = _scan_for_cards_dict(reader)
    wrapper_addr = _find_parent_wrapper(reader, cards_ptr)
    cache["last_cards_ptr"] = int(cards_ptr)
    cache["last_cards_count"] = int(count)
    if wrapper_addr:
        print(f"[+] Recovered wrapper object via cards dictionary at {hex(wrapper_addr)}")
        return wrapper_addr, cards_ptr, count
    return 0, cards_ptr, count


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
    if not isw_addr or not reader.is_valid_ptr(isw_addr):
        return PlayerInventory()

    direct = PlayerInventory(
        wc_common=reader.read_i32(isw_addr + 16),
        wc_uncommon=reader.read_i32(isw_addr + 20),
        wc_rare=reader.read_i32(isw_addr + 24),
        wc_mythic=reader.read_i32(isw_addr + 28),
        gold=reader.read_i32(isw_addr + 32),
        gems=reader.read_i32(isw_addr + 36),
        vault_progress=reader.read_i32(isw_addr + 48),
    )
    if (
        0 <= direct.gold <= 1_000_000
        and 0 <= direct.gems <= 100_000
        and 0 <= direct.wc_common <= 10_000
        and 0 <= direct.wc_uncommon <= 10_000
        and 0 <= direct.wc_rare <= 10_000
        and 0 <= direct.wc_mythic <= 10_000
        and (direct.gold or direct.gems or direct.wc_common or direct.wc_uncommon or direct.wc_rare or direct.wc_mythic)
    ):
        return direct

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
