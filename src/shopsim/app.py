"""shopsim: a small mock commerce API (separate service) with fault injection.

    uv run uvicorn shopsim.app:create_app --factory --port 8006

Writes require an Idempotency-Key: repeating a key returns the original result instead of a
second refund. Fault injection can fail a request *after* the write committed, which is the
case that makes idempotency necessary.
"""

import asyncio
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import normalize_database_url


class ShopSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOPSIM_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5436/app"
    service_token: str = "dev-shopsim-token"  # noqa: S105  (dev default; set SHOPSIM_SERVICE_TOKEN in prod)
    failure_rate: float = Field(0.0, ge=0, le=1)  # fraction of requests that fail with 503
    fail_after_commit: bool = True  # a failing write still commits (the realistic, nasty case)
    latency_ms: int = Field(0, ge=0, le=10_000)
    seed: int | None = None  # fixed RNG for reproducible chaos tests

    # The template's engine factory expects these
    db_pool_size: int = 5
    db_max_overflow: int = 5
    app_name: str = "shopsim"


@lru_cache
def get_shop_settings() -> ShopSettings:
    return ShopSettings()


ORDER_SQL = text("""
SELECT o.id, o.customer_email, o.status, o.total, o.refunded, o.placed_at, o.delivered_at, o.address,
       coalesce(json_agg(json_build_object('sku', i.sku, 'name', i.name, 'price', i.price, 'qty', i.qty,
                'final_sale', i.final_sale) ORDER BY i.id) FILTER (WHERE i.id IS NOT NULL), '[]') AS items
FROM shop_orders o LEFT JOIN shop_order_items i ON i.order_id = o.id
WHERE o.id = :id GROUP BY o.id
""")


def _order(row: Any) -> dict[str, Any]:
    items = row.items if isinstance(row.items, list) else json.loads(row.items)
    return {
        "id": row.id,
        "customer_email": row.customer_email,
        "status": row.status,
        "total": str(row.total),
        "refunded": str(row.refunded),
        "placed_at": str(row.placed_at),
        "delivered_at": str(row.delivered_at) if row.delivered_at else None,
        "address": row.address,
        "items": [{**i, "price": str(i["price"])} for i in items],
    }


class RefundIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(max_length=40)


class AddressIn(BaseModel):
    address: str = Field(min_length=10, max_length=300)


