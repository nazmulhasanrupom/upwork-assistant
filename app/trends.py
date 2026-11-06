"""Skill counts and median budgets (plan I1, I3). Pure Python on the DB, zero tokens.

Source: `skills_seen` (one row per job and skill, also for dropped jobs) joined with `jobs` for the
budget and the first-seen time. A window is [start, end) in UTC days. Job text is untrusted: skill
names come from job posts, so callers that give them to Claude must mark them untrusted.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from app.metrics import week_start
from app.settings import Settings


def _bounds(start: date, end: date) -> tuple[str, str]:
    return start.isoformat(), end.isoformat()


def _budget(row: sqlite3.Row) -> float | None:
    for key in ("budget_max", "budget_min"):
        value = row[key]
        if value is not None and value > 0:
            return float(value)
    return None


def _median(values: list[float], minimum: int) -> float | None:
    if len(values) < max(1, minimum):
        return None
    return round(float(statistics.median(values)), 2)


def window_stats(
    conn: sqlite3.Connection, settings: Settings, start: date, end: date
) -> dict[str, Any]:
    """{'jobs': n, 'skills': {skill: {'count', 'median_fixed', 'median_hourly'}},
    'median_fixed', 'median_hourly'} for jobs first seen in [start, end)."""
    lo, hi = _bounds(start, end)
    minimum = int(settings.get("trends.min_jobs_for_median"))
    jobs = conn.execute(
        "SELECT job_id, job_type, budget_min, budget_max FROM jobs "
        "WHERE first_seen >= ? AND first_seen < ?",
        (lo, hi),
    ).fetchall()
    info = {j["job_id"]: j for j in jobs}
    per: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, int] = {}
    if info:
        rows = conn.execute(
            "SELECT s.job_id, s.skill FROM skills_seen s JOIN jobs j ON j.job_id = s.job_id "
            "WHERE j.first_seen >= ? AND j.first_seen < ?",
            (lo, hi),
        ).fetchall()
        for r in rows:
            skill = str(r["skill"]).strip()
            if not skill:
                continue
            counts[skill] = counts.get(skill, 0) + 1
            value = _budget(info[r["job_id"]])
            kind = "fixed" if info[r["job_id"]]["job_type"] == "fixed" else "hourly"
            if value is not None:
                per.setdefault(skill, {"fixed": [], "hourly": []})[kind].append(value)
    skills = {
        s: {
            "count": n,
            "median_fixed": _median(per.get(s, {}).get("fixed", []), minimum),
            "median_hourly": _median(per.get(s, {}).get("hourly", []), minimum),
        }
        for s, n in counts.items()
    }
    all_fixed = [v for j in jobs if j["job_type"] == "fixed" and (v := _budget(j)) is not None]
    all_hourly = [v for j in jobs if j["job_type"] != "fixed" and (v := _budget(j)) is not None]
    return {
        "jobs": len(jobs),
        "skills": skills,
        "median_fixed": _median(all_fixed, minimum),
        "median_hourly": _median(all_hourly, minimum),
    }


def _change(now_v: float | None, before_v: float | None) -> float | None:
    if now_v is None or before_v is None:
        return None
    return round(now_v - before_v, 2)


def weekly_comparison(
    conn: sqlite3.Connection, settings: Settings, today: date, days: int | None = None
) -> dict[str, Any]:
    """The last `days` days (ending today, included) against the same length before."""
    days = days or int(settings.get("weekly.window_days"))
    end = today + timedelta(days=1)
    mid = end - timedelta(days=days)
    start = mid - timedelta(days=days)
    now_w = window_stats(conn, settings, mid, end)
    before_w = window_stats(conn, settings, start, mid)
    top = int(settings.get("trends.top_skills"))
    ranked = sorted(now_w["skills"].items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:top]
    rows = []
    for skill, cur in ranked:
        old = before_w["skills"].get(skill, {})
        rows.append(
            {
                "skill": skill,
                "count": cur["count"],
                "count_before": old.get("count", 0),
                "median_fixed": cur["median_fixed"],
                "median_fixed_change": _change(cur["median_fixed"], old.get("median_fixed")),
                "median_hourly": cur["median_hourly"],
                "median_hourly_change": _change(cur["median_hourly"], old.get("median_hourly")),
            }
        )
    return {
        "from": mid.isoformat(),
        "to": today.isoformat(),
        "jobs": now_w["jobs"],
        "jobs_before": before_w["jobs"],
        "median_fixed": now_w["median_fixed"],
        "median_hourly": now_w["median_hourly"],
        "skills": rows,
    }


def proposal_results(
    conn: sqlite3.Connection, settings: Settings, start: date, end: date
) -> dict[str, int]:
    """Proposals sent in [start, end): sent, viewed, interviews, hires (statuses from settings)."""
    lo, hi = _bounds(start, end)
    rows = conn.execute(
        "SELECT status, viewed_by_client, connects_bid FROM proposals "
        "WHERE created_at >= ? AND created_at < ?",
        (lo, hi),
    ).fetchall()
    interview = {str(s) for s in settings.get("metrics.interview_statuses")}
    hired = {str(s) for s in settings.get("metrics.hire_statuses")}
    return {
        "sent": len(rows),
        "viewed": sum(1 for r in rows if r["viewed_by_client"]),
        "interviews": sum(1 for r in rows if r["status"] in interview),
        "hires": sum(1 for r in rows if r["status"] in hired),
        "connects": sum(int(r["connects_bid"] or 0) for r in rows),
    }


def weekly_series(conn: sqlite3.Connection, settings: Settings, today: date) -> dict[str, Any]:
    """For /trends: per week (Monday start) over `trends.weeks` weeks, the count of each of the
    overall top skills, and the median fixed and hourly budget of all jobs."""
    n = int(settings.get("trends.weeks"))
    first = week_start(today) - timedelta(weeks=n - 1)
    weeks = [first + timedelta(weeks=i) for i in range(n)]
    stats = [window_stats(conn, settings, w, w + timedelta(days=7)) for w in weeks]
    totals: dict[str, int] = {}
    for st in stats:
        for skill, v in st["skills"].items():
            totals[skill] = totals.get(skill, 0) + v["count"]
    top = [
        s
        for s, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[
            : int(settings.get("trends.top_skills"))
        ]
    ]
    return {
        "weeks": weeks,
        "top": [(s, totals[s]) for s in top],
        "jobs": [st["jobs"] for st in stats],
        "median_fixed": [st["median_fixed"] for st in stats],
        "median_hourly": [st["median_hourly"] for st in stats],
    }


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def today_in(settings: Settings, now: datetime) -> date:
    from app import keywords

    return now.astimezone(keywords._zone(settings)).date() if now.tzinfo else now.date()
