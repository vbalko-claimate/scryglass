#!/usr/bin/env python3
"""Entry point for MTGA Advisor."""
import os
import sys
import uvicorn
from dotenv import load_dotenv

load_dotenv()

HOST = "127.0.0.1"
PORT = int(os.environ.get("SCRY_PORT", 8765))

if __name__ == "__main__":
    sys.stdout.flush()
    print(f"\n  Scryglass starting → http://{HOST}:{PORT}\n", flush=True)
    from advisor.server import app
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
