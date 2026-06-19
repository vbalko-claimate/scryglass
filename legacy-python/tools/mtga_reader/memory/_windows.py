"""Windows memory reader using kernel32 ReadProcessMemory."""

import ctypes
import ctypes.wintypes
import struct

from ._base import BaseMemoryReader

kernel32 = ctypes.windll.kernel32

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400


class WindowsMemoryReader(BaseMemoryReader):
    """Read memory from a Windows process via ReadProcessMemory."""

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
        )
        if not self.handle:
            raise PermissionError(
                f"OpenProcess failed for PID {pid}. Run as Administrator."
            )
        print(f"[+] Attached to PID {pid}")

    def read_bytes(self, addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(bytes_read)
        )
        if not ok:
            return b"\x00" * size
        return buf.raw

    def find_game_assembly_data_base(self) -> int:
        """Find GameAssembly.dll data section via EnumProcessModulesEx."""
        import ctypes.wintypes as wt

        psapi = ctypes.windll.psapi
        hMods = (ctypes.c_void_p * 1024)()
        cbNeeded = wt.DWORD()
        psapi.EnumProcessModulesEx(
            self.handle, ctypes.byref(hMods), ctypes.sizeof(hMods),
            ctypes.byref(cbNeeded), 0x03,
        )
        count = cbNeeded.value // ctypes.sizeof(ctypes.c_void_p)
        for i in range(count):
            name_buf = ctypes.create_unicode_buffer(260)
            psapi.GetModuleFileNameExW(self.handle, hMods[i], name_buf, 260)
            if "GameAssembly" in name_buf.value:
                # Module base is the handle value
                base = ctypes.cast(hMods[i], ctypes.c_void_p).value
                print(f"[+] GameAssembly.dll at {hex(base)}")

                # Parse PE headers to find .data section
                # DOS header -> PE offset at +0x3C
                pe_off = struct.unpack("<I", self.read_bytes(base + 0x3C, 4))[0]
                pe_addr = base + pe_off
                # Number of sections at PE+6
                n_sections = struct.unpack("<H", self.read_bytes(pe_addr + 6, 2))[0]
                # Optional header size at PE+20
                opt_size = struct.unpack("<H", self.read_bytes(pe_addr + 20, 2))[0]
                section_start = pe_addr + 24 + opt_size

                for s in range(n_sections):
                    s_addr = section_start + s * 40
                    s_name = self.read_bytes(s_addr, 8).rstrip(b"\x00").decode("ascii", errors="replace")
                    s_vsize = struct.unpack("<I", self.read_bytes(s_addr + 8, 4))[0]
                    s_rva = struct.unpack("<I", self.read_bytes(s_addr + 12, 4))[0]
                    s_chars = struct.unpack("<I", self.read_bytes(s_addr + 36, 4))[0]
                    is_writable = bool(s_chars & 0x80000000)
                    is_readable = bool(s_chars & 0x40000000)
                    if s_name == ".data" and is_writable and is_readable:
                        data_base = base + s_rva
                        print(f"[+] .data section at {hex(data_base)} ({s_vsize // 1024}KB)")
                        return data_base

                raise RuntimeError("No .data section in GameAssembly.dll")
        raise RuntimeError("GameAssembly.dll not found in process modules")

    def get_heap_ranges(self) -> list[tuple[int, int]]:
        return [
            (0x10000000, 0x70000000),
            (0x180000000, 0x300000000),
        ]

    def is_valid_ptr(self, addr: int) -> bool:
        return 0x10000 < addr < 0x7FFFFFFFFFFF

    def __del__(self):
        if hasattr(self, "handle") and self.handle:
            kernel32.CloseHandle(self.handle)


def find_mtga_pid() -> int:
    """Find MTGA.exe process ID."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq MTGA.exe", "/FO", "CSV", "/NH"],
            text=True,
        ).strip()
        for line in out.splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 2 and "MTGA" in parts[0]:
                return int(parts[1])
    except (subprocess.CalledProcessError, ValueError):
        pass
    raise RuntimeError("MTGA not running")
