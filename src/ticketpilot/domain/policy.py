"""Deterministic refund / address-change policy. Pure functions over ground-truth order data.

The agent proposes an action; this engine decides: auto (execute now), approval (pause for a
supervisor), or deny (escalate to a human). Nothing the model says can change these numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

Outcome = Literal["auto", "approval", "deny"]
RefundReason = Literal["damaged", "return", "not_received"]
CENT = Decimal("0.01")


def money(x: Decimal | float | str) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


class Policy(BaseModel):
    auto_refund_limit_usd: Decimal = Decimal(50)
    return_window_days: int = 30
    damage_claim_window_days: int = 30
    lost_after_days_past_eta: int = 7
    max_actions_per_run: int = 2


def load_policy(path: Path = Path("config/policy.yaml")) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Item(BaseModel):
    sku: str
    name: str
    price: Decimal
    qty: int
    final_sale: bool = False


class Order(BaseModel):
    """Ground truth from shopsim (fetched by code)."""

    id: str
    customer_email: str
    status: Literal["processing", "shipped", "delivered", "cancelled"]
    total: Decimal
    refunded: Decimal
    items: list[Item]
    placed_at: date
    delivered_at: date | None = None

    @property
    def remaining(self) -> Decimal:
        return money(self.total - self.refunded)


class Shipment(BaseModel):
    status: Literal["label_created", "in_transit", "delivered", "lost"]
    carrier: str = ""
    eta: date | None = None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    args: dict[str, Any]


def _deny(reason: str, args: dict[str, Any]) -> Decision:
    return Decision("deny", reason, args)


def decide_refund(
    order: Order, shipment: Shipment | None, amount: Decimal, reason: str, today: date, p: Policy
) -> Decision:
    amount = money(amount)
    args = {"order_id": order.id, "amount": str(amount), "reason": reason}
    if reason not in ("damaged", "return", "not_received"):
        return _deny(f"unknown refund reason {reason!r}", args)
    if amount <= 0:
        return _deny("refund amount must be positive", args)
    if order.status == "cancelled":
        return _deny("order is cancelled", args)
    if amount > order.remaining:
        return _deny(f"amount {amount} exceeds refundable remainder {order.remaining}", args)

    if reason in ("damaged", "return"):
        if order.status != "delivered" or order.delivered_at is None:
            return _deny(f"{reason} refunds need a delivered order (status {order.status})", args)
        age = (today - order.delivered_at).days
        window = p.damage_claim_window_days if reason == "damaged" else p.return_window_days
        if age > window:
            return _deny(f"delivered {age} days ago; window is {window} days", args)
        if reason == "return":
            returnable = money(
                sum((i.price * i.qty for i in order.items if not i.final_sale), Decimal(0))
            )
            if returnable == 0:
                return _deny("all items are final sale", args)
            if amount > money(returnable - order.refunded):
                return _deny(
                    f"amount {amount} exceeds returnable (non-final-sale) value {returnable}", args
                )
    else:  # not_received
        if order.status == "delivered" or (shipment and shipment.status == "delivered"):
            return _deny("carrier shows the order as delivered", args)
        lost = shipment is not None and shipment.status == "lost"
        overdue = (
            shipment is not None
            and shipment.status == "in_transit"
            and shipment.eta is not None
            and (today - shipment.eta).days > p.lost_after_days_past_eta
        )
        if not (lost or overdue):
            return _deny("shipment is not lost or overdue yet", args)

    if amount <= p.auto_refund_limit_usd:
        return Decision("auto", f"amount {amount} <= auto limit {p.auto_refund_limit_usd}", args)
    return Decision("approval", f"amount {amount} > auto limit {p.auto_refund_limit_usd}", args)


def decide_address(order: Order, address: str, p: Policy) -> Decision:
    args = {"order_id": order.id, "address": address.strip()}
    if not 10 <= len(address.strip()) <= 300:
        return _deny("address must be 10-300 characters", args)
    if order.status == "processing":
        return Decision("auto", "order not shipped yet", args)
    if order.status == "shipped":
        return Decision("approval", "order already shipped: carrier redirect needs approval", args)
    return _deny(f"cannot change address of a {order.status} order", args)
