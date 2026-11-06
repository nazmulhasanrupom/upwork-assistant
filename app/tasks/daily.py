"""Daily cycle, steps 1 to 7 of SPEC 6.2 (no Claude yet). Run: python -m app.tasks.daily

Flow: find jobs, drop the ones already seen, pre-filter on search data, `find_jobs get` for the
survivors, tier + red flags, save to the DB, record Connects, write the daily note.
Steps 8 to 10 (judge and draft with Claude) are a hook: `judge_and_draft`.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Config, load_config
from app.db import init_db
from app.filters.flags import flags as red_flags
from app.filters.tiers import prefilter, tier
from app.settings import Settings
from app.upwork import accounts
from app.upwork import jobs as J
from app.upwork.client import (
    CallBudgetExceeded,
    UpworkAuthError,
    UpworkMCP,
    UpworkToolError,
)
from app.upwork.oauth import UpworkOAuth

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


def judge_and_draft(conn: sqlite3.Connection, candidates: list[dict[str, Any]]) -> None:
    """Steps 8 to 10 of SPEC 6.2 (Opus judges, Sonnet drafts). Not built yet: does nothing.

    `candidates` are the new MID/HIGH jobs of this run: the cut-down job dict plus the keys
    `tier`, `tier_estimated`, `rate_paid`, `flags`, `status`.
    """
    return None


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
) -> str:
    tiers = ", ".join(f"{k} {v}" for k, v in sorted(result.tiers.items())) or "none"
    top = ", ".join(f"{s} ({n})" for s, n in skills.most_common(top_n)) or "none"
    lines = [
        f"## {now_local.strftime('%H:%M')} daily run {run_id}",
        f"- Status: {result.status}",
        f"- New jobs seen: {result.seen}",
        f"- Kept (MID/HIGH): {result.kept} ({tiers}); dropped: {result.dropped}",
        f"- Top skills seen: {top}",
    ]
    if balance is not None:
        lines.append(f"- Connects balance: {balance}")
    if oddities:
        lines.append("- Oddities:")
        lines.extend(f"  - {o}" for o in oddities)
    return "\n".join(lines) + "\n\n"


def write_note(config: Config, now_local: datetime, block: str) -> None:
    path = config.data_dir / "notes" / "daily" / f"{now_local.strftime('%Y-%m-%d')}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
    except OSError:
        log.exception("Could not write the daily note")


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


def _process(
    conn: sqlite3.Connection,
    settings: Settings,
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

    new_jobs = J.fetch_candidates(client, settings, conn)  # steps 2, 3
    result.seen = len(new_jobs)

    survivors: list[dict[str, Any]] = []
    for job in new_jobs:  # step 4
        skills.update(set(job.get("skills") or []))
        J.save_skills(conn, job["id"], job.get("skills") or [], week)
        keep, reason = prefilter(job, settings)
        if keep:
            survivors.append(job)
        else:
            J.save_job(conn, job, status="dropped", drop_reason=reason)
            result.dropped += 1

    kept: list[dict[str, Any]] = []
    balance: int | None = None
    for i, job in enumerate(survivors):  # steps 5, 6
        try:
            detail = J.fetch_detail(client, job["id"], org_uid)
        except CallBudgetExceeded:
            left = len(survivors) - i
            oddities.append(
                f"MCP call limit reached: {left} job(s) left for the next run (not saved yet)."
            )
            break
        except (UpworkToolError, J.JobsError) as e:
            oddities.append(f"tool error on job {job['id']}: {e}"[:300])
            continue
        full = J.apply_detail(job, detail, settings)
        if full.get("connects_balance") is not None:
            balance = full["connects_balance"]
        keep, reason = prefilter(full, settings)
        if not keep:
            J.save_job(conn, full, status="dropped", drop_reason=reason)
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

    if kept:  # step 7: nothing new and good means no Claude
        judge_and_draft(conn, kept)
    return balance


def run_daily(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    client: Any = None,
    *,
    now: datetime | None = None,
) -> DailyResult:
    """Run the daily cycle once. `client` is for tests (a fake MCP client)."""
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
    balance: int | None = None
    owns_client = client is None
    try:
        if client is None:
            client = UpworkMCP(settings, UpworkOAuth(config, settings, conn), conn, run_id)
        ensure_talent_account(client, settings)
        balance = _process(conn, settings, client, run_id, now, result, skills, oddities)
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
    now_local = now.astimezone(_zone(config.tz))
    top_n = int(settings.get("daily.top_skills"))
    write_note(
        config,
        now_local,
        _note_block(now_local, run_id, result, skills, top_n, balance, oddities),
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
    conn = init_db(config.db_path)
    result = run_daily(conn, Settings(conn), config)
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
