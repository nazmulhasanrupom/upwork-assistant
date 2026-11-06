"""Runs and costs page (plan G4, SPEC 10.1).

- GET /runs               cost cards (today, week, month, cap bar) + table of runs, filter, paging
- GET /runs/log/<name>    the log file of a run, read only, only files directly under
                          <DATA_DIR>/logs with a name made by the task starter

Log names are checked against a strict pattern and the resolved path must stay inside the logs
folder, so a crafted name or a symlink cannot read other files.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app import costs
from app.auth_routes import render
from app.pages import zone
from app.tasks import trigger

router = APIRouter()

LOG_NAME_RE = re.compile(r"^([A-Za-z0-9_]+)-(\d{8}T\d{6})\.log$")  # <task>-<UTC stamp>.log


def logs_dir(request: Request) -> Path:
    return request.app.state.config.data_dir / "logs"


def safe_log_path(base: Path, name: str) -> Path | None:
    """The log file for `name`, or None if the name or the file is not safe to show."""
    if not LOG_NAME_RE.fullmatch(name):
        return None
    path = base / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.resolve().parent != base.resolve():
            return None
    except OSError:
        return None
    return path


def _stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


def log_index(base: Path) -> dict[str, list[tuple[datetime, str]]]:
    """task -> [(start time, file name)] sorted by time, from the files in the logs folder."""
    out: dict[str, list[tuple[datetime, str]]] = {}
    try:
        names = [e.name for e in os.scandir(base) if e.is_file(follow_symlinks=False)]
    except OSError:
        return out
    for name in names:
        m = LOG_NAME_RE.fullmatch(name)
        if not m:
            continue
        try:
            out.setdefault(m.group(1), []).append((_stamp(m.group(2)), name))
        except ValueError:
            continue
    for items in out.values():
        items.sort()
    return out


def log_for_run(
    index: dict[str, list[tuple[datetime, str]]], task: str, started: str, tolerance: timedelta
) -> str | None:
    """The log started by the task starter just before this run row was written."""
    try:
        at = datetime.fromisoformat(started)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    best = None
    for stamp, name in index.get(task, []):
        if stamp <= at + timedelta(seconds=1):
            best = (stamp, name)
    if best and at - best[0] <= tolerance:
        return best[1]
    return None


def _duration(started: str | None, ended: str | None) -> str:
    try:
        a, b = datetime.fromisoformat(started or ""), datetime.fromisoformat(ended or "")
    except ValueError:
        return ""
    seconds = max(0, int((b - a).total_seconds()))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


@router.get("/runs")
def runs_page(request: Request, task: str = "", page: int = 1) -> Response:
    app = request.app
    conn, settings, config = app.state.db, app.state.settings, app.state.config
    tz = zone(config.tz)
    known = [r[0] for r in conn.execute("SELECT DISTINCT task FROM runs ORDER BY task").fetchall()]
    tasks = sorted(set(known) | set(trigger.TASKS))
    task = task if task in tasks else ""

    size = max(1, int(settings.get("ui.runs_page_size")))
    where, args = ("WHERE r.task = ?", [task]) if task else ("", [])
    total = conn.execute(f"SELECT COUNT(*) FROM runs r {where}", args).fetchone()[0]
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    rows = conn.execute(
        "SELECT r.*, COALESCE((SELECT SUM(c.cost_usd) FROM claude_calls c "
        f"WHERE c.run_id = r.run_id), 0) AS cost FROM runs r {where} "
        "ORDER BY r.started DESC, r.rowid DESC LIMIT ? OFFSET ?",
        [*args, size, (page - 1) * size],
    ).fetchall()

    index = log_index(logs_dir(request))
    tolerance = timedelta(minutes=float(settings.get("ui.log_match_minutes")))
    runs = [
        {
            **dict(r),
            "duration": _duration(r["started"], r["ended"]),
            "log": log_for_run(index, r["task"], r["started"], tolerance),
        }
        for r in rows
    ]

    def page_url(n: int) -> str:
        q = {"task": task, "page": str(n)}
        return "/runs?" + urlencode({k: v for k, v in q.items() if v})

    return render(
        request,
        "runs.html",
        runs=runs,
        total=total,
        page=page,
        pages=pages,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < pages else "",
        tasks=tasks,
        f_task=task,
        costs=costs.cost_summary(conn, config, settings, tz),
    )


@router.get("/runs/log/{name}")
def run_log(request: Request, name: str) -> Response:
    base = logs_dir(request)
    path = safe_log_path(base, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Log not found")
    limit = int(request.app.state.settings.get("ui.log_view_max_kb")) * 1024
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            cut = size > limit
            if cut:
                f.seek(size - limit)
            data = f.read(limit)
    except OSError as e:
        raise HTTPException(status_code=404, detail="Log not found") from e
    return render(
        request,
        "run_log.html",
        log_name=name,
        text=data.decode("utf-8", errors="replace"),
        cut=cut,
        size=size,
        limit_kb=limit // 1024,
    )
