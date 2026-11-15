from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from typing import Any

import pytest
from tenacity import wait_none

import app.adapters.llm as llm_module
from app.config import Settings
from ticketpilot.config import TicketSettings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5436/app_test"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "database_url": TEST_DATABASE_URL,
        "llm_provider": "fake",
        "worker_mode": "off",
        "otel_endpoint": None,
        "_env_file": None,  # tests never read the developer's .env
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests fast: tenacity waits are zero in tests."""
    monkeypatch.setattr(llm_module, "_WAIT", wait_none())


def pytest_asyncio_loop_factories(config: Any, item: Any) -> dict[str, Any]:
    """psycopg's async driver (LangGraph checkpointer) cannot run on Windows' Proactor loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def make_ticket_settings(**overrides: Any) -> TicketSettings:
    """TicketPilot settings for tests: scripted LLM, fixed business date, never the dev .env."""
    values: dict[str, Any] = {
        "env": "test",
        "database_url": TEST_DATABASE_URL,
        "llm_provider": "fake",
        "worker_mode": "off",
        "otel_endpoint": None,
        "business_date": date(2026, 11, 10),
        "_env_file": None,
    }
    values.update(overrides)
    return TicketSettings(**values)
