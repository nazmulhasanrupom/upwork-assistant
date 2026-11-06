"""Daily digest email (SPEC 9, 11; plan G3). Run: python -m app.tasks.digest

One email about today (in the panel time zone): new MID/HIGH jobs with fit score, why apply and
a link, the counts (seen, kept, drafted), the Connects balance, and today's Claude cost.

No Upwork or Claude call is made: it only reads the database. It is sent through
`mail.send_event("digest", ref_id=<date>)`, so it goes out once per day (dedupe in the `emails`
table). The event switch is `email.events.digest`. With `email.digest_skip_empty` (default on),
a day with no kept job sends nothing. Emails are not Upwork writes, so they go out in dry-run too.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app import costs, mail
from app.config import Config, load_config
from app.db import init_db
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("digest")

TASK = "digest"
EVENT = "digest"  # the key under [email.events] in settings
UPWORK_PREFIX = "https://www.upwork.com/"
WHY_LINES = 3  # why-apply lines shown per job


@dataclass
class DigestResult:
    run_id: str
    status: str  # ok | skipped | error
    day: str = ""
    seen: int = 0
    kept: int = 0
    drafted: int = 0
    sent: str | None = None  # the mail.send_event result, if an email was tried
    error: str | None = None
    jobs: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
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
        (_iso(datetime.now(UTC)), status, error, run_id),
    )


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _lines(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").splitlines() if x.strip()]


# ---------------------------------------------------------------- the day's numbers


def collect(
    conn: sqlite3.Connection, config: Config, settings: Settings, now: datetime
) -> dict[str, Any]:
    """Everything the email says, read from the database. `now` decides which day it is."""
    tz = _zone(config.tz)
    today = now.astimezone(tz).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    since = _iso(start)

    seen = conn.execute("SELECT COUNT(*) FROM jobs WHERE first_seen >= ?", (since,)).fetchone()[0]
    rows = conn.execute(
        "SELECT j.job_id, j.title, j.tier, j.tier_estimated, j.rate_paid, j.url, "
        "g.fit_score, g.why_apply, g.plan AS plan_json "
        "FROM jobs j LEFT JOIN judgements g ON g.job_id = j.job_id "
        "WHERE j.first_seen >= ? AND j.tier IN ('HIGH','MID') AND j.status != 'dropped' "
        "ORDER BY g.fit_score IS NULL, g.fit_score DESC, "
        "CASE j.tier WHEN 'HIGH' THEN 0 ELSE 1 END, j.rate_paid DESC",
        (since,),
    ).fetchall()
    jobs = []
    for r in rows:
        plan = _json(r["plan_json"], {})
        plan = plan if isinstance(plan, dict) else {}
        jobs.append(
            {
                "id": r["job_id"],
                "title": r["title"] or r["job_id"],
                "tier": r["tier"],
                "tier_estimated": bool(r["tier_estimated"]),
                "rate_paid": r["rate_paid"],
                "url": r["url"],
                "fit": r["fit_score"],
                "why": _lines(r["why_apply"]),
                "can_afford": plan.get("can_afford"),
                "connects_cost": plan.get("connects_cost"),
                "connects_balance": plan.get("connects_balance"),
            }
        )
    drafted = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE created_at >= ?", (since,)
    ).fetchone()[0]
    connects = conn.execute("SELECT balance FROM connects ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "day": today.isoformat(),
        "seen": seen,
        "kept": len(jobs),
        "tiers": {t: sum(1 for j in jobs if j["tier"] == t) for t in ("HIGH", "MID")},
        "drafted": drafted,
        "jobs": jobs,
        "connects": None if connects is None else int(connects["balance"]),
        "connects_low": bool(
            conn.execute("SELECT 1 FROM alerts WHERE key = 'connects_low'").fetchone()
        ),
        "costs": costs.cost_summary(conn, config, settings, tz, now),
    }


# ---------------------------------------------------------------- the email


def _rate(v: Any) -> str:
    return f"${v:,.2f}/hr paid" if isinstance(v, (int, float)) else ""


def build(data: dict[str, Any], config: Config, max_jobs: int) -> tuple[str, str, str]:
    """(subject, plain text, HTML)."""
    base = config.public_base_url.rstrip("/")
    jobs = data["jobs"]
    shown = jobs[: max(0, max_jobs)]
    tiers = data["tiers"]
    c = data["costs"]

    summary = (
        f"New jobs seen: {data['seen']}. Kept (MID/HIGH): {data['kept']}"
        f" (HIGH {tiers['HIGH']}, MID {tiers['MID']}). Drafts written: {data['drafted']}."
    )
    lines = [summary]
    if not jobs:
        lines.append("No new MID or HIGH job today.")
    elif len(jobs) > len(shown):
        lines.append(f"Showing the best {len(shown)} of {len(jobs)} by fit score.")

    items = []
    for j in shown:
        meta = [j["tier"] + (" (estimated)" if j["tier_estimated"] else "")]
        meta.append(f"fit {j['fit']}" if j["fit"] is not None else "not judged yet")
        if _rate(j["rate_paid"]):
            meta.append(_rate(j["rate_paid"]))
        body = [f"Why apply: {w}" for w in j["why"][:WHY_LINES]]
        if j["can_afford"] is False:
            body.append(
                f"Not enough Connects yet: it costs {j['connects_cost']}, you have "
                f"{j['connects_balance']}."
            )
        links = []
        if base:
            links.append(("Open in panel", f"{base}/jobs/{j['id']}"))
        if isinstance(j["url"], str) and j["url"].startswith(UPWORK_PREFIX):
            links.append(("Open on Upwork", j["url"]))
        items.append({"title": j["title"], "meta": ", ".join(meta), "lines": body, "links": links})

    facts = [
        ("Connects", "unknown" if data["connects"] is None else str(data["connects"])),
        ("Claude cost today", f"${c['today']:.2f}"),
        ("Claude cost this month", f"${c['month']:.2f} of ${c['cap']:.2f}"),
    ]
    if data["connects_low"]:
        lines.append("Connects are low for the top job.")
    text, html = mail.build_email(
        f"Daily digest for {data['day']}",
        lines,
        facts=facts,
        items=items,
        link_path="/jobs",
        base_url=config.public_base_url,
        button_label="Open jobs in the panel",
    )
    subject = f"Daily digest {data['day']}: {data['kept']} new job(s), {data['drafted']} draft(s)"
    return subject, text, html


# ---------------------------------------------------------------- the run

SEND_NOTES = {
    mail.OFF: "Not sent: the digest email is switched off in the Email settings.",
    mail.DUPLICATE: "Not sent: today's digest was already sent.",
    mail.NO_TRANSPORT: "Not sent: connect Gmail or set an SMTP server on the Email page.",
}


def run_digest(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    *,
    now: datetime | None = None,
) -> DigestResult:
    now = now or datetime.now(UTC)
    run_id = _start_run(conn, now)
    result = DigestResult(run_id=run_id, status="running")

    if settings.get("general.kill_switch"):
        result.status, result.error = "skipped", "kill switch is on"
        _end_run(conn, run_id, result.status, result.error)
        log.warning("Kill switch is on: digest skipped")
        return result

    try:
        data = collect(conn, config, settings, now)
        result.day, result.seen = data["day"], data["seen"]
        result.kept, result.drafted, result.jobs = data["kept"], data["drafted"], data["jobs"]
        if not data["kept"] and settings.get("email.digest_skip_empty"):
            result.status, result.error = "skipped", "Nothing new today, so no digest was sent."
        else:
            subject, text, html = build(data, config, int(settings.get("email.digest_max_jobs")))
            result.sent = mail.send_event(
                conn,
                EVENT,
                data["day"],
                subject,
                text,
                html,
                config=config,
                settings=settings,
                now=now,
            )
            if result.sent == mail.SENT:
                result.status = "ok"
            elif result.sent == mail.FAILED:
                result.status = "error"
                result.error = "The digest email could not be sent. See the alert on the dashboard."
            else:
                result.status, result.error = "skipped", SEND_NOTES.get(result.sent, result.sent)
    except Exception as e:  # any other failure ends the run as "error"
        log.exception("Digest failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:1000]

    _end_run(conn, run_id, result.status, result.error)
    log.info(
        "digest %s: seen=%d kept=%d drafted=%d sent=%s",
        result.status,
        result.seen,
        result.kept,
        result.drafted,
        result.sent,
    )
    return result


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the Run button and the scheduler share this lock
            conn = init_db(config.db_path)
            result = run_digest(conn, Settings(conn), config)
    except TaskBusy:
        log.warning("Digest task is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
