"""The TicketPilot graph (LangGraph StateGraph + Postgres checkpointer).

screen -> agent (bounded read-only tool loop) -> policy_check -> [approval interrupt] ->
execute (idempotent writes) -> draft_reply -> qa_check -> done | escalate

The model reads and proposes. Code decides (policy_check), a human approves money above the
limit (interrupt, durable across restarts), and only the execute node writes.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.llm import LLMBadRequest, LLMClient, LLMError, LLMResult, complete_with_retries
from ticketpilot import repo
from ticketpilot.adapters.shop import ShopClient, ShopError
from ticketpilot.agent import baseline
from ticketpilot.agent.tools import (
    TOOLS,
    ToolContext,
    ToolDenied,
    _owned_order,
    openai_tool_schemas,
    run_read_tool,
)
from ticketpilot.config import TicketSettings
from ticketpilot.domain.policy import Order, Policy, Shipment, decide_address, decide_refund, money

RunStatus = Literal["running", "awaiting_approval", "done", "escalated", "failed"]
PROMPTS = Path("prompts")
_INJECTION = re.compile(
    r"(?i)(ignore\s+(all\s+|any\s+)?(of\s+)?(your\s+|the\s+|my\s+)?(previous\s+|prior\s+|above\s+|earlier\s+)?(instructions|rules)|\bsystem\s*:|you are (an? )?(ai|assistant|bot)"
    r"|developer mode|act as|override|jailbreak|refund (me )?\$?\d{3,}\b.*(now|immediately)|admin"
    r"|skip (the )?(policy|checks|policy checks|verification)|authori[sz]ed by (your|the) (manager|supervisor|admin|owner)"
    r"|as the (store |shop )?owner|new instruction)"
)


class RunState(TypedDict, total=False):
    run_id: str
    ticket: dict[str, Any]
    flags: dict[str, Any]
    proposal: dict[str, Any] | None
    decision: dict[str, Any] | None
    facts: dict[str, Any]
    approval: dict[str, Any] | None
    executed: list[dict[str, Any]]
    reply: str | None
    qa: dict[str, Any] | None
    status: RunStatus
    escalation: str | None
    usage: dict[str, Any]
    trace: list[dict[str, Any]]


@dataclass
class Deps:
    llm: LLMClient
    shop: ShopClient
    sm: async_sessionmaker[AsyncSession]
    settings: TicketSettings
    policy: Policy


def _usage(state: RunState, res: LLMResult | None = None, step: bool = False) -> dict[str, Any]:
    u = dict(state.get("usage") or {"tokens_in": 0, "tokens_out": 0, "cost_usd": "0", "steps": 0})
    if res is not None:
        u["tokens_in"] += res.usage.tokens_in
        u["tokens_out"] += res.usage.tokens_out
        u["cost_usd"] = str(Decimal(u["cost_usd"]) + res.usage.cost_usd)
    if step:
        u["steps"] += 1
    return u


def _trace(state: RunState, **event: Any) -> list[dict[str, Any]]:
    return [*(state.get("trace") or []), {"t": round(time.time(), 3), **event}]


def build_graph(
    deps: Deps, decider: Literal["agent", "baseline"] = "agent"
) -> StateGraph[RunState]:
    s, p = deps.settings, deps.policy
    agent_prompt = (PROMPTS / f"{s.prompt_version}.md").read_text(encoding="utf-8")
    reply_prompt = (PROMPTS / "reply_v1.md").read_text(encoding="utf-8")
    tool_schemas = openai_tool_schemas()

    async def screen(state: RunState) -> dict[str, Any]:
        t = state["ticket"]
        suspected = bool(_INJECTION.search(f"{t['subject']}\n{t['body']}"))
        return {
            "flags": {"injection_suspected": suspected},
            "status": "running",
            "executed": [],
            "trace": _trace(state, node="screen", injection_suspected=suspected),
        }

    async def agent(state: RunState) -> dict[str, Any]:
        t = state["ticket"]
        ctx = ToolContext(ticket_email=t["email"], shop=deps.shop)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent_prompt},
            {
                "role": "user",
                "content": f"Customer email: {t['email']}\n<ticket>\nSubject: {t['subject']}\n"
                f"{t['body'].replace('</ticket>', '')}\n</ticket>",
            },
        ]
        upd: RunState = {"usage": state.get("usage"), "trace": state.get("trace") or []}  # type: ignore[typeddict-item]
        started = time.monotonic()
        seen: Counter[str] = Counter()
        bad_args = 0

        def stop(reason: str) -> dict[str, Any]:
            return {**upd, "proposal": {"tool": "escalate", "args": {"reason": reason}}}

        while True:
            u = upd.get("usage") or {}
            if u.get("steps", 0) >= s.max_steps:
                return stop(f"step budget exhausted ({s.max_steps})")
            if u.get("tokens_in", 0) + u.get("tokens_out", 0) > s.token_budget:
                return stop("token budget exhausted")
            if time.monotonic() - started > s.deadline_s:
                return stop("deadline exceeded")
            res = None
            for attempt in range(
                2
            ):  # one resend: some providers reject a malformed tool call with 400
                try:
                    res = await complete_with_retries(
                        deps.llm,
                        s.llm_max_attempts,
                        prompt_version=s.prompt_version,
                        messages=messages,
                        tools=tool_schemas,
                    )
                    break
                except LLMBadRequest as e:
                    if attempt == 1:
                        return {
                            **stop(f"model unavailable: {type(e).__name__}: {str(e)[:160]}"),
                            "status": "failed",
                        }
                except LLMError as e:
                    return {
                        **stop(f"model unavailable: {type(e).__name__}: {str(e)[:160]}"),
                        "status": "failed",
                    }
            assert res is not None  # noqa: S101
            upd["usage"] = _usage(upd, res, step=True)
            calls = res.tool_calls
            if not calls:
                return stop("model gave no decision")
            messages.append({"role": "assistant", "content": res.text or None, "tool_calls": calls})
            for n, call in enumerate(calls):
                fn = call.get("function", {})
                name, raw = fn.get("name", ""), fn.get("arguments") or "{}"
                call_id = call.get("id") or f"call_{u.get('steps', 0)}_{n}"
                tool = TOOLS.get(name)
                if tool is not None and tool.risk == "propose":
                    try:
                        args = tool.args.model_validate(
                            json.loads(raw) if isinstance(raw, str) else raw
                        )
                    except (ValidationError, json.JSONDecodeError) as e:
                        bad_args += 1
                        if bad_args > 1:
                            return stop("invalid decision arguments twice")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "name": name,
                                "content": json.dumps(
                                    {"error": f"invalid arguments: {str(e)[:300]}"}
                                ),
                            }
                        )
                        continue
                    upd["trace"] = _trace(
                        upd, node="agent", decision=name, args=args.model_dump(mode="json")
                    )
                    return {**upd, "proposal": {"tool": name, "args": args.model_dump(mode="json")}}
                key = f"{name}:{raw}"
                seen[key] += 1
                if seen[key] >= 3:
                    return stop(f"repeated identical tool call: {name}")
                out = await run_read_tool(ctx, name, raw)
                upd["trace"] = _trace(
                    upd,
                    node="agent",
                    tool=name,
                    args=raw if isinstance(raw, str) else json.dumps(raw),
                    result=out[:300],
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": f"<tool_result>{out}</tool_result>",
                    }
                )

    async def baseline_node(state: RunState) -> dict[str, Any]:
        t = state["ticket"]
        ctx = ToolContext(ticket_email=t["email"], shop=deps.shop)

        def overdue(ship: dict[str, Any]) -> bool:
            if ship.get("status") == "lost":
                return True
            eta = ship.get("eta")
            return (
                ship.get("status") == "in_transit"
                and bool(eta)
                and ((s.today() - date.fromisoformat(str(eta))).days > p.lost_after_days_past_eta)
            )

        prop = await baseline.decide(ctx, t["subject"], t["body"], overdue)
        return {
            "proposal": prop,
            "trace": _trace(state, node="baseline", decision=prop["tool"], args=prop["args"]),
        }

    async def policy_check(state: RunState) -> dict[str, Any]:
        prop = state.get("proposal") or {"tool": "escalate", "args": {"reason": "no proposal"}}
        tool, args = prop["tool"], prop["args"]
        if tool == "escalate":
            return {
                "decision": {"outcome": "deny", "reason": args.get("reason", ""), "action": tool}
            }
        if (state.get("flags") or {}).get("injection_suspected") and tool in (
            "propose_refund",
            "propose_address_change",
        ):
            # Deterministic guard: text that tries to instruct the agent never triggers a write,
            # whatever the model concluded. A human looks at it instead.
            return {
                "decision": {
                    "outcome": "deny",
                    "reason": "possible prompt injection in the ticket: routed to a human",
                    "action": tool,
                },
                "trace": _trace(
                    state, node="policy_check", outcome="deny", reason="injection_suspected"
                ),
            }
        if tool == "no_action_needed":
            facts: dict[str, Any] = {}
            if args.get("order_id"):
                try:
                    facts = await _facts(state, args["order_id"])
                except (ToolDenied, ShopError):
                    facts = {}
            return {
                "decision": {"outcome": "info", "reason": "informative reply", "action": tool},
                "facts": facts,
            }
        async with deps.sm() as ss:
            if await repo.flag(ss, "writes_disabled"):
                return {
                    "decision": {
                        "outcome": "deny",
                        "reason": "kill switch: write tools disabled",
                        "action": tool,
                    }
                }
        if len(state.get("executed") or []) >= p.max_actions_per_run:
            return {
                "decision": {"outcome": "deny", "reason": "per-run action limit", "action": tool}
            }
        try:
            facts = await _facts(state, args["order_id"])
        except ToolDenied as e:
            return {"decision": {"outcome": "deny", "reason": f"ownership: {e}", "action": tool}}
        except ShopError as e:
            return {
                "decision": {"outcome": "deny", "reason": f"shop unavailable: {e}", "action": tool}
            }
        order = Order.model_validate(facts["order"])
        ship = Shipment.model_validate(facts["shipment"]) if facts.get("shipment") else None
        if tool == "propose_refund":
            d = decide_refund(
                order, ship, Decimal(str(args["amount"])), args["reason"], s.today(), p
            )
            risk = "write_irreversible"
        else:
            d = decide_address(order, args["new_address"], p)
            risk = "write_reversible"
        return {
            "facts": facts,
            "decision": {
                "outcome": d.outcome,
                "reason": d.reason,
                "action": tool,
                "args": d.args,
                "risk": risk,
            },
            "trace": _trace(state, node="policy_check", outcome=d.outcome, reason=d.reason),
        }

    async def _facts(state: RunState, order_id: str) -> dict[str, Any]:
        ctx = ToolContext(ticket_email=state["ticket"]["email"], shop=deps.shop)
        order = await _owned_order(ctx, order_id)  # ground truth, ownership re-checked in code
        try:
            shipment: dict[str, Any] | None = await deps.shop.get_shipment(order_id)
        except ShopError:
            shipment = None
        return {"order": order, "shipment": shipment}

    async def approval(state: RunState) -> dict[str, Any]:
        d = state["decision"] or {}
        async with deps.sm() as ss:
            apr_id = await repo.ensure_approval(
                ss,
                state["run_id"],
                {"tool": d["action"], "args": d["args"]},
                d.get("risk", "write"),
                d["reason"],
            )
        answer = interrupt(
            {"approval_id": apr_id, "action": d["action"], "args": d["args"], "reason": d["reason"]}
        )
        ok = isinstance(answer, dict) and answer.get("decision") == "approve"
        return {
            "approval": {"id": apr_id, **(answer if isinstance(answer, dict) else {})},
            "trace": _trace(
                state, node="approval", approval_id=apr_id, decision="approve" if ok else "reject"
            ),
        }

    async def execute(state: RunState) -> dict[str, Any]:
        d = state["decision"] or {}
        executed = list(state.get("executed") or [])
        key = f"{state['run_id']}:{len(executed)}"  # same key on any retry or resume -> one side effect
        async with deps.sm() as ss:
            if await repo.flag(ss, "writes_disabled"):
                return {"escalation": "kill switch engaged before execution"}
            await repo.log_action_start(ss, state["run_id"], d["action"], d["args"], key)
        t0 = time.perf_counter()
        try:
            if d["action"] == "propose_refund":
                result = await deps.shop.create_refund(
                    d["args"]["order_id"], money(d["args"]["amount"]), d["args"]["reason"], key
                )
            else:
                result = await deps.shop.update_address(
                    d["args"]["order_id"], d["args"]["address"], key
                )
            status = "done"
        except ShopError as e:
            result, status = {"error": str(e)}, "failed"
        async with deps.sm() as ss:
            await repo.log_action_end(
                ss, key, result, status, int((time.perf_counter() - t0) * 1000)
            )
        if status != "done":
            return {
                "escalation": f"write failed after retries: {result['error']}",
                "trace": _trace(state, node="execute", key=key, status=status),
            }
        executed.append({"action": d["action"], "args": d["args"], "result": result, "key": key})
        return {
            "executed": executed,
            "trace": _trace(state, node="execute", key=key, status=status),
        }

    async def draft_reply(state: RunState) -> dict[str, Any]:
        if decider == "baseline":
            return {"reply": baseline.template_reply(dict(state))}
        d = state.get("decision") or {}
        appr = state.get("approval") or {}
        outcome = {
            "decision": d.get("outcome"),
            "policy_reason": d.get("reason"),
            "executed": [
                {"action": e["action"], "args": e["args"]} for e in state.get("executed") or []
            ],
            "proposal": state.get("proposal"),
            "approval": appr.get("decision"),
        }
        facts = state.get("facts") or {}
        msgs = [
            {"role": "system", "content": reply_prompt},
            {
                "role": "user",
                "content": f"<ticket>{state['ticket']['subject']}\n{state['ticket']['body']}</ticket>\n"
                f"<facts>{json.dumps(facts, default=str)[:3000]}</facts>\n"
                f"<outcome>{json.dumps(outcome, default=str)}</outcome>",
            },
        ]
        schema = {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
        }
        try:
            res = await complete_with_retries(
                deps.llm,
                s.llm_max_attempts,
                prompt_version="reply_v1",
                messages=msgs,
                schema=schema,
            )
        except LLMError:
            return {"reply": None, "escalation": "could not draft a reply"}
        raw = res.parsed if res.parsed is not None else _json(res.text)
        return {"reply": str(raw.get("reply") or "").strip() or None, "usage": _usage(state, res)}

    async def qa_check(state: RunState) -> dict[str, Any]:
        problems = qa_problems(state)
        return {
            "qa": {"ok": not problems, "problems": problems},
            "trace": _trace(state, node="qa_check", problems=problems),
        }

    async def escalate(state: RunState) -> dict[str, Any]:
        d = state.get("decision") or {}
        reason = state.get("escalation") or d.get("reason") or "escalated"
        appr = state.get("approval") or {}
        if appr and appr.get("decision") != "approve":
            reason = f"supervisor rejected: {appr.get('note') or 'no note'}"
        qa = state.get("qa") or {}
        if qa and not qa.get("ok"):
            reason = f"reply failed QA: {'; '.join(qa['problems'])}"
        status: RunStatus = "failed" if state.get("status") == "failed" else "escalated"
        return {
            "status": status,
            "escalation": reason,
            "trace": _trace(state, node="escalate", reason=reason),
        }

    async def done(state: RunState) -> dict[str, Any]:
        return {"status": "done", "trace": _trace(state, node="done")}

    def after_policy(state: RunState) -> str:
        o = (state.get("decision") or {}).get("outcome")
        if state.get("status") == "failed":
            return "escalate"
        return {"auto": "execute", "approval": "approval", "info": "draft_reply"}.get(
            str(o), "escalate"
        )

    def after_approval(state: RunState) -> str:
        return (
            "execute" if (state.get("approval") or {}).get("decision") == "approve" else "escalate"
        )

    def after_execute(state: RunState) -> str:
        return "escalate" if state.get("escalation") else "draft_reply"

    def after_draft(state: RunState) -> str:
        return "escalate" if state.get("escalation") or not state.get("reply") else "qa_check"

    def after_qa(state: RunState) -> str:
        return "done" if (state.get("qa") or {}).get("ok") else "escalate"

    g: StateGraph[RunState] = StateGraph(RunState)
    for name, fn in [
        ("screen", screen),
        ("agent", agent if decider == "agent" else baseline_node),
        ("policy_check", policy_check),
        ("approval", approval),
        ("execute", execute),
        ("draft_reply", draft_reply),
        ("qa_check", qa_check),
        ("escalate", escalate),
        ("done", done),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "screen")
    g.add_edge("screen", "agent")
    g.add_edge("agent", "policy_check")
    g.add_conditional_edges(
        "policy_check", after_policy, ["execute", "approval", "draft_reply", "escalate"]
    )
    g.add_conditional_edges("approval", after_approval, ["execute", "escalate"])
    g.add_conditional_edges("execute", after_execute, ["draft_reply", "escalate"])
    g.add_conditional_edges("draft_reply", after_draft, ["qa_check", "escalate"])
    g.add_conditional_edges("qa_check", after_qa, ["done", "escalate"])
    g.add_edge("escalate", END)
    g.add_edge("done", END)
    return g


def _json(text: str | None) -> dict[str, Any]:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


_AMOUNT = re.compile(r"(?:USD|\$)\s?(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*\.\d{2})")
_ORDER = re.compile(r"\b(1\d{4})\b")
_ISSUED = re.compile(
    r"(?i)\b(has|have|was|were|we've|we have)\s+(already\s+)?(been\s+)?(issued|processed|refunded|sent)\b"
    r"|\brefund(ed)?\b[^.]{0,40}\b(issued|processed|completed)\b"
)


def qa_problems(state: RunState) -> list[str]:
    """Deterministic reply QA: every amount and order number must come from the facts or the
    executed actions, and no refund may be described as issued unless one was executed."""
    reply = state.get("reply") or ""
    problems: list[str] = []
    facts = state.get("facts") or {}
    allowed: set[Decimal] = set()
    order = facts.get("order") or {}
    for k in ("total", "refunded"):
        if order.get(k) is not None:
            allowed.add(money(order[k]))
    for it in order.get("items", []):
        allowed.add(money(it["price"]))
        allowed.add(money(Decimal(str(it["price"])) * it["qty"]))
    if order:
        allowed.add(money(Decimal(order["total"]) - Decimal(order["refunded"])))
    for e in state.get("executed") or []:
        if "amount" in e["args"]:
            allowed.add(money(e["args"]["amount"]))
    d = state.get("decision") or {}
    if d.get("args", {}).get("amount"):
        allowed.add(money(d["args"]["amount"]))
    for m in _AMOUNT.finditer(reply):
        val = money((m.group(1) or m.group(2)).replace(",", ""))
        if val not in allowed:
            problems.append(f"amount {val} not in facts")
    known_orders = {order.get("id")} | set(
        _ORDER.findall(state["ticket"]["subject"] + " " + state["ticket"]["body"])
    )
    for o in _ORDER.findall(reply):
        if o not in known_orders:
            problems.append(f"order {o} not part of this ticket")
    refunded_now = any(e["action"] == "propose_refund" for e in state.get("executed") or [])
    refunded_before = bool(order) and money(order.get("refunded") or 0) > 0
    if (
        not (refunded_now or refunded_before)
        and _ISSUED.search(reply)
        and "refund" in reply.lower()
    ):
        problems.append("reply says a refund was issued but none was executed or on record")
    return problems
