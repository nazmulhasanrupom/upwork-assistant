"""Claude cost totals for the dashboard and /runs (plan G4, SPEC 10.1).

Cost comes from `claude_calls.cost_usd` (the `total_cost_usd` Claude reports). "Today", "this
week" (Monday first) and "this month" are in the panel time zone. The month total is the same
number the monthly cap check uses (`runner.monthly_spend`), so the bar and the stop agree.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from app.config import Config
from app.llm.runner import monthly_spend
from app.settings import Settings


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def cost_since(conn: sqlite3.Connection, start: datetime) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM claude_calls WHERE created_at >= ?",
        (_utc(start),),
    ).fetchone()
    return float(row[0])


def day_start(now: datetime, tz: tzinfo) -> datetime:
    return datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)


def week_start(now: datetime, tz: tzinfo) -> datetime:
    today = now.astimezone(tz).date()
    return datetime.combine(today - timedelta(days=today.weekday()), time.min, tzinfo=tz)


def cost_summary(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    tz: tzinfo,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    cap = float(settings.get("claude.monthly_cap_usd"))
    warn_pct = float(settings.get("claude.cap_warn_percent"))
    month = monthly_spend(conn, config, settings, now)
    percent = 100 * month / cap if cap > 0 else (100.0 if month > 0 else 0.0)
    reached = month >= cap
    return {
        "today": cost_since(conn, day_start(now, tz)),
        "week": cost_since(conn, week_start(now, tz)),
        "month": month,
        "cap": cap,
        "percent": min(100, round(percent)),
        "reached": reached,
        "warn": (not reached) and percent >= warn_pct,
        "warn_percent": warn_pct,
    }


def cap_alert(costs: dict[str, Any]) -> dict[str, str] | None:
    """A dashboard alert when the month's cost is near or at the cap."""
    if costs["reached"]:
        return {
            "kind": "error",
            "message": (
                f"Monthly Claude cap reached: ${costs['month']:.2f} of ${costs['cap']:.2f}. "
                "Claude steps are paused until next month or until you raise the cap."
            ),
            "link": "/runs",
        }
    if costs["warn"]:
        return {
            "kind": "warn",
            "message": (
                f"Claude cost is ${costs['month']:.2f} of the ${costs['cap']:.2f} monthly cap "
                f"({costs['percent']}%)."
            ),
            "link": "/runs",
        }
    return None
