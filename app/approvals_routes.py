"""Approvals page `/approvals` (plan H3). Login and CSRF come from the app-wide guard.

- GET  /approvals                 open proposal previews (link to the job page) + generic items
- POST /approvals/<id>/approve    record the decision for a generic item (handler if one exists)
- POST /approvals/<id>/reject     record the decision
- POST /approvals/keywords/run    start keyword discovery (plan KW1) as a detached process
- POST /approvals/profile-test/run  start the profile test loop (plan O3) as a detached process
- POST /approvals/small/approve   "Approve all small changes": one click for all small profile items

A proposal is never confirmed here: its Approve button is a link to the job page, where the H2
page shows the Connects cost, the boost and the questions first. The generic buttons are POSTs and
they take a `writes.issue_click` first, so a page load can never decide anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import approvals, keywords, playbook, profile_loop
from app import profile_versions as pv
from app.auth_routes import render
from app.tasks import lock, trigger
from app.upwork import writes
from app.upwork.routes import bridge_for

router = APIRouter()

NOTICES = {
    "approved": ("ok", "Approved. The decision is recorded."),
    "rejected": ("ok", "Rejected. The decision is recorded."),
    "started": ("ok", "Keyword search started. It takes a few minutes; the result shows up here."),
    "busy": ("warn", "Keyword search is already running."),
    "killed": ("warn", "The kill switch is on, so the keyword search was not started."),
    "rollback_queued": (
        "ok",
        "The roll back is waiting below. Approve it to make Upwork previews.",
    ),
    "unbuilt": ("warn", "The keyword search task is not available."),
    "pt_started": ("ok", "Profile test started. It takes a few minutes; the change shows up here."),
    "pt_busy": ("warn", "The profile test is already running."),
    "pt_killed": ("warn", "The kill switch is on, so the profile test was not started."),
    "pt_unbuilt": ("warn", "The profile test task is not available."),
    "batch_none": ("warn", "No small profile change is waiting."),
}


def _keywords_panel(request: Request) -> dict:
    s = request.app.state
    since = lock.running_since(s.config.data_dir, keywords.TASK)
    return {
        "running": since is not None,
        "since": since or "",
        "kill_switch": bool(s.settings.get("general.kill_switch")),
        "baseline_active": keywords.baseline_active(s.settings),
        "days_left": keywords.baseline_days_left(s.settings),
        "started": str(s.settings.get("keywords.baseline_started") or ""),
        "baseline_days": int(s.settings.get("keywords.baseline_days")),
        "current": s.settings.get("rank.keywords"),
        "seed_count": len(keywords.seed_list(s.settings)),
    }


def _test_panel(request: Request) -> dict:
    s = request.app.state
    since = lock.running_since(s.config.data_dir, profile_loop.TASK)
    return {
        "running": since is not None,
        "since": since or "",
        "kill_switch": bool(s.settings.get("general.kill_switch")),
        "blocked": playbook.can_start_test(s.db, s.settings),
        "pending": profile_loop.pending_change(s.db) is not None,
        "every_days": int(s.settings.get("profile_loop.every_days")),
        "test_days": int(s.settings.get("playbook.test_days")),
    }


def _page(
    request: Request,
    status: int = 200,
    notice: tuple[str, str] | None = None,
    kind: str = "",
) -> Response:
    s = request.app.state
    approvals.expire_old(s.db, s.settings)
    items = approvals.pending_items(s.db, s.settings)
    return render(
        request,
        "approvals.html",
        status,
        proposals=[] if kind and kind != "proposal" else approvals.open_previews(s.db, s.settings),
        proposal_count=len(approvals.open_previews(s.db, s.settings)),
        items=[it for it in items if not kind or it["kind"] == kind],
        all_items=items,
        kind=kind,
        decided=approvals.recent_decided(s.db),
        dry_run=bool(s.settings.get("general.dry_run")),
        notice=notice,
        kw=_keywords_panel(request),
        small=profile_loop.small_items(s.db, s.settings),
        small_max_words=int(s.settings.get("profile_loop.small_change_max_words")),
        batch_confirms=bool(s.settings.get("profile_loop.batch_confirms")),
        pt=_test_panel(request),
        metas={it["id"]: profile_loop.meta(it) for it in items if it["kind"] == profile_loop.KIND},
        rollbacks={
            it["id"]: pv.fields_diff({**it["before_data"]}, {**it["after_data"]}, s.settings)
            for it in items
            if it["kind"] in (pv.KIND, profile_loop.KIND)
            and isinstance(it["before_data"], dict)
            and isinstance(it["after_data"], dict)
        },
    )


@router.get("/approvals")
def approvals_page(request: Request, done: str = "", kind: str = "") -> Response:
    # only fixed texts, never the query itself
    return _page(request, notice=NOTICES.get(done), kind=kind[:40])


def _context(request: Request, *, confirm: bool = False) -> dict:
    """What a handler may need (profile roll back or change); each preview gets its own click.
    Only the batch button adds `confirm_click`."""
    s = request.app.state
    ctx = {
        "issue_click": lambda: writes.issue_click(request, "preview"),
        "settings": s.settings,
        "config": s.config,
        "reader": lambda: bridge_for(request),
        "writer": lambda: writes.ClaudeUpworkWriter(s.config, s.settings, s.db),
    }
    if confirm:
        ctx["confirm_click"] = lambda: writes.issue_click(request, "confirm")
    return ctx


def _decide(request: Request, item_id: int, decision: str) -> Response:
    try:
        writes.issue_click(request, "confirm")  # proof of a real click; consumed by nothing
    except writes.ClickRequired as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    s = request.app.state
    context = _context(request)
    try:
        row = approvals.decide(s.db, item_id, decision, datetime.now(UTC), context)
    except approvals.ApprovalError as e:
        return _page(request, 409, ("warn", str(e)))
    if row["kind"] in (pv.KIND, profile_loop.KIND) and decision == "approved":
        return RedirectResponse("/profile/versions?done=previewed#previews", status_code=303)
    return RedirectResponse(f"/approvals?done={decision}", status_code=303)


@router.post("/approvals/{item_id:int}/approve")
def approve(request: Request, item_id: int) -> Response:
    return _decide(request, item_id, "approved")


@router.post("/approvals/{item_id:int}/reject")
def reject(request: Request, item_id: int) -> Response:
    return _decide(request, item_id, "rejected")


@router.post("/approvals/keywords/run")
def run_keywords(request: Request) -> Response:
    """Start the keyword discovery now, detached. Login + CSRF come from the app-wide guard."""
    s = request.app.state
    if s.settings.get("general.kill_switch"):
        outcome = "killed"
    else:
        try:
            trigger.start(s.config, keywords.TASK)
            outcome = "started"
        except trigger.NotBuilt:
            outcome = "unbuilt"
        except lock.TaskBusy:
            outcome = "busy"
    return RedirectResponse(f"/approvals?done={outcome}", status_code=303)


@router.post("/approvals/small/approve")
def approve_small(request: Request) -> Response:
    """ "Approve all small changes" (D9): ONE user click for every small pending profile item. Each
    item gets its preview, then (setting profile_loop.batch_confirms) its confirm, only after this
    click. Dry-run blocks the confirms. Login + CSRF come from the app-wide guard."""
    try:
        writes.issue_click(request, "confirm")  # proof of a real click; consumed by nothing
    except writes.ClickRequired as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    s = request.app.state
    if not profile_loop.small_items(s.db, s.settings):
        return RedirectResponse("/approvals?done=batch_none", status_code=303)
    res = profile_loop.approve_small(
        s.db, s.settings, s.config, _context(request, confirm=True), datetime.now(UTC)
    )
    if not res.approved:
        msg = res.errors[0] if res.errors else "Nothing was approved."
        return _page(request, 409, ("warn", msg[:300]))
    q = f"done=batch&ap={res.approved}&cf={res.confirmed}&bl={res.blocked}&er={len(res.errors)}"
    return RedirectResponse(f"/profile/versions?{q}#previews", status_code=303)


@router.post("/approvals/profile-test/run")
def run_profile_test(request: Request) -> Response:
    """Start the profile test loop now, detached (it only makes an approval item)."""
    s = request.app.state
    if s.settings.get("general.kill_switch"):
        outcome = "pt_killed"
    else:
        try:
            trigger.start(s.config, profile_loop.TASK)
            outcome = "pt_started"
        except trigger.NotBuilt:
            outcome = "pt_unbuilt"
        except lock.TaskBusy:
            outcome = "pt_busy"
    return RedirectResponse(f"/approvals?done={outcome}", status_code=303)
