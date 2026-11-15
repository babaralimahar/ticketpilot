"""TicketPilot HTTP API. Roles (template API keys): clerk = support agent, approver = supervisor."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.api.auth import Principal, require_role
from app.api.deps import SessionDep
from app.api.errors import DomainError
from ticketpilot import repo
from ticketpilot.service import Runner

router = APIRouter(prefix="/v1", tags=["tickets"])
Agent = Annotated[Principal, Depends(require_role("clerk"))]
Supervisor = Annotated[Principal, Depends(require_role("approver"))]
Admin = Annotated[Principal, Depends(require_role("admin"))]


async def get_runner(request: Request) -> Runner:
    getter = request.app.state.get_runner
    return cast(Runner, await getter())


def spawn(request: Request, coro: Any) -> None:
    """Run in the background, keeping a reference so the task is not garbage-collected."""
    tasks: set[asyncio.Task[Any]] = request.app.state.tasks
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


class TicketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["email", "chat"] = "email"
    customer_email: EmailStr
    subject: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=2, max_length=5000)


@router.post("/tickets", status_code=202)
async def create_ticket(body: TicketIn, request: Request, _: Agent) -> dict[str, Any]:
    runner = await get_runner(request)
    ticket = {
        "email": str(body.customer_email),
        "subject": body.subject,
        "body": body.body,
        "channel": body.channel,
    }
    tid, rid = await runner.start(ticket)
    spawn(request, runner.run(rid, ticket))
    return {"ticket_id": tid, "run_id": rid, "status": "running"}


def _public_run(r: dict[str, Any]) -> dict[str, Any]:
    s = r.get("summary") or {}
    return {
        "run_id": r["id"],
        "ticket_id": r["ticket_id"],
        "status": r["status"],
        "engine": r["engine"],
        "intent": r["intent"],
        "steps": r["steps"],
        "tokens": {"in": r["tokens_in"], "out": r["tokens_out"]},
        "cost_usd": str(r["cost_usd"]),
        "proposal": s.get("proposal"),
        "decision": s.get("decision"),
        "executed": s.get("executed"),
        "reply": s.get("reply"),
        "escalation": s.get("escalation"),
        "approval_id": r.get("approval_id"),
        "approval_status": r.get("approval_status"),
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: SessionDep, _: Agent) -> dict[str, Any]:
    r = await repo.get_run(session, run_id)
    if r is None:
        raise DomainError(404, "not-found", "Run not found")
    return _public_run(r)


@router.get("/runs/{run_id}/trace")
async def get_trace(run_id: str, session: SessionDep, _: Agent) -> dict[str, Any]:
    r = await repo.get_run(session, run_id)
    if r is None:
        raise DomainError(404, "not-found", "Run not found")
    return {"run_id": run_id, "trace": (r.get("summary") or {}).get("trace", [])}


@router.get("/approvals")
async def approvals(
    session: SessionDep,
    _: Supervisor,
    status: Literal["pending", "approved", "rejected"] = "pending",
) -> dict[str, Any]:
    return {
        "items": [
            {k: repo.jsonable(v) for k, v in a.items()}
            for a in await repo.list_approvals(session, status)
        ]
    }


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    note: str = Field("", max_length=1000)


@router.post("/approvals/{approval_id}")
async def decide(
    approval_id: str, body: DecisionIn, request: Request, session: SessionDep, p: Supervisor
) -> dict[str, Any]:
    row = await repo.decide_approval(session, approval_id, body.decision, p.key_id, body.note)
    if row is None:
        raise DomainError(409, "conflict", "Approval is not pending")
    runner = await get_runner(request)
    spawn(request, runner.resume(row["run_id"], body.decision, p.key_id, body.note))
    return {
        "approval_id": approval_id,
        "decision": body.decision,
        "run_id": row["run_id"],
        "status": "resuming",
    }


class KillSwitchIn(BaseModel):
    writes_disabled: bool


@router.post("/admin/kill-switch")
async def kill_switch(body: KillSwitchIn, session: SessionDep, p: Admin) -> dict[str, Any]:
    await repo.set_flag(session, "writes_disabled", body.writes_disabled, p.key_id)
    return {"writes_disabled": body.writes_disabled}
