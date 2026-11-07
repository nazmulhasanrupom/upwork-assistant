"""Instant job alerts. Run: python -m app.tasks.job_alerts

Started by the scheduler every `job_alerts.interval_minutes`. One cheap check of the newest jobs:
one search, a few detail reads, then the same tier / flags / judge / draft steps as the daily
run. An email goes out only for the tiers in `job_alerts.email_tiers` with a fit score of at
least `job_alerts.fit_min`. Other jobs are still saved and drafted. Read-only on Upwork.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

from app import auto_send_flow, mail
from app.config import Config, load_config
from app.db import init_db
from app.filters.activity import HIDE, classify
from app.filters.flags import flags as red_flags
from app.filters.tiers import prefilter, tier
from app.llm.live import ClaudeCancelled
from app.llm.runner import BudgetCapReached, ClaudeRateLimited
from app.settings import Settings
from app.tasks import daily
from app.tasks.lock import TaskBusy, task_lock
from app.upwork import jobs as J
from app.upwork.bridge import BUDGET_TEXT, ClaudeUpworkBridge, _error_result
from app.upwork.client import UpworkAuthError, UpworkToolError

log = logging.getLogger("job_alerts")

TASK = "job_alerts"
PREFIX = "job_alerts"


@dataclass
class JobAlertsResult:
    status: str  # ok | error | auth_expired | rate_limited | skipped
    run_id: str | None = None  # None when a gate stopped the run before anything started
    seen: int = 0
    kept: int = 0
    dropped: int = 0
    emailed: int = 0
    calls: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


class _CappedClient:
    """Counts calls and refuses more than `cap` (the answer is a "budget" error result)."""

    def __init__(self, inner: Any, cap: int):
        self.inner = inner
        self.cap = cap
        self.used = 0
        self.settings = getattr(inner, "settings", None)

    def call_many(self, calls: list[tuple[str, dict[str, Any] | None]]) -> list[Any]:
        out: list[Any] = []
        for tool, args in calls:
            if self.used >= self.cap:
                out.append(_error_result(tool, BUDGET_TEXT, "budget"))
                continue
            self.used += 1
            out.extend(self.inner.call_many([(tool, args)]))
        return out

    def close(self) -> None:
        self.inner.close()


def _parse_hhmm(text: str) -> time:
    hh, mm = text.strip().split(":")
    return time(int(hh), int(mm))


def in_quiet_hours(window: str, now: datetime, tz: Any) -> bool:
    """True if local `now` is inside "HH:MM-HH:MM" (may cross midnight). Empty or bad = False."""
    window = (window or "").strip()
    if not window:
        return False
    try:
        start_s, end_s = window.split("-")
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    except ValueError:
        log.warning("job_alerts.quiet_hours is not HH:MM-HH:MM: %r (ignored)", window)
        return False
    local = now.astimezone(tz).time().replace(second=0, microsecond=0)
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def calls_today(conn: sqlite3.Connection, config: Config, settings: Settings, now: datetime) -> int:
    """Upwork calls of this task since local midnight."""
    tz = daily._zone(str(settings.get("general.timezone") or config.tz))
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COALESCE(SUM(mcp_calls), 0) AS n FROM runs WHERE task = ? AND started >= ?",
        (TASK, daily._iso(midnight)),
    ).fetchone()
    return int(row["n"])


def _gate(
    conn: sqlite3.Connection, settings: Settings, config: Config, now: datetime
) -> str | None:
    """Reason to skip this run (no Upwork call), or None."""
    if not settings.get("job_alerts.enabled"):
        return "job alerts are off"
    if settings.get("general.kill_switch"):
        return "kill switch is on"
    tz = daily._zone(str(settings.get("general.timezone") or config.tz))
    if in_quiet_hours(str(settings.get("job_alerts.quiet_hours") or ""), now, tz):
        return "quiet hours"
    limit = int(settings.get("job_alerts.max_calls_per_day"))
    if calls_today(conn, config, settings, now) >= limit:
        return f"daily call limit reached ({limit})"
    left = int(settings.get("job_alerts.backoff_left") or 0)
    if left > 0:
        settings.set("job_alerts.backoff_left", left - 1)
        return f"backing off after an Upwork limit ({left} run(s) left)"
    return None


def _start_run(conn: sqlite3.Connection, now: datetime) -> str:
    run_id = f"{PREFIX}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES (?,?,?, 'running')",
        (run_id, TASK, daily._iso(now)),
    )
    return run_id


def _end_run(
    conn: sqlite3.Connection, run_id: str, status: str, error: str | None, calls: int
) -> None:
    conn.execute(
        "UPDATE runs SET ended = ?, status = ?, error = ?, mcp_calls = MAX(mcp_calls, ?) "
        "WHERE run_id = ?",
        (daily._iso(daily._now()), status, error, calls, run_id),
    )


def _newest_first(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest posted first. Jobs with no readable time keep their order, after the others."""

    def stamp(job: dict[str, Any]) -> float:
        try:
            when = datetime.fromisoformat(str(J.posted_text(job)).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()

    return sorted(jobs, key=lambda j: -stamp(j))


def _email_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Job facts for the alert email, from the saved row."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    job: dict[str, Any] = {
        "id": row["job_id"],
        "title": row["title"],
        "client_spend": row["client_spend"],
        "client_rating": row["client_rating"],
        "client_reviews": row["client_reviews"],
        "proposals_tier": row["proposals_tier"],
        "posted_at": row["posted_at"],
    }
    if row["job_type"] == "fixed":
        job["fixed_budget"] = row["budget_min"]
    else:
        job["budget_min"], job["budget_max"] = row["budget_min"], row["budget_max"]
    return job


