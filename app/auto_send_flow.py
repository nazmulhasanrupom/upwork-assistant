"""Auto-send, the doing part (round 3, A2). The owner approved it in plain words: when a job is
excellent, tell them, count down, and if there is no Stop, send the AI proposal.

- `start_countdowns` (daily pipeline): eligible job -> preview -> countdown row -> email.
- `deliver`: re-check everything, make a fresh preview, confirm. Used by the deadline task (system
  consent) and by the Approve button (the user's own clicks).
- `run_due`: the task body. Claims each ended countdown atomically, then delivers it.

Safety, in order: Stop wins if it lands first (atomic UPDATE); dry-run, kill switch, eligibility,
"already applied", the draft text and the proposal rules are checked again right before the send;
a failed send is never retried; a double tick cannot send twice (the claim).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import auto_send, mail, proposal_rules
from app import job_actions as ja
from app.config import Config
from app.llm.live import ClaudeCancelled
from app.llm.runner import ClaudeError
from app.settings import Settings
from app.upwork import writes
from app.upwork.client import UpworkMCPError

log = logging.getLogger("auto_send")

DONE_JOB_STATUSES = ("sent", "skipped", "dropped")
OPEN_JOB_STATUSES = ("prepared", "candidate", "judged", "drafted", "seen")


@dataclass
class Outcome:
    status: str  # sent | failed | cancelled
    message: str = ""


def _now() -> datetime:
    return datetime.now(UTC)


def _job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def _judgement(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM judgements WHERE job_id = ?", (job_id,)).fetchone()


def _draft(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM drafts WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


def _balance(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT balance FROM connects ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else row["balance"]


def _lines(text: str) -> list[str]:
    return [x for x in (text or "").splitlines() if x.strip()]


def _job_facts(job: sqlite3.Row | None, job_id: str) -> dict[str, Any]:
    return {"job_id": job_id, "title": (job["title"] if job else None) or f"Job {job_id}"}


# ---------------------------------------------------------------- the emails


def _email_waiting(conn, config, settings, row_id, job, judgement, letter, cost, balance, now):
    row = auto_send.get(conn, row_id)
    why = [f"Why: {x}" for x in _lines(judgement["why_apply"] if judgement else "")][:4]
    fit = judgement["fit_score"] if judgement else None
    mail.notify_auto_send(
        conn,
        config,
        "auto_send_waiting",
        row_id,
        _job_facts(job, row["job_id"]),
        heading="Proposal will be sent automatically",
        lines=[
            "Sends on Upwork automatically when the timer ends. Open the job to Approve now "
            "or Stop.",
            *why,
            "",
            "The proposal:",
            *_lines(letter),
        ],
        facts=[
            ("Fit", f"{fit} / 100" if fit is not None else "n/a"),
            ("Sends at (UTC)", str(row["deadline"])),
            ("Connects", f"{cost if cost is not None else 'n/a'} of {balance}"),
        ],
        settings=settings,
        now=now,
    )


def _email_end(conn, config, settings, row_id, event, heading, lines, now):
    row = auto_send.get(conn, row_id)
    job = _job_facts(_job(conn, row["job_id"]), row["job_id"])
    mail.notify_auto_send(
        conn, config, event, row_id, job, heading=heading, lines=lines, settings=settings, now=now
    )


def _letter_of(conn: sqlite3.Connection, preview_row_id: int | None) -> tuple[str, list[Any]]:
    pv = writes.get_preview(conn, preview_row_id) if preview_row_id else None
    if pv is None:
        return "", []
    try:
        params = json.loads(pv["request_json"])["params"]
    except (ValueError, KeyError, TypeError):
        return "", []
    return str(params.get("cover_letter") or ""), list(params.get("answers") or [])


# ---------------------------------------------------------------- creation (daily pipeline)


def start_countdowns(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    reader: Any,
    job_ids: list[str],
    *,
    writer: writes.ClaudeUpworkWriter | None = None,
    now: datetime | None = None,
) -> list[int]:
    """For each eligible job: make the preview (nothing is sent), start the countdown, email the
    owner. Returns the new countdown ids. Does nothing while dry-run or the kill switch is on."""
    now = now or _now()
    if (
        settings.get("general.dry_run")
        or settings.get("general.kill_switch")
        or not settings.get("auto_send.enabled")
    ):
        return []
    made: list[int] = []
    for job_id in job_ids:
        try:
            row_id = _start_one(conn, settings, config, reader, str(job_id), writer, now)
        except ClaudeCancelled:
            raise
        except Exception:  # one job must not stop the others or fail the daily run
            log.exception("Auto-send could not start for job %s", job_id)
            continue
        if row_id:
            made.append(row_id)
    return made


def _start_one(conn, settings, config, reader, job_id, writer, now) -> int | None:
    job, judgement, draft = _job(conn, job_id), _judgement(conn, job_id), _draft(conn, job_id)
    if job is None or judgement is None or draft is None or auto_send.for_job(conn, job_id):
        return None
    ok, _ = auto_send.eligible(conn, job, judgement, draft, settings, _balance(conn), now)
    if not ok:
        return None
    letter = draft["cover_letter"] or ""
    answers = [a for a in ja.draft_answers(draft) if a["answer"].strip() or a["question"]]
    rate = draft["suggested_rate"]
    if proposal_rules.check_draft(letter, answers) or not rate or rate <= 0:
        return None
    org = str(settings.get("upwork.talent_org_uid") or "")
    extra: dict[str, Any] = {}
    items = None
    wanted = int(settings.get("auto_send.max_highlights"))
    if wanted > 0:
        items = ja.fetch_highlights(reader, org, int(settings.get("writes.highlights_max_shown")))
        extra = ja.highlight_extra(items, wanted)
    writer = writer or writes.ClaudeUpworkWriter(config, settings, conn)
    click = writes.issue_system_click(conn, settings, "preview", job_id=job_id)
    try:
        result = writes.preview_proposal(
            click,
            conn=conn,
            config=config,
            settings=settings,
            reader=reader,
            writer=writer,
            job_id=job_id,
            cover_letter=letter,
            charged_amount=rate,
            answers=answers,
            extra=extra,
            attachments_asked=True,  # the system answers "no files" and picks the highlights
            highlights_asked=True,
        )
    except writes.WriteError:
        log.info("Auto-send preview refused for job %s", job_id, exc_info=True)
        return None
    if result.status != "previewed":
        return None
    conn.execute(
        "UPDATE previews SET highlights_json = ? WHERE id = ?",
        (json.dumps(items, ensure_ascii=False) if items is not None else None, result.row_id),
    )
    seen = {**dict(job), "connects_cost": result.connects_cost or job["connects_cost"]}
    ok, why = auto_send.eligible(
        conn, seen, judgement, draft, settings, result.connects_balance, now
    )
    if not ok:
        log.info("Auto-send not started for job %s: %s", job_id, "; ".join(why))
        return None
    row_id = auto_send.create(conn, settings, job_id, result.row_id, now)
    if row_id is None:
        return None
    _email_waiting(
        conn, config, settings, row_id, job, judgement, letter,
        result.connects_cost, result.connects_balance, now,
    )  # fmt: skip
    log.warning("Auto-send countdown %s started for job %s", row_id, job_id)
    return row_id


# ---------------------------------------------------------------- delivery


def _end(conn, config, settings, row_id, by, status, message, now) -> Outcome:
    """Close a `sending` row as cancelled or failed, and email."""
    if status == "failed":
        auto_send.finish_failed(conn, row_id, message)
        _email_end(
            conn, config, settings, row_id, "auto_send_failed", "Auto-send failed",
            [f"Nothing was retried. {message}", "Check the job on Upwork before you try by hand."],
            now,
        )  # fmt: skip
    else:
        auto_send.cancel(conn, row_id, message, from_status="sending")
        if by == "system":
            _email_end(
                conn, config, settings, row_id, "auto_send_cancelled", "Auto-send cancelled",
                [f"The proposal was not sent: {message}"], now,
            )  # fmt: skip
    return Outcome(status, message)


def deliver(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    row_id: int,
    *,
    by: str,
    make_click: Callable[[str], writes.UserClick],
    reader: Any,
    writer: writes.ClaudeUpworkWriter,
    now: datetime | None = None,
) -> Outcome:
    """Send one claimed countdown (status `sending`). `by` is "system" (deadline) or "user"
    (Approve). Never retries. Always ends the row as sent, failed or cancelled."""
    now = now or _now()
    row = auto_send.get(conn, row_id)
    if row is None or row["status"] != "sending":
        return Outcome("cancelled", "This countdown is not ready to send.")
    try:
        return _deliver(conn, config, settings, row, by, make_click, reader, writer, now)
    except ClaudeCancelled:
        _end(conn, config, settings, row_id, by, "cancelled", "Cancelled in the Claude dock", now)
        raise
    except Exception as exc:
        log.exception("Auto-send %s failed", row_id)
        text = f"{type(exc).__name__}: {exc}"[:200]
        return _end(conn, config, settings, row_id, by, "failed", text, now)


def _deliver(conn, config, settings, row, by, make_click, reader, writer, now) -> Outcome:
    row_id, job_id = row["id"], row["job_id"]

    def stop(status: str, message: str) -> Outcome:
        return _end(conn, config, settings, row_id, by, status, message, now)

    if settings.get("general.kill_switch"):
        return stop("cancelled", "the kill switch is on")
    if settings.get("general.dry_run"):
        return stop("cancelled", "dry-run is on, nothing was sent")
    job = _job(conn, job_id)
    if job is None or job["status"] in DONE_JOB_STATUSES:
        return stop("cancelled", "the job is already applied, skipped or dropped")
    letter, answers = _letter_of(conn, row["preview_id"])
    if not letter.strip():
        return stop("cancelled", "the stored proposal cannot be read")
    judgement, draft = _judgement(conn, job_id), _draft(conn, job_id)
    if by == "system":
        if draft is None or (draft["cover_letter"] or "") != letter:
            return stop("cancelled", "the draft changed after the countdown started")
        left = proposal_rules.check_draft(letter, answers)
        if left:
            return stop("cancelled", "the proposal breaks the rules: " + "; ".join(left)[:150])

    try:
        fresh = writes.remake_preview(
            make_click("preview"),
            conn=conn,
            config=config,
            settings=settings,
            reader=reader,
            writer=writer,
            row_id=row["preview_id"],
        )
    except writes.ExistingProposal:
        return stop("cancelled", "already applied: Upwork shows a proposal from you")
    except (writes.WriteError, UpworkMCPError, ClaudeError) as exc:
        return stop("failed", f"the fresh preview failed: {str(exc)[:150]}")
    if fresh.status != "previewed":
        return stop("failed", fresh.message or "the fresh preview failed")
    old = writes.get_preview(conn, row["preview_id"])
    conn.execute(
        "UPDATE previews SET highlights_json = ? WHERE id = ?",
        (old["highlights_json"], fresh.row_id),
    )
    conn.execute("UPDATE auto_sends SET preview_id = ? WHERE id = ?", (fresh.row_id, row_id))

    cost, balance = fresh.connects_cost, fresh.connects_balance
    if by == "system":
        seen = {**dict(job), "connects_cost": cost if cost is not None else job["connects_cost"]}
        ok, why = auto_send.eligible(
            conn, seen, judgement, draft, settings, balance, now, exclude_id=row_id
        )
        if not ok:
            return stop("cancelled", "no longer eligible: " + "; ".join(why)[:200])
    elif cost is not None and balance is not None and cost > balance:
        return stop("cancelled", f"not enough Connects: it costs {cost}, you have {balance}")

    click = make_click("confirm")
    result = writes.confirm_preview(
        click, conn=conn, config=config, settings=settings, writer=writer, row_id=fresh.row_id
    )
    if result.status == "dry_run_blocked":
        return stop("cancelled", "dry-run is on, nothing was sent")
    if not result.sent:
        return stop("failed", result.message or "Upwork did not accept the proposal")
    auto_send.finish_sent(conn, row_id, click.click_id)
    marks = ",".join("?" for _ in OPEN_JOB_STATUSES)
    conn.execute(
        f"UPDATE jobs SET status = 'sent' WHERE job_id = ? AND status IN ({marks})",
        (job_id, *OPEN_JOB_STATUSES),
    )
    who = "you approved it" if by == "user" else "the countdown ended with no Stop"
    _email_end(
        conn, config, settings, row_id, "auto_send_sent", "Proposal sent",
        [f"Sent on Upwork because {who}.", "", "The proposal:", *_lines(letter)], now,
    )  # fmt: skip
    return Outcome("sent", "Sent to Upwork.")


# ---------------------------------------------------------------- the task body


def run_due(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    *,
    reader_factory: Callable[[], Any] | None = None,
    writer_factory: Callable[[], writes.ClaudeUpworkWriter] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Handle every countdown whose deadline has passed. Returns one line per row."""
    from app.upwork.bridge import ClaudeUpworkBridge

    now = now or _now()
    lines: list[str] = []
    for row in auto_send.due_rows(conn, now):
        rid = row["id"]
        blocker = (
            "the kill switch was on at the deadline"
            if settings.get("general.kill_switch")
            else "dry-run was on at the deadline"
            if settings.get("general.dry_run")
            else "auto-send was switched off"
            if not settings.get("auto_send.enabled")
            else ""
        )
        if blocker:
            if auto_send.cancel(conn, rid, blocker, from_status="waiting"):
                _email_end(
                    conn, config, settings, rid, "auto_send_cancelled", "Auto-send cancelled",
                    [f"The proposal was not sent: {blocker}."], now,
                )  # fmt: skip
                lines.append(f"{rid}: cancelled ({blocker})")
            continue
        if not auto_send.claim_due(conn, rid, now):  # a Stop or another tick got there first
            continue
        reader = (reader_factory or (lambda: ClaudeUpworkBridge(config, settings, conn)))()
        try:
            writer = (
                writer_factory or (lambda: writes.ClaudeUpworkWriter(config, settings, conn))
            )()
            outcome = deliver(
                conn,
                config,
                settings,
                rid,
                by="system",
                make_click=lambda action, rid=rid: writes.issue_system_click(
                    conn, settings, action, auto_send_id=rid
                ),
                reader=reader,
                writer=writer,
                now=now,
            )
        finally:
            getattr(reader, "close", lambda: None)()
        lines.append(f"{rid}: {outcome.status} ({outcome.message})")
    return lines
