<#
  Windows equivalent of the Makefile.
  Usage:  .\tasks.ps1 check        .\tasks.ps1 eval -Suite toy -Set dev
          .\tasks.ps1 key -Name me -Role admin
#>
param(
    [Parameter(Position = 0)][string]$Task = "help",
    [string]$Suite = "toy",
    [string]$Set = "dev",
    [string]$Name = "",
    [string]$Role = "viewer"
)
$ErrorActionPreference = "Stop"

function Run([string]$cmd) {
    Write-Host "> $cmd" -ForegroundColor DarkGray
    Invoke-Expression $cmd
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Task) {
    "install"   { Run "uv sync" }
    "up"        { Run "docker compose up -d --build" }
    "down"      { Run "docker compose down" }
    "db"        { Run "docker compose up -d --wait db" }
    "test"      { Run "uv run pytest -q" }
    "test-unit" { Run "uv run pytest -q tests/unit" }
    "check"     {
        Run "uv run ruff check ."
        Run "uv run ruff format --check ."
        Run "uv run mypy src tests"
        Run "uv run pytest -q"
    }
    "fmt"       { Run "uv run ruff check --fix ."; Run "uv run ruff format ." }
    "migrate"   { Run "uv run alembic upgrade head" }
    "run"       { Run "uv run python -m ticketpilot.serve --port 8000" }
    "shopsim"   { Run "uv run python -m ticketpilot.serve --shopsim --port 8006" }
    "seed"      { Run "uv run python -m shopsim.seed" }
    "scenarios" { Run "uv run python -m ticketpilot.evals.scenarios" }
    "eval-baseline" { Run "uv run python -m ticketpilot.evals --engine baseline --split dev" }
    "eval-agent"    { Run "uv run python -m ticketpilot.evals --engine agent --split dev --runs 1 --max-cost 1.00" }
    "eval"      { Run "uv run python -m app.evals --suite $Suite --split $Set" }
    "eval-test" { Run "uv run python -m app.evals --suite $Suite --split test" }
    "freeze"    { Run "uv run python -c `"from pathlib import Path; from app.evals.dataset import write_sums; print(write_sums(Path('evals/datasets/$Suite')))`"" }
    "key"       { Run "uv run python -m app.cli create-key --name $Name --role $Role" }
    default     { Write-Host "tasks: install up down db test test-unit check fmt migrate run eval eval-test freeze key" }
}
