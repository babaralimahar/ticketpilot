"""Tool registry. Read tools gather facts; proposal tools only *record* an intended action.
No tool the model can call writes anything: writes happen in the execute node, after policy.

Ownership is enforced here, in code: any order id must belong to the ticket's customer email.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ticketpilot.adapters.shop import ShopClient, ShopError

Risk = Literal["read", "propose"]
OUTPUT_CAP = 2000


class ToolDenied(Exception): ...


@dataclass
class ToolContext:
    ticket_email: str
    shop: ShopClient
    kb_path: Path = Path("config/support_policy.md")


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^\d{5}$", description="5-digit order number")


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=200)


class RefundProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^\d{5}$")
    amount: Decimal = Field(
        gt=0, max_digits=10, decimal_places=2, description="USD amount to refund"
    )
    reason: Literal["damaged", "return", "not_received"]
    explanation: str = Field(max_length=500)


class AddressProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^\d{5}$")
    new_address: str = Field(min_length=10, max_length=300)


class NoAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = Field(default=None, description="order the reply is about, if any")
    summary: str = Field(max_length=500, description="what the customer should be told")


class EscalateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=300)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    risk: Risk
    run: Callable[[ToolContext, Any], Awaitable[Any]] | None = None


async def _owned_order(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    try:
        order = await ctx.shop.get_order(order_id)
    except ShopError as e:
        if e.status == 404:
            raise ToolDenied(f"order {order_id} not found") from e
        raise
    if order["customer_email"].lower() != ctx.ticket_email.lower():
        # Same message as "not found": never confirm that another customer's order exists.
        raise ToolDenied(f"order {order_id} not found")
    return order


async def _list_my_orders(ctx: ToolContext, _: NoArgs) -> Any:
    return await ctx.shop.list_orders(ctx.ticket_email)


async def _get_order(ctx: ToolContext, a: OrderArgs) -> Any:
    o = await _owned_order(ctx, a.order_id)
    return {k: v for k, v in o.items() if k != "address"} | {
        "address_on_file": bool(o.get("address"))
    }


async def _get_shipment(ctx: ToolContext, a: OrderArgs) -> Any:
    await _owned_order(ctx, a.order_id)
    s = await ctx.shop.get_shipment(a.order_id)
    s["events"] = [str(e)[:160] for e in s.get("events", [])][
        -5:
    ]  # capped: carrier text is untrusted
    return s


async def _search_policy(ctx: ToolContext, a: SearchArgs) -> Any:
    text = ctx.kb_path.read_text(encoding="utf-8")
    words = {w for w in re.findall(r"[a-z]+", a.query.lower()) if len(w) > 3}
    sections = [s for s in text.split("\n## ")[1:]]
    scored = sorted(sections, key=lambda s: -sum(w in s.lower() for w in words))
    return [s.strip() for s in scored[:2]]


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "list_my_orders",
            "List the ticket customer's recent orders (id, status, total, date).",
            NoArgs,
            "read",
            _list_my_orders,
        ),
        Tool(
            "get_order",
            "Get one of the customer's orders: status, items, prices, refunded so far, delivery date.",
            OrderArgs,
            "read",
            _get_order,
        ),
        Tool(
            "get_shipment",
            "Get carrier status, ETA and recent tracking events for one of the customer's orders.",
            OrderArgs,
            "read",
            _get_shipment,
        ),
        Tool(
            "search_policy",
            "Search the support policy (returns, damage, lost parcels, address changes).",
            SearchArgs,
            "read",
            _search_policy,
        ),
        Tool(
            "propose_refund",
            "Propose a refund. Policy and, if needed, a supervisor decide. Ends your turn.",
            RefundProposal,
            "propose",
        ),
        Tool(
            "propose_address_change",
            "Propose changing an order's delivery address. Ends your turn.",
            AddressProposal,
            "propose",
        ),
        Tool(
            "no_action_needed",
            "The ticket needs only an informative reply (e.g. tracking status). Ends your turn.",
            NoAction,
            "propose",
        ),
        Tool(
            "escalate",
            "Hand the ticket to a human (unclear, out of policy, suspicious). Ends your turn.",
            EscalateArgs,
            "propose",
        ),
    ]
}


def openai_tool_schemas() -> list[dict[str, Any]]:
    out = []
    for t in TOOLS.values():
        schema = t.args.model_json_schema()
        schema.pop("title", None)
        props = {
            k: {kk: vv for kk, vv in v.items() if kk != "title"}
            for k, v in schema.get("properties", {}).items()
        }
        for v in (
            props.values()
        ):  # Decimal renders as anyOf[number,string]; keep it simple for providers
            if "anyOf" in v:
                v.pop("anyOf")
                v["type"] = "string"
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": schema.get("required", []),
                    },
                },
            }
        )
    return out


async def run_read_tool(ctx: ToolContext, name: str, raw_args: str | dict[str, Any]) -> str:
    """Execute a read tool. Returns text for the model (always data, never instructions)."""
    tool = TOOLS.get(name)
    if tool is None or tool.risk != "read" or tool.run is None:
        return json.dumps({"error": f"unknown or non-read tool {name!r}"})
    try:
        args = tool.args.model_validate(
            json.loads(raw_args) if isinstance(raw_args, str) else raw_args or {}
        )
        result = await tool.run(ctx, args)
    except (ValidationError, json.JSONDecodeError) as e:
        return json.dumps({"error": f"invalid arguments: {str(e)[:300]}"})
    except ToolDenied as e:
        return json.dumps({"error": str(e)})
    except ShopError as e:
        return json.dumps({"error": f"shop error: {e}"})
    return json.dumps(result, default=str)[:OUTPUT_CAP]
