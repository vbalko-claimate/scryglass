"""macOS memory reader using Mach kernel APIs."""

import ctypes
import ctypes.util
import subprocess

from ._base import BaseMemoryReader

# ── Mach kernel types ─────────────────────────────────────────────

libc = ctypes.CDLL(ctypes.util.find_library("c"))

mach_port_t = ctypes.c_uint32
kern_return_t = ctypes.c_int32
mach_vm_address_t = ctypes.c_uint64
mach_vm_size_t = ctypes.c_uint64
pid_t = ctypes.c_int32

libc.task_for_pid.restype = kern_return_t
libc.task_for_pid.argtypes = [mach_port_t, pid_t, ctypes.POINTER(mach_port_t)]

libc.mach_vm_read_overwrite.restype = kern_return_t
libc.mach_vm_read_overwrite.argtypes = [
    mach_port_t, mach_vm_address_t, mach_vm_size_t,
    mach_vm_address_t, ctypes.POINTER(mach_vm_size_t),
]

KERN_SUCCESS = 0


def _mach_task_self() -> int:
    return ctypes.c_uint32.in_dll(libc, "mach_task_self_").value


# ── VM region enumeration ─────────────────────────────────────────

class _vm_region_basic_info_64(ctypes.Structure):
    _fields_ = [
        ("protection", ctypes.c_int32),
        ("max_protection", ctypes.c_int32),
        ("inheritance", ctypes.c_uint32),
        ("shared", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("offset", ctypes.c_uint64),
        ("behavior", ctypes.c_int32),
        ("user_wired_count", ctypes.c_uint16),
    ]


_VM_REGION_BASIC_INFO_64 = 9
_VM_REGION_BASIC_INFO_COUNT_64 = ctypes.sizeof(_vm_region_basic_info_64) // 4

libc.mach_vm_region.restype = kern_return_t
libc.mach_vm_region.argtypes = [
    mach_port_t,
    ctypes.POINTER(mach_vm_address_t),
    ctypes.POINTER(mach_vm_size_t),
    ctypes.c_int32,
    ctypes.POINTER(_vm_region_basic_info_64),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(mach_port_t),
]

libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
libproc.proc_regionfilename.restype = ctypes.c_int32
libproc.proc_regionfilename.argtypes = [
    ctypes.c_int32, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_uint32,
]


# ── Implementation ────────────────────────────────────────────────

class MachMemoryReader(BaseMemoryReader):
    """Read memory from a macOS process via Mach kernel APIs."""

    def __init__(self, pid: int):
        self.pid = pid
        self.task = mach_port_t(0)
        kr = libc.task_for_pid(
            mach_port_t(_mach_task_self()), pid_t(pid), ctypes.byref(self.task)
        )
        if kr != KERN_SUCCESS:
            raise PermissionError(
                f"task_for_pid failed (kr={kr}). Run with sudo."
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

    def find_game_assembly_data_segments(self) -> list[tuple[int, int]]:
        regions = self._find_ga_regions()
        if not regions:
            raise RuntimeError("No GameAssembly.dylib regions found")

        print(f"[+] GameAssembly.dylib regions ({len(regions)}):")
        rw_regions = []
        for addr, size, prot in regions:
            print(f"    {hex(addr)} - {hex(addr + size)} ({size // 1024}KB) [{prot}]")
            if "rw" in prot and "x" not in prot:
                rw_regions.append((addr, size))

        if not rw_regions:
            raise RuntimeError("No r/w data segments in GameAssembly")

        print(f"[+] Found {len(rw_regions)} writable data segments")
        return rw_regions

    def get_heap_ranges(self) -> list[tuple[int, int]]:
        return [
            (0x110000000, 0x170000000),
            (0x280000000, 0x340000000),
        ]

    def _find_ga_regions(self) -> list[tuple[int, int, str]]:
        address = mach_vm_address_t(0)
        size = mach_vm_size_t(0)
        info = _vm_region_basic_info_64()
        count = ctypes.c_uint32(_VM_REGION_BASIC_INFO_COUNT_64)
        object_name = mach_port_t(0)
        ga_regions = []

        while True:
            count.value = _VM_REGION_BASIC_INFO_COUNT_64
            kr = libc.mach_vm_region(
                self.task, ctypes.byref(address), ctypes.byref(size),
                _VM_REGION_BASIC_INFO_64, ctypes.byref(info),
                ctypes.byref(count), ctypes.byref(object_name),
            )
            if kr != KERN_SUCCESS:
                break
            addr_val, size_val = address.value, size.value
            filename = self._region_filename(addr_val)
            if "GameAssembly" in filename:
                prot = info.protection
                prot_str = f"{'r' if prot & 1 else '-'}{'w' if prot & 2 else '-'}{'x' if prot & 4 else '-'}"
                ga_regions.append((addr_val, size_val, prot_str))
            address.value = addr_val + size_val

        return ga_regions

    def _region_filename(self, addr: int) -> str:
        buf = ctypes.create_string_buffer(1024)
        ret = libproc.proc_regionfilename(self.pid, addr, buf, 1024)
        return buf.value.decode("utf-8", errors="replace") if ret > 0 else ""


def find_mtga_pid() -> int:
    """Find MTGA process ID via pgrep."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "MTGA"], text=True).strip()
        pids = [int(p) for p in out.split("\n") if p.strip()]
        if not pids:
            raise RuntimeError("MTGA not running")
        return min(pids)
    except subprocess.CalledProcessError:
        raise RuntimeError("MTGA not running")
