"""HTTP client for shopsim with bounded retries. Writes always carry an Idempotency-Key, so
retrying after a timeout or a 5xx can never create a second refund."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)


class ShopError(Exception):
    """A shop call failed after retries (or was rejected)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _Transient(Exception): ...


class ShopClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 3.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"X-Service-Token": token},
        )
        self._attempts = retries + 1

    async def aclose(self) -> None:
        await self._http.aclose()

    async def wake(self, max_wait_s: float) -> bool:
        """Wait until the shop answers /healthz. Free hosts put idle services to sleep and answer
        502 while they boot (up to about a minute). Returns False if it is still down."""
        deadline = time.monotonic() + max_wait_s
        while True:
            try:
                r = await self._http.get("/healthz", timeout=min(30.0, max_wait_s))
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(3)

    async def _call(
        self, method: str, path: str, *, json: Any = None, key: str | None = None
    ) -> Any:
        headers = {"Idempotency-Key": key} if key else None
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_Transient),
                stop=stop_after_attempt(self._attempts),
                wait=wait_exponential_jitter(multiplier=0.2, max=2),
                reraise=True,
            ):
                with attempt:
                    try:
                        r = await self._http.request(method, path, json=json, headers=headers)
                    except httpx.HTTPError as e:
                        raise _Transient(type(e).__name__) from e
                    if r.status_code >= 500:
                        raise _Transient(f"HTTP {r.status_code}")
        except _Transient as e:
            raise ShopError(f"shop unavailable: {e}") from e
        if r.status_code >= 400:
            detail = (
                r.json().get("detail", r.text)
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text
            )
            raise ShopError(str(detail)[:200], r.status_code)
        return r.json()

    async def get_customer(self, email: str) -> dict[str, Any]:
        return dict(await self._call("GET", f"/customers/{email}"))

    async def list_orders(self, email: str) -> list[dict[str, Any]]:
        return list(await self._call("GET", f"/customers/{email}/orders"))

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(await self._call("GET", f"/orders/{order_id}"))

    async def get_shipment(self, order_id: str) -> dict[str, Any]:
        return dict(await self._call("GET", f"/orders/{order_id}/shipment"))

    async def list_refunds(self, order_id: str) -> list[dict[str, Any]]:
        return list(await self._call("GET", f"/orders/{order_id}/refunds"))

    async def create_refund(
        self, order_id: str, amount: Decimal, reason: str, key: str
    ) -> dict[str, Any]:
        return dict(
            await self._call(
                "POST",
                f"/orders/{order_id}/refunds",
                json={"amount": str(amount), "reason": reason},
                key=key,
            )
        )

    async def update_address(self, order_id: str, address: str, key: str) -> dict[str, Any]:
        return dict(
            await self._call(
                "POST", f"/orders/{order_id}/address", json={"address": address}, key=key
            )
        )
