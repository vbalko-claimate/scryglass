#!/usr/bin/env python3
"""Backup scryglass advisor.db — intended for cron/launchd."""
import sys
from pathlib import Path

# Add project root to path for advisor imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from advisor.database import backup_db, DB_PATH

def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    result = backup_db()
    if result:
        print(f"Backed up to {result} ({result.stat().st_size // 1024} KB)")
    else:
        print("No backup needed")

if __name__ == "__main__":
    main()
