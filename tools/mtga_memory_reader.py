#!/usr/bin/env python3
"""
PoC: Read MTGA card collection from process memory on macOS ARM64.

Based on research from mtgatool/mtga-reader (GPL-3.0).
Uses Mach kernel APIs via ctypes to read IL2CPP data structures.

Usage:
    sudo python tools/mtga_memory_reader.py

Requires: MTGA running, sudo (for task_for_pid).
"""

import ctypes
import ctypes.util
import json
import struct
import subprocess
import sys
from dataclasses import dataclass

# ── Mach kernel types ─────────────────────────────────────────────

libc = ctypes.CDLL(ctypes.util.find_library("c"))

# Mach types
mach_port_t = ctypes.c_uint32
kern_return_t = ctypes.c_int32
mach_vm_address_t = ctypes.c_uint64
mach_vm_size_t = ctypes.c_uint64
pid_t = ctypes.c_int32

# kern_return_t task_for_pid(mach_port_t, int pid, mach_port_t *task)
libc.task_for_pid.restype = kern_return_t
libc.task_for_pid.argtypes = [mach_port_t, pid_t, ctypes.POINTER(mach_port_t)]

# mach_task_self_ is a GLOBAL VARIABLE (not a function!)
# mach_task_self() in C is a macro expanding to mach_task_self_
def mach_task_self() -> int:
    return ctypes.c_uint32.in_dll(libc, "mach_task_self_").value

# kern_return_t mach_vm_read_overwrite(task, addr, size, data, *outsize)
libc.mach_vm_read_overwrite.restype = kern_return_t
libc.mach_vm_read_overwrite.argtypes = [
    mach_port_t, mach_vm_address_t, mach_vm_size_t,
    mach_vm_address_t, ctypes.POINTER(mach_vm_size_t),
]

KERN_SUCCESS = 0


# ── Memory reader ─────────────────────────────────────────────────

class MachMemoryReader:
    """Read memory from another process via Mach APIs."""

    def __init__(self, pid: int):
        self.pid = pid
        self.task = mach_port_t(0)
        self_task = mach_task_self()
        kr = libc.task_for_pid(mach_port_t(self_task), pid_t(pid), ctypes.byref(self.task))
        if kr != KERN_SUCCESS:
            raise PermissionError(
                f"task_for_pid failed (kr={kr}). Run with sudo and ensure SIP allows debugging."
            )
        print(f"[+] Attached to PID {pid} (task port: {self.task.value})")

    def read_bytes(self, addr: int, size: int) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        out_size = mach_vm_size_t(0)
        kr = libc.mach_vm_read_overwrite(
            self.task,
            mach_vm_address_t(addr),
            mach_vm_size_t(size),
            ctypes.cast(buf, ctypes.c_void_p).value,
            ctypes.byref(out_size),
        )
        if kr != KERN_SUCCESS:
            return b"\x00" * size
        return bytes(buf)

    def read_ptr(self, addr: int) -> int:
        data = self.read_bytes(addr, 8)
        return struct.unpack("<Q", data)[0]

    def read_i32(self, addr: int) -> int:
        data = self.read_bytes(addr, 4)
        return struct.unpack("<i", data)[0]

    def read_u32(self, addr: int) -> int:
        data = self.read_bytes(addr, 4)
        return struct.unpack("<I", data)[0]

    def read_cstring(self, addr: int, max_len: int = 256) -> str:
        data = self.read_bytes(addr, max_len)
        end = data.find(b"\x00")
        if end >= 0:
            data = data[:end]
        try:
            return data.decode("ascii")
        except UnicodeDecodeError:
            return ""

    def is_valid_ptr(self, addr: int) -> bool:
        return 0x100000 < addr < 0x400000000


# ── IL2CPP struct offsets (MTGA / Unity 2022.3) ──────────────────

# Il2CppClass offsets
CLASS_NAME = 0x10
CLASS_NAMESPACE = 0x18
CLASS_PARENT = 0x48
CLASS_GENERIC_CLASS = 0x50
CLASS_FIELDS = 0x80
CLASS_STATIC_FIELDS = 0xA8
CLASS_INSTANCE_SIZE = 0xF8
CLASS_FIELD_COUNT = 0x124

