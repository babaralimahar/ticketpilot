"""Decision table for the deterministic refund / address policy (the safety boundary)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ticketpilot.domain.policy import (
    Item,
    Order,
    Policy,
    Shipment,
    decide_address,
    decide_refund,
    money,
)

TODAY = date(2026, 11, 10)
P = Policy()


def order(
    *,
    status: str = "delivered",
    total: str = "40.00",
    refunded: str = "0",
    days: int | None = 5,
    final_sale: bool = False,
    items: list[Item] | None = None,
) -> Order:
    its = items or [Item(sku="S1", name="Mug", price=Decimal(total), qty=1, final_sale=final_sale)]
    return Order(
        id="10001",
        customer_email="a@x.test",
        status=status,
        total=Decimal(total),
        refunded=Decimal(refunded),
        items=its,
        placed_at=TODAY - timedelta(days=40),
        delivered_at=TODAY - timedelta(days=days)
        if days is not None and status == "delivered"
        else None,
    )


def ship(status: str, eta_days_ago: int | None = None) -> Shipment:
    eta = TODAY - timedelta(days=eta_days_ago) if eta_days_ago is not None else None
    return Shipment(status=status, eta=eta)


REFUND_CASES: list[tuple[str, dict[str, Any], Shipment | None, str, str, str]] = [
    # (case, order kwargs, shipment, amount, reason, expected outcome)
    ("damaged small auto", {}, None, "40.00", "damaged", "auto"),
    ("damaged exactly at limit", {"total": "50.00"}, None, "50.00", "damaged", "auto"),
    ("damaged one cent over limit", {"total": "50.01"}, None, "50.01", "damaged", "approval"),
    ("damaged large -> approval", {"total": "120.00"}, None, "120.00", "damaged", "approval"),
    ("damaged partial of large order auto", {"total": "120.00"}, None, "30.00", "damaged", "auto"),
    ("damaged on day 30", {"days": 30}, None, "40.00", "damaged", "auto"),
    ("damaged on day 31", {"days": 31}, None, "40.00", "damaged", "deny"),
    ("damaged long ago", {"days": 90}, None, "40.00", "damaged", "deny"),
    ("damaged but not delivered", {"status": "shipped"}, None, "40.00", "damaged", "deny"),
    ("damaged but processing", {"status": "processing"}, None, "40.00", "damaged", "deny"),
    ("cancelled order", {"status": "cancelled"}, None, "10.00", "damaged", "deny"),
    ("amount above order total", {}, None, "40.01", "damaged", "deny"),
    ("amount above remaining", {"refunded": "30.00"}, None, "20.00", "damaged", "deny"),
    ("amount equals remaining", {"refunded": "30.00"}, None, "10.00", "damaged", "auto"),
    ("fully refunded already", {"refunded": "40.00"}, None, "1.00", "damaged", "deny"),
    ("zero amount", {}, None, "0", "damaged", "deny"),
    ("negative amount", {}, None, "-5", "damaged", "deny"),
    ("unknown reason", {}, None, "10.00", "goodwill", "deny"),
    ("return ok", {}, None, "40.00", "return", "auto"),
    ("return final sale", {"final_sale": True}, None, "40.00", "return", "deny"),
    ("return day 30", {"days": 30}, None, "40.00", "return", "auto"),
    ("return day 31", {"days": 31}, None, "40.00", "return", "deny"),
    ("return large -> approval", {"total": "80.00"}, None, "80.00", "return", "approval"),
    ("return not delivered", {"status": "shipped"}, None, "40.00", "return", "deny"),
    ("not received, lost", {"status": "shipped"}, ship("lost"), "40.00", "not_received", "auto"),
    ("not received, lost, large", {"status": "shipped", "total": "90.00"}, ship("lost"), "90.00",
     "not_received", "approval"),
    ("not received, 8 days past ETA", {"status": "shipped"}, ship("in_transit", 8), "40.00",
     "not_received", "auto"),
    ("not received, 7 days past ETA (too early)", {"status": "shipped"}, ship("in_transit", 7),
     "40.00", "not_received", "deny"),
    ("not received, ETA in future", {"status": "shipped"}, ship("in_transit", -3), "40.00",
     "not_received", "deny"),
    ("not received, but delivered", {}, ship("delivered"), "40.00", "not_received", "deny"),
    ("not received, carrier says delivered", {"status": "shipped"}, ship("delivered"), "40.00",
     "not_received", "deny"),
    ("not received, no shipment", {"status": "shipped"}, None, "40.00", "not_received", "deny"),
    ("not received, no ETA", {"status": "shipped"}, ship("in_transit"), "40.00", "not_received",
     "deny"),
]  # fmt: skip


@pytest.mark.parametrize(("case", "kw", "shipment", "amount", "reason", "expected"), REFUND_CASES,
                         ids=[c[0] for c in REFUND_CASES])  # fmt: skip
def test_refund_decision_table(
    case: str,
    kw: dict[str, Any],
    shipment: Shipment | None,
    amount: str,
    reason: str,
    expected: str,
) -> None:
    d = decide_refund(order(**kw), shipment, Decimal(amount), reason, TODAY, P)
    assert d.outcome == expected, f"{case}: {d.reason}"


def test_return_excludes_final_sale_items() -> None:
    o = order(
        total="70.00",
        items=[
            Item(sku="A", name="Lamp", price=Decimal("30.00"), qty=1),
            Item(sku="B", name="Clearance vase", price=Decimal("40.00"), qty=1, final_sale=True),
        ],
    )
    assert decide_refund(o, None, Decimal("30.00"), "return", TODAY, P).outcome == "auto"
    assert decide_refund(o, None, Decimal("30.01"), "return", TODAY, P).outcome == "deny"
    # damage claims are not limited by final sale
    assert decide_refund(o, None, Decimal("70.00"), "damaged", TODAY, P).outcome == "approval"


def test_policy_limits_are_configurable() -> None:
    strict = Policy(auto_refund_limit_usd=Decimal(10), damage_claim_window_days=3)
    assert (
        decide_refund(order(days=2), None, Decimal("40"), "damaged", TODAY, strict).outcome
        == "approval"
    )
    assert (
        decide_refund(order(days=4), None, Decimal("5"), "damaged", TODAY, strict).outcome == "deny"
    )


def test_decision_args_are_normalised_money() -> None:
    d = decide_refund(order(), None, Decimal("12.345"), "damaged", TODAY, P)
    assert d.args == {"order_id": "10001", "amount": "12.35", "reason": "damaged"}


@given(amount=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000"), places=2),
       refunded=st.decimals(min_value=Decimal(0), max_value=Decimal("200"), places=2))  # fmt: skip
def test_never_approves_more_than_remaining(amount: Decimal, refunded: Decimal) -> None:
    o = order(total="200.00", refunded=str(min(refunded, Decimal(200))))
    d = decide_refund(o, None, amount, "damaged", TODAY, P)
    if d.outcome != "deny":
        assert money(amount) <= o.remaining
        assert (d.outcome == "auto") == (money(amount) <= P.auto_refund_limit_usd)


@pytest.mark.parametrize(
    ("status", "address", "expected"),
    [
        ("processing", "12 Garden Town, Lahore 54000", "auto"),
        ("shipped", "12 Garden Town, Lahore 54000", "approval"),
        ("delivered", "12 Garden Town, Lahore 54000", "deny"),
        ("cancelled", "12 Garden Town, Lahore 54000", "deny"),
        ("processing", "short", "deny"),
        ("processing", "x" * 301, "deny"),
        ("processing", "   9 Harbor View Road   ", "auto"),
    ],
)
def test_address_decision_table(status: str, address: str, expected: str) -> None:
    assert decide_address(order(status=status), address, P).outcome == expected


def test_money_rounds_half_up() -> None:
    assert money("2.345") == Decimal("2.35")
    assert money(Decimal("2.344")) == Decimal("2.34")
