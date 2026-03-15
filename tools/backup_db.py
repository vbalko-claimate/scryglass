#!/usr/bin/env python3
"""Backup scryglass advisor.db — intended for cron/launchd."""
import shutil
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "advisor.db"
BACKUP_DIR = DB_PATH.parent / "backups"
MAX_BACKUPS = 20

def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"advisor_{ts}.db"
    shutil.copy2(DB_PATH, dest)
    print(f"Backed up to {dest} ({dest.stat().st_size // 1024} KB)")

    # Rotate
    backups = sorted(BACKUP_DIR.glob("advisor_*.db"))
    while len(backups) > MAX_BACKUPS:
        backups[0].unlink()
        backups.pop(0)

if __name__ == "__main__":
    main()
