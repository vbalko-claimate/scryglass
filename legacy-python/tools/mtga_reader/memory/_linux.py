"""Linux memory reader using process_vm_readv."""

import ctypes
import ctypes.util
import struct

from ._base import BaseMemoryReader

libc = ctypes.CDLL(ctypes.util.find_library("c"))


class _iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


class LinuxMemoryReader(BaseMemoryReader):
    """Read memory from a Linux process via process_vm_readv."""

    def __init__(self, pid: int):
        self.pid = pid
        # Quick validation: try to read /proc/{pid}/maps
        try:
            with open(f"/proc/{pid}/maps") as f:
                f.readline()
        except (FileNotFoundError, PermissionError) as e:
            raise PermissionError(
                f"Cannot access PID {pid}. Run as root or use ptrace scope 0."
            ) from e
        print(f"[+] Attached to PID {pid}")

    def read_bytes(self, addr: int, size: int) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        local = _iovec(ctypes.cast(buf, ctypes.c_void_p), size)
        remote = _iovec(ctypes.c_void_p(addr), size)
        nread = libc.process_vm_readv(
            self.pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0
        )
        if nread < 0:
            return b"\x00" * size
        return bytes(buf)

    def find_game_assembly_data_base(self) -> int:
        """Find GameAssembly.so data segment via /proc/{pid}/maps."""
        rw_regions = []
        with open(f"/proc/{self.pid}/maps") as f:
            for line in f:
                if "GameAssembly" not in line:
                    continue
                parts = line.split()
                addr_range = parts[0]
                perms = parts[1]
                start_hex, end_hex = addr_range.split("-")
                start = int(start_hex, 16)
                end = int(end_hex, 16)
                if "rw" in perms and "x" not in perms:
                    rw_regions.append((start, end - start))
                    print(f"[+] GameAssembly rw region: {hex(start)} ({(end - start) // 1024}KB)")

        if not rw_regions:
            raise RuntimeError("GameAssembly.so not found in /proc/maps")

        base = rw_regions[1][0] if len(rw_regions) >= 2 else rw_regions[0][0]
        print(f"[+] Using data segment: {hex(base)}")
        return base

    def get_heap_ranges(self) -> list[tuple[int, int]]:
        # Parse actual heap from /proc/maps
        ranges = []
        try:
            with open(f"/proc/{self.pid}/maps") as f:
                for line in f:
                    if "[heap]" in line or "anon" in line.lower():
                        parts = line.split()
                        start_hex, end_hex = parts[0].split("-")
                        start = int(start_hex, 16)
                        end = int(end_hex, 16)
                        if end - start > 0x100000:  # > 1MB
                            ranges.append((start, end))
        except (FileNotFoundError, PermissionError):
            pass
        return ranges or [(0x500000000, 0x800000000)]

    def is_valid_ptr(self, addr: int) -> bool:
        return 0x10000 < addr < 0x7FFFFFFFFFFF


def find_mtga_pid() -> int:
    """Find MTGA process ID via /proc."""
    import os
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        try:
            with open(f"/proc/{pid_str}/comm") as f:
                if "MTGA" in f.read():
                    return int(pid_str)
        except (FileNotFoundError, PermissionError):
            continue
    raise RuntimeError("MTGA not running")
