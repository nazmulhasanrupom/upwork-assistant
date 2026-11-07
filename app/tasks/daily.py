"""Daily cycle, steps 1 to 7 of SPEC 6.2 (no Claude yet). Run: python -m app.tasks.daily

Upwork is reached through the Claude bridge (`app/upwork/bridge.py`), in batches.

Flow: find jobs, drop the ones already seen, pre-filter on search data, `find_jobs get` for the
survivors, tier + red flags, save to the DB, record Connects, write the daily note.
Steps 8 to 10 (judge and draft with Claude) are a hook: `judge_and_draft`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app import alerts, auto_send_flow, mail, memory, metrics, notes, profile_loop, rank
from app import profile_versions as profile_versions_mod
from app.config import Config, load_config
from app.db import init_db
from app.filters.activity import HIDE, classify
from app.filters.flags import flags as red_flags
from app.filters.tiers import prefilter, tier
from app.llm import judging
from app.llm.judging import JudgeResult
from app.llm.live import ClaudeCancelled
from app.llm.runner import BudgetCapReached, ClaudeRateLimited
from app.review import after_send
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock
from app.upwork import accounts
from app.upwork import jobs as J
from app.upwork.bridge import ClaudeUpworkBridge
from app.upwork.client import (
    CallBudgetExceeded,
    UpworkAuthError,
    UpworkToolError,
)

log = logging.getLogger("daily")

TASK = "daily"
ALERT_CONNECTS_LOW = "connects_low"
TIER_ORDER = {"HIGH": 0, "MID": 1}


@dataclass
class DailyResult:
    run_id: str
    status: str  # ok | error | auth_expired | skipped
    seen: int = 0
    kept: int = 0
    dropped: int = 0
    judged: int = 0
    drafted: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------- small helpers


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:  # unknown zone name: fall back to UTC
        return UTC


def _start_run(conn: sqlite3.Connection, now: datetime) -> str:
    run_id = f"{TASK}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES (?,?,?, 'running')",
        (run_id, TASK, _iso(now)),
    )
    return run_id


def _end_run(conn: sqlite3.Connection, run_id: str, status: str, error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET ended = ?, status = ?, error = ? WHERE run_id = ?",
        (_iso(_now()), status, error, run_id),
    )


def _rank(job: dict[str, Any]) -> tuple[int, float]:
    """Sort key for 'top job': HIGH before MID, then higher rate_paid first."""
    rate = job.get("rate_paid")
    return TIER_ORDER.get(job.get("tier") or "", 9), -(rate if rate is not None else -1.0)


# ---------------------------------------------------------------- hook for Phase G


def judge_and_draft(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    config: Config | None = None,
    run_id: str | None = None,
) -> JudgeResult:
    """Steps 8 to 10 of SPEC 6.2 (thinking model judges, writing model drafts): see
    `app/llm/judging.py`. `candidates` are the new MID/HIGH jobs of this run."""
    return judging.judge_and_draft(
        conn, candidates, settings=settings, config=config, run_id=run_id
    )


# ---------------------------------------------------------------- connects


def _record_connects(
    conn: sqlite3.Connection,
    run_id: str,
    balance: int | None,
    top: dict[str, Any] | None,
    now: datetime,
) -> str | None:
    """Store the latest balance seen, and set or clear the low-Connects alert.

    Returns a note line (or None). Email for this alert comes in Phase G.
    """
    needed = top.get("connects_cost") if top else None
    if balance is not None:
        conn.execute(
            "INSERT INTO connects(balance, needed, seen_at, run_id) VALUES (?,?,?,?)",
            (balance, needed, _iso(now), run_id),
        )
    else:
        row = conn.execute("SELECT balance FROM connects ORDER BY id DESC LIMIT 1").fetchone()
        balance = row["balance"] if row else None
    if top is None:
        # No new job: compare against the best candidate still waiting.
        row = conn.execute(
            "SELECT title, connects_cost FROM jobs WHERE status = 'candidate' "
            "AND connects_cost IS NOT NULL "
            "ORDER BY CASE tier WHEN 'HIGH' THEN 0 ELSE 1 END, rate_paid DESC LIMIT 1"
        ).fetchone()
        if row:
            top = {"title": row["title"], "connects_cost": row["connects_cost"]}
            needed = row["connects_cost"]
    if balance is not None and needed is not None and balance < needed:
        message = (
            f"Connects balance is {balance}, below the {needed} needed for the top job"
            f" ({top.get('title') if top else '?'})."
        )
        conn.execute(
            "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (ALERT_CONNECTS_LOW, message, _iso(now)),
        )
        return f"Connects low: {message}"
    conn.execute("DELETE FROM alerts WHERE key = ?", (ALERT_CONNECTS_LOW,))
    return None


# ---------------------------------------------------------------- note


def _note_block(
    now_local: datetime,
    run_id: str,
    result: DailyResult,
    skills: Counter,
    top_n: int,
    balance: int | None,
    oddities: list[str],
    proposals: list[str] | None = None,
    metric_lines: list[str] | None = None,
    rank_lines: list[str] | None = None,
) -> str:
    tiers = ", ".join(f"{k} {v}" for k, v in sorted(result.tiers.items())) or "none"
    top = ", ".join(f"{s} ({n})" for s, n in skills.most_common(top_n)) or "none"
    lines = [
        f"## {now_local.strftime('%H:%M')} daily run {run_id}",
        f"- Status: {result.status}",
        f"- New jobs seen: {result.seen}",
        f"- Kept (MID/HIGH): {result.kept} ({tiers}); dropped: {result.dropped}",
        f"- Judged: {result.judged}; drafts written: {result.drafted}",
        f"- Top skills seen: {top}",
    ]
    if balance is not None:
        lines.append(f"- Connects balance: {balance}")
    if proposals:
        lines.append("- Sent proposals:")
        lines.extend(f"  - {p}" for p in proposals)
    if metric_lines:
        lines.append("- Metrics:")
        lines.extend(f"  - {m}" for m in metric_lines)
    if rank_lines:
        lines.append("- Keyword rank:")
        lines.extend(f"  - {r}" for r in rank_lines)
    if oddities:
        lines.append("- Oddities:")
        lines.extend(f"  - {o}" for o in oddities)
    return "\n".join(lines) + "\n\n"


def write_note(
    config: Config, now_local: datetime, block: str, conn: sqlite3.Connection | None = None
) -> None:
    """Append to memory/daily/YYYY-MM-DD.md (old notes/daily files are only read, see notes.py)."""
    day = now_local.strftime("%Y-%m-%d")
    if notes.append_note(config, day, block) and conn is not None:
        memory.index_note(conn, day, notes.read_note(config, day))


# ---------------------------------------------------------------- after-send review (plan R3)


def after_send_part(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any,
    run_id: str,
    now: datetime,
) -> list[str]:
    """Sync my sent proposals and review the new ones. Returns lines for the daily note.

    Never raises: an error here is logged and shown as an alert, and the daily run stays ok."""
    if not settings.get("review.after_send_enabled"):
        return []
    try:
        res = after_send.run_after_send(client, conn, config, settings, run_id=run_id, now=now)
    except Exception as e:
        log.exception("After-send review failed")
        message = f"The after-send proposal review failed: {type(e).__name__}: {e}"[:300]
        conn.execute(
            "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (after_send.ALERT_KEY, message, _iso(now)),
        )
        return [message]
    conn.execute("DELETE FROM alerts WHERE key = ?", (after_send.ALERT_KEY,))
    return res.lines()


# ---------------------------------------------------------------- metrics snapshot (plan X1)


def metrics_part(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any,
    now: datetime,
    out: dict[str, Any] | None = None,
) -> list[str]:
    """Store today's metrics row. Returns lines for the daily note. The row (with the pending
    invitations just read) is also put in `out["row"]` for the alerts step.

    Never raises: an error is logged and shown as the alert `metrics_failed`, the daily run
    stays ok. The bridge applies the Claude cap and the rate-limit stop; if they stop the
    snapshot it is skipped quietly (a note) and tried again at the next run."""
    if not settings.get("metrics.enabled"):
        return []
    try:
        row = metrics.snapshot(client, conn, settings, now, tz=_zone(config.tz))
    except (BudgetCapReached, ClaudeRateLimited) as e:
        return [f"Metrics not stored (Claude limit): {e}"[:300]]
    except Exception as e:
        log.exception("Metrics snapshot failed")
        message = f"The daily metrics snapshot failed: {type(e).__name__}: {e}"[:300]
        conn.execute(
            "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (metrics.ALERT_KEY, message, _iso(now)),
        )
        return [message]
    conn.execute("DELETE FROM alerts WHERE key = ?", (metrics.ALERT_KEY,))
    if out is not None:
        out["row"] = row
    jss = "unknown" if row["jss"] is None else f"{row['jss']:g}"
    return [
        f"Metrics {row['date']}: Connects {row['connects']}, JSS {jss}, "
        f"invites {row['invites_open']} open / {row['invites_new']} new, "
        f"proposals {row['proposals_sent']} sent / {row['proposals_viewed']} viewed / "
        f"{row['interviews']} interviews / {row['hires']} hires"
    ]


# ---------------------------------------------------------------- profile copy (plan O1)


def profile_versions_part(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any,
    now: datetime,
) -> list[str]:
    """Save a copy of my profile when it changed. Returns lines for the daily note.

    Never raises (a login problem is left to the other parts, it is only an alert here): an
    error is logged and shown as the alert `profile_snapshot_failed`, the daily run stays ok."""
    if not settings.get("profile_versions.daily"):
        return []
    try:
        res = profile_versions_mod.snapshot(client, conn, settings, config, now, reason="daily")
    except (BudgetCapReached, ClaudeRateLimited) as e:
        return [f"Profile copy not saved (Claude limit): {e}"[:300]]
    except Exception as e:
        log.exception("Profile snapshot failed")
        message = f"The daily profile copy failed: {type(e).__name__}: {e}"[:300]
        conn.execute(
            "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (profile_versions_mod.ALERT_KEY, message, _iso(now)),
        )
        return [message]
    conn.execute("DELETE FROM alerts WHERE key = ?", (profile_versions_mod.ALERT_KEY,))
    if res.saved:
        return [f"Profile copy saved (version {res.version_id})."]
    return ["Profile unchanged since the last copy."]


# ---------------------------------------------------------------- profile test judge (plan O3)


def profile_loop_part(
    conn: sqlite3.Connection, settings: Settings, config: Config, now: datetime
) -> list[str]:
    """Judge a profile test whose days are over. Never raises. Only queues an approval for a roll
    back; it writes nothing to Upwork."""
    try:
        return profile_loop.judge_due(conn, settings, config, now)
    except Exception as e:
        log.exception("Profile test judge failed")
        return [f"Profile test judge failed: {type(e).__name__}: {e}"[:300]]


# ---------------------------------------------------------------- keyword rank (plan X2)


def _rank_alert(conn: sqlite3.Connection, message: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
        (rank.ALERT_KEY, message[:300], _iso(now)),
    )


def rank_part(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any,
    now: datetime,
) -> list[str]:
    """Check the keyword rank once per local day. Returns lines for the daily note.

    Never raises: a problem is logged and shown as the alert `rank_failed`, the daily run stays
    ok. Not due (off, too early, already done today) = no calls and no lines."""
    tz = _zone(config.tz)
    if not rank.is_due(conn, settings, now, tz):
        return []
    # Rank has its own MCP call budget (rank.max_mcp_calls): it neither eats the budget of the
    # jobs part nor is starved by it. runs.mcp_calls still counts every call. Fake clients in
    # tests have no call_budget, so they just run without a separate limit.
    budget = getattr(client, "call_budget", None)
    try:
        with budget("rank.max_mcp_calls") if budget else nullcontext():
            # my profile key is needed here even when the metrics snapshot is off or has no JSS
            metrics.ensure_profile_key(client, settings)
            res = rank.check_ranks(client, conn, settings, now, tz=tz)
    except rank.RankSkipped as e:
        _rank_alert(conn, f"Keyword rank skipped: {e}", now)
        return [f"Rank skipped: {e}"[:300]]
    except (BudgetCapReached, ClaudeRateLimited) as e:
        return [f"Rank not checked (Claude limit): {e}"[:300]]
    except Exception as e:
        log.exception("Keyword rank check failed")
        message = f"The keyword rank check failed: {type(e).__name__}: {e}"
        _rank_alert(conn, message, now)
        return [message[:300]]
    if res.failed:
        names = ", ".join(res.failed)
        _rank_alert(conn, f"The keyword rank check could not search: {names}", now)
    else:
        conn.execute("DELETE FROM alerts WHERE key = ?", (rank.ALERT_KEY,))
    return res.lines()


# ---------------------------------------------------------------- alerts (plan X3)


def alerts_part(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    now: datetime,
    snapshot: dict[str, Any] | None,
) -> list[str]:
    """New invites, JSS drop, rank drop: emails plus the JSS guard. Reads the database and the
    snapshot only (no Upwork call). Never raises."""
    try:
        today = now.astimezone(_zone(config.tz)).date()
        return alerts.run_alerts(conn, settings, config, now, today, snapshot)
    except Exception as e:
        log.exception("Alerts failed")
        return [f"Alerts failed: {type(e).__name__}: {e}"[:300]]


# ---------------------------------------------------------------- the run


def ensure_talent_account(client: Any, settings: Settings) -> None:
    """If no freelancer account is saved, try list_accounts once (a single TALENT only)."""
    if settings.get("upwork.talent_org_uid"):
        return
    res = accounts.detect_and_save(client, settings)
    if res.status != "saved":
        extra = " Open the Upwork page and pick one." if res.status == "choose" else ""
        raise accounts.AccountError(res.message + extra)
    log.info("Freelancer account detected automatically")


def _email_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Job facts for the high_job email, from the saved row."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return {
        "id": row["job_id"],
        "title": row["title"],
        "tier": row["tier"],
        "tier_estimated": bool(row["tier_estimated"]),
        "rate_paid": row["rate_paid"],
        "client_spend": row["client_spend"],
        "flags": json.loads(row["red_flags"] or "[]"),
    }


def _process(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any,
    run_id: str,
    now: datetime,
    result: DailyResult,
    skills: Counter,
    oddities: list[str],
) -> int | None:
    """Steps 2 to 7. Fills `result`. Returns the latest Connects balance seen (or None)."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    week = J.iso_week(now)

    if settings.get("job_alerts.enabled"):
        # Instant alerts do the searching. Only older pending jobs are retried below.
        new_jobs = []
        oddities.append("job search skipped: instant alerts are on")
    else:
        new_jobs = J.fetch_candidates(client, settings, conn)  # steps 2, 3
    result.seen = len(new_jobs)

    survivors: list[dict[str, Any]] = []
    for job in new_jobs:  # step 4
        skills.update(set(job.get("skills") or []))
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

    kept: list[dict[str, Any]] = []
    balance: int | None = None
    batch = max(1, int(settings.get("claude.fetch_batch_size")))

    def fetched_details():
        """Step 5: `find_jobs get` in batches. Yields (search job, detail) as each batch returns,
        so a later failure does not lose earlier jobs."""
        for start in range(0, len(survivors), batch):
            chunk = survivors[start : start + batch]
            outcomes = J.fetch_details(client, [j["id"] for j in chunk], org_uid)
            for pos, (job, outcome) in enumerate(zip(chunk, outcomes, strict=True)):
                if isinstance(outcome, CallBudgetExceeded):
                    left = len(survivors) - start - pos
                    oddities.append(
                        f"MCP call limit reached: {left} job(s) left for the next run"
                        " (not saved yet)."
                    )
                    return
                if isinstance(outcome, (UpworkToolError, J.JobsError)):
                    oddities.append(f"tool error on job {job['id']}: {outcome}"[:300])
                    continue
                yield job, outcome

    for job, detail in fetched_details():  # step 6
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
        if t not in TIER_ORDER:  # LOW
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
        result.tiers[t] = result.tiers.get(t, 0) + 1
    result.kept = len(kept)

    kept.sort(key=_rank)
    warning = _record_connects(conn, run_id, balance, kept[0] if kept else None, now)
    if warning:
        oddities.append(warning)
        mail.notify_connects_low(
            conn,
            config,
            warning.removeprefix("Connects low: "),
            now.date().isoformat(),
            settings=settings,
            now=now,
        )

    # Steps 8 to 10. Also retries jobs an earlier run could not finish (cap, limit, no USER.md).
    judged = judge_and_draft(conn, kept, settings=settings, config=config, run_id=run_id)
    oddities.extend(judged.notes)
    result.judged, result.drafted = len(judged.judged), judged.drafted
    fit_min = int(settings.get("pay.fit_min"))
    for item in judged.judged:  # emails are not Upwork writes: sent in dry-run too
        if item["tier"] == "HIGH" and item["fit_score"] >= fit_min:
            mail.notify_high_job(
                conn,
                config,
                _email_job(conn, item["job_id"]),
                judgement=item,
                settings=settings,
                now=now,
            )
    # Round 3: an excellent job starts an auto-send countdown (nothing is sent here: a preview,
    # a countdown row and an email). It does nothing while dry-run or the kill switch is on.
    try:
        started = auto_send_flow.start_countdowns(
            conn, settings, config, client, [i["job_id"] for i in judged.judged], now=now
        )
        if started:
            oddities.append(f"auto-send: {len(started)} countdown(s) started")
    except ClaudeCancelled:
        raise
    except Exception:
        log.exception("Auto-send could not start")
        oddities.append("auto-send: could not start (see the log)")
    return balance


