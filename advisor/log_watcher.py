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

    def read_from_beginning(self, resume_position: int = 0) -> list[dict]:
        """Read and parse the log file for catching up.

        If resume_position > 0, seeks to that byte offset first (skipping
        already-processed content). Otherwise reads the last 10MB as a
        safe default to avoid multi-minute startup on huge logs.
        """
        if not self.log_path.exists():
            return []

        file_size = os.path.getsize(self.log_path)

        if resume_position > 0 and resume_position < file_size:
            skip = resume_position
        else:
            # Fallback: read last 10MB
            skip = max(0, file_size - 10 * 1024 * 1024)

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            if skip > 0:
                f.seek(skip)
                f.readline()  # skip partial line after seek
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
