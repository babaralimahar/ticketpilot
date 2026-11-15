"""ticketpilot + shopsim tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15

shopsim is a separate service; its tables live in the same Postgres for the demo (prefix
shop_). TicketPilot never reads them directly, only through shopsim's HTTP API.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str = "created_at") -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    # ---------------------------------------------------------------- shopsim
    op.create_table(
        "shop_customers",
        sa.Column("email", sa.String(200), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("address", sa.Text, nullable=False),
    )
    op.create_table(
        "shop_orders",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column(
            "customer_email", sa.String(200), sa.ForeignKey("shop_customers.email"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total", sa.Numeric(10, 2), nullable=False),
        sa.Column("refunded", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("placed_at", sa.Date, nullable=False),
        sa.Column("delivered_at", sa.Date),
        sa.Column("address", sa.Text, nullable=False),
        sa.CheckConstraint(
            "refunded >= 0 AND refunded <= total", name="ck_shop_orders_refund_bounds"
        ),
    )
    op.create_index("ix_shop_orders_customer", "shop_orders", ["customer_email"])
    op.create_table(
        "shop_order_items",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(20),
            sa.ForeignKey("shop_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sku", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("final_sale", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_shop_items_order", "shop_order_items", ["order_id"])
    op.create_table(
        "shop_shipments",
        sa.Column(
            "order_id",
            sa.String(20),
            sa.ForeignKey("shop_orders.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("carrier", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("eta", sa.Date),
        sa.Column("events", postgresql.JSONB, nullable=False, server_default="[]"),
    )
    op.create_table(
        "shop_refunds",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.String(20), sa.ForeignKey("shop_orders.id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        _ts(),
    )
    op.create_table(
        "shop_address_changes",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.String(20), sa.ForeignKey("shop_orders.id"), nullable=False),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        _ts(),
    )

    # ---------------------------------------------------------------- ticketpilot
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("customer_email", sa.String(200), nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        _ts(),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("ticket_id", sa.String(40), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("thread_id", sa.String(80), nullable=False, unique=True),
        sa.Column("engine", sa.String(20), nullable=False, server_default="agent"),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("intent", sa.String(40)),
        sa.Column("steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("summary", postgresql.JSONB, nullable=False, server_default="{}"),
        _ts("started_at"),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("action", postgresql.JSONB, nullable=False),
        sa.Column("risk", sa.String(30), nullable=False),
        sa.Column("policy_reason", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(40)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
        _ts(),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_approvals_status"
        ),
    )
    op.create_index("ix_approvals_status", "approvals", ["status", "created_at"])
    op.create_table(
        "action_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("tool", sa.String(40), nullable=False),
        sa.Column("args", postgresql.JSONB, nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        sa.Column("result", postgresql.JSONB),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        _ts(),
    )
    op.create_table(
        "app_flags",
        sa.Column("name", sa.String(60), primary_key=True),
        sa.Column("value", sa.Boolean, nullable=False),
        sa.Column("changed_by", sa.String(40)),
        _ts("changed_at"),
    )


def downgrade() -> None:
    for t in (
        "app_flags",
        "action_log",
        "approvals",
        "runs",
        "tickets",
        "shop_address_changes",
        "shop_refunds",
        "shop_shipments",
        "shop_order_items",
        "shop_orders",
        "shop_customers",
    ):
        op.drop_table(t)
