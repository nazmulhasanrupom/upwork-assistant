"""Learning page `/learning` (plan L2): the version history of memory/ and skills/ with an Undo.

- GET  /learning              commits with diffs
- POST /learning/{sha}/undo   `git revert` of one commit (login and CSRF come from the app guard)
- POST /learning/run/{name}   start the weekly consolidation (plan L4) or research (plan L5) now,
                              as a detached process
The page also shows DREAMS.md (what the consolidation kept), the skills, and the weekly jobs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import consolidation, memory, research, skills, versioning
from app.auth_routes import render
from app.tasks import lock, trigger

log = logging.getLogger("learning")
router = APIRouter()

NOTICES = {
    "undone": ("ok", "Undone. A new entry in the history reverts that change."),
    "consolidate_started": ("ok", "The weekly consolidation started. It takes a few minutes."),
    "consolidate_busy": ("warn", "The weekly consolidation is already running."),
    "consolidate_killed": ("warn", "The kill switch is on, so nothing was started."),
    "consolidate_off": ("warn", "The weekly consolidation is switched off in the settings."),
    "consolidate_unbuilt": ("warn", "The weekly consolidation task is not available."),
    "research_started": ("ok", "The weekly research started. It takes a few minutes."),
    "research_busy": ("warn", "The weekly research is already running."),
    "research_killed": ("warn", "The kill switch is on, so nothing was started."),
    "research_off": ("warn", "The weekly research is switched off in the settings."),
    "research_unbuilt": ("warn", "The weekly research task is not available."),
}
JOBS = {
    "consolidate": ("consolidation.enabled", consolidation.TASK),
    "research": ("research.enabled", research.TASK),
}


def _jobs(request: Request) -> list[dict]:
    s = request.app.state
    rows = []
    for name, (switch, task) in JOBS.items():
        last = s.db.execute(
            "SELECT started, status, error FROM runs WHERE task = ? ORDER BY started DESC LIMIT 1",
            (task,),
        ).fetchone()
        rows.append(
            {
                "name": name,
                "enabled": bool(s.settings.get(switch)),
                "running": lock.running_since(s.config.data_dir, task) is not None,
                "schedule": s.settings.get(f"schedules.{name}"),
                "last": dict(last) if last else None,
            }
        )
    return rows


def _page(request: Request, status: int = 200, notice: tuple[str, str] | None = None) -> Response:
    s = request.app.state
    cut = int(s.settings.get("versioning.diff_max_chars"))
    commits = []
    error = ""
    if not versioning.git_available():
        error = "git is not installed, so no history can be shown."
    else:
        try:
            for c in versioning.list_commits(s.config, s.settings):
                try:
                    diff = versioning.show_diff(s.config, s.settings, c["sha"])
                except versioning.VersioningError:
                    diff = ""
                commits.append(
                    {
                        **c,
                        "diff": diff[:cut],
                        "cut": len(diff) > cut,
                        "root": c["subject"] == "Start version history",
                    }
                )
        except versioning.VersioningError as e:
            error = str(e)
    dreams = memory.read_file(consolidation.dreams_path(s.config)).strip()
    return render(
        request,
        "learning.html",
        status,
        commits=commits,
        error=error,
        notice=notice,
        jobs=_jobs(request),
        dreams=dreams,
        skill_rows=skills.list_skills(s.config, s.settings),
    )


@router.get("/learning")
def learning_page(request: Request, done: str = "") -> Response:
    return _page(request, notice=NOTICES.get(done))


@router.post("/learning/{sha}/undo")
def undo(request: Request, sha: str) -> Response:
    s = request.app.state
    try:
        versioning.revert(s.config, s.settings, sha)
    except versioning.VersioningError as e:
        log.info("Undo refused: %s", e)
        return _page(request, 409, ("warn", str(e)[:300]))
    except Exception:
        log.exception("Undo failed")
        return _page(request, 500, ("error", "The undo failed. See the log."))
    return RedirectResponse("/learning?done=undone", status_code=303)


@router.post("/learning/run/{name}")
def run_job(request: Request, name: str) -> Response:
    """Start one weekly job now, detached. Login + CSRF come from the app-wide guard."""
    if name not in JOBS:
        raise HTTPException(status_code=404)
    s = request.app.state
    switch, task = JOBS[name]
    if s.settings.get("general.kill_switch"):
        outcome = "killed"
    elif not s.settings.get(switch):
        outcome = "off"
    else:
        try:
            trigger.start(s.config, task)
            outcome = "started"
        except trigger.NotBuilt:
            outcome = "unbuilt"
        except lock.TaskBusy:
            outcome = "busy"
    return RedirectResponse(f"/learning?done={name}_{outcome}", status_code=303)
