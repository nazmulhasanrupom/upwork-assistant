"""Auto-send eligibility (round 3, A1). Pure decision: no Upwork, no Claude, no sending.

The owner approved sending a proposal by itself after a countdown (A2 builds the countdown and
the send). This module only answers "may this job start a countdown?" with the list of reasons
when it may not. Every number is read from the `[auto_send]` settings.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.settings import Settings

# Statuses of an `auto_sends` row (A2) that count against the daily cap.
CAP_STATUSES = ("waiting", "approved", "sending", "sent")
ACTIVE = ("waiting", "approved", "sending")  # a row in one of these blocks a second one per job


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _details(job: Any) -> Mapping[str, Any]:
    raw = _get(job, "details")
    if isinstance(raw, Mapping):
        return raw
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def started_today(
    conn: sqlite3.Connection, now: datetime | None = None, exclude_id: int | None = None
) -> int:
    """Auto-sends created since 00:00 UTC that still count (0 when the table does not exist)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'auto_sends'"
    ).fetchone()
    if not exists:
        return 0
    day = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT00:00:00")
    marks = ",".join("?" for _ in CAP_STATUSES)
    row = conn.execute(
        f"SELECT COUNT(*) FROM auto_sends WHERE created >= ? AND status IN ({marks}) AND id != ?",
        (day, *CAP_STATUSES, exclude_id if exclude_id is not None else -1),
    ).fetchone()
    return int(row[0])


def eligible(
    conn: sqlite3.Connection,
    job: Any,
    judgement: Any,
    draft: Any,
    settings: Settings,
    connects_balance: int | None,
    now: datetime | None = None,
    exclude_id: int | None = None,
) -> tuple[bool, list[str]]:
    """(True, []) when the job may start an auto-send countdown, else (False, reasons).

    All checks run, so the reasons list everything that is wrong. Missing data never passes.
    `exclude_id` is the countdown being checked again at its deadline: it does not count itself
    against the daily limit.
    """
    g = settings.get
    why: list[str] = []
    if g("general.dry_run"):
        why.append("dry-run is on")
    if g("general.kill_switch"):
        why.append("the kill switch is on")
    if not g("auto_send.enabled"):
        why.append("auto-send is off")

    fit = _number(_get(judgement, "fit_score"))
    min_fit = float(g("auto_send.min_fit"))
    if fit is None or fit < min_fit:
        why.append(f"fit {'unknown' if fit is None else int(fit)} is below {int(min_fit)}")
    if _get(judgement, "skills_match_all") not in (1, True):
        why.append("the judge did not confirm that your skills match all the job's skills")
    if _get(judgement, "win_chance") != "high":
        why.append(f"win chance is {_get(judgement, 'win_chance', 'unknown')}, not high")
    if _get(judgement, "client_quality") != "good":
        why.append(f"client quality is {_get(judgement, 'client_quality', 'unknown')}, not good")

    details = _details(job)
    if details.get("payment_verified") is not True:
        why.append("client payment is not verified")
    rating = _number(_get(job, "client_rating"))
    min_rating = float(g("auto_send.min_client_rating"))
    if rating is None or rating < min_rating:
        why.append(f"client rating {'unknown' if rating is None else rating} is below {min_rating}")
    spend = _number(_get(job, "client_spend"))
    min_spend = float(g("auto_send.min_client_spend"))
    if spend is None or spend < min_spend:
        shown = "unknown" if spend is None else int(spend)
        why.append(f"client spend {shown} is below {min_spend:g}")
    label = _get(job, "activity_label")
    allowed = [str(x).upper() for x in g("auto_send.allowed_labels") or []]
    if str(label or "").upper() not in allowed:
        why.append(f"activity label {label or 'unknown'} is not allowed")
    if details.get("applied") is True or _get(job, "status") in ("sent", "skipped", "dropped"):
        why.append("already applied, skipped or dropped")

    cost = _number(_get(job, "connects_cost"))
    keep = int(g("auto_send.keep_connects"))
    if cost is None or connects_balance is None:
        why.append("Connects cost or balance is unknown")
    elif connects_balance < cost + keep:
        why.append(f"Connects {connects_balance} are below cost {int(cost)} plus {keep} to keep")

    if _get(draft, "rules_ok") != 1:
        why.append("the draft is missing or breaks the proposal rules")
    cap = int(g("auto_send.max_per_day"))
    if started_today(conn, now, exclude_id) >= cap:
        why.append(f"the daily limit of {cap} auto-sends is reached")
    return (not why, why)


# ---------------------------------------------------------------- the countdown rows (A2)
#
# Every status change is ONE `UPDATE ... WHERE id = ? AND status = '<expected>'` and the caller
# looks at the row count. Whoever gets 1 owns that step; whoever gets 0 lost the race (a Stop
# that lands first wins, a second tick finds nothing to claim).

