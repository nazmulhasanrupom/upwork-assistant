"""Profile history `/profile/versions` (plan O1). Login and CSRF come from the app-wide guard.

- GET  /profile/versions                    list, diff of two versions, pending previews
- POST /profile/versions/snapshot           save a copy now (READ bridge, no write)
- POST /profile/versions/{id}/rollback      put a roll back item in /approvals (no write)
- POST /profile/previews/{id}/confirm       confirm one profile preview (a user click; plan O3)

A profile change or roll back is decided on /approvals; approving it only makes Upwork PREVIEWS.
The confirm button on this page is the second click: it needs a fresh confirm click, and dry-run
blocks it (`writes.confirm_profile_preview`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import profile_loop
from app import profile_versions as pv
from app.auth_routes import render
from app.llm.runner import ClaudeError
from app.upwork import writes
from app.upwork.bridge import ClaudeUpworkBridge
from app.upwork.client import UpworkAuthError, UpworkMCPError
from app.worker import WorkerError, ensure_writes_open, finished_url, known_action

log = logging.getLogger("profile_routes")
router = APIRouter()

NOTICES = {
    "saved": ("ok", "A new version of the profile was saved."),
    "unchanged": ("ok", "The profile has not changed since the last saved version."),
    "previewed": (
        "ok",
        "Previews were made on Upwork. Your profile is not changed: see the previews below.",
    ),
    "confirmed": ("ok", "Confirmed. The change is live on your profile."),
    "test_started": ("ok", "Confirmed. The change is live and the 14-day test has started."),
    "dry_run": ("warn", "Dry-run is on: the confirm was blocked and nothing was sent to Upwork."),
}


def _page(
    request: Request,
    status: int = 200,
    notice: tuple[str, str] | None = None,
    action_id: str = "",
    **q,
) -> Response:
    s = request.app.state
    rows = pv.list_versions(s.db)
    by_id = {int(v["id"]): v for v in rows}
    versions = [{**dict(v), "skills_count": len(pv.row_fields(v)["skills"])} for v in rows]

    def pick(key: str, default: int | None) -> int | None:
        try:
            n = int(q.get(key) or "")
        except ValueError:
            return default
        return n if n in by_id else default

    new_id = pick("b", int(versions[0]["id"]) if versions else None)
    old_id = pick("a", int(versions[1]["id"]) if len(versions) > 1 else None)
    if new_id is not None and old_id is not None and new_id == old_id:
        old_id = None
    compare = None
    if new_id is not None and old_id is not None:
        compare = {
            "old": by_id[old_id],
            "new": by_id[new_id],
            "fields": pv.fields_diff(
                pv.row_fields(by_id[old_id]), pv.row_fields(by_id[new_id]), s.settings
            ),
        }
    return render(
        request,
        "profile_versions.html",
        status,
        versions=versions,
        compare=compare,
        old_id=old_id,
        new_id=new_id,
        newest_id=int(versions[0]["id"]) if versions else None,
        previews=pv.previews_for_page(s.db, settings=s.settings),
        dry_run=bool(s.settings.get("general.dry_run")),
        notice=notice,
        action_id=action_id,
        keep=int(s.settings.get("profile_versions.keep")),
    )


@router.get("/profile/versions")
def versions_page(
    request: Request,
    done: str = "",
    a: str = "",
    b: str = "",
    ap: int = 0,
    cf: int = 0,
    bl: int = 0,
    er: int = 0,
    action: str = "",
) -> Response:
    worker = request.app.state.worker
    action_id = known_action(worker, action)
    if url := finished_url(worker, action_id):
        return RedirectResponse(url, status_code=303)  # done already: go to its result
    notice = NOTICES.get(done)
    if done == "batch":  # numbers only, never text from the query
        parts = [f"{ap} small change(s) approved, previews made"]
        if cf:
            parts.append(f"{cf} confirmed and live")
        if bl:
            parts.append(f"{bl} confirm(s) blocked by dry-run")
        if er:
            parts.append(f"{er} problem(s): see Approvals")
        notice = ("warn" if er or bl else "ok", ". ".join(parts) + ".")
    return _page(request, notice=notice, action_id=action_id, a=a, b=b)


@router.post("/profile/versions/snapshot")
def snapshot_now(request: Request) -> Response:
    # Reading the profile is slow (a Claude call): it runs on the worker (plan W3).
    config = request.app.state.config
    factory = getattr(request.app.state, "mcp_factory", None)

    def work(ctx: Any) -> tuple[str, str]:
        client = None
        try:
            client = factory() if factory else ClaudeUpworkBridge(config, ctx.settings, ctx.conn)
            res = pv.snapshot(
                client, ctx.conn, ctx.settings, config, datetime.now(UTC), reason="manual"
            )
        except (pv.ProfileVersionError, UpworkAuthError) as e:
            raise WorkerError(str(e)) from e
        except (UpworkMCPError, ClaudeError) as e:
            raise WorkerError(f"Could not read the profile: {str(e)[:200]}") from e
        finally:
            if client is not None:
                try:
                    getattr(client, "close", lambda: None)()
                except Exception:
                    log.exception("Could not close the Upwork bridge")
        return f"/profile/versions?done={res.note}", NOTICES[res.note][1]

    action_id = request.app.state.worker.submit(
        "profile_snapshot", "profile", "Saving a copy of the profile", work, "profile_snapshot"
    )
    return RedirectResponse(f"/profile/versions?action={action_id}", status_code=303)


@router.post("/profile/versions/{version_id}/rollback")
def rollback(request: Request, version_id: int) -> Response:
    try:
        writes.issue_click(request, "preview")  # proof of a real click
    except writes.ClickRequired as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    try:
        pv.request_rollback(request.app.state.db, version_id)
    except pv.ProfileVersionError as e:
        return _page(request, 409, ("warn", str(e)))
    return RedirectResponse("/approvals?done=rollback_queued", status_code=303)


@router.post("/profile/previews/{preview_row_id}/confirm")
def confirm_preview(request: Request, preview_row_id: int) -> Response:
    try:
        click = writes.issue_click(request, "confirm")  # proof of a real click
    except writes.ClickRequired as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    s = request.app.state
    config = s.config

    def work(ctx: Any) -> tuple[str, str]:
        ensure_writes_open(ctx.settings)  # again, right before the write (the queue can be long)
        try:
            out = profile_loop.confirm(
                click,
                ctx.conn,
                config,
                ctx.settings,
                writes.ClaudeUpworkWriter(config, ctx.settings, ctx.conn),
                preview_row_id,
                datetime.now(UTC),
            )
        except writes.WriteError as e:
            raise WorkerError(str(e)[:300]) from e
        status = out.confirm.status
        if status == "dry_run_blocked":
            return "/profile/versions?done=dry_run#previews", NOTICES["dry_run"][1]
        if status != "confirmed":
            raise WorkerError(out.note)
        done = "test_started" if out.experiment_id else "confirmed"
        return f"/profile/versions?done={done}#previews", NOTICES[done][1]

    action_id = s.worker.submit(
        "profile_confirm",
        str(preview_row_id),
        "Confirming the profile change",
        work,
        f"profile_confirm:{preview_row_id}",
    )
    return RedirectResponse(f"/profile/versions?action={action_id}", status_code=303)
