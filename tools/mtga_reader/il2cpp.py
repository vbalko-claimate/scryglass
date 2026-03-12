"""IL2CPP type discovery and field traversal (platform-agnostic)."""

from .memory import BaseMemoryReader

# ── Il2CppClass offsets (Unity 2022.3) ────────────────────────────

CLASS_NAME = 0x10
CLASS_NAMESPACE = 0x18
CLASS_PARENT = 0x48
CLASS_GENERIC_CLASS = 0x50
CLASS_FIELDS = 0x80
CLASS_STATIC_FIELDS = 0xA8
CLASS_INSTANCE_SIZE = 0xF8
CLASS_FIELD_COUNT = 0x124

# FieldInfo stride
FIELD_NAME = 0x00
FIELD_TYPE = 0x08
FIELD_OFFSET = 0x18
FIELD_SIZE = 0x20  # 32 bytes per field entry

# Il2CppArray
ARRAY_LENGTH = 0x18
ARRAY_DATA = 0x20

# Known offsets for s_TypeInfoTable in GameAssembly __DATA (cached from previous runs)
_KNOWN_OFFSETS = [0x24C10, 0x24360, 0x24350, 0x24370, 0x24340, 0x24380, 0x243A0]


# ── Type info table discovery ─────────────────────────────────────

def find_type_info_table(reader: BaseMemoryReader, data_base: int) -> int:
    """Find s_TypeInfoTable by scanning all GameAssembly __DATA segments.

    Auto-discovers the table by scanning every pointer-aligned address in every
    writable data segment. No hardcoded offsets required (they're just hints
    for faster first-try).
    """
    segments = reader.find_game_assembly_data_segments()

    # Phase 1: try known offsets on each segment (fast)
    for seg_base, seg_size in segments:
        for offset in _KNOWN_OFFSETS:
            if offset >= seg_size:
                continue
            table = _validate_table(reader, seg_base + offset)
            if table:
                print(f"[+] type_info_table at {hex(seg_base)}+{hex(offset)} -> {hex(table)}")
                return table

    # Phase 2: full scan of all writable segments (thorough)
    print("[*] Known offsets failed — scanning all data segments...")
    for seg_base, seg_size in segments:
        print(f"    Scanning {hex(seg_base)} ({seg_size // 1024}KB)...")
        for off in range(0, seg_size, 8):
            table = _validate_table(reader, seg_base + off, threshold=5)
            if table:
                print(f"[+] type_info_table at {hex(seg_base)}+{hex(off)} -> {hex(table)}")
                # Remember for next time
                if off not in _KNOWN_OFFSETS:
                    _KNOWN_OFFSETS.insert(0, off)
                return table

    raise RuntimeError("Could not find type_info_table")


def _validate_table(reader: BaseMemoryReader, ptr_addr: int, threshold: int = 3) -> int | None:
    """Check if ptr_addr points to a valid type info table."""
    table_addr = reader.read_ptr(ptr_addr)
    if not reader.is_valid_ptr(table_addr):
        return None
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
    return table_addr if valid >= threshold else None


# ── Class lookup ──────────────────────────────────────────────────

def find_class_by_name(reader: BaseMemoryReader, table_addr: int,
                       target_name: str, max_scan: int = 80000) -> int:
    """Scan type_info_table for a class by name. Returns class pointer."""
    for i in range(max_scan):
        class_ptr = reader.read_ptr(table_addr + i * 8)
        if not class_ptr or not reader.is_valid_ptr(class_ptr):
            continue
        name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
        if not reader.is_valid_ptr(name_ptr):
            continue
        name = reader.read_cstring(name_ptr, 128)
        if name == target_name:
            ns_ptr = reader.read_ptr(class_ptr + CLASS_NAMESPACE)
            ns = reader.read_cstring(ns_ptr, 128) if reader.is_valid_ptr(ns_ptr) else ""
            print(f"[+] Found class '{ns}.{target_name}' at index {i} -> {hex(class_ptr)}")
            return class_ptr
    raise RuntimeError(f"Class '{target_name}' not found (scanned {max_scan} entries)")


# ── Field traversal ───────────────────────────────────────────────

def get_field_offset(reader: BaseMemoryReader, class_ptr: int, field_name: str) -> int:
    """Get instance offset for a named field. Returns offset."""
    fields_ptr = reader.read_ptr(class_ptr + CLASS_FIELDS)
    field_count = reader.read_i32(class_ptr + CLASS_FIELD_COUNT)

    if not reader.is_valid_ptr(fields_ptr):
        raise RuntimeError(f"Invalid fields ptr for class at {hex(class_ptr)}")

    scan_count = min(field_count, 200) if field_count > 0 else 200

    for i in range(scan_count):
        f_addr = fields_ptr + i * FIELD_SIZE
        f_name_ptr = reader.read_ptr(f_addr + FIELD_NAME)
        if not reader.is_valid_ptr(f_name_ptr):
            continue
        f_name = reader.read_cstring(f_name_ptr, 128)
        if not f_name or not f_name[0].isascii():
            continue
        if f_name == field_name:
            return reader.read_i32(f_addr + FIELD_OFFSET)

    raise RuntimeError(f"Field '{field_name}' not found in class at {hex(class_ptr)}")


def list_fields(reader: BaseMemoryReader, class_ptr: int) -> list[tuple[str, int]]:
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
