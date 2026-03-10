"""Watch MTGA Player.log for new messages in real-time."""
import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator, Callable

from .log_parser import iter_messages_from_lines

LOG_PATH = Path.home() / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log"


class LogWatcher:
    """Watches Player.log and yields new parsed messages."""

    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path or LOG_PATH
        self._position = 0
        self._running = False

    def read_from_beginning(self) -> list[dict]:
        """Read and parse the entire log file (for catching up on current match)."""
        if not self.log_path.exists():
            return []

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            self._position = f.tell()

        lines = content.split("\n")
        return list(iter_messages_from_lines(lines))

    def read_new(self) -> list[dict]:
        """Read only new lines since last read."""
        if not self.log_path.exists():
            return []

        file_size = os.path.getsize(self.log_path)

        # Log was rotated/truncated — reset
        if file_size < self._position:
            self._position = 0

        if file_size == self._position:
            return []

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._position)
            new_content = f.read()
            self._position = f.tell()

        if not new_content.strip():
            return []

        lines = new_content.split("\n")
        return list(iter_messages_from_lines(lines))

    async def watch(self, callback: Callable[[dict], None], poll_interval: float = 0.1):
        """Watch for new messages and call callback for each."""
        self._running = True
        while self._running:
            messages = self.read_new()
            if messages:
                for msg in messages:
                    callback(msg)
                # Yield to event loop after processing batch so async tasks
                # (broadcast, advice) run before next poll
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(poll_interval)

    def stop(self):
        self._running = False