def _send_failure_emails(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    run_id: str,
    result: DailyResult,
    now: datetime,
) -> None:
    """auth_expired right away; run_failed when the last N runs all failed (SPEC 11)."""
    if result.status == "auth_expired":
        mail.notify_auth_expired(conn, config, run_id, settings=settings, now=now)
    elif result.status == "error":
        n = int(settings.get("alerts.run_failed_threshold"))
        last = conn.execute(
            "SELECT status FROM runs WHERE task = ? ORDER BY started DESC, rowid DESC LIMIT ?",
            (TASK, n),
        ).fetchall()
        if len(last) == n and all(r["status"] == "error" for r in last):
            mail.notify_run_failed(
                conn,
                config,
                TASK,
                run_id,
                n,
                result.error or "unknown error",
                settings=settings,
                now=now,
            )


def run_daily(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any = None,
    *,
    now: datetime | None = None,
) -> DailyResult:
    """Run the daily cycle once. `client` is for tests (any object with `call_many`)."""
    now = now or _now()
    run_id = _start_run(conn, now)
    result = DailyResult(run_id=run_id, status="running")

    if settings.get("general.kill_switch"):
        result.status = "skipped"
        result.error = "kill switch is on"
        _end_run(conn, run_id, "skipped", result.error)
        log.warning("Kill switch is on: daily run skipped")
        return result

    skills: Counter = Counter()
    oddities: list[str] = []
    proposal_lines: list[str] = []
    metric_lines: list[str] = []
    rank_lines: list[str] = []
    balance: int | None = None
    owns_client = client is None
    try:
        if client is None:
            client = ClaudeUpworkBridge(config, settings, conn, run_id)
        ensure_talent_account(client, settings)
        balance = _process(conn, settings, config, client, run_id, now, result, skills, oddities)
        proposal_lines = after_send_part(conn, settings, config, client, run_id, now)
        snap: dict[str, Any] = {}
        metric_lines = metrics_part(conn, settings, config, client, now, snap)
        metric_lines += profile_versions_part(conn, settings, config, client, now)
        metric_lines += profile_loop_part(conn, settings, config, now)
        rank_lines = rank_part(conn, settings, config, client, now)
        oddities.extend(alerts_part(conn, settings, config, now, snap.get("row")))
        result.status = "ok"
    except UpworkAuthError as e:
        result.status, result.error = "auth_expired", str(e)
        oddities.append(f"Upwork login problem: {e}")
    except Exception as e:  # any other failure ends the run as "error"
        log.exception("Daily run failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:1000]
        oddities.append(f"run failed: {result.error}")
    finally:
        if owns_client and client is not None:
            client.close()

    _end_run(conn, run_id, result.status, result.error)
    _send_failure_emails(conn, settings, config, run_id, result, now)
    now_local = now.astimezone(_zone(config.tz))
    top_n = int(settings.get("daily.top_skills"))
    write_note(
        config,
        now_local,
        _note_block(
            now_local,
            run_id,
            result,
            skills,
            top_n,
            balance,
            oddities,
            proposal_lines,
            metric_lines,
            rank_lines,
        ),
        conn,
    )
    log.info(
        "daily %s: seen=%d kept=%d dropped=%d",
        result.status,
        result.seen,
        result.kept,
        result.dropped,
    )
    return result


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # panel button and scheduler share this lock
            conn = init_db(config.db_path)
            result = run_daily(conn, Settings(conn), config)
    except ClaudeCancelled:  # the user pressed Cancel: stop cleanly, the lock is released
        log.warning("%s was cancelled by the user", TASK)
        return 0
    except TaskBusy:
        log.warning("Daily task is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
