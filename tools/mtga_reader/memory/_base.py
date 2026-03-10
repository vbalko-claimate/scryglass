"""Abstract base for platform-specific memory readers."""

import struct
from abc import ABC, abstractmethod


class BaseMemoryReader(ABC):
    """Read memory from another process. Subclasses implement read_bytes."""

    pid: int

    @abstractmethod
    def read_bytes(self, addr: int, size: int) -> bytes:
        """Read raw bytes from target process memory."""
        ...

    @abstractmethod
    def find_game_assembly_data_base(self) -> int:
        """Find the GameAssembly writable data segment base address."""
        ...

    @abstractmethod
    def get_heap_ranges(self) -> list[tuple[int, int]]:
        """Return (start, end) ranges to scan for heap objects."""
        ...

    def read_ptr(self, addr: int) -> int:
        return struct.unpack("<Q", self.read_bytes(addr, 8))[0]

    def read_i32(self, addr: int) -> int:
        return struct.unpack("<i", self.read_bytes(addr, 4))[0]

    def read_u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read_bytes(addr, 4))[0]

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
