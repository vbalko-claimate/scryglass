#!/usr/bin/env python3
"""Refresh the local MTGA collection snapshot via a sudo-approved wrapper.

Expected flow:
1. A root-owned wrapper runs `python -m tools.mtga_reader ...` with sudo rights.
2. That wrapper writes a fresh memory snapshot to `~/MTG/my_collection_memory.json`.
3. This helper validates the snapshot and copies it into repo-local files used by tools.

Default command:
    sudo /usr/local/sbin/mtga-read-collection
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from advisor.collection_refresh import DEFAULT_SOURCE, sync_collection_snapshot


DEFAULT_CMD = "sudo /usr/local/sbin/mtga-read-collection"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cmd",
        default=DEFAULT_CMD,
        help="Command used to refresh the root-owned collection snapshot",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Collection snapshot written by the sudo wrapper",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Extra target file to overwrite with the normalized snapshot",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for the sudo wrapper",
    )
    parser.add_argument(
        "--skip-command",
        action="store_true",
        help="Do not run the refresh command; just validate/copy an existing snapshot",
    )
    return parser.parse_args()
def main() -> int:
    args = parse_args()
    stdout = ""
    stderr = ""
    returncode = 0

    if not args.skip_command:
        proc = subprocess.run(
            shlex.split(args.cmd),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
        if proc.returncode != 0:
            print(json.dumps({
                "status": "error",
                "returncode": proc.returncode,
                "command": args.cmd,
                "stdout": stdout[-2000:],
                "stderr": stderr[-2000:],
            }, ensure_ascii=False, indent=2))
            return proc.returncode

    if not args.source.exists():
        print(json.dumps({
            "status": "error",
            "returncode": returncode,
            "command": args.cmd,
            "detail": f"Snapshot not found: {args.source}",
            "stdout": stdout[-2000:],
            "stderr": stderr[-2000:],
        }, ensure_ascii=False, indent=2))
        return 1

    extra_targets = [Path(path) for path in args.target]
    summary = sync_collection_snapshot(
        args.source,
        stderr_text=stderr,
        extra_targets=extra_targets,
        command=args.cmd,
        returncode=returncode,
        stdout_text=stdout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
