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
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import approvals, auto_send, keywords, playbook, profile_loop
from app import profile_versions as pv
from app.auth_routes import render
from app.tasks import lock, trigger
from app.upwork import writes
from app.upwork.bridge import ClaudeUpworkBridge
from app.worker import WorkerError, ensure_writes_open, finished_url, known_action

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
    action_id: str = "",
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
        auto_cards=[] if kind and kind != "auto_send" else auto_send.waiting_cards(s.db),
        items=[it for it in items if not kind or it["kind"] == kind],
        all_items=items,
        kind=kind,
        decided=approvals.recent_decided(s.db),
        dry_run=bool(s.settings.get("general.dry_run")),
        notice=notice,
        action_id=action_id,
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
def approvals_page(request: Request, done: str = "", kind: str = "", action: str = "") -> Response:
    # only fixed texts, never the query itself
    worker = request.app.state.worker
    action_id = known_action(worker, action)
    if url := finished_url(worker, action_id):
        return RedirectResponse(url, status_code=303)  # done already: go to its result
    return _page(request, notice=NOTICES.get(done), kind=kind[:40], action_id=action_id)


class ClickBank:
    """Clicks made in the POST route, handed out one by one to the work function. The worker
    never makes a click: a click is only ever issued from the user's own POST (`issue_click`)."""

    def __init__(self, request: Request, action: str, count: int):
        self._clicks = [writes.issue_click(request, action) for _ in range(count)]
        self._action = action

    def take(self) -> writes.UserClick:
        if not self._clicks:
            raise writes.ClickRequired("The click ran out. Click again.")
        return self._clicks.pop()


def _context(request: Request, *, confirm: bool = False, count: int = 0) -> dict:
    """What a handler may need (profile roll back or change); each preview gets its own click,
    made here in the route (a bank of `count` items' worth of clicks, none for an item that
    makes no Upwork preview). Only the batch button adds `confirm_click`.
    The reader and writer are built inside the work function."""
    s = request.app.state
    per = int(s.settings.get("worker.clicks_per_item"))
    previews = ClickBank(request, "preview", count * per)
    ctx = {
        "issue_click": previews.take,
        "settings": s.settings,
        "config": s.config,
        "factory": getattr(s, "mcp_factory", None),
    }
    if confirm:
        ctx["confirms"] = ClickBank(request, "confirm", count * per)
    return ctx


def _work_context(base: dict, ctx: Any) -> dict:
    """The handler context for the worker thread: its own settings and database connection."""
    config, factory = base["config"], base["factory"]
    out = {
        "issue_click": base["issue_click"],
        "settings": ctx.settings,
        "config": config,
        "reader": lambda: (
            factory() if factory else ClaudeUpworkBridge(config, ctx.settings, ctx.conn)
        ),
        "writer": lambda: writes.ClaudeUpworkWriter(config, ctx.settings, ctx.conn),
    }
    if "confirms" in base:
        out["confirm_click"] = base["confirms"].take
    return out


def _decide(request: Request, item_id: int, decision: str) -> Response:
    try:
        writes.issue_click(request, "confirm")  # proof of a real click; consumed by nothing
    except writes.ClickRequired as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    s = request.app.state
    if decision == "approved":
        return _approve_in_worker(request, item_id)
    try:
        approvals.decide(s.db, item_id, decision, datetime.now(UTC), _context(request))
    except approvals.ApprovalError as e:
        return _page(request, 409, ("warn", str(e)))
    return RedirectResponse(f"/approvals?done={decision}", status_code=303)


def _approve_in_worker(request: Request, item_id: int) -> Response:
    """Approving can make Upwork previews (profile items): that is slow, so it runs on the worker
    (plan W3). The clicks are made here; the kill switch is read again right before the write."""
    s = request.app.state
    found = s.db.execute("SELECT kind FROM approvals WHERE id = ?", (item_id,)).fetchone()
    writes_previews = found is not None and found["kind"] in (pv.KIND, profile_loop.KIND)
    base = _context(request, count=1 if writes_previews else 0)

    def work(ctx: Any) -> tuple[str, str]:
        if writes_previews:
            ensure_writes_open(ctx.settings)
        try:
            row = approvals.decide(
                ctx.conn, item_id, "approved", datetime.now(UTC), _work_context(base, ctx)
            )
        except approvals.ApprovalError as e:
            raise WorkerError(str(e)) from e
        if row["kind"] in (pv.KIND, profile_loop.KIND):
            return "/profile/versions?done=previewed#previews", NOTICES["approved"][1]
        return "/approvals?done=approved", NOTICES["approved"][1]

    action_id = s.worker.submit(
        "approval_approve", str(item_id), "Approving", work, f"approve:{item_id}"
    )
    return RedirectResponse(f"/approvals?action={action_id}", status_code=303)


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
    small = profile_loop.small_items(s.db, s.settings)
    if not small:
        return RedirectResponse("/approvals?done=batch_none", status_code=303)
    base = _context(request, confirm=True, count=len(small))

    def work(ctx: Any) -> tuple[str, str]:
        ensure_writes_open(ctx.settings)  # again, right before the writes
        res = profile_loop.approve_small(
            ctx.conn, ctx.settings, base["config"], _work_context(base, ctx),
            datetime.now(UTC),
        )  # fmt: skip
        if not res.approved:
            raise WorkerError((res.errors[0] if res.errors else "Nothing was approved.")[:300])
        q = f"done=batch&ap={res.approved}&cf={res.confirmed}&bl={res.blocked}&er={len(res.errors)}"
        return f"/profile/versions?{q}#previews", "Small changes approved."

    action_id = s.worker.submit(
        "approval_small", "small", "Approving small changes", work, "approve:small"
    )
    return RedirectResponse(f"/approvals?action={action_id}", status_code=303)


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
