"""TicketPilot app. Run with: uv run python -m ticketpilot.serve (sets the Windows event loop)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.main import AppContext
from app.main import create_app as create_base_app
from app.worker import Handler
from ticketpilot import __version__
from ticketpilot.adapters.shop import ShopClient
from ticketpilot.agent.graph import Deps
from ticketpilot.api import router
from ticketpilot.config import TicketSettings, get_ticket_settings
from ticketpilot.domain.policy import load_policy
from ticketpilot.service import Runner
from ticketpilot.ui import router as ui_router


def _no_handlers(_: AppContext) -> Mapping[str, Handler]:
    return {}


def create_app(settings: TicketSettings | None = None, shop: ShopClient | None = None) -> FastAPI:
    s = settings or get_ticket_settings()
    app = create_base_app(
        s,
        routers=[router, ui_router],
        handlers=_no_handlers,
        title="TicketPilot",
        version=__version__,
    )
    shop_client = shop or ShopClient(s.shopsim_url, s.shopsim_token, timeout_s=s.shop_timeout_s)
    lock = asyncio.Lock()
    state: dict[str, Any] = {}

    async def get_runner() -> Runner:
        """Lazily open the checkpointer pool on first use (inside the running event loop)."""
        async with lock:
            if "runner" not in state:
                pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
                    s.checkpoint_url,
                    open=False,
                    max_size=5,
                    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                )
                await pool.open()
                saver = AsyncPostgresSaver(pool)
                await saver.setup()
                deps = Deps(
                    app.state.llm,
                    shop_client,
                    app.state.sessionmaker,
                    s,
                    load_policy(Path(s.policy_path)),
                )
                state["pool"], state["runner"] = pool, Runner(deps, saver)
            return state["runner"]  # type: ignore[no-any-return]

    getter: Callable[[], Coroutine[Any, Any, Runner]] = get_runner
    app.state.get_runner = getter
    app.state.tasks = set()
    app.state.runner_state = state

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    return app
