"""Weekly task (SPEC 9.2, plan I1). Run: python -m app.tasks.weekly

Python first (zero tokens, zero Upwork calls): skill counts and median budgets against last week
(`app/trends.py`), proposal results from the `proposals` table, the daily notes of the week.
Then Opus (`models.think`, `prompts/weekly_strategy.md`) writes the trend report and may suggest
ONE profile change. Then Sonnet (`models.write`, `prompts/write_profile.md`) writes the text.

The report is saved (`/reports`). A profile change goes to Approvals as a `profile_change` item
with before and after, the same item the profile test loop makes (approve -> Upwork preview ->
second click confirms; dry-run blocks). Nothing is applied here (decision D9). No change is asked
for when `playbook.can_start_test` says no (baseline, JSS guard, a test already running) or an
earlier change still waits. The weekly consolidation (L4) and research (L5) are separate tasks.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import mail, notes, playbook, profile_loop, rank, reports, skills, trends
from app import profile_versions as pv
from app.base_url import get_base_url
from app.config import Config, load_config
from app.db import init_db
from app.llm import prompting
from app.llm.live import ClaudeCancelled
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("weekly")

TASK = "weekly"
THINK_TASK, THINK_PROMPT = "weekly_strategy", "prompts/weekly_strategy.md"
WRITE_TASK, WRITE_PROMPT = "write_profile", "prompts/write_profile.md"
JOB_TAG, NOTES_TAG = "untrusted_job_data", "untrusted_notes"


@dataclass
class WeeklyResult:
    run_id: str
    status: str  # ok | skipped | error
    report_id: int | None = None
    item_id: int | None = None
    note: str = ""  # why no profile change was made (not an error)
    error: str | None = None


def _call(
    task, prompt, data, model_key, effort_key, prefix, conn, config, settings, run_id, extra=""
):
    return run_claude(
        task,
        prompt,
        data,
        str(settings.get(model_key)),
        int(settings.get(f"weekly.{prefix}_max_turns")),
        float(settings.get(f"weekly.{prefix}_max_budget_usd")),
        config=config,
        conn=conn,
        settings=settings,
        effort=settings.get(effort_key) or None,
        timeout=int(settings.get(f"weekly.{prefix}_timeout_seconds")),
        prompt_text=prompting.build_prompt(prompt, extra, config=config),
        record_run_id=run_id,
    )


def week_notes(config: Config, settings: Settings, today: date) -> str:
    days = int(settings.get("weekly.window_days"))
    parts = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        text = notes.read_note(config, day).strip()
        if text:
            parts.append(f"## {day.isoformat()}\n{text}")
    return "\n\n".join(parts)[-int(settings.get("weekly.notes_max_chars")) :]


def build_stats(conn, settings: Settings, today: date) -> dict[str, Any]:
    days = int(settings.get("weekly.window_days"))
    end = today + timedelta(days=1)
    return {
        "trends": trends.weekly_comparison(conn, settings, today, days),
        "proposals": trends.proposal_results(conn, settings, end - timedelta(days=days), end),
        "proposals_before": trends.proposal_results(
            conn, settings, end - timedelta(days=2 * days), end - timedelta(days=days)
        ),
    }


def clean_report(value: Any, settings: Settings) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Claude's answer had no report.")
    text = prompting.no_em_dash(value.replace("\r\n", "\n").strip())
    return text[: int(settings.get("weekly.report_max_chars"))]


def _blocked_reason(conn, config, settings, now) -> tuple[str, list[dict[str, Any]]]:
    """('', candidates) when a profile change may be asked for, else (reason, [])."""
    reason = playbook.can_start_test(conn, settings, now)
    if reason:
        return reason, []
    if profile_loop.pending_change(conn) is not None:
        return "An earlier profile change still waits in approvals.", []
    cands = profile_loop.candidates(config, settings)
    if not cands:
        return "No untested tactic can be tested with the profile write tools.", []
    if pv.latest(conn) is None:
        return "No saved copy of the profile yet (the daily run saves one).", []
    return "", cands


def _suggest_change(
    conn, config, settings, change: Any, cands, current, trend_skills, run_id, now
) -> tuple[int | None, str]:
    """Validate the Opus suggestion, let Sonnet write it, make the approval item."""
    if change in (None, {}):
        return None, "The strategy found no profile change worth testing this week."
    pick = profile_loop.validate_pick(change, cands, settings)
    fld = pick["field"]
    data = {
        "field": fld,
        "tactic": pick["tactic"],
        "reason": pick["reason"],
        "current": current[fld],
        "profile": reports.wrap(profile_loop.PROFILE_TAG, current),
        "trend_skills": reports.wrap(JOB_TAG, trend_skills),
        "limits": {
            "title_max_chars": int(settings.get("profile_loop.title_max_chars")),
            "description_max_chars": int(settings.get("profile_loop.description_max_chars")),
            "skills_max": int(settings.get("profile_loop.skills_max")),
            "skill_max_chars": int(settings.get("profile_loop.skill_max_chars")),
        },
    }
    res = _call(WRITE_TASK, WRITE_PROMPT, data, "models.write", "models.write_effort", "write",
                conn, config, settings, run_id,
                skills.task_extra(config, settings, "draft_profile_change"))  # fmt: skip
    sources = "\n".join(
        [
            prompting.user_facts(config),
            *(profile_loop._field_text(f, current[f]) for f in current),
            *trend_skills,
        ]
    )
    value, summary = profile_loop.validate_draft(
        res["output"], pick, current[fld], settings, sources, []
    )
    item_id = profile_loop.create_item(conn, run_id, pick, current, value, summary, now)
    settings.set("profile_loop.last_run", now.astimezone(UTC).isoformat(timespec="seconds"))
    profile_loop.notify(conn, config, settings, item_id, pick, now)
    return item_id, ""


def run_weekly(
    conn, settings: Settings, config: Config, *, now: datetime | None = None
) -> WeeklyResult:
    """The whole task. Never raises: the outcome is in the result and in the runs row."""
    now = now or datetime.now(UTC)
    run_id = reports.start_run(conn, TASK, now)
    result = WeeklyResult(run_id=run_id, status="running")
    try:
        if settings.get("general.kill_switch"):
            result.status, result.error = "skipped", "kill switch is on"
        elif not settings.get("weekly.enabled"):
            result.status, result.error = "skipped", "the weekly task is switched off"
        else:
            _work(conn, settings, config, now, result)
            result.status = "ok"
    except (BudgetCapReached, ClaudeRateLimited, ClaudeFailed, ValueError) as e:
        result.status, result.error = "error", str(e)[:500]
    except Exception as e:
        log.exception("Weekly task failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:500]
    reports.end_run(conn, run_id, result.status, result.error)
    log.info("weekly %s: report=%s item=%s", result.status, result.report_id, result.item_id)
    return result


def _work(conn, settings, config, now, result: WeeklyResult) -> None:
    run_id = result.run_id
    today = trends.today_in(settings, now)
    stats = build_stats(conn, settings, today)
    blocked, cands = _blocked_reason(conn, config, settings, now)
    current = pv.row_fields(pv.latest(conn))
    trend_skills = [r["skill"] for r in stats["trends"]["skills"]]
    data = {
        "stats": reports.wrap(JOB_TAG, stats),
        "notes": reports.wrap(NOTES_TAG, week_notes(config, settings, today)),
        "profile": reports.wrap(profile_loop.PROFILE_TAG, current),
        "playbook": playbook.read(config),
        "change_allowed": not blocked,
        "blocked_reason": blocked,
        "candidates": cands,
        "fields": list(profile_loop.FIELDS),
        "keywords": rank.approved_keywords(conn, settings),
        "reason_max_chars": int(settings.get("weekly.reason_max_chars")),
    }
    res = _call(THINK_TASK, THINK_PROMPT, data, "models.think", "models.think_effort", "think",
                conn, config, settings, run_id)  # fmt: skip
    out = res["output"]
    if not isinstance(out, dict):
        raise ValueError("Claude's answer was not an object.")
    body = clean_report(out.get("report"), settings)
    period = trends.iso_week(today)
    result.report_id = reports.save(
        conn, "weekly", period, f"Weekly report {period}", body, stats, run_id, now
    )
    if blocked:
        result.note = blocked
    else:
        try:
            result.item_id, result.note = _suggest_change(
                conn, config, settings, out.get("change"), cands, current, trend_skills, run_id, now
            )
        except profile_loop.ProfileLoopError as e:  # the report is kept; only the change is lost
            result.note = f"No profile change: {e}"
    if settings.get("weekly.email"):
        lines = [body[:3000]]
        if result.item_id:
            lines.append("A profile change waits in Approvals. Nothing changes until you approve.")
        text, html = mail.build_email(
            f"Weekly report {period}", lines, link_path=f"/reports/{result.report_id}",
            base_url=get_base_url(settings, config), button_label="Open the report",
        )  # fmt: skip
        mail.send_event(conn, "weekly", period, f"Weekly report {period}", text, html,
                        config=config, settings=settings, now=now)  # fmt: skip


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the button and the scheduler share this lock
            conn = init_db(config.db_path)
            result = run_weekly(conn, Settings(conn), config)
    except ClaudeCancelled:  # the user pressed Cancel: stop cleanly, the lock is released
        log.warning("%s was cancelled by the user", TASK)
        return 0
    except TaskBusy:
        log.warning("weekly is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
