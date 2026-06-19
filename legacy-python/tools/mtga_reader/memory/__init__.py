"""Platform-specific memory reader factory."""

import sys

from ._base import BaseMemoryReader


def create_reader(pid: int) -> BaseMemoryReader:
    """Create a memory reader for the current platform."""
    if sys.platform == "darwin":
        from ._macos import MachMemoryReader
        return MachMemoryReader(pid)
    elif sys.platform == "win32":
        from ._windows import WindowsMemoryReader
        return WindowsMemoryReader(pid)
    elif sys.platform == "linux":
        from ._linux import LinuxMemoryReader
        return LinuxMemoryReader(pid)
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def find_pid() -> int:
    """Find MTGA process ID on the current platform."""
    if sys.platform == "darwin":
        from ._macos import find_mtga_pid
    elif sys.platform == "win32":
        from ._windows import find_mtga_pid
    elif sys.platform == "linux":
        from ._linux import find_mtga_pid
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")
    return find_mtga_pid()


__all__ = ["BaseMemoryReader", "create_reader", "find_pid"]
