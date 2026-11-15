# On Windows without make, use the same targets in tasks.ps1:  .\tasks.ps1 check
.PHONY: install up down db test test-unit check fmt migrate run shopsim seed scenarios eval-baseline eval-agent eval eval-test freeze key

install:
	uv sync

up:
	docker compose up -d --build

down:
	docker compose down

db:
	docker compose up -d --wait db

test:
	uv run pytest -q

test-unit:
	uv run pytest -q tests/unit

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src tests
	uv run pytest -q

fmt:
	uv run ruff check --fix .
	uv run ruff format .

migrate:
	uv run alembic upgrade head

run:
	uv run python -m ticketpilot.serve --port 8000

shopsim:
	uv run python -m ticketpilot.serve --shopsim --port 8006

seed:
	uv run python -m shopsim.seed

scenarios:
	uv run python -m ticketpilot.evals.scenarios

eval-baseline:
	uv run python -m ticketpilot.evals --engine baseline --split dev

eval-agent:
	uv run python -m ticketpilot.evals --engine agent --split dev --runs 1 --max-cost 1.00

SET ?= dev
SUITE ?= toy
eval:
	uv run python -m app.evals --suite $(SUITE) --split $(SET)

eval-test:
	uv run python -m app.evals --suite $(SUITE) --split test

freeze:
	uv run python -c "from pathlib import Path; from app.evals.dataset import write_sums; print(write_sums(Path('evals/datasets/$(SUITE)')))"

key:
	uv run python -m app.cli create-key --name $(NAME) --role $(ROLE)
