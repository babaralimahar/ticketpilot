"""Supervisor UI (Jinja2 + HTMX): submit tickets, watch runs, approve or reject actions.
Same service calls and role checks as the API; the API key is kept in an HttpOnly cookie."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.adapters.db.api_keys import SqlApiKeyRepo, hash_key
from app.api.auth import Principal
from app.api.deps import SessionDep
from app.domain.roles import has_role
from ticketpilot import repo
from ticketpilot.api import get_runner, spawn

COOKIE = "tp_key"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/ui", include_in_schema=False)
SAMPLE = {
    "email": "",
    "subject": "Order arrived broken",
    "body": "The mug in my order was shattered. Refund please.",
}


async def _me(request: Request, session: Any) -> Principal | None:
    raw = request.cookies.get(COOKIE)
    rec = await SqlApiKeyRepo(session).find_by_hash(hash_key(raw)) if raw else None
    if rec is None or rec.disabled or not has_role(rec.role, "clerk"):
        return None
    return Principal(rec.id, rec.role)


def _login() -> RedirectResponse:
    return RedirectResponse("/ui/login", status_code=303)


@router.get("/login")
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"me": None, "error": None})


@router.post("/login")
async def login(
    request: Request, session: SessionDep, api_key: Annotated[str, Form(max_length=200)]
) -> Response:
    rec = await SqlApiKeyRepo(session).find_by_hash(hash_key(api_key.strip()))
    if rec is None or rec.disabled or not has_role(rec.role, "clerk"):
        return templates.TemplateResponse(
            request, "login.html", {"me": None, "error": "Key not recognised."}, status_code=401
        )
    resp = RedirectResponse("/ui", status_code=303)
    resp.set_cookie(COOKIE, api_key.strip(), httponly=True, samesite="strict", max_age=8 * 3600)
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = _login()
    resp.delete_cookie(COOKIE)
    return resp


@router.get("")
async def home(request: Request, session: SessionDep) -> Response:
    me = await _me(request, session)
    if me is None:
        return _login()
    rows = (
        (
            await session.execute(
                text(
                    "SELECT r.id, r.status, r.intent, r.cost_usd, t.customer_email, t.subject "
                    "FROM runs r "
                    "JOIN tickets t ON t.id = r.ticket_id ORDER BY r.started_at DESC LIMIT 30"
                )
            )
        )
        .mappings()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "me": me,
            "runs": rows,
            "sample": SAMPLE,
            "killed": await repo.flag(session, "writes_disabled"),
        },
    )


@router.post("/tickets")
async def submit(
    request: Request,
    session: SessionDep,
    email: Annotated[str, Form(max_length=200)],
    subject: Annotated[str, Form(max_length=200)],
    body: Annotated[str, Form(max_length=5000)],
) -> Response:
    if await _me(request, session) is None:
        return _login()
    runner = await get_runner(request)
    ticket = {"email": email.strip(), "subject": subject, "body": body}
    _, rid = await runner.start(ticket)
    spawn(request, runner.run(rid, ticket))
    return RedirectResponse(f"/ui/runs/{rid}", status_code=303)


@router.get("/runs/{run_id}")
async def run_page(request: Request, run_id: str, session: SessionDep) -> Response:
    me = await _me(request, session)
    if me is None:
        return _login()
    r = await repo.get_run(session, run_id)
    if r is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "run.html", {"me": me, "r": r})


@router.get("/runs/{run_id}/status")
async def run_status(request: Request, run_id: str, session: SessionDep) -> Response:
    r = await repo.get_run(session, run_id)
    if r is None:
        return Response(status_code=404)
    resp = HTMLResponse(f'<span class="pill">{r["status"].replace("_", " ")}</span>')
    if r["status"] not in ("running",):
        resp.headers["HX-Refresh"] = "true"
    return resp


@router.get("/approvals")
async def approvals_page(request: Request, session: SessionDep) -> Response:
    me = await _me(request, session)
    if me is None:
        return _login()
    if not has_role(me.role, "approver"):
        return templates.TemplateResponse(
            request,
            "approvals.html",
            {"me": me, "items": [], "error": "Supervisor (approver) role required."},
            status_code=403,
        )
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {"me": me, "items": await repo.list_approvals(session, "pending")},
    )


@router.post("/approvals/{approval_id}")
async def approve(
    request: Request,
    approval_id: str,
    session: SessionDep,
    decision: Annotated[str, Form()],
    note: Annotated[str, Form(max_length=1000)] = "",
) -> Response:
    me = await _me(request, session)
    if me is None:
        return _login()
    if not has_role(me.role, "approver") or decision not in ("approve", "reject"):
        return templates.TemplateResponse(
            request,
            "approvals.html",
            {"me": me, "items": [], "error": "Supervisor (approver) role required."},
            status_code=403,
        )
    row = await repo.decide_approval(session, approval_id, decision, me.key_id, note)
    if row is None:
        msg = {"error": "That approval is no longer pending."}
    else:
        runner = await get_runner(request)
        spawn(request, runner.resume(row["run_id"], decision, me.key_id, note))
        msg = {"message": f"{decision.title()}d. The run is resuming."}
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {"me": me, "items": await repo.list_approvals(session, "pending"), **msg},
    )
