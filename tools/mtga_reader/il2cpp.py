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

# Default offsets for s_TypeInfoTable in GameAssembly __DATA. The persistent
# cache (`_cache.py`) extends and reorders this list across runs so a layout
# learned once is tried first on the next run.
_DEFAULT_OFFSETS = [0x24C10, 0x24360, 0x24350, 0x24370, 0x24340, 0x24380, 0x243A0]


# ── Type info table discovery ─────────────────────────────────────

def find_type_info_table(
    reader: BaseMemoryReader,
    data_base: int,
    cache: dict | None = None,
) -> int:
    """Find s_TypeInfoTable by scanning all GameAssembly __DATA segments.

    Tries cached offsets first (cheap), then falls back to a full scan of
    every pointer-aligned address in every writable data segment. New
    successful offsets are written back into ``cache`` if provided so the
    next run skips the slow path.
    """
    segments = reader.find_game_assembly_data_segments()

    cached_offsets: list[int] = []
    if cache is not None:
        cached_offsets = list(cache.get("type_info_offsets", _DEFAULT_OFFSETS))
    if not cached_offsets:
        cached_offsets = list(_DEFAULT_OFFSETS)

    # Phase 1: try cached offsets on each segment (fast)
    for seg_base, seg_size in segments:
        for offset in cached_offsets:
            if offset >= seg_size:
                continue
            table = _validate_table(reader, seg_base + offset)
            if table:
                print(f"[+] type_info_table at {hex(seg_base)}+{hex(offset)} -> {hex(table)}")
                if cache is not None:
                    from . import _cache as _cache_mod
                    _cache_mod.remember_int_list(cache, "type_info_offsets", offset)
                return table

    # Phase 2: full scan of all writable segments (thorough)
    print("[*] Cached offsets failed — scanning all data segments...")
    for seg_base, seg_size in segments:
        print(f"    Scanning {hex(seg_base)} ({seg_size // 1024}KB)...")
        for off in range(0, seg_size, 8):
            table = _validate_table(reader, seg_base + off, threshold=5)
            if table:
                print(f"[+] type_info_table at {hex(seg_base)}+{hex(off)} -> {hex(table)}")
                if cache is not None:
                    from . import _cache as _cache_mod
                    _cache_mod.remember_int_list(cache, "type_info_offsets", off)
                return table

    raise RuntimeError("Could not find type_info_table")


_IL2CPP_SENTINELS = (
    "Object",
    "String",
    "Int32",
    "Boolean",
    "Type",
    "Single",
    "Int64",
)


def _validate_table(reader: BaseMemoryReader, ptr_addr: int, threshold: int = 3) -> int | None:
    """Check if ptr_addr points to a real il2cpp type info table.

    Three-stage fast-fail to keep the brute scan cheap:
      1. The pointer at ``ptr_addr`` must point into mapped memory.
      2. Read up to 8 candidate class entries; if fewer than ``threshold``
         have ASCII names, abort immediately. This filters out the vast
         majority of pointer-aligned slots in the data segment.
      3. Only addresses that survive (1)+(2) get the deeper sentinel
         check (256 entries scanning for ``Object`` / ``String`` /
         ``Int32`` …). Without this gate, a phantom string table can
         match step (2) but won't contain il2cpp built-ins, so we
         reject it.
    """
    table_addr = reader.read_ptr(ptr_addr)
    if not reader.is_valid_ptr(table_addr):
        return None

    # Stage 1: cheap ASCII-name probe over a small window.
    valid_quick = 0
    for i in range(8):
        class_ptr = reader.read_ptr(table_addr + i * 8)
        if not reader.is_valid_ptr(class_ptr):
            continue
        name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
        if not reader.is_valid_ptr(name_ptr):
            continue
        name = reader.read_cstring(name_ptr, 64)
        if name and name.isascii() and len(name) < 200:
            valid_quick += 1
    if valid_quick < threshold:
        return None

    # Stage 2: deeper scan for sentinel class names. Real tables hit a
    # sentinel within the first ~64 entries; we cap at 256 just in case
    # the layout reorders early entries in some game version.
    valid = 0
    for i in range(256):
        class_ptr = reader.read_ptr(table_addr + i * 8)
        if not reader.is_valid_ptr(class_ptr):
            continue
        name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
        if not reader.is_valid_ptr(name_ptr):
            continue
        name = reader.read_cstring(name_ptr, 64)
        if not name or not name.isascii() or len(name) >= 200:
            continue
        valid += 1
        if name in _IL2CPP_SENTINELS:
            return table_addr
    return None


# ── Class lookup ──────────────────────────────────────────────────

def find_class_by_name(reader: BaseMemoryReader, table_addr: int,
                       target_name: str, max_scan: int = 300000) -> int:
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


def find_classes_by_substring(
    reader: BaseMemoryReader,
    table_addr: int,
    substring: str,
    max_scan: int = 300000,
) -> list[tuple[str, int]]:
    """Return classes whose fully-qualified name contains substring."""
    matches: list[tuple[str, int]] = []
    needle = substring.lower()
    for i in range(max_scan):
        class_ptr = reader.read_ptr(table_addr + i * 8)
        if not class_ptr or not reader.is_valid_ptr(class_ptr):
            continue
        name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
        if not reader.is_valid_ptr(name_ptr):
            continue
        name = reader.read_cstring(name_ptr, 128)
        if not name:
            continue
        ns_ptr = reader.read_ptr(class_ptr + CLASS_NAMESPACE)
        ns = reader.read_cstring(ns_ptr, 128) if reader.is_valid_ptr(ns_ptr) else ""
        full_name = f"{ns}.{name}" if ns else name
        if needle in full_name.lower():
            matches.append((full_name, class_ptr))
    return matches


def find_classes_with_fields(
    reader: BaseMemoryReader,
    table_addr: int,
    required_fields: set[str],
    max_scan: int = 300000,
) -> list[tuple[str, int, list[tuple[str, int]]]]:
    """Return classes that contain any of the requested field names."""
    matches: list[tuple[str, int, list[tuple[str, int]]]] = []
    for i in range(max_scan):
        class_ptr = reader.read_ptr(table_addr + i * 8)
        if not class_ptr or not reader.is_valid_ptr(class_ptr):
            continue
        fields = list_fields(reader, class_ptr)
        if not fields:
            continue
        present = [(name, offset) for name, offset in fields if name in required_fields]
        if not present:
            continue
        name_ptr = reader.read_ptr(class_ptr + CLASS_NAME)
        if not reader.is_valid_ptr(name_ptr):
            continue
        name = reader.read_cstring(name_ptr, 128)
        ns_ptr = reader.read_ptr(class_ptr + CLASS_NAMESPACE)
        ns = reader.read_cstring(ns_ptr, 128) if reader.is_valid_ptr(ns_ptr) else ""
        full_name = f"{ns}.{name}" if ns else name
        matches.append((full_name, class_ptr, present))
    return matches


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
