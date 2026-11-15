"""Entry point. psycopg's async driver needs a selector event loop on Windows.

uv run python -m ticketpilot.serve --port 8000
uv run python -m ticketpilot.serve --shopsim --port 8006
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--shopsim", action="store_true", help="serve the mock shop instead")
    a = p.parse_args()
    target = "shopsim.app:create_app" if a.shopsim else "ticketpilot.main:create_app"
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config = uvicorn.Config(target, factory=True, host=a.host, port=a.port, loop="none")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