# FieldInfo (32 bytes each)
FIELD_NAME = 0x00
FIELD_TYPE = 0x08
FIELD_PARENT = 0x10
FIELD_OFFSET = 0x18
FIELD_SIZE = 0x20  # stride between fields

# Il2CppType
TYPE_DATA = 0x00
TYPE_ATTRS = 0x08

# Il2CppString
STRING_LENGTH = 0x10
STRING_CHARS = 0x14

# Il2CppArray
ARRAY_LENGTH = 0x18
ARRAY_DATA = 0x20

# Type enum codes (low byte of attrs)
TYPE_CLASS = 0x12
TYPE_VALUETYPE = 0x11
TYPE_SZARRAY = 0x1D
TYPE_GENERIC = 0x15
TYPE_I4 = 0x08
TYPE_U4 = 0x09

# Known offsets for finding s_TypeInfoTable in GameAssembly __DATA
TYPE_INFO_TABLE_OFFSETS = [0x24360, 0x24350, 0x24370, 0x24340, 0x24380, 0x243A0]


# ── Mach VM region enumeration ────────────────────────────────────

# VM region info structs
class vm_region_basic_info_64(ctypes.Structure):
    _fields_ = [
        ("protection", ctypes.c_int32),
        ("max_protection", ctypes.c_int32),
        ("inheritance", ctypes.c_uint32),
        ("shared", ctypes.c_uint32),  # boolean
        ("reserved", ctypes.c_uint32),  # boolean
        ("offset", ctypes.c_uint64),
        ("behavior", ctypes.c_int32),
        ("user_wired_count", ctypes.c_uint16),
    ]

VM_REGION_BASIC_INFO_64 = 9
VM_REGION_BASIC_INFO_COUNT_64 = ctypes.sizeof(vm_region_basic_info_64) // 4

# kern_return_t mach_vm_region(task, *addr, *size, flavor, info, *count, *object_name)
libc.mach_vm_region.restype = kern_return_t
libc.mach_vm_region.argtypes = [
    mach_port_t,
    ctypes.POINTER(mach_vm_address_t),
    ctypes.POINTER(mach_vm_size_t),
    ctypes.c_int32,
    ctypes.POINTER(vm_region_basic_info_64),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(mach_port_t),
]

# int proc_regionfilename(pid, addr, buf, bufsize)
libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
libproc.proc_regionfilename.restype = ctypes.c_int32
libproc.proc_regionfilename.argtypes = [
    ctypes.c_int32, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_uint32,
]


def get_region_filename(pid: int, addr: int) -> str:
    """Get the mapped file for a memory region."""
    buf = ctypes.create_string_buffer(1024)
    ret = libproc.proc_regionfilename(pid, addr, buf, 1024)
    if ret > 0:
        return buf.value.decode("utf-8", errors="replace")
    return ""


# ── Process + GameAssembly discovery ──────────────────────────────

def find_mtga_pid() -> int:
    """Find MTGA process ID."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "MTGA"], text=True).strip()
        pids = [int(p) for p in out.split("\n") if p.strip()]
        if not pids:
            raise RuntimeError("MTGA not running")
        return min(pids)
    except subprocess.CalledProcessError:
        raise RuntimeError("MTGA not running (pgrep found nothing)")


def find_game_assembly_regions(task: mach_port_t, pid: int) -> list[tuple[int, int, str]]:
    """Enumerate VM regions belonging to GameAssembly.dylib.
    Returns [(addr, size, filename), ...] using mach_vm_region + proc_regionfilename.
    Much faster than vmmap."""
    regions = []
    address = mach_vm_address_t(0)
    size = mach_vm_size_t(0)
    info = vm_region_basic_info_64()
    count = ctypes.c_uint32(VM_REGION_BASIC_INFO_COUNT_64)
    object_name = mach_port_t(0)

    ga_regions = []

    while True:
        count.value = VM_REGION_BASIC_INFO_COUNT_64
        kr = libc.mach_vm_region(
            task,
            ctypes.byref(address),
            ctypes.byref(size),
            VM_REGION_BASIC_INFO_64,
            ctypes.byref(info),
            ctypes.byref(count),
            ctypes.byref(object_name),
        )
        if kr != KERN_SUCCESS:
            break

        addr_val = address.value
        size_val = size.value

        # Check if this region belongs to GameAssembly
        filename = get_region_filename(pid, addr_val)
        if "GameAssembly" in filename:
            prot = info.protection
            # protection bits: 1=read, 2=write, 4=execute
            prot_str = f"{'r' if prot & 1 else '-'}{'w' if prot & 2 else '-'}{'x' if prot & 4 else '-'}"
            ga_regions.append((addr_val, size_val, prot_str))

        # Move to next region
        address.value = addr_val + size_val

    return ga_regions


def find_game_assembly_data_base(task: mach_port_t, pid: int) -> int:
    """Find GameAssembly.dylib writable data segment base.
    The type_info_table lives in a r/w data segment (not r/x code)."""
    regions = find_game_assembly_regions(task, pid)

    if not regions:
        raise RuntimeError("Could not find any GameAssembly.dylib regions")

    print(f"[+] GameAssembly.dylib regions ({len(regions)} total):")
    rw_regions = []
    for addr, size, prot in regions:
        print(f"    {hex(addr)} - {hex(addr + size)} ({size // 1024}KB) [{prot}]")
        if "rw" in prot and "x" not in prot:
            rw_regions.append((addr, size))

    if not rw_regions:
        raise RuntimeError("No r/w data segments found in GameAssembly")

    # The type_info_table is typically in the largest rw segment,
    # or the second one if there are multiple (matching mtga-reader behavior)
    if len(rw_regions) >= 2:
        # Use second rw region (like mtga-reader)
        base = rw_regions[1][0]
        print(f"[+] Using second rw region: {hex(base)}")
    else:
        base = rw_regions[0][0]
        print(f"[+] Using first rw region: {hex(base)}")

    return base


# ── IL2CPP class discovery ────────────────────────────────────────

def find_type_info_table(reader: MachMemoryReader, data_base: int) -> int:
    """Find s_TypeInfoTable in GameAssembly __DATA segment."""
    for offset in TYPE_INFO_TABLE_OFFSETS:
        candidate_ptr = data_base + offset
        table_addr = reader.read_ptr(candidate_ptr)
        if not reader.is_valid_ptr(table_addr):
            continue

        # Validate: read first 20 entries, check if they look like Il2CppClass pointers
        valid = 0
        for i in range(20):
            class_ptr = reader.read_ptr(table_addr + i * 8)
            if not reader.is_valid_ptr(class_ptr):
                continue
            name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
            if reader.is_valid_ptr(name_ptr):
                name = reader.read_cstring(name_ptr, 64)
                if name and name.isascii() and len(name) < 200:
                    valid += 1

        if valid >= 3:
            print(f"[+] Found type_info_table at offset {hex(offset)} -> {hex(table_addr)} ({valid}/20 valid)")
            return table_addr

    # Brute force scan
    print("[!] Known offsets failed, brute-force scanning __DATA (first 256KB)...")
    for off in range(0, 0x40000, 8):
        candidate_ptr = data_base + off
        table_addr = reader.read_ptr(candidate_ptr)
        if not reader.is_valid_ptr(table_addr):
            continue
        valid = 0
        for i in range(10):
            class_ptr = reader.read_ptr(table_addr + i * 8)
            if not reader.is_valid_ptr(class_ptr):
                continue
            name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
            if reader.is_valid_ptr(name_ptr):
                name = reader.read_cstring(name_ptr, 64)
                if name and name.isascii() and len(name) < 200:
                    valid += 1
        if valid >= 5:
            print(f"[+] Found type_info_table at offset {hex(off)} -> {hex(table_addr)} ({valid}/10 valid)")
            return table_addr

    raise RuntimeError("Could not find type_info_table")


def find_class_by_name(reader: MachMemoryReader, table_addr: int, target_name: str, max_scan: int = 80000) -> int:
    """Scan type_info_table for a class with the given name."""
    for i in range(max_scan):
        class_ptr = reader.read_ptr(table_addr + i * 8)
        if class_ptr == 0:
            continue
        if not reader.is_valid_ptr(class_ptr):
            continue
        name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
        if not reader.is_valid_ptr(name_ptr):
            continue
        name = reader.read_cstring(name_ptr, 128)
        if name == target_name:
            ns_ptr = reader.read_ptr(class_ptr + CLASS_NAMESPACE)
            ns = reader.read_cstring(ns_ptr, 128) if reader.is_valid_ptr(ns_ptr) else ""
            print(f"[+] Found class '{ns}.{name}' at index {i} -> {hex(class_ptr)}")
            return class_ptr
    raise RuntimeError(f"Class '{target_name}' not found in type_info_table (scanned {max_scan} entries)")


# ── Field traversal ───────────────────────────────────────────────

def get_field_offset(reader: MachMemoryReader, class_ptr: int, field_name: str) -> tuple[int, int]:
    """Get a field's instance offset and type pointer from a class.
    Returns (offset, type_ptr)."""
    fields_ptr = reader.read_ptr(class_ptr + CLASS_FIELDS)
    field_count = reader.read_i32(class_ptr + CLASS_FIELD_COUNT)

    if not reader.is_valid_ptr(fields_ptr):
        raise RuntimeError(f"Invalid fields ptr for class at {hex(class_ptr)}")

    # Cap iteration — field_count offset may be wrong for this Unity version,
    # but we know fields_ptr is valid from list_fields working
    scan_count = min(field_count, 200) if field_count > 0 else 200

    for i in range(scan_count):
        f_addr = fields_ptr + i * FIELD_SIZE
        f_name_ptr = reader.read_ptr(f_addr + FIELD_NAME)
        if not reader.is_valid_ptr(f_name_ptr):
            continue
        f_name = reader.read_cstring(f_name_ptr, 128)
        if not f_name or not f_name[0].isascii():
            continue  # hit garbage, stop
        f_offset = reader.read_i32(f_addr + FIELD_OFFSET)
        f_type_ptr = reader.read_ptr(f_addr + FIELD_TYPE)
        if f_name == field_name:
            return f_offset, f_type_ptr

    raise RuntimeError(f"Field '{field_name}' not found in class at {hex(class_ptr)}")


def list_fields(reader: MachMemoryReader, class_ptr: int) -> list[tuple[str, int]]:
    """List all fields of a class. Returns [(name, offset), ...]."""
    fields_ptr = reader.read_ptr(class_ptr + CLASS_FIELDS)
    field_count = reader.read_i32(class_ptr + CLASS_FIELD_COUNT)
    result = []
    if not reader.is_valid_ptr(fields_ptr) or field_count <= 0:
        return result
    for i in range(min(field_count, 200)):
        f_addr = fields_ptr + i * FIELD_SIZE
        f_name_ptr = reader.read_ptr(f_addr + FIELD_NAME)
        if reader.is_valid_ptr(f_name_ptr):
            f_name = reader.read_cstring(f_name_ptr, 128)
            f_offset = reader.read_i32(f_addr + FIELD_OFFSET)
            result.append((f_name, f_offset))
    return result


# ── PAPA / WrapperController singleton discovery ──────────────────

def find_papa_instance(reader: MachMemoryReader, papa_class_ptr: int) -> int:
    """Find the PAPA singleton instance via static fields.
    Validates that the candidate's class pointer matches papa_class_ptr."""
    static_fields_ptr = reader.read_ptr(papa_class_ptr + CLASS_STATIC_FIELDS)
    print(f"[*] PAPA static_fields at {hex(static_fields_ptr)}")

    if not reader.is_valid_ptr(static_fields_ptr):
        raise RuntimeError(f"PAPA static_fields pointer invalid: {hex(static_fields_ptr)}")

    # Dump first 64 bytes of static fields area for debugging
    print(f"[*] Static fields area (first 64 bytes):")
    for off in range(0, 64, 8):
        val = reader.read_ptr(static_fields_ptr + off)
        valid = "PTR" if reader.is_valid_ptr(val) else "---"
        print(f"    +{off:2d}: {hex(val)} [{valid}]")

    # Try each pointer-sized offset, validate by checking class pointer
    for off in range(0, 64, 8):
        candidate = reader.read_ptr(static_fields_ptr + off)
        if not reader.is_valid_ptr(candidate):
            continue
        # A managed object's first 8 bytes = pointer to its Il2CppClass
        candidate_class = reader.read_ptr(candidate)
        if candidate_class == papa_class_ptr:
            print(f"[+] PAPA instance at {hex(candidate)} (static_fields + {off}, class verified)")
            return candidate
        else:
            class_name = ""
            if reader.is_valid_ptr(candidate_class):
                name_ptr = reader.read_ptr(candidate_class + CLASS_NAME)
                if reader.is_valid_ptr(name_ptr):
                    class_name = reader.read_cstring(name_ptr, 64)
            print(f"[*]   +{off}: {hex(candidate)} -> class={hex(candidate_class)} ({class_name}), not PAPA")

    # Fallback: try scanning heap ranges for PAPA instances
    print(f"[!] Static fields didn't contain PAPA instance. Trying heap scan...")
    # Typical IL2CPP heap ranges on macOS ARM64
    heap_ranges = [
        (0x145000000, 0x170000000),
        (0x280000000, 0x2C0000000),
        (0x305000000, 0x340000000),
    ]
    # Read PAPA instance_size to know object size
    instance_size = reader.read_i32(papa_class_ptr + CLASS_INSTANCE_SIZE)
    print(f"[*] PAPA instance_size: {instance_size} bytes")

    # Scan in large chunks, looking for class pointer matches
    CHUNK = 0x100000  # 1MB
    found = []
    for start, end in heap_ranges:
        for base in range(start, end, CHUNK):
            data = reader.read_bytes(base, CHUNK)
            if data == b"\x00" * CHUNK:
                continue
            # Search for papa_class_ptr as a 8-byte LE value
            target = struct.pack("<Q", papa_class_ptr)
            pos = 0
            while True:
                idx = data.find(target, pos)
                if idx < 0:
                    break
                addr = base + idx
                # Verify: object at addr should have class ptr and reasonable data
                found.append(addr)
                if len(found) >= 5:
                    break
                pos = idx + 8
            if len(found) >= 5:
                break
        if len(found) >= 5:
            break

    if found:
        print(f"[+] Found {len(found)} PAPA instance candidate(s) on heap:")
        for addr in found:
            print(f"    {hex(addr)}")
        # Use the first one
        instance = found[0]
        print(f"[+] Using PAPA instance at {hex(instance)}")
        return instance

    raise RuntimeError("Could not find PAPA singleton instance")


