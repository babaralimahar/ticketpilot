"""Deterministic baseline: keyword router + regex + templates. The bar the agent must beat.

It uses the same tools, policy engine, approval flow and idempotent execution as the agent;
only the decision (what to propose) and the reply text differ.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from ticketpilot.adapters.shop import ShopError
from ticketpilot.agent.tools import ToolContext, ToolDenied, _owned_order
from ticketpilot.domain.policy import money

ORDER_RE = re.compile(r"\b(1\d{4})\b")
INTENTS = [
    (
        "damaged",
        re.compile(r"(?i)\b(broken|damaged|shattered|cracked|smashed|defective|dented|torn)\b"),
    ),
    ("address", re.compile(r"(?i)\b(address|deliver (it )?to|ship (it )?to|moved)\b")),
    (
        "not_received",
        re.compile(
            r"(?i)\b(never|not|hasn't|haven't|didn't|still not)\b[^.]{0,20}\b(arriv\w*|receiv\w*|come|came)\b|\blost\b|\bmissing\b"
        ),
    ),
    ("wismo", re.compile(r"(?i)\b(where|track\w*|status|when will)\b")),
    (
        "return",
        re.compile(
            r"(?i)\b(return|send (it )?back|don't want|do not want|changed my mind|refund)\b"
        ),
    ),
]
ADDRESS_RE = re.compile(
    r"(?i)(?:new address(?: is)?|address to|deliver(?: it)? to|ship(?: it)? to)[:\s]+(.{10,200}?)(?:[.\n]|$)"
)


def classify(text: str) -> str:
    for name, rx in INTENTS:
        if rx.search(text):
            return name
    return "other"


def _pick_item(order: dict[str, Any], text: str) -> dict[str, Any] | None:
    t = text.lower()
    items: list[dict[str, Any]] = order["items"]
    hits = [i for i in items if any(w in t for w in re.findall(r"[a-z]{4,}", i["name"].lower()))]
    if len(hits) == 1:
        return hits[0]
    return items[0] if len(items) == 1 else None


async def decide(ctx: ToolContext, subject: str, body: str, today_overdue: Any) -> dict[str, Any]:
    """Return a proposal {tool, args} exactly like the agent's decision tools."""
    text = f"{subject}\n{body}"
    intent = classify(text)
    if intent == "other":
        return {"tool": "escalate", "args": {"reason": "baseline: no rule matched"}}
    ids = ORDER_RE.findall(text)
    try:
        if ids:
            order = await _owned_order(ctx, ids[0])
        else:
            mine = await ctx.shop.list_orders(ctx.ticket_email)
            if len(mine) != 1:
                return {
                    "tool": "escalate",
                    "args": {"reason": "baseline: no order number and not exactly one order"},
                }
            order = await _owned_order(ctx, mine[0]["id"])
    except (ToolDenied, ShopError) as e:
        return {"tool": "escalate", "args": {"reason": f"baseline: {e}"}}

    oid = order["id"]
    if intent in ("damaged", "return"):
        item = _pick_item(order, text)
        if item is None:
            return {"tool": "escalate", "args": {"reason": "baseline: cannot tell which item"}}
        amount = money(Decimal(str(item["price"])) * item["qty"])
        return {
            "tool": "propose_refund",
            "args": {
                "order_id": oid,
                "amount": str(amount),
                "reason": "damaged" if intent == "damaged" else "return",
                "explanation": "baseline rule",
            },
        }
    if intent == "address":
        m = ADDRESS_RE.search(body)
        if not m:
            return {"tool": "escalate", "args": {"reason": "baseline: new address not found"}}
        return {
            "tool": "propose_address_change",
            "args": {"order_id": oid, "new_address": m.group(1).strip()},
        }
    try:
        ship = await ctx.shop.get_shipment(oid)
    except ShopError:
        ship = None
    if intent == "not_received" and ship and today_overdue(ship):
        remaining = money(Decimal(order["total"]) - Decimal(order["refunded"]))
        return {
            "tool": "propose_refund",
            "args": {
                "order_id": oid,
                "amount": str(remaining),
                "reason": "not_received",
                "explanation": "baseline rule",
            },
        }
    status = (
        f"{ship['status'].replace('_', ' ')} with {ship['carrier']}, ETA {ship['eta']}"
        if ship
        else "unknown"
    )
    return {
        "tool": "no_action_needed",
        "args": {"order_id": oid, "summary": f"Order {oid} is {status}."},
    }


def template_reply(state: dict[str, Any]) -> str:
    d = state.get("decision") or {}
    ex = state.get("executed") or []
    order = (state.get("facts") or {}).get("order") or {}
    oid = order.get("id") or (state.get("proposal") or {}).get("args", {}).get("order_id", "")
    if ex and ex[-1]["action"] == "propose_refund":
        return f"We have issued a refund of USD {ex[-1]['args']['amount']} for order {oid}. Thank you for your patience."
    if ex and ex[-1]["action"] == "propose_address_change":
        return f"We have updated the delivery address for order {oid}."
    if d.get("outcome") == "info":
        return str(
            (state.get("proposal") or {}).get("args", {}).get("summary", "Thanks for reaching out.")
        )
    return "Thanks for reaching out. A member of our team will follow up shortly."