# ---------------------------------------------------------------- the work


def _process(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any,
    raw_client: Any,
    run_id: str,
    now: datetime,
    result: JobAlertsResult,
) -> str | None:
    """Search, details, tier, save, judge, email. Returns a stop kind (rate_limited) or None."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    week = J.iso_week(now)

    new_jobs, kind = J.fetch_recent(client, settings, conn)
    if kind == J.KIND_RATE_LIMITED:
        result.notes.append("Upwork rate limit on the search")
        return kind
    if kind:
        result.notes.append(f"search stopped: {kind}")
        if kind != J.KIND_COOLDOWN:
            result.error = f"search failed ({kind})"
        return None
    result.seen = len(new_jobs)

    survivors: list[dict[str, Any]] = []
    for job in new_jobs:
        J.save_skills(conn, job["id"], job.get("skills") or [], week)
        label, rule = classify(job, settings, now)
        keep, reason = prefilter(job, settings)
        if keep and label == HIDE:
            keep, reason = False, f"activity: {rule}"
        if keep:
            survivors.append(job)
        else:
            J.save_job(conn, job, status="dropped", drop_reason=reason, activity_label=label)
            result.dropped += 1

    wanted = _newest_first(survivors)[: int(settings.get("job_alerts.max_details_per_run"))]
    outcomes, stop = J.fetch_details_until_stop(client, [j["id"] for j in wanted], org_uid)
    if stop:
        result.notes.append(f"detail reads stopped: {stop}")
    rate_limited = stop == J.KIND_RATE_LIMITED

    kept: list[dict[str, Any]] = []
    balance: int | None = None
    for job, detail in zip(wanted, outcomes, strict=False):
        if isinstance(detail, Exception):
            result.notes.append(f"job {job['id']}: {detail}"[:300])
            continue
        full = J.apply_detail(job, detail, settings)
        if full.get("connects_balance") is not None:
            balance = full["connects_balance"]
        label, rule = classify(full, settings, now)
        keep, reason = prefilter(full, settings)
        if keep and label == HIDE:
            keep, reason = False, f"activity: {rule}"
        if not keep:
            J.save_job(conn, full, status="dropped", drop_reason=reason, activity_label=label)
            result.dropped += 1
            continue
        t, estimated, rate = tier(full, settings)
        flag_list = red_flags(full, settings)
        if t not in daily.TIER_ORDER:  # LOW
            why = f"tier LOW (rate {rate:.2f}/hr)" if rate is not None else "tier LOW (no rate)"
            J.save_job(
                conn,
                full,
                status="dropped",
                tier=t,
                tier_estimated=estimated,
                rate_paid=rate,
                red_flags=flag_list,
                drop_reason=why,
                activity_label=label,
            )
            result.dropped += 1
            continue
        J.save_job(
            conn,
            full,
            status="candidate",
            tier=t,
            tier_estimated=estimated,
            rate_paid=rate,
            red_flags=flag_list,
            activity_label=label,
        )
        full.update(tier=t, tier_estimated=estimated, rate_paid=rate, flags=flag_list)
        full["status"] = "candidate"
        kept.append(full)
    result.kept = len(kept)

    kept.sort(key=daily._rank)
    warning = daily._record_connects(conn, run_id, balance, kept[0] if kept else None, now)
    if warning:
        result.notes.append(warning)
        mail.notify_connects_low(
            conn,
            config,
            warning.removeprefix("Connects low: "),
            now.date().isoformat(),
            settings=settings,
            now=now,
        )

    judged = daily.judge_and_draft(conn, kept, settings=settings, config=config, run_id=run_id)
    result.notes.extend(judged.notes)
    this_run = {j["id"] for j in kept}
    fit_min = int(settings.get("job_alerts.fit_min"))
    tiers = {str(t) for t in settings.get("job_alerts.email_tiers") or []}
    for item in judged.judged:  # emails are not Upwork writes: sent in dry-run too
        if item["job_id"] not in this_run:
            continue  # an older job judged again: not a fresh alert
        if item["tier"] in tiers and item["fit_score"] >= fit_min:
            mail.notify_job_alert(
                conn,
                config,
                _email_job(conn, item["job_id"]),
                judgement=item,
                settings=settings,
                now=now,
            )
            result.emailed += 1
    try:  # same auto-send countdowns as the daily run (no-op in dry-run or kill switch)
        started = auto_send_flow.start_countdowns(
            conn, settings, config, raw_client, [i["job_id"] for i in judged.judged], now=now
        )
        if started:
            result.notes.append(f"auto-send: {len(started)} countdown(s) started")
    except ClaudeCancelled:
        raise
    except Exception:
        log.exception("Auto-send could not start")
        result.notes.append("auto-send: could not start (see the log)")
    return J.KIND_RATE_LIMITED if rate_limited else None


def run_job_alerts(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any = None,
    *,
    now: datetime | None = None,
) -> JobAlertsResult:
    """One alert check. `client` is for tests (any object with `call_many`)."""
    now = now or daily._now()
    reason = _gate(conn, settings, config, now)
    if reason:
        log.info("job alerts skipped: %s", reason)
        return JobAlertsResult(status="skipped", error=reason)

    run_id = _start_run(conn, now)
    result = JobAlertsResult(status="running", run_id=run_id)
    cap = 1 + int(settings.get("job_alerts.max_details_per_run"))
    owns_client = client is None
    capped: _CappedClient | None = None
    try:
        if client is None:
            client = ClaudeUpworkBridge(config, settings, conn, run_id)
        capped = _CappedClient(client, cap)
        stop = _process(conn, settings, config, capped, client, run_id, now, result)
        if stop == J.KIND_RATE_LIMITED:
            settings.set("job_alerts.backoff_left", int(settings.get("job_alerts.backoff_runs")))
            result.status, result.error = "rate_limited", "Upwork rate limit: backing off"
        else:
            result.status = "error" if result.error else "ok"
    except UpworkAuthError as e:
        result.status, result.error = "auth_expired", str(e)
    except (BudgetCapReached, ClaudeRateLimited) as e:
        result.status, result.error = "skipped", f"Claude limit: {e}"[:1000]
    except ClaudeCancelled:
        _end_run(conn, run_id, "cancelled", "cancelled by the user", capped.used if capped else 0)
        raise
    except UpworkToolError as e:
        stop_kind = J.error_kind_of(e)
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:1000]
        if stop_kind == J.KIND_RATE_LIMITED:
            settings.set("job_alerts.backoff_left", int(settings.get("job_alerts.backoff_runs")))
            result.status = "rate_limited"
    except Exception as e:
        log.exception("Job alert run failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:1000]
    finally:
        if owns_client and client is not None:
            client.close()

    result.calls = capped.used if capped else 0
    _end_run(conn, run_id, result.status, result.error, result.calls)
    if result.status == "auth_expired":
        mail.notify_auth_expired(conn, config, run_id, settings=settings, now=now)
    log.info(
        "job_alerts %s: seen=%d kept=%d dropped=%d emailed=%d",
        result.status,
        result.seen,
        result.kept,
        result.dropped,
        result.emailed,
    )
    return result


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # a second tick finds the lock taken
            conn = init_db(config.db_path)
            result = run_job_alerts(conn, Settings(conn), config)
    except ClaudeCancelled:
        log.warning("%s was cancelled by the user", TASK)
        return 0
    except TaskBusy:
        log.warning("job_alerts is already running: nothing started")
        return 0
    return 1 if result.status in ("error", "auth_expired") else 0


if __name__ == "__main__":
    sys.exit(main())
