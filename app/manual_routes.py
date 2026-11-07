"""Manual job add routes (plan M1). Login and CSRF come from the app-wide guard.

- GET  /jobs/add   the form; ?action=<id> shows the action, ?found=<action id> lists the matches
- POST /jobs/add   text (link, id or words), or job_id + found (Add from the list); on the worker
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app import manual_add as M
from app.auth_routes import render
from app.job_routes import _make_bridge
from app.upwork import jobs as J
from app.worker import WorkerError

router = APIRouter()


def _page(
    request: Request,
    *,
    text: str = "",
    action: str = "",
    found_id: str = "",
    notice: tuple[str, str] | None = None,
    status: int = 200,
) -> Response:
    s = request.app.state
    found = None
    if found_id:
        rows = M.load_found(s.config, found_id)
        known = J.known_ids(s.db, [str(r.get("id")) for r in rows if r.get("id")])
        found = M.describe_rows(rows, known)
    if action and s.worker.get(action) is None:
        action = ""
    return render(
        request,
        "jobs_add.html",
        status=status,
        text=text,
        action_id=action,
        found=found,
        found_id=found_id,
        notice=notice,
        max_chars=int(s.settings.get("manual.max_input_chars")),
    )


@router.get("/jobs/add")
def add_form(request: Request, action: str = "", found: str = "") -> Response:
    return _page(request, action=action, found_id=found)


def _queue(request: Request, label: str, fn: Any, key: str) -> Response:
    action_id = request.app.state.worker.submit("manual_add", "add", label, fn, key)
    return RedirectResponse(f"/jobs/add?action={action_id}", status_code=303)


@router.post("/jobs/add")
def add_post(
    request: Request, text: str = Form(""), job_id: str = Form(""), found: str = Form("")
) -> Response:
    s = request.app.state
    config = s.config
    factory = getattr(s, "mcp_factory", None)
    if job_id:  # Add from the list of matches
        job_id = job_id.strip()
        row = next((r for r in M.load_found(config, found) if str(r.get("id")) == job_id), None)
        if row is None:
            return _page(request, notice=("warn", "That list is gone. Search again."), status=422)

        def work_from_list(ctx: Any) -> tuple[str, str]:
            bridge = _make_bridge(factory, config, ctx)
            try:
                return M.add_job(ctx, config, bridge, job_id, row)
            finally:
                _close(bridge)

        return _queue(request, "Add a job from the list", work_from_list, f"manual:{job_id}")

    max_chars = int(s.settings.get("manual.max_input_chars"))
    try:
        parsed = M.parse_input(text, max_chars)
    except M.ManualError as exc:
        return _page(request, text=text[:max_chars], notice=("warn", str(exc)), status=422)

    def work(ctx: Any) -> tuple[str, str]:
        bridge = _make_bridge(factory, config, ctx)
        try:
            if parsed.kind in ("id", "url"):
                return M.add_job(ctx, config, bridge, parsed.value)
            rows = M.search(bridge, ctx.settings, parsed.value)
            M.save_found(config, ctx.action_id, rows)
            return f"/jobs/add?found={ctx.action_id}", f"{len(rows)} job(s) found."
        except M.ManualError as exc:
            raise WorkerError(str(exc)) from exc
        finally:
            _close(bridge)

    return _queue(request, f"Add a job: {parsed.value}"[:100], work, f"manual:{parsed.value}")


def _close(bridge: Any) -> None:
    close = getattr(bridge, "close", None)
    if callable(close):
        close()