def create_app(settings: ShopSettings | None = None) -> FastAPI:
    s = settings or get_shop_settings()
    engine: AsyncEngine = create_async_engine(
        normalize_database_url(s.database_url), pool_pre_ping=True
    )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    rng = random.Random(s.seed)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="shopsim", lifespan=lifespan)
    app.state.settings = s
    app.state.engine = engine

    async def auth(x_service_token: Annotated[str | None, Header()] = None) -> None:
        if x_service_token != s.service_token:
            raise HTTPException(401, "bad service token")

    async def session() -> AsyncIterator[AsyncSession]:
        async with sm() as ss:
            yield ss

    Auth = Annotated[None, Depends(auth)]
    Session = Annotated[AsyncSession, Depends(session)]

    async def chaos(request: Request) -> bool:
        """Maybe delay, and decide whether this request will fail."""
        cfg: ShopSettings = request.app.state.settings
        if cfg.latency_ms:
            await asyncio.sleep(cfg.latency_ms / 1000)
        return rng.random() < cfg.failure_rate

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/customers/{email}")
    async def customer(email: str, _: Auth, ss: Session, request: Request) -> dict[str, Any]:
        if await chaos(request):
            raise HTTPException(503, "injected failure")
        row = (
            await ss.execute(
                text("SELECT email, name, address FROM shop_customers WHERE email = :e"),
                {"e": email},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "customer not found")
        return {"email": row.email, "name": row.name, "address": row.address}

    @app.get("/customers/{email}/orders")
    async def orders(email: str, _: Auth, ss: Session, request: Request) -> list[dict[str, Any]]:
        if await chaos(request):
            raise HTTPException(503, "injected failure")
        rows = (
            await ss.execute(
                text(
                    "SELECT id, status, total, placed_at FROM shop_orders WHERE customer_email = :e "
                    "ORDER BY placed_at DESC LIMIT 20"
                ),
                {"e": email},
            )
        ).all()
        return [
            {"id": r.id, "status": r.status, "total": str(r.total), "placed_at": str(r.placed_at)}
            for r in rows
        ]

    @app.get("/orders/{order_id}")
    async def order(order_id: str, _: Auth, ss: Session, request: Request) -> dict[str, Any]:
        if await chaos(request):
            raise HTTPException(503, "injected failure")
        row = (await ss.execute(ORDER_SQL, {"id": order_id})).first()
        if row is None:
            raise HTTPException(404, "order not found")
        return _order(row)

    @app.get("/orders/{order_id}/shipment")
    async def shipment(order_id: str, _: Auth, ss: Session, request: Request) -> dict[str, Any]:
        if await chaos(request):
            raise HTTPException(503, "injected failure")
        row = (
            await ss.execute(
                text("SELECT carrier, status, eta, events FROM shop_shipments WHERE order_id = :o"),
                {"o": order_id},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "no shipment")
        events = row.events if isinstance(row.events, list) else json.loads(row.events)
        return {
            "carrier": row.carrier,
            "status": row.status,
            "eta": str(row.eta) if row.eta else None,
            "events": events,
        }

    @app.post("/orders/{order_id}/refunds")
    async def refund(
        order_id: str,
        body: RefundIn,
        _: Auth,
        ss: Session,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=120)],
    ) -> dict[str, Any]:
        fail = await chaos(request)
        if fail and not s.fail_after_commit:
            raise HTTPException(503, "injected failure")
        existing = (
            await ss.execute(
                text(
                    "SELECT id, order_id, amount, reason FROM shop_refunds "
                    "WHERE idempotency_key = :k"
                ),
                {"k": idempotency_key},
            )
        ).first()
        if existing is not None:  # replay: same key -> same refund, never a second one
            return {
                "refund_id": existing.id,
                "order_id": existing.order_id,
                "amount": str(existing.amount),
                "reason": existing.reason,
                "replayed": True,
            }
        o = (
            await ss.execute(
                text("SELECT total, refunded FROM shop_orders WHERE id = :id FOR UPDATE"),
                {"id": order_id},
            )
        ).first()
        if o is None:
            raise HTTPException(404, "order not found")
        if body.amount > o.total - o.refunded:
            raise HTTPException(422, "refund exceeds remaining order value")
        rid = (
            await ss.execute(
                text(
                    "INSERT INTO shop_refunds (order_id, amount, reason, idempotency_key) VALUES (:o, :a, :r, :k) "
                    "RETURNING id"
                ),
                {"o": order_id, "a": body.amount, "r": body.reason, "k": idempotency_key},
            )
        ).scalar_one()
        await ss.execute(
            text("UPDATE shop_orders SET refunded = refunded + :a WHERE id = :id"),
            {"a": body.amount, "id": order_id},
        )
        await ss.commit()
        if fail:  # the refund exists, but the caller sees an error and will retry
            raise HTTPException(503, "injected failure after commit")
        return {
            "refund_id": rid,
            "order_id": order_id,
            "amount": str(body.amount),
            "reason": body.reason,
            "replayed": False,
        }

    @app.post("/orders/{order_id}/address")
    async def address(
        order_id: str,
        body: AddressIn,
        _: Auth,
        ss: Session,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=120)],
    ) -> dict[str, Any]:
        fail = await chaos(request)
        if fail and not s.fail_after_commit:
            raise HTTPException(503, "injected failure")
        existing = (
            await ss.execute(
                text("SELECT id FROM shop_address_changes WHERE idempotency_key = :k"),
                {"k": idempotency_key},
            )
        ).first()
        if existing is None:
            await ss.execute(
                text(
                    "INSERT INTO shop_address_changes (order_id, address, idempotency_key) "
                    "VALUES (:o, :a, :k)"
                ),
                {"o": order_id, "a": body.address, "k": idempotency_key},
            )
            await ss.execute(
                text("UPDATE shop_orders SET address = :a WHERE id = :id"),
                {"a": body.address, "id": order_id},
            )
            await ss.commit()
        if fail:
            raise HTTPException(503, "injected failure after commit")
        return {"order_id": order_id, "address": body.address, "replayed": existing is not None}

    @app.get("/orders/{order_id}/refunds")
    async def list_refunds(order_id: str, _: Auth, ss: Session) -> list[dict[str, Any]]:
        rows = (
            await ss.execute(
                text(
                    "SELECT id, amount, reason, idempotency_key FROM shop_refunds "
                    "WHERE order_id = :o ORDER BY id"
                ),
                {"o": order_id},
            )
        ).all()
        return [
            {
                "refund_id": r.id,
                "amount": str(r.amount),
                "reason": r.reason,
                "key": r.idempotency_key,
            }
            for r in rows
        ]

    return app
