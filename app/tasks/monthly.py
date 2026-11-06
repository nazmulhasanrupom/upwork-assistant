"""Monthly task (SPEC 9.3, plan I2). Run: python -m app.tasks.monthly

Python: proposals sent/viewed/interviews/hires, win rate, Connects spent (from the `proposals`
table), skill demand and budgets, the Claude cost of the month. Optional (`monthly.financials`):
earnings and hours from `get_freelancer_financials` through the READ bridge; if that fails the
report says so and carries on. Then Opus (`models.think`, `prompts/monthly_plan.md`) writes the
full profile audit and the plan for next month. The report is saved (`/reports`) and emailed.
It proposes no profile write: audit findings are advice; profile changes come from the weekly
task and the profile test loop, each with a click (D9).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import mail, playbook, reports, trends
from app import profile_versions as pv
from app.config import Config, load_config
from app.db import init_db
from app.llm import prompting
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("monthly")

TASK = "monthly"
PROMPT = "prompts/monthly_plan.md"
JOB_TAG, UPWORK_TAG = "untrusted_job_data", "untrusted_upwork_data"
PROFILE_TAG = "profile_data"


@dataclass
class MonthlyResult:
    run_id: str
    status: str  # ok | skipped | error
    report_id: int | None = None
    financials: str = ""  # "", "read", "off" or the reason it could not be read
    error: str | None = None


def month_range(today: date) -> tuple[date, date]:
    """The calendar month before `today`: (first day, first day of the next month)."""
    end = today.replace(day=1)
    return (end - timedelta(days=1)).replace(day=1), end


def read_financials(bridge: Any, settings: Settings) -> Any:
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise ValueError("No freelancer account is chosen yet.")
    [answer] = bridge.call_many(
        [("get_freelancer_financials", {"action": "get", "org_uid": org_uid, "params": {}})]
    )
    return answer.unwrap()


def claude_cost(conn, start: date, end: date) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM claude_calls "
        "WHERE created_at >= ? AND created_at < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return round(float(row[0]), 2)


def build_stats(conn, settings: Settings, start: date, end: date) -> dict[str, Any]:
    prop = trends.proposal_results(conn, settings, start, end)
    window = trends.window_stats(conn, settings, start, end)
    top = int(settings.get("trends.top_skills"))
    skills = sorted(window["skills"].items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:top]
    return {
        "month": start.strftime("%Y-%m"),
        "proposals": prop,
        "win_rate_percent": round(100 * prop["hires"] / prop["sent"], 1) if prop["sent"] else None,
        "connects_spent": prop["connects"],
        "jobs_seen": window["jobs"],
        "median_fixed": window["median_fixed"],
        "median_hourly": window["median_hourly"],
        "skills": [{"skill": s, **v} for s, v in skills],
        "claude_cost_usd": claude_cost(conn, start, end),
    }


def run_monthly(
    conn, settings: Settings, config: Config, bridge: Any = None, *, now: datetime | None = None
) -> MonthlyResult:
    """The whole task. Never raises: the outcome is in the result and in the runs row."""
    now = now or datetime.now(UTC)
    run_id = reports.start_run(conn, TASK, now)
    result = MonthlyResult(run_id=run_id, status="running")
    try:
        if settings.get("general.kill_switch"):
            result.status, result.error = "skipped", "kill switch is on"
        elif not settings.get("monthly.enabled"):
            result.status, result.error = "skipped", "the monthly task is switched off"
        else:
            _work(conn, settings, config, bridge, now, result)
            result.status = "ok"
    except (BudgetCapReached, ClaudeRateLimited, ClaudeFailed, ValueError) as e:
        result.status, result.error = "error", str(e)[:500]
    except Exception as e:
        log.exception("Monthly task failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:500]
    reports.end_run(conn, run_id, result.status, result.error)
    log.info("monthly %s: report=%s", result.status, result.report_id)
    return result


def _financials(conn, settings, config, bridge, run_id, result) -> Any:
    if not settings.get("monthly.financials"):
        result.financials = "off"
        return None
    from app.upwork.bridge import ClaudeUpworkBridge

    owns = bridge is None
    try:
        if bridge is None:
            bridge = ClaudeUpworkBridge(config, settings, conn, run_id)
        data = read_financials(bridge, settings)
        result.financials = "read"
        return data
    except Exception as e:  # earnings are a nice-to-have: the report goes on without them
        log.warning("Financials could not be read: %s", type(e).__name__)
        result.financials = f"not read ({type(e).__name__})"
        return None
    finally:
        if owns and bridge is not None:
            bridge.close()


def _work(conn, settings, config, bridge, now, result: MonthlyResult) -> None:
    today = trends.today_in(settings, now)
    start, end = month_range(today)
    stats = build_stats(conn, settings, start, end)
    financials = _financials(conn, settings, config, bridge, result.run_id, result)
    cut = int(settings.get("monthly.financials_max_chars"))
    fin_text = None if financials is None else str(financials)[:cut]
    weekly = [
        {"period": r["period"], "title": r["title"], "body": r["body"][:1500]}
        for r in conn.execute(
            "SELECT period, title, body FROM reports WHERE kind = 'weekly' "
            "AND created_at >= ? AND created_at < ? ORDER BY created_at",
            (start.isoformat(), end.isoformat()),
        )
    ]
    data = {
        "month": stats["month"],
        "stats": reports.wrap(JOB_TAG, stats),
        "financials": reports.wrap(UPWORK_TAG, fin_text) if fin_text is not None else None,
        "profile": reports.wrap(PROFILE_TAG, pv.row_fields(pv.latest(conn))),
        "playbook": playbook.read(config),
        "weekly_reports": reports.wrap(JOB_TAG, weekly),
    }
    res = run_claude(
        TASK,
        PROMPT,
        data,
        str(settings.get("models.think")),
        int(settings.get("monthly.max_turns")),
        float(settings.get("monthly.max_budget_usd")),
        config=config,
        conn=conn,
        settings=settings,
        effort=settings.get("models.think_effort") or None,
        timeout=int(settings.get("monthly.timeout_seconds")),
        prompt_text=prompting.build_prompt(PROMPT, config=config),
        record_run_id=result.run_id,
    )
    out = res["output"]
    text = out.get("report") if isinstance(out, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Claude's answer had no report.")
    body = prompting.no_em_dash(text.replace("\r\n", "\n").strip())
    body = body[: int(settings.get("monthly.report_max_chars"))]
    stats["financials"] = result.financials
    title = f"Monthly report {stats['month']}"
    result.report_id = reports.save(
        conn, "monthly", stats["month"], title, body, stats, result.run_id, now
    )
    if settings.get("monthly.email"):
        text_mail, html = mail.build_email(
            title, [body[:4000]], link_path=f"/reports/{result.report_id}",
            base_url=config.public_base_url, button_label="Open the report",
        )  # fmt: skip
        mail.send_event(conn, "monthly", stats["month"], title, text_mail, html,
                        config=config, settings=settings, now=now)  # fmt: skip


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the button and the scheduler share this lock
            conn = init_db(config.db_path)
            result = run_monthly(conn, Settings(conn), config)
    except TaskBusy:
        log.warning("monthly is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
