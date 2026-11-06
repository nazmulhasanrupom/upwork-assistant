"""Alerts (plan X3, SPEC2 4.D): new invites, JSS drop, keyword rank drop.

Runs once per daily run, right after the metrics snapshot and the rank check. It only reads the
database (`metrics_daily`, `rank_daily`, `proposals`) and the invitations the snapshot just read;
it makes no Upwork call. Emails go through `app/mail.py` (`send_event`: on/off switch, dedupe in
the `emails` table, never raises). Emails are not Upwork writes, so they go out in dry-run too.

- Invites: a new pending invitation is stored in `invites` and mailed at once (event `invite`).
  Still not answered after `alerts.invite_reminder_hours`: ONE more email (`invite_reminder`).
  Answered = no longer pending in a complete dashboard read, or I sent a proposal for that job.
- JSS drop: today's JSS is lower than the last known one by `alerts.jss_drop_points` or more ->
  email (`jss_drop`) and `safety.jss_guard` is set. The guard is NEVER cleared here.
- Rank drop: the 7-day average place of a keyword is worse than the 7 days before by
  `alerts.rank_drop_positions` or more on `alerts.rank_drop_days` days in a row -> email
  (`rank_drop`), once per keyword per week.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import mail, rank
from app.config import Config
from app.settings import Settings
from app.upwork.jobs import CLOSE_TAG, OPEN_TAG

log = logging.getLogger("alerts")

GUARD_KEY = "safety.jss_guard"
GUARD_NOTE_KEY = "safety.jss_guard_note"
_DONE = (mail.SENT, mail.DUPLICATE)  # the email is out (now or before)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _plain(text: Any) -> str:
    return str(text or "").replace(OPEN_TAG, "").replace(CLOSE_TAG, "").strip()


def jss_guard_on(settings: Settings) -> bool:
    """True while the JSS guard is set. Phase O must check this before any profile test."""
    return bool(settings.get(GUARD_KEY))


# ---------------------------------------------------------------- invites


def process_invites(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    invites: list[dict[str, str]],
    complete: bool,
    now: datetime,
) -> list[str]:
    """Store the pending invitations, mail the new ones, mark answered ones, send the one
    reminder. `invites` = pending ones read now ({id, job_id, title}); `complete` = the read
    shows all of them (a missing one is then known to be answered)."""
    if not settings.get("alerts.invites_enabled"):
        return []
    lines: list[str] = []
    pending_ids = {i["id"] for i in invites}

    for inv in invites:
        row = conn.execute("SELECT * FROM invites WHERE invite_id = ?", (inv["id"],)).fetchone()
        title = _plain(inv.get("title"))[:300]
        if row is None:
            conn.execute(
                "INSERT INTO invites(invite_id, job_id, job_title, first_seen, status) "
                "VALUES (?,?,?,?, 'pending')",
                (inv["id"], inv.get("job_id") or None, title or None, _iso(now)),
            )
            lines.append(f"New invite: {title or inv['id']}")
        elif row["answered_at"]:  # it is pending again: wait for an answer again
            conn.execute(
                "UPDATE invites SET answered_at = NULL, status = 'pending' WHERE invite_id = ?",
                (inv["id"],),
            )

    # answered: gone from a complete read, or a proposal of mine exists for that job
    for row in conn.execute("SELECT * FROM invites WHERE answered_at IS NULL").fetchall():
        gone = complete and row["invite_id"] not in pending_ids
        proposed = bool(row["job_id"]) and (
            conn.execute(
                "SELECT 1 FROM proposals WHERE job_id = ? LIMIT 1", (row["job_id"],)
            ).fetchone()
            is not None
        )
        if gone or proposed:
            conn.execute(
                "UPDATE invites SET answered_at = ?, status = 'answered' WHERE invite_id = ?",
                (_iso(now), row["invite_id"]),
            )

    hours = float(settings.get("alerts.invite_reminder_hours"))
    for row in conn.execute(
        "SELECT * FROM invites WHERE answered_at IS NULL ORDER BY first_seen"
    ).fetchall():
        invite = {"id": row["invite_id"], "job_id": row["job_id"], "title": row["job_title"]}
        kw = {"settings": settings, "now": now}
        if not row["alerted_at"]:
            if mail.notify_invite(conn, config, invite, **kw) in _DONE:
                conn.execute(
                    "UPDATE invites SET alerted_at = ? WHERE invite_id = ?",
                    (_iso(now), row["invite_id"]),
                )
        seen = _parse(row["first_seen"])
        if (
            not row["second_alert_at"]
            and seen is not None
            and now - seen >= timedelta(hours=hours)
            and mail.notify_invite(conn, config, invite, reminder=True, hours=hours, **kw) in _DONE
        ):
            conn.execute(
                "UPDATE invites SET second_alert_at = ? WHERE invite_id = ?",
                (_iso(now), row["invite_id"]),
            )
            lines.append(f"Invite reminder: {_plain(row['job_title']) or row['invite_id']}")
    return lines


# ---------------------------------------------------------------- JSS drop


def check_jss_drop(
    conn: sqlite3.Connection, settings: Settings, config: Config, today: date, now: datetime
) -> list[str]:
    """Today's JSS against the last known one from an earlier day. A drop of at least
    `alerts.jss_drop_points` sets the JSS guard and sends the email. Nothing here clears it."""
    if not settings.get("alerts.jss_drop_enabled"):
        return []
    row = conn.execute(
        "SELECT jss FROM metrics_daily WHERE date = ?", (today.isoformat(),)
    ).fetchone()
    if row is None or row["jss"] is None:
        return []
    before = conn.execute(
        "SELECT jss FROM metrics_daily WHERE date < ? AND jss IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (today.isoformat(),),
    ).fetchone()
    if before is None:
        return []
    drop = float(before["jss"]) - float(row["jss"])
    if drop < float(settings.get("alerts.jss_drop_points")) - 1e-9:
        return []
    note = f"{today.isoformat()}: JSS fell from {before['jss']:g} to {row['jss']:g}"
    already_today = str(settings.get(GUARD_NOTE_KEY)).startswith(today.isoformat())
    if not settings.get(GUARD_KEY) and not already_today:  # the owner may have cleared it today
        settings.set(GUARD_KEY, True)
        settings.set(GUARD_NOTE_KEY, note)
    mail.notify_jss_drop(
        conn,
        config,
        today.isoformat(),
        float(before["jss"]),
        float(row["jss"]),
        settings=settings,
        now=now,
    )
    return [f"JSS dropped: {note[12:]}. JSS guard is on."]


# ---------------------------------------------------------------- rank drop


def _average(conn: sqlite3.Connection, keyword: str, end: date, days: int) -> float | None:
    """Average place over `days` days ending on `end`. A day where I was not found counts as one
    place after the last freelancer looked at (that is a drop, not a missing value)."""
    first = end - timedelta(days=days - 1)
    value = conn.execute(
        "SELECT AVG(COALESCE(position, total_seen + 1)) FROM rank_daily "
        "WHERE keyword = ? AND date >= ? AND date <= ?",
        (keyword, first.isoformat(), end.isoformat()),
    ).fetchone()[0]
    return None if value is None else float(value)


def check_rank_drops(
    conn: sqlite3.Connection, settings: Settings, config: Config, today: date, now: datetime
) -> list[str]:
    if not settings.get("alerts.rank_drop_enabled"):
        return []
    window = max(1, int(settings.get("rank.average_days")))
    need = max(1, int(settings.get("alerts.rank_drop_days")))
    places = float(settings.get("alerts.rank_drop_positions"))
    year, week, _ = today.isocalendar()
    lines: list[str] = []
    for keyword in rank.approved_keywords(conn, settings):
        fresh = conn.execute(
            "SELECT 1 FROM rank_daily WHERE keyword = ? AND date = ?",
            (keyword, today.isoformat()),
        ).fetchone()
        if fresh is None:
            continue
        pairs = []
        for back in range(need):
            day = today - timedelta(days=back)
            now_avg = _average(conn, keyword, day, window)
            was_avg = _average(conn, keyword, day - timedelta(days=window), window)
            if now_avg is None or was_avg is None or now_avg - was_avg < places - 1e-9:
                break
            pairs.append((was_avg, now_avg))
        else:
            was_avg, now_avg = pairs[0][0], pairs[0][1]
            mail.notify_rank_drop(
                conn,
                config,
                keyword,
                f"{year}-W{week:02d}",
                was_avg,
                now_avg,
                need,
                settings=settings,
                now=now,
            )
            lines.append(f"Rank drop: {keyword} #{was_avg:.1f} -> #{now_avg:.1f}")
    return lines


# ---------------------------------------------------------------- entry point for the daily run


def run_alerts(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    now: datetime,
    today: date,
    snapshot: dict[str, Any] | None = None,
) -> list[str]:
    """All alert checks. `snapshot` is the row `metrics.snapshot` returned (None when there was
    none today: the invite step is then skipped, because it needs a fresh read). Never raises."""
    lines: list[str] = []
    steps: list[tuple[str, Any]] = []
    if snapshot is not None and "invites" in snapshot:
        steps.append(
            (
                "invites",
                lambda: process_invites(
                    conn,
                    settings,
                    config,
                    snapshot["invites"],
                    bool(snapshot.get("invites_complete")),
                    now,
                ),
            )
        )
    steps.append(("jss", lambda: check_jss_drop(conn, settings, config, today, now)))
    steps.append(("rank", lambda: check_rank_drops(conn, settings, config, today, now)))
    for name, step in steps:
        try:
            lines.extend(step())
        except Exception as e:
            log.exception("Alert step %s failed", name)
            lines.append(f"Alert step {name} failed: {type(e).__name__}: {e}"[:300])
    return lines
