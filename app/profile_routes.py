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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import profile_loop
from app import profile_versions as pv
from app.auth_routes import render
from app.llm.runner import ClaudeError
from app.upwork import writes
from app.upwork.client import UpworkAuthError, UpworkMCPError
from app.upwork.routes import bridge_for

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
    request: Request, status: int = 200, notice: tuple[str, str] | None = None, **q
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
) -> Response:
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
    return _page(request, notice=notice, a=a, b=b)


@router.post("/profile/versions/snapshot")
def snapshot_now(request: Request) -> Response:
    s = request.app.state
    client = None
    try:
        client = bridge_for(request)
        res = pv.snapshot(client, s.db, s.settings, s.config, datetime.now(UTC), reason="manual")
    except (pv.ProfileVersionError, UpworkAuthError) as e:
        return _page(request, 400, ("error", str(e)))
    except (UpworkMCPError, ClaudeError) as e:
        return _page(request, 502, ("error", f"Could not read the profile: {str(e)[:200]}"))
    except Exception:
        log.exception("Profile snapshot failed")
        return _page(request, 500, ("error", "Could not read the profile. See the log."))
    finally:
        if client is not None:
            try:
                getattr(client, "close", lambda: None)()
            except Exception:
                log.exception("Could not close the Upwork bridge")
    return RedirectResponse(f"/profile/versions?done={res.note}", status_code=303)


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
    try:
        out = profile_loop.confirm(
            click,
            s.db,
            s.config,
            s.settings,
            writes.ClaudeUpworkWriter(s.config, s.settings, s.db),
            preview_row_id,
            datetime.now(UTC),
        )
    except writes.WriteError as e:
        return _page(request, 409, ("warn", str(e)[:300]))
    except Exception:
        log.exception("Profile confirm failed")
        return _page(request, 500, ("error", "The confirm failed. See the log."))
    status = out.confirm.status
    if status == "dry_run_blocked":
        return RedirectResponse("/profile/versions?done=dry_run#previews", status_code=303)
    if status != "confirmed":
        return _page(request, 502, ("error", out.note))
    done = "test_started" if out.experiment_id else "confirmed"
    return RedirectResponse(f"/profile/versions?done={done}#previews", status_code=303)