REASON_MAX = 300


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_time(text: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def get(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM auto_sends WHERE id = ?", (row_id,)).fetchone()


def for_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """The newest countdown of the job (any status), or None."""
    return conn.execute(
        "SELECT * FROM auto_sends WHERE job_id = ? ORDER BY id DESC LIMIT 1", (str(job_id),)
    ).fetchone()


def waiting_for_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM auto_sends WHERE job_id = ? AND status = 'waiting' LIMIT 1", (str(job_id),)
    ).fetchone()


def create(
    conn: sqlite3.Connection,
    settings: Settings,
    job_id: str,
    preview_row_id: int,
    now: datetime | None = None,
) -> int | None:
    """Start a countdown. None when the job already has a countdown (any status): a job gets one
    chance, so a Stop is not undone by the next run."""
    now = now or datetime.now(UTC)
    if for_job(conn, job_id) is not None:
        return None
    deadline = now + timedelta(minutes=float(settings.get("auto_send.wait_minutes")))
    try:
        cur = conn.execute(
            "INSERT INTO auto_sends(job_id, preview_id, created, deadline, status) "
            "VALUES (?, ?, ?, ?, 'waiting')",
            (str(job_id), preview_row_id, _iso(now), _iso(deadline)),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid or 0)


def _move(
    conn: sqlite3.Connection,
    row_id: int,
    from_status: str,
    to_status: str,
    *,
    decided_by: str | None = None,
    reason: str = "",
    error: str | None = None,
    sent: bool = False,
    click_id: str | None = None,
    extra_where: str = "",
    args: tuple = (),
) -> bool:
    now = _iso(datetime.now(UTC))
    cur = conn.execute(
        "UPDATE auto_sends SET status = ?, decided_by = COALESCE(?, decided_by), "
        "reason = CASE WHEN ? != '' THEN ? ELSE reason END, decided_at = COALESCE(decided_at, ?), "
        "error = COALESCE(?, error), sent_at = CASE WHEN ? THEN ? ELSE sent_at END, "
        "click_id = COALESCE(?, click_id) "
        f"WHERE id = ? AND status = ?{extra_where}",
        (
            to_status,
            decided_by,
            reason[:REASON_MAX],
            reason[:REASON_MAX],
            now,
            None if error is None else error[:REASON_MAX],
            int(sent),
            now,
            click_id,
            row_id,
            from_status,
            *args,
        ),
    )
    return cur.rowcount == 1


def stop(conn: sqlite3.Connection, row_id: int, reason: str = "Stopped by you") -> bool:
    """The Stop click. Only a waiting countdown can be stopped; False means too late."""
    return _move(conn, row_id, "waiting", "stopped", decided_by="user", reason=reason)


def approve(conn: sqlite3.Connection, row_id: int) -> bool:
    """The Approve click, step 1: waiting -> approved (by the user)."""
    return _move(conn, row_id, "waiting", "approved", decided_by="user", reason="Approved by you")


def begin_user_send(conn: sqlite3.Connection, row_id: int) -> bool:
    return _move(conn, row_id, "approved", "sending")


def claim_due(conn: sqlite3.Connection, row_id: int, now: datetime | None = None) -> bool:
    """The deadline claim: waiting -> sending, only after the deadline, by the system."""
    now = now or datetime.now(UTC)
    return _move(
        conn,
        row_id,
        "waiting",
        "sending",
        decided_by="system",
        reason="Countdown ended with no Stop",
        extra_where=" AND deadline <= ?",
        args=(_iso(now),),
    )


def cancel(conn: sqlite3.Connection, row_id: int, reason: str, *, from_status: str) -> bool:
    return _move(conn, row_id, from_status, "cancelled", reason=reason)


def cancel_waiting_for_job(conn: sqlite3.Connection, job_id: str, reason: str) -> bool:
    """The owner took over (edited the draft, prepared by hand, skipped): drop the countdown."""
    row = waiting_for_job(conn, job_id)
    return row is not None and cancel(conn, row["id"], reason, from_status="waiting")


def finish_sent(conn: sqlite3.Connection, row_id: int, click_id: str | None) -> bool:
    return _move(conn, row_id, "sending", "sent", sent=True, click_id=click_id)


def finish_failed(conn: sqlite3.Connection, row_id: int, error: str) -> bool:
    return _move(conn, row_id, "sending", "failed", reason="Send failed", error=error)


def due_rows(conn: sqlite3.Connection, now: datetime | None = None) -> list[sqlite3.Row]:
    now = now or datetime.now(UTC)
    return conn.execute(
        "SELECT * FROM auto_sends WHERE status = 'waiting' AND deadline <= ? ORDER BY deadline, id",
        (_iso(now),),
    ).fetchall()


def active_preview_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT preview_id FROM auto_sends WHERE status IN ('waiting', 'approved', 'sending') "
        "AND preview_id IS NOT NULL"
    ).fetchall()
    return {int(r[0]) for r in rows}


def waiting_cards(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """Waiting countdowns for the To do page, the job page and the top bar, soonest first."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT a.*, j.title AS job_title, p.request_json, p.connects_cost AS cost, "
        "p.connects_balance AS balance FROM auto_sends a "
        "LEFT JOIN jobs j ON j.job_id = a.job_id LEFT JOIN previews p ON p.id = a.preview_id "
        "WHERE a.status = 'waiting' ORDER BY a.deadline, a.id"
    ).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r["request_json"] or "{}").get("params", {})
        except (ValueError, AttributeError):
            params = {}
        deadline = parse_time(r["deadline"]) or now
        left = max(0, int((deadline - now).total_seconds()))
        out.append(
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "title": r["job_title"] or f"Job {r['job_id']}",
                "link": f"/jobs/{r['job_id']}",
                "created": r["created"],
                "created_at": r["created"],
                "deadline": r["deadline"],
                "seconds_left": left,
                "timer": clock(left),
                "cover_letter": params.get("cover_letter", "") if isinstance(params, dict) else "",
                "connects_cost": r["cost"],
                "connects_balance": r["balance"],
            }
        )
    return out


def clock(seconds: int) -> str:
    """mm:ss (h:mm:ss from an hour up)."""
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