# ── Card collection reading ───────────────────────────────────────

@dataclass
class PlayerInventory:
    wc_common: int = 0
    wc_uncommon: int = 0
    wc_rare: int = 0
    wc_mythic: int = 0
    gold: int = 0
    gems: int = 0
    vault_progress: int = 0


def read_cards_dictionary(reader: MachMemoryReader, cards_ptr: int) -> dict[int, int]:
    """Read CardsAndQuantity dictionary (custom Dictionary<uint, int>)."""
    if not reader.is_valid_ptr(cards_ptr):
        raise RuntimeError(f"Cards pointer invalid: {hex(cards_ptr)}")

    # CardsAndQuantity layout:
    # +0x10: buckets (Int32[])
    # +0x18: entries (Entry[])
    # +0x20: count (i32)
    entries_arr_ptr = reader.read_ptr(cards_ptr + 0x18)
    count = reader.read_i32(cards_ptr + 0x20)

    print(f"[+] Cards dictionary: count={count}, entries_array={hex(entries_arr_ptr)}")

    if count <= 0 or count > 50000:
        # Try alternative offsets
        for off in [0x10, 0x18, 0x20, 0x28]:
            test_count = reader.read_i32(cards_ptr + off)
            if 100 < test_count < 30000:
                print(f"[?] Possible count at +{hex(off)}: {test_count}")

        raise RuntimeError(f"Suspicious card count: {count}")

    if not reader.is_valid_ptr(entries_arr_ptr):
        raise RuntimeError(f"Entries array pointer invalid: {hex(entries_arr_ptr)}")

    # Array data starts at +0x20 (after Il2CppArray header)
    entries_data = entries_arr_ptr + ARRAY_DATA

    cards: dict[int, int] = {}
    # Read in chunks for efficiency
    chunk_size = min(count, 1000)
    entry_stride = 16  # 4 ints: hashCode, next, key, value

    for chunk_start in range(0, count, chunk_size):
        chunk_end = min(chunk_start + chunk_size, count)
        n = chunk_end - chunk_start
        data = reader.read_bytes(entries_data + chunk_start * entry_stride, n * entry_stride)

        for i in range(n):
            off = i * entry_stride
            hash_code = struct.unpack_from("<i", data, off)[0]
            card_id = struct.unpack_from("<i", data, off + 8)[0]
            quantity = struct.unpack_from("<i", data, off + 12)[0]
            if hash_code >= 0 and card_id > 0 and 0 < quantity <= 99:
                cards[card_id] = quantity

    return cards


