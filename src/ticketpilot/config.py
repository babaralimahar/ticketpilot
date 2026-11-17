"""TicketPilot settings."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import Field

from app.config import Settings


class TicketSettings(Settings):
    app_name: str = "ticketpilot"
    worker_mode: str = "off"  # type: ignore[assignment]  # runs execute as asyncio tasks, not jobs

    shopsim_url: str = "http://127.0.0.1:8006"
    shopsim_token: str = "dev-shopsim-token"  # noqa: S105  (dev default; set SHOPSIM_TOKEN in prod)
    shop_timeout_s: float = Field(3.0, gt=0)
    shop_wake_s: float = Field(0.0, ge=0)  # >0 on hosts that sleep idle services

    # Agent budgets (F1): every run ends in a defined state, never an exception.
    max_steps: int = Field(8, ge=1, le=20)
    token_budget: int = Field(30_000, ge=1000)
    deadline_s: float = Field(60.0, gt=0)

    prompt_version: str = "agent_v1"
    policy_path: str = "config/policy.yaml"
    # Business date for policy windows. None = today. Set for the synthetic shop (dated 2026-11-10).
    business_date: date | None = None

    def today(self) -> date:
        return self.business_date or date.today()

    @property
    def checkpoint_url(self) -> str:
        """psycopg-style URL for the LangGraph Postgres checkpointer."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "?ssl=", "?sslmode="
        )


@lru_cache
def get_ticket_settings() -> TicketSettings:
    return TicketSettings()
