"""TicketPilot persistence (SQL Core). Each function commits its own small unit of work."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


async def flag(ss: AsyncSession, name: str) -> bool:
    v = await ss.scalar(text("SELECT value FROM app_flags WHERE name = :n"), {"n": name})
    return bool(v)


async def set_flag(ss: AsyncSession, name: str, value: bool, by: str) -> None:
    await ss.execute(
        text(
            "INSERT INTO app_flags (name, value, changed_by) VALUES (:n, :v, :b) "
            "ON CONFLICT (name) DO UPDATE SET value = :v, changed_by = :b, changed_at = now()"
        ),
        {"n": name, "v": value, "b": by},
    )
    await ss.commit()


async def create_ticket_and_run(
    ss: AsyncSession, ticket: dict[str, Any], engine: str = "agent"
) -> tuple[str, str]:
    tid, rid = new_id("tkt"), new_id("run")
    await ss.execute(
        text(
            "INSERT INTO tickets (id, channel, customer_email, subject, body) VALUES (:i, :c, :e, :s, :b)"
        ),
        {
            "i": tid,
            "c": ticket.get("channel", "email"),
            "e": ticket["email"],
            "s": ticket["subject"],
            "b": ticket["body"],
        },
    )
    await ss.execute(
        text(
            "INSERT INTO runs (id, ticket_id, thread_id, engine, status) VALUES (:r, :t, :r, :g, 'running')"
        ),
        {"r": rid, "t": tid, "g": engine},
    )
    await ss.commit()
    return tid, rid


async def update_run(ss: AsyncSession, run_id: str, state: dict[str, Any]) -> None:
    u = state.get("usage") or {}
    status = state.get("status") or "running"
    summary = {
        k: state.get(k)
        for k in (
            "proposal",
            "decision",
            "executed",
            "reply",
            "qa",
            "escalation",
            "flags",
            "approval",
        )
    }
    summary["trace"] = state.get("trace") or []
    intent = (state.get("proposal") or {}).get("tool")
    await ss.execute(
        text(
            "UPDATE runs SET status = :s, intent = :i, steps = :st, tokens_in = :ti, tokens_out = :to, cost_usd = :c, "
            "summary = CAST(:sum AS jsonb), ended_at = CASE WHEN :ended THEN now() ELSE NULL END "
            "WHERE id = :r"
        ),
        {
            "s": status,
            "i": intent,
            "st": u.get("steps", 0),
            "ti": u.get("tokens_in", 0),
            "to": u.get("tokens_out", 0),
            "c": Decimal(str(u.get("cost_usd", "0"))),
            "sum": json.dumps(summary, default=str),
            "r": run_id,
            "ended": status in ("done", "escalated", "failed"),
        },
    )
    await ss.commit()


async def get_run(ss: AsyncSession, run_id: str) -> dict[str, Any] | None:
    row = (
        (
            await ss.execute(
                text(
                    "SELECT r.*, t.customer_email, t.subject, t.body FROM runs r JOIN tickets t ON t.id = r.ticket_id "
                    "WHERE r.id = :r"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    out = dict(row)
    apr = (
        await ss.execute(
            text(
                "SELECT id, status FROM approvals WHERE run_id = :r ORDER BY created_at DESC LIMIT 1"
            ),
            {"r": run_id},
        )
    ).first()
    out["approval_id"] = apr.id if apr else None
    out["approval_status"] = apr.status if apr else None
    return out


async def ensure_approval(
    ss: AsyncSession, run_id: str, action: dict[str, Any], risk: str, reason: str
) -> str:
    """One approval per run (the node re-runs on resume, so this must be idempotent)."""
    existing = await ss.scalar(text("SELECT id FROM approvals WHERE run_id = :r"), {"r": run_id})
    if existing:
        return str(existing)
    aid = new_id("apr")
    await ss.execute(
        text(
            "INSERT INTO approvals (id, run_id, action, risk, policy_reason) "
            "VALUES (:i, :r, CAST(:a AS jsonb), :k, :p)"
        ),
        {"i": aid, "r": run_id, "a": json.dumps(action, default=str), "k": risk, "p": reason},
    )
    await ss.execute(
        text("UPDATE runs SET status = 'awaiting_approval' WHERE id = :r"), {"r": run_id}
    )
    await ss.commit()
    return aid


async def decide_approval(
    ss: AsyncSession, approval_id: str, decision: str, by: str, note: str
) -> dict[str, Any] | None:
    """Mark a pending approval decided. Returns the row, or None if it is not pending."""
    row = (
        await ss.execute(
            text(
                "UPDATE approvals SET status = :s, decided_by = :b, decided_at = now(), note = :n "
                "WHERE id = :i AND status = 'pending' RETURNING id, run_id, action"
            ),
            {
                "s": "approved" if decision == "approve" else "rejected",
                "b": by,
                "n": note,
                "i": approval_id,
            },
        )
    ).first()
    await ss.commit()
    return dict(row._mapping) if row else None


async def list_approvals(ss: AsyncSession, status: str = "pending") -> list[dict[str, Any]]:
    rows = (
        (
            await ss.execute(
                text(
                    "SELECT a.id, a.run_id, a.action, a.risk, a.policy_reason, a.status, a.created_at, a.decided_by, a.note, "
                    "t.customer_email, t.subject FROM approvals a JOIN runs r ON r.id = a.run_id JOIN tickets t ON t.id = r.ticket_id "
                    "WHERE a.status = :s ORDER BY a.created_at"
                ),
                {"s": status},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def log_action_start(
    ss: AsyncSession, run_id: str, tool: str, args: dict[str, Any], key: str
) -> None:
    await ss.execute(
        text(
            "INSERT INTO action_log (run_id, tool, args, idempotency_key, status) "
            "VALUES (:r, :t, CAST(:a AS jsonb), :k, 'started') ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {"r": run_id, "t": tool, "a": json.dumps(args, default=str), "k": key},
    )
    await ss.commit()


async def log_action_end(
    ss: AsyncSession, key: str, result: dict[str, Any], status: str, latency_ms: int
) -> None:
    await ss.execute(
        text(
            "UPDATE action_log SET result = CAST(:res AS jsonb), status = :s, latency_ms = :l "
            "WHERE idempotency_key = :k"
        ),
        {"res": json.dumps(result, default=str), "s": status, "l": latency_ms, "k": key},
    )
    await ss.commit()


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v