def read_inventory(reader: MachMemoryReader, inventory_ptr: int) -> PlayerInventory:
    """Read ClientPlayerInventory (wildcards, gold, gems, vault)."""
    if not reader.is_valid_ptr(inventory_ptr):
        return PlayerInventory()

    return PlayerInventory(
        wc_common=reader.read_i32(inventory_ptr + 16),
        wc_uncommon=reader.read_i32(inventory_ptr + 20),
        wc_rare=reader.read_i32(inventory_ptr + 24),
        wc_mythic=reader.read_i32(inventory_ptr + 28),
        gold=reader.read_i32(inventory_ptr + 32),
        gems=reader.read_i32(inventory_ptr + 36),
        vault_progress=reader.read_i32(inventory_ptr + 48),
    )


# ── Main ──────────────────────────────────────────────────────────

def main():
    print("=== MTGA Memory Reader PoC ===\n")

    # 1. Find MTGA process
    try:
        pid = find_mtga_pid()
    except RuntimeError as e:
        print(f"[!] {e}")
        sys.exit(1)
    print(f"[+] MTGA PID: {pid}")

    # 2. Attach to process
    try:
        reader = MachMemoryReader(pid)
    except PermissionError as e:
        print(f"[!] {e}")
        sys.exit(1)

    # 3. Find GameAssembly __DATA base (using mach_vm_region, not vmmap)
    data_base = find_game_assembly_data_base(reader.task, pid)
    print(f"[+] GameAssembly __DATA base: {hex(data_base)}")

    # 4. Find type_info_table
    table = find_type_info_table(reader, data_base)

    # 5. Find PAPA (WrapperController) class
    # Try both names - historically "PAPA" but might be "WrapperController"
    papa_class = None
    for name in ["PAPA", "WrapperController"]:
        try:
            papa_class = find_class_by_name(reader, table, name)
            break
        except RuntimeError:
            continue

    if papa_class is None:
        print("[!] Could not find PAPA or WrapperController class.")
        print("[*] Dumping first 200 class names for debugging...")
        for i in range(200):
            cp = reader.read_ptr(table + i * 8)
            if cp and reader.is_valid_ptr(cp):
                np = reader.read_ptr(cp + CLASS_NAME)
                if reader.is_valid_ptr(np):
                    n = reader.read_cstring(np, 128)
                    if n:
                        nsp = reader.read_ptr(cp + CLASS_NAMESPACE)
                        ns = reader.read_cstring(nsp, 128) if reader.is_valid_ptr(nsp) else ""
                        print(f"    [{i}] {ns}.{n}")
        sys.exit(1)

    # 6. List PAPA fields for debugging
    print(f"\n[*] PAPA class fields:")
    for name, offset in list_fields(reader, papa_class):
        print(f"    {name} (offset={offset})")

    # 7. Get singleton instance
    instance = find_papa_instance(reader, papa_class)

    # 8. Find inventory classes by name
    print(f"\n[*] Searching for inventory-related classes...")
    class_map = {}
    for search_name in ["InventoryManager", "AwsInventoryServiceWrapper",
                        "CardsAndQuantity", "ClientPlayerInventory"]:
        try:
            cls = find_class_by_name(reader, table, search_name)
            class_map[search_name] = cls
        except RuntimeError:
            print(f"[*]   {search_name}: not found")

    # Confirmed offsets from class field listings:
    # InventoryManager._inventoryServiceWrapper = +56
    # AwsInventoryServiceWrapper.m_inventory = +64
    # AwsInventoryServiceWrapper.<Cards>k__BackingField = +72
    # ClientPlayerInventory: wcCommon(+16), wcUncommon(+20), wcRare(+24), wcMythic(+28), gold(+32), gems(+36), vaultProgress(+48)

    # 9. Direct heap scan for AwsInventoryServiceWrapper instances
    # This is more reliable than navigating from PAPA (which gave false positives)
    isw_class = class_map.get("AwsInventoryServiceWrapper")
    if not isw_class:
        raise RuntimeError("AwsInventoryServiceWrapper class not found")

    print(f"\n[*] Scanning heap for AwsInventoryServiceWrapper instances (class={hex(isw_class)})...")

    CHUNK = 0x100000  # 1MB
    target = struct.pack("<Q", isw_class)
    isw_candidates = []

    # Scan heap ranges
    heap_ranges = [
        (0x110000000, 0x170000000),
        (0x280000000, 0x340000000),
    ]
    for start, end in heap_ranges:
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
                # Validate: Cards at +72 should be a valid pointer
                cards_ptr = reader.read_ptr(addr + 72)
                if reader.is_valid_ptr(cards_ptr):
                    # Dictionary should have entries at +0x18 and reasonable count at +0x20
                    entries_ptr = reader.read_ptr(cards_ptr + 0x18)
                    count = reader.read_i32(cards_ptr + 0x20)
                    if reader.is_valid_ptr(entries_ptr) and 100 < count < 30000:
                        isw_candidates.append((addr, cards_ptr, count))
                        print(f"[+] Found ISW at {hex(addr)}, Cards dict count={count}")
                pos = idx + 8
            if isw_candidates:
                break
        if isw_candidates:
            break

    if not isw_candidates:
        print("[!] No AwsInventoryServiceWrapper instances found on heap.")
        # Fallback: try InventoryManager class scan
        inv_class = class_map.get("InventoryManager")
        if inv_class:
            print(f"\n[*] Trying InventoryManager heap scan (class={hex(inv_class)})...")
            target2 = struct.pack("<Q", inv_class)
            for start, end in heap_ranges:
                for base in range(start, end, CHUNK):
                    data = reader.read_bytes(base, CHUNK)
                    if data == b"\x00" * CHUNK:
                        continue
                    pos = 0
                    while True:
                        idx = data.find(target2, pos)
                        if idx < 0:
                            break
                        addr = base + idx
                        print(f"[+] Found InventoryManager at {hex(addr)}")
                        # Try _inventoryServiceWrapper at +56
                        isw_ptr = reader.read_ptr(addr + 56)
                        if reader.is_valid_ptr(isw_ptr):
                            cards_ptr = reader.read_ptr(isw_ptr + 72)
                            if reader.is_valid_ptr(cards_ptr):
                                entries_ptr = reader.read_ptr(cards_ptr + 0x18)
                                count = reader.read_i32(cards_ptr + 0x20)
                                if reader.is_valid_ptr(entries_ptr) and 100 < count < 30000:
                                    isw_candidates.append((isw_ptr, cards_ptr, count))
                                    print(f"[+] -> ISW at {hex(isw_ptr)}, Cards dict count={count}")
                        pos = idx + 8
                    if isw_candidates:
                        break
                if isw_candidates:
                    break

    if not isw_candidates:
        raise RuntimeError("Could not find any inventory data in MTGA memory")

    # Use best candidate (highest card count)
    isw_addr, cards_ptr, count = max(isw_candidates, key=lambda x: x[2])
    print(f"\n[+] Using ISW at {hex(isw_addr)} with {count} cards")

    # 10. Read inventory
    inv_ptr = reader.read_ptr(isw_addr + 64)  # m_inventory at +64
    if reader.is_valid_ptr(inv_ptr):
        inventory = read_inventory(reader, inv_ptr)
        print(f"\n[+] Player Inventory:")
        print(f"    Wildcards: {inventory.wc_common}C / {inventory.wc_uncommon}U / {inventory.wc_rare}R / {inventory.wc_mythic}M")
        print(f"    Gold: {inventory.gold}, Gems: {inventory.gems}")
        print(f"    Vault: {inventory.vault_progress / 10:.1f}%")

    # 11. Read card collection
    print(f"\n[*] Reading card collection...")
    cards = read_cards_dictionary(reader, cards_ptr)
    print(f"[+] Read {len(cards)} unique cards!")

    total_cards = sum(cards.values())
    print(f"\n=== Collection Summary ===")
    print(f"Unique cards: {len(cards)}")
    print(f"Total cards:  {total_cards}")

    print(f"\nSample (first 20 entries, GrpId -> count):")
    for grp_id, cnt in sorted(cards.items())[:20]:
        print(f"    {grp_id}: {cnt}x")

    output_path = "mtga_collection_raw.json"
    with open(output_path, "w") as f:
        json.dump(cards, f, indent=2, sort_keys=True)
    print(f"\n[+] Saved to {output_path}")

    return cards


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
