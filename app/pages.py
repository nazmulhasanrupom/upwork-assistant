"""Panel pages: dashboard `/`, jobs list `/jobs`, job detail `/jobs/<id>` (SPEC 10.1).

All of these need login (the global guard in main.py). Job text comes from clients, so it is
always escaped by Jinja, and links are only made for real upwork.com addresses.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.auth_routes import render
from app.tasks import lock, trigger
from app.upwork.jobs import CLOSE_TAG, OPEN_TAG

router = APIRouter()

TIERS = ("HIGH", "MID", "LOW")
STATUSES = ("candidate", "judged", "drafted", "prepared", "sent", "skipped", "dropped", "seen")
STATUS_ACTIVE = ""  # default: everything except dropped jobs
STATUS_ALL = "all"
UPWORK_PREFIX = "https://www.upwork.com/"
TASKS = trigger.TASKS  # shown on the dashboard even if never run


# ---------------------------------------------------------------- helpers


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return UTC


def fmt_dt(value: str | None, tz_name: str = "UTC") -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(zone(tz_name))
    return f"{local.strftime('%Y-%m-%d %H:%M')} {local.tzname() or ''}".strip()


def fmt_money(value: float | None) -> str:
    return "" if value is None else f"${value:,.0f}"


def fmt_rate(value: float | None) -> str:
    return "" if value is None else f"${value:,.2f}/hr"


def strip_untrusted(text: str | None) -> str:
    """Remove the safety wrapper tags before showing client text (it is still escaped)."""
    if not text:
        return ""
    return str(text).replace(OPEN_TAG, "").replace(CLOSE_TAG, "").strip()


def upwork_link(url: str | None, job_id: str | None) -> str | None:
    if isinstance(url, str) and url.startswith(UPWORK_PREFIX):
        return url
    if job_id and job_id.isdigit():
        return f"{UPWORK_PREFIX}jobs/~02{job_id}"
    return None


def install_filters(templates: Any, tz_name: str) -> None:
    env = templates.env
    env.filters["dt"] = lambda v: fmt_dt(v, tz_name)
    env.filters["money"] = fmt_money
    env.filters["rate"] = fmt_rate
    env.filters["untrusted"] = strip_untrusted


def _tz(request: Request) -> tzinfo:
    return zone(request.app.state.config.tz)


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _local_day_start_utc(day: date, tz: tzinfo) -> str:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC).isoformat(timespec="seconds")


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- dashboard


def dashboard_data(conn: sqlite3.Connection, tz: tzinfo, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    since = _local_day_start_utc(today, tz)

    connects = conn.execute("SELECT * FROM connects ORDER BY id DESC LIMIT 1").fetchone()
    stored_alerts = [
        dict(r) for r in conn.execute("SELECT key, message, created_at FROM alerts ORDER BY key")
    ]
    low_connects = any(a["key"] == "connects_low" for a in stored_alerts)

    last_runs: dict[str, sqlite3.Row | None] = {t: None for t in TASKS}
    for r in conn.execute(
        "SELECT r.* FROM runs r JOIN (SELECT task, MAX(started) AS m FROM runs GROUP BY task) x "
        "ON r.task = x.task AND r.started = x.m ORDER BY r.task"
    ):
        last_runs[r["task"]] = r

    alerts: list[dict[str, str]] = []
    for r in last_runs.values():
        if r is None:
            continue
        if r["status"] == "auth_expired":
            alerts.append(
                {
                    "kind": "error",
                    "message": (
                        f"Upwork login expired ({r['task']} run). "
                        "Log in to Upwork in Claude Code again (see the Upwork page)."
                    ),
                    "link": "/upwork",
                }
            )
        elif r["status"] == "error":
            alerts.append(
                {
                    "kind": "error",
                    "message": f"Last {r['task']} run failed: {r['error'] or 'unknown error'}",
                    "link": "",
                }
            )
    for a in stored_alerts:
        link = "/settings/email" if a["key"] in EMAIL_ALERTS else ""
        alerts.append({"kind": "warn", "message": a["message"], "link": link})

    counts = {
        t: conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tier = ? AND status != 'dropped' AND first_seen >= ?",
            (t, since),
        ).fetchone()[0]
        for t in ("MID", "HIGH")
    }
    return {
        "connects": connects,
        "low_connects": low_connects,
        "last_runs": last_runs,
        "alerts": alerts,
        "counts": counts,
        "today": today.isoformat(),
    }


EMAIL_ALERTS = {"gmail_expired", "email_failed"}  # their message points to the Email page

# What the dashboard says after a "Run now" click. Fixed texts only (never echo the query).
RUN_MESSAGES = {
    "started": ("ok", "Started. The table below refreshes while it runs."),
    "busy": ("warn", "That task is already running."),
    "unbuilt": ("warn", "That task is not built yet."),
    "killed": ("error", "The kill switch is on, so nothing was started."),
}


def runs_panel(request: Request) -> dict:
    """Data for the Last runs table: one row per task, with the Run now state."""
    config = request.app.state.config
    data = dashboard_data(request.app.state.db, _tz(request))
    busy = trigger.running(config)
    rows = []
    for task, run in data["last_runs"].items():
        rows.append(
            {
                "task": task,
                "run": run,
                "built": trigger.is_built(task),
                "busy": task in busy,
                "since": busy.get(task, ""),
            }
        )
    return {
        "rows": rows,
        "any_busy": bool(busy),
        "kill_switch": bool(request.app.state.settings.get("general.kill_switch")),
    }


@router.get("/")
def dashboard(request: Request, msg: str = "") -> Response:
    data = dashboard_data(request.app.state.db, _tz(request))
    return render(
        request, "dashboard.html", **data, **runs_panel(request), notice=RUN_MESSAGES.get(msg)
    )


@router.get("/tasks/runs")
def runs_partial(request: Request) -> Response:
    """The Last runs table alone. HTMX asks for it every 5 s while a task runs."""
    return render(request, "_runs_table.html", **runs_panel(request))


@router.post("/tasks/{name}/run")
def run_task(request: Request, name: str) -> Response:
    """Start a task now, detached. Login + CSRF are checked by the app-wide guard."""
    if name not in TASKS:
        raise HTTPException(status_code=404, detail="Unknown task")
    app = request.app
    if app.state.settings.get("general.kill_switch"):
        outcome = "killed"
    else:
        try:
            trigger.start(app.state.config, name)
            outcome = "started"
        except trigger.NotBuilt:
            outcome = "unbuilt"
        except lock.TaskBusy:
            outcome = "busy"
    return RedirectResponse(f"/?msg={outcome}", status_code=303)


# ---------------------------------------------------------------- jobs list


@router.get("/jobs")
def jobs_list(
    request: Request,
    tier: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
) -> Response:
    conn: sqlite3.Connection = request.app.state.db
    tz = _tz(request)
    tier = tier.upper() if tier.upper() in TIERS else ""
    status = status if status in STATUSES or status == STATUS_ALL else STATUS_ACTIVE
    day_from, day_to = _parse_day(date_from), _parse_day(date_to)

    where, args = [], []
    if tier:
        where.append("j.tier = ?")
        args.append(tier)
    if status == STATUS_ACTIVE:
        where.append("j.status != 'dropped'")
    elif status != STATUS_ALL:
        where.append("j.status = ?")
        args.append(status)
    if day_from:
        where.append("j.first_seen >= ?")
        args.append(_local_day_start_utc(day_from, tz))
    if day_to:
        where.append("j.first_seen < ?")
        args.append(_local_day_start_utc(day_to + timedelta(days=1), tz))
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    size = max(1, int(request.app.state.settings.get("ui.jobs_page_size")))
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j {clause}", args).fetchone()[0]
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    rows = conn.execute(
        f"SELECT j.*, g.fit_score, g.why_apply FROM jobs j "
        f"LEFT JOIN judgements g ON g.job_id = j.job_id {clause} "
        f"ORDER BY j.first_seen DESC, j.rate_paid DESC LIMIT ? OFFSET ?",
        [*args, size, (page - 1) * size],
    ).fetchall()
    cards = [{**dict(r), "flags": _json(r["red_flags"], [])} for r in rows]

    def page_url(n: int) -> str:
        q = {"tier": tier, "status": status, "date_from": date_from, "date_to": date_to}
        q = {k: v for k, v in q.items() if v}
        q["page"] = str(n)
        return "/jobs?" + urlencode(q)

    return render(
        request,
        "jobs.html",
        cards=cards,
        total=total,
        page=page,
        pages=pages,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < pages else "",
        tiers=TIERS,
        statuses=STATUSES,
        f_tier=tier,
        f_status=status,
        f_from=day_from.isoformat() if day_from else "",
        f_to=day_to.isoformat() if day_to else "",
    )


# ---------------------------------------------------------------- job detail


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str) -> Response:
    conn: sqlite3.Connection = request.app.state.db
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    judgement = conn.execute("SELECT * FROM judgements WHERE job_id = ?", (job_id,)).fetchone()
    details = _json(row["details"], {})
    if not isinstance(details, dict):
        details = {}
    return render(
        request,
        "job_detail.html",
        job=dict(row),
        flags=_json(row["red_flags"], []),
        details=details,
        judgement=dict(judgement) if judgement else None,
        link=upwork_link(row["url"], row["job_id"]),
    )
