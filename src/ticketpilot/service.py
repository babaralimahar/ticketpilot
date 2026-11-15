"""Runs tickets through the graph with a durable Postgres checkpointer; resumes after approval."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from ticketpilot import repo
from ticketpilot.agent.graph import Deps, RunState, build_graph

log = structlog.get_logger()


class Runner:
    def __init__(
        self, deps: Deps, checkpointer: BaseCheckpointSaver[Any], decider: str = "agent"
    ) -> None:
        self.deps = deps
        self.decider = decider
        self.graph = build_graph(deps, decider).compile(checkpointer=checkpointer)  # type: ignore[arg-type]

    async def start(self, ticket: dict[str, Any]) -> tuple[str, str]:
        async with self.deps.sm() as ss:
            tid, rid = await repo.create_ticket_and_run(ss, ticket, self.decider)
        return tid, rid

    async def run(self, run_id: str, ticket: dict[str, Any]) -> RunState:
        state: RunState = {"run_id": run_id, "ticket": ticket}
        return await self._invoke(run_id, state)

    async def resume(self, run_id: str, decision: str, by: str, note: str = "") -> RunState:
        return await self._invoke(
            run_id, Command(resume={"decision": decision, "by": by, "note": note})
        )

    async def _invoke(self, run_id: str, inp: Any) -> RunState:
        cfg: RunnableConfig = {"configurable": {"thread_id": run_id}, "recursion_limit": 30}
        try:
            out: dict[str, Any] = await self.graph.ainvoke(inp, cfg)
        except Exception:  # never leave a run in limbo
            log.exception("run_crashed", run_id=run_id)
            snapshot = await self.graph.aget_state(cfg)
            out = {**(snapshot.values or {}), "status": "failed", "escalation": "internal error"}
        if out.get("__interrupt__"):
            out["status"] = "awaiting_approval"
        async with self.deps.sm() as ss:
            await repo.update_run(ss, run_id, out)
        return out  # type: ignore[return-value]

    async def submit(self, ticket: dict[str, Any]) -> tuple[str, str, RunState]:
        tid, rid = await self.start(ticket)
        return tid, rid, await self.run(rid, ticket)
