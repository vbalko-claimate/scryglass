#!/usr/bin/env python3
"""Entry point for MTGA Advisor."""
import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "advisor.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )
