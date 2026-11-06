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

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import charts, job_actions
from app import costs as cost_totals
from app import metrics as metrics_data
from app import rank as rank_data
from app.auth_routes import render
from app.llm import prompting
from app.tasks import lock, trigger
from app.upwork.jobs import CLOSE_TAG, OPEN_TAG

router = APIRouter()

TIERS = ("HIGH", "MID", "LOW")
STATUSES = ("candidate", "judged", "drafted", "prepared", "sent", "skipped", "dropped", "seen")
STATUS_ACTIVE = ""  # default: everything except dropped jobs
STATUS_ALL = "all"
AFFORD_CHOICES = ("show", "hide")
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


def _amount(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def budget_text(job: dict[str, Any]) -> tuple[str, str]:
    """(main text, small note) for the Budget row on the job page (plan G0).

    Fixed job: "$300 fixed", no note. Hourly job: "$15-$35/hr" plus a note about where the
    range comes from (the client, or Upwork's default range, which is not the client's rate).
    """
    lo, hi = job.get("budget_min"), job.get("budget_max")
    if lo is None and hi is None:
        return "n/a", ""
    lo = hi if lo is None else lo
    hi = lo if hi is None else hi
    if str(job.get("job_type") or "").lower() == "fixed":
        return f"{_amount(lo)} fixed", ""  # the hourly budget type means nothing here
    text = _amount(lo) if lo == hi else f"{_amount(lo)}-{_amount(hi)}"
    kind = str(job.get("hourly_budget_type") or "").strip().lower()
    if kind.startswith("platform default"):
        note = "platform default range, not the client's rate"
    elif kind.startswith("client"):
        note = "client range"
    else:
        note = kind
    return f"{text}/hr", note


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
    env.filters["budget"] = lambda job: budget_text(job)[0]
    env.filters["budget_note"] = lambda job: budget_text(job)[1]


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


def dashboard_data(
    conn: sqlite3.Connection,
    tz: tzinfo,
    now: datetime | None = None,
    *,
    config: Any = None,
    settings: Any = None,
) -> dict:
    """`config` and `settings` add the Claude cost card and the cap warning (plan G4)."""
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
    costs = None
    if config is not None and settings is not None:
        costs = cost_totals.cost_summary(conn, config, settings, tz, now)
        cap_alert = cost_totals.cap_alert(costs)
        if cap_alert:
            alerts.append(cap_alert)
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
        "costs": costs,
        "metrics": (
            metrics_data.dashboard_summary(conn, settings, tz, now)
            if settings is not None
            else None
        ),
        "rank": (
            rank_data.dashboard_summary(conn, settings, tz, now) if settings is not None else None
        ),
        "charts": chart_data(conn, settings, tz, now) if settings is not None else None,
        "jss_guard": (
            {
                "on": bool(settings.get("safety.jss_guard")),
                "note": settings.get("safety.jss_guard_note"),
            }
            if settings is not None
            else None
        ),
    }


def chart_data(conn: sqlite3.Connection, settings: Any, tz: tzinfo, now: datetime) -> dict:
    """The funnel chart and one rank chart per keyword (plan X3). SVG is built in app/charts.py."""
    funnel = charts.funnel_data(conn, settings, tz, now)
    keywords = rank_data.approved_keywords(conn, settings) if settings.get("rank.enabled") else []
    ranks = []
    for entry in charts.rank_data(conn, settings, tz, now, keywords):
        ranks.append({**entry, "svg": charts.rank_svg(entry), "latest": _latest_point(entry)})
    return {
        "funnel": funnel,
        "funnel_svg": charts.funnel_svg(funnel),
        "ranks": ranks,
        "funnel_weeks": len(funnel["weeks"]),
        "rank_days": ranks[0]["days"] if ranks else int(settings.get("ui.rank_chart_days")),
    }


def _latest_point(entry: dict) -> str:
    points = entry["points"]
    if not points:
        return ""
    last = points[-1]
    return f"#{last['position']}" if last["position"] is not None else "not found"


EMAIL_ALERTS = {"gmail_expired", "email_failed"}  # their message points to the Email page

# What the dashboard says after a "Run now" click. Fixed texts only (never echo the query).
RUN_MESSAGES = {
    "started": ("ok", "Started. The table below refreshes while it runs."),
    "busy": ("warn", "That task is already running."),
    "unbuilt": ("warn", "That task is not built yet."),
    "killed": ("error", "The kill switch is on, so nothing was started."),
    "views_saved": ("ok", "Profile views saved for this week."),
    "guard_cleared": ("ok", "The JSS guard is off. Profile tests may run again."),
    "views_bad": ("error", "Profile views must be a whole number, 0 or more."),
}


def runs_panel(request: Request) -> dict:
    """Data for the Last runs table: one row per task, with the Run now state."""
    config = request.app.state.config
    data = dashboard_data(request.app.state.db, _tz(request))  # no costs: the table needs none
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
    s = request.app.state
    data = dashboard_data(s.db, _tz(request), config=s.config, settings=s.settings)
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
    if name not in TASKS and name not in trigger.MANUAL_TASKS:
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


@router.post("/safety/jss-guard/clear")
def clear_jss_guard(request: Request) -> Response:
    """The owner clears the JSS guard by hand (nothing in the code ever does). Login + CSRF are
    checked by the app-wide guard."""
    s = request.app.state.settings
    s.set("safety.jss_guard", False)
    return RedirectResponse("/?msg=guard_cleared", status_code=303)


@router.post("/metrics/profile-views")
def save_profile_views(request: Request, views: str = Form("")) -> Response:
    """Save this week's profile views (typed by hand, Upwork gives no number). Login + CSRF are
    checked by the app-wide guard."""
    s = request.app.state
    try:
        number = int(views.strip())
        metrics_data.save_profile_views(
            s.db, s.settings, number, datetime.now(UTC), tz=_tz(request)
        )
    except ValueError:
        return RedirectResponse("/?msg=views_bad", status_code=303)
    return RedirectResponse("/?msg=views_saved", status_code=303)


# ---------------------------------------------------------------- jobs list


@router.get("/jobs")
def jobs_list(
    request: Request,
    tier: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    afford: str = "",
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
    # Jobs that cost more Connects than the latest balance (plan X3). `afford` is "show", "hide"
    # or "" (= the setting ui.hide_unaffordable).
    afford = afford if afford in AFFORD_CHOICES else ""
    hide = afford == "hide" or (
        afford == "" and bool(request.app.state.settings.get("ui.hide_unaffordable"))
    )
    latest = conn.execute("SELECT balance FROM connects ORDER BY id DESC LIMIT 1").fetchone()
    balance = latest["balance"] if latest else None
    if hide and balance is not None:
        where.append("(j.connects_cost IS NULL OR j.connects_cost <= ?)")
        args.append(balance)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    size = max(1, int(request.app.state.settings.get("ui.jobs_page_size")))
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j {clause}", args).fetchone()[0]
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    rows = conn.execute(
        f"SELECT j.*, g.fit_score, g.why_apply, g.plan AS plan_json, "
        f"EXISTS (SELECT 1 FROM drafts d WHERE d.job_id = j.job_id) AS has_draft FROM jobs j "
        f"LEFT JOIN judgements g ON g.job_id = j.job_id {clause} "
        f"ORDER BY j.first_seen DESC, j.rate_paid DESC LIMIT ? OFFSET ?",
        [*args, size, (page - 1) * size],
    ).fetchall()
    cards = []
    for r in rows:
        plan = _json(r["plan_json"], {})
        # Python decides from the latest balance; the judgement's answer (older) is the fallback
        can_afford = plan.get("can_afford") if isinstance(plan, dict) else None
        if balance is not None and r["connects_cost"] is not None:
            can_afford = r["connects_cost"] <= balance
        cards.append(
            {
                **dict(r),
                "flags": _json(r["red_flags"], []),
                "can_afford": can_afford,
                "connects_balance": balance,
            }
        )

    def page_url(n: int) -> str:
        q = {
            "tier": tier,
            "status": status,
            "date_from": date_from,
            "date_to": date_to,
            "afford": afford,
        }
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
        f_afford=afford,
        hide_default=bool(request.app.state.settings.get("ui.hide_unaffordable")),
        balance=balance,
    )


# ---------------------------------------------------------------- job detail


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str, msg: str = "") -> Response:
    return job_page(request, job_id, notice=job_actions.JOB_MESSAGES.get(msg))


def job_page(
    request: Request,
    job_id: str,
    *,
    notice: tuple[str, str] | None = None,
    status: int = 200,
    edit: dict[str, Any] | None = None,
    edit_dashes: bool = False,
) -> Response:
    """The job page. `notice` is (kind, text) for the flash on top; `edit` re-opens the draft form
    with what the user typed (plan H2)."""
    conn: sqlite3.Connection = request.app.state.db
    settings = request.app.state.settings
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    judgement = conn.execute("SELECT * FROM judgements WHERE job_id = ?", (job_id,)).fetchone()
    draft = conn.execute(
        "SELECT * FROM drafts WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    details = _json(row["details"], {})
    if not isinstance(details, dict):
        details = {}
    fit_min = int(settings.get("pay.fit_min"))
    return render(
        request,
        "job_detail.html",
        status,
        job=dict(row),
        flags=_json(row["red_flags"], []),
        details=details,
        judgement=judgement_view(judgement),
        draft=draft_view(draft),
        draft_note=draft_note(request, row, judgement, draft, fit_min),
        link=upwork_link(row["url"], row["job_id"]),
        notice=notice,
        act=job_actions.build_view(conn, settings, row, draft),
        edit=edit,
        edit_dashes=edit_dashes,
        max_letter=int(settings.get("writes.cover_letter_max_chars")),
        max_answer=int(settings.get("writes.answer_max_chars")),
    )


def _lines(value: str | None) -> list[str]:
    return [x for x in (value or "").splitlines() if x.strip()]


def judgement_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """A judgement with its text fields split into lists, for the templates."""
    if row is None:
        return None
    plan = _json(row["plan"], {})
    plan = plan if isinstance(plan, dict) else {}
    return {
        **dict(row),
        "why": _lines(row["why_apply"]),
        "risk_list": _lines(row["risks"]),
        "plan_data": plan,
        "can_afford": plan.get("can_afford"),
    }


def draft_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        **dict(row),
        "answer_list": _json(row["answers"], []),
        "material_list": _json(row["materials"], []),
        "flag_list": _json(row["flags"], []),
        "length": len(row["cover_letter"] or ""),
    }


def draft_note(
    request: Request,
    job: sqlite3.Row,
    judgement: sqlite3.Row | None,
    draft: sqlite3.Row | None,
    fit_min: int,
) -> str:
    """Why there is no draft (shown in the empty Draft card)."""
    if draft is not None:
        return ""
    if judgement is None:
        if job["status"] == "dropped":
            return "This job was dropped, so it is not judged or drafted."
        return "Not judged yet. Claude judges new MID and HIGH jobs on the next run."
    if judgement["fit_score"] is not None and judgement["fit_score"] < fit_min:
        return f"No draft: fit {judgement['fit_score']} is below the minimum of {fit_min}."
    if not prompting.user_facts(request.app.state.config):
        return "No draft yet: add your profile facts (USER.md) so Claude can write for you."
    return "The draft is made on the next run."
