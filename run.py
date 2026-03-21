#!/usr/bin/env python3
"""Entry point for MTGA Advisor."""
import uvicorn
from dotenv import load_dotenv

load_dotenv()

HOST = "127.0.0.1"
PORT = 8765

if __name__ == "__main__":
    print(f"\n  Scryglass starting → http://{HOST}:{PORT}\n")
    uvicorn.run(
        "advisor.server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
