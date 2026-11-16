"""Create the LangGraph checkpointer tables once, before the server starts.

Run from start.sh. Doing it lazily on the first request deadlocks on a fresh database:
setup() uses CREATE INDEX CONCURRENTLY, which waits for every open transaction, including
the one held by the request that triggered it.
"""

from __future__ import annotations

import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ticketpilot.config import get_ticket_settings


async def main() -> None:
    conn = await AsyncConnection.connect(
        get_ticket_settings().checkpoint_url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    async with conn:
        await AsyncPostgresSaver(conn).setup()
    print("checkpointer tables ready")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # psycopg async
    asyncio.run(main())
