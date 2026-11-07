"""After-send review (plan R3, SPEC2 4.B and 5.5).

Part of the daily task, after the jobs part:

1. `sync_proposals`: one `list_freelancer_proposals list` call through the bridge. Every listed
   proposal is stored (status and "viewed" are updated on later runs). `get` runs only for
   proposals whose text is not stored yet, in batches. A stored draft for the same job is
   compared with the text that was sent (the learning signal).
2. `review_new`: the proposal review of `app/review/service.py`, once per proposal, never for
   proposals older than `review.after_send_max_age_days`, at most `review.after_send_max_per_run`
   per run. Cap, rate limit and a missing USER.md stop the step and nothing is marked.
3. One short email per run (`after_send_review` event).

Nothing here writes to Upwork. The questions of a proposal are Upwork text: Claude sees them only
inside the untrusted job tags (`screening_questions` of the job input).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app import mail
from app.base_url import get_base_url
from app.config import Config
from app.llm.runner import BudgetCapReached, ClaudeRateLimited
from app.pages import strip_untrusted
from app.review import diff, service
from app.settings import Settings
from app.upwork.client import CallBudgetExceeded, UpworkToolError

log = logging.getLogger("review.after_send")

TOOL = "list_freelancer_proposals"
ALERT_KEY = "after_send_failed"
EVENT = "after_send_review"
NO_JOB_TEXT = "Only the job title is known. The job post was not stored, so check what you can."

STATUS_LABELS = {"Accepted": "Submitted"}


@dataclass
class SyncResult:
    listed: int = 0
    new_ids: list[str] = field(default_factory=list)  # proposals stored for the first time
    fetched: int = 0  # `get` calls that worked
    draft_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ReviewBatch:
    reviewed: list[dict[str, Any]] = field(default_factory=list)
    stopped: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class AfterSendResult:
    sync: SyncResult = field(default_factory=SyncResult)
    reviews: ReviewBatch = field(default_factory=ReviewBatch)
    email: str | None = None

    def lines(self) -> list[str]:
        """Lines for the daily note."""
        s, r = self.sync, self.reviews
        out = [
            f"Proposals synced: {s.listed} listed, {len(s.new_ids)} new, {len(r.reviewed)} reviewed"
        ]
        out += s.draft_notes + s.notes + r.notes
        return out


# ---------------------------------------------------------------- parsing


def _iso_now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _created(node: dict[str, Any]) -> str | None:
    text = _get(node, "auditDetails", "createdDateTime", "displayValue")
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_node(node: Any) -> dict[str, Any] | None:
    """Fields of one proposal from a `list` edge or a `get` answer. None without an id."""
    if not isinstance(node, dict) or node.get("id") in (None, ""):
        return None
    bid = _number(_get(node, "terms", "connectsBid"))
    status = _get(node, "status", "status")
    return {
        "proposal_id": str(node["id"]),
        "job_id": str(_get(node, "marketplaceJobPosting", "id") or "") or None,
        "job_title": strip_untrusted(_get(node, "marketplaceJobPosting", "content", "title"))
        or None,
        "created_at": _created(node),
        "rate": _number(_get(node, "terms", "chargeRate", "rawValue")),
        "status": status if isinstance(status, str) else None,
        "viewed": 1 if node.get("viewedByClient") else 0,
        "boosted": 1 if node.get("boosted") else 0,
        "connects_bid": int(bid) if bid is not None else None,
    }


def parse_list(payload: Any) -> list[dict[str, Any]]:
    edges = _get(payload, "data", "vendorProposals", "edges")
    out = []
    for edge in edges if isinstance(edges, list) else []:
        parsed = parse_node(edge.get("node") if isinstance(edge, dict) else None)
        if parsed:
            out.append(parsed)
    return out


def parse_detail(payload: Any) -> dict[str, Any] | None:
    """The `get` answer: the list fields plus cover letter and screening answers."""
    node = _get(payload, "data", "vendorProposal")
    parsed = parse_node(node)
    if parsed is None:
        return None
    letter = node.get("proposalCoverLetter")
    answers = []
    for qa in (
        node.get("questionsAnswers") if isinstance(node.get("questionsAnswers"), list) else []
    ):
        if isinstance(qa, dict):
            answers.append(
                {
                    "question": strip_untrusted(qa.get("question")),
                    "answer": str(qa.get("answer") or "").strip(),
                }
            )
    parsed["cover_letter"] = letter.strip() if isinstance(letter, str) else ""
    parsed["answers"] = answers
    return parsed


def list_call(org_uid: str, limit: int) -> tuple[str, dict[str, Any]]:
    return TOOL, {"action": "list", "org_uid": org_uid, "params": {"limit": limit}}


def get_call(org_uid: str, proposal_id: str) -> tuple[str, dict[str, Any]]:
    return TOOL, {"action": "get", "org_uid": org_uid, "params": {"id": proposal_id}}


# ---------------------------------------------------------------- draft vs sent


def _draft_answers(raw: str | None) -> list[str]:
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        out.append(str(item.get("answer") or "") if isinstance(item, dict) else str(item))
    return out


def match_draft(
    conn: sqlite3.Connection, job_id: str | None, cover_letter: str, answers: list[dict[str, str]]
) -> tuple[int, dict[str, Any]] | None:
    """(draft id, diff data) for the newest draft of this job, or None."""
    if not job_id:
        return None
    row = conn.execute(
        "SELECT id, cover_letter, answers FROM drafts WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None or not (row["cover_letter"] or "").strip():
        return None
    draft_answers = _draft_answers(row["answers"])
    pairs = []
    for i in range(max(len(draft_answers), len(answers))):
        before = draft_answers[i] if i < len(draft_answers) else ""
        after = answers[i]["answer"] if i < len(answers) else ""
        data = diff.compare(before, after)
        data["n"] = i + 1
        pairs.append(data)
    return int(row["id"]), {
        "cover_letter": diff.compare(row["cover_letter"], cover_letter),
        "answers": pairs,
    }


def diff_note(title: str, data: dict[str, Any]) -> str:
    cover = data["cover_letter"]
    if not cover["changed"]:
        return f"Draft vs sent for '{title}': sent as written ({len(data['answers'])} answers)."
    pct = round(float(cover["similarity"]) * 100)
    return (
        f"Draft vs sent for '{title}': cover letter {pct}% the same "
        f"(+{cover['added_words']} / -{cover['removed_words']} words), "
        f"{sum(1 for a in data['answers'] if a['changed'])} of {len(data['answers'])} answers "
        "changed."
    )


# ---------------------------------------------------------------- sync


def _store_list(conn: sqlite3.Connection, items: list[dict[str, Any]], now: str) -> list[str]:
    new_ids = []
    for p in items:
        exists = conn.execute(
            "SELECT 1 FROM proposals WHERE proposal_id = ?", (p["proposal_id"],)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE proposals SET status = ?, viewed_by_client = ?, boosted = ?, "
                "connects_bid = COALESCE(?, connects_bid), job_title = COALESCE(?, job_title), "
                "fetched_at = ? WHERE proposal_id = ?",
                (
                    p["status"],
                    p["viewed"],
                    p["boosted"],
                    p["connects_bid"],
                    p["job_title"],
                    now,
                    p["proposal_id"],
                ),
            )
            continue
        conn.execute(
            "INSERT INTO proposals(proposal_id, job_id, job_title, created_at, rate, status, "
            "viewed_by_client, boosted, connects_bid, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                p["proposal_id"],
                p["job_id"],
                p["job_title"],
                p["created_at"],
                p["rate"],
                p["status"],
                p["viewed"],
                p["boosted"],
                p["connects_bid"],
                now,
            ),
        )
        new_ids.append(p["proposal_id"])
    return new_ids


def _store_detail(conn: sqlite3.Connection, d: dict[str, Any], now: str) -> str | None:
    """Save text and answers. Returns the draft-vs-sent note when a draft matched."""
    row = conn.execute(
        "SELECT job_id, job_title, rate FROM proposals WHERE proposal_id = ?", (d["proposal_id"],)
    ).fetchone()
    job_id = d["job_id"] or row["job_id"]
    title = d["job_title"] or row["job_title"] or d["proposal_id"]
    matched = match_draft(conn, job_id, d["cover_letter"], d["answers"])
    conn.execute(
        "UPDATE proposals SET cover_letter = ?, answers_json = ?, job_id = ?, "
        "rate = COALESCE(?, rate), status = COALESCE(?, status), viewed_by_client = ?, "
        "boosted = ?, connects_bid = COALESCE(?, connects_bid), fetched_at = ?, "
        "draft_id = ?, draft_diff = ? WHERE proposal_id = ?",
        (
            d["cover_letter"],
            json.dumps(d["answers"], ensure_ascii=False),
            job_id,
            d["rate"],
            d["status"],
            d["viewed"],
            d["boosted"],
            d["connects_bid"],
            now,
            matched[0] if matched else None,
            json.dumps(matched[1], ensure_ascii=False) if matched else None,
            d["proposal_id"],
        ),
    )
    return diff_note(title, matched[1]) if matched else None


def sync_proposals(
    bridge: Any, conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> SyncResult:
    """List my proposals (one call), store them, then `get` the ones without stored text.

    Auth and Claude errors of the bridge are raised. A failed `get` for one proposal is a note;
    the proposal is tried again in the next run.
    """
    result = SyncResult()
    org = str(settings.get("upwork.talent_org_uid") or "")
    if not org:
        result.notes.append("Proposals not synced: no freelancer account is set.")
        return result
    stamp = _iso_now(now)
    limit = max(1, int(settings.get("review.after_send_list_limit")))
    listed = bridge.call_many([list_call(org, limit)])[0]
    try:
        payload = listed.unwrap()
    except (UpworkToolError, CallBudgetExceeded) as e:
        result.notes.append(f"Proposal list failed: {e}"[:300])
        return result
    items = parse_list(payload)
    result.listed = len(items)
    result.new_ids = _store_list(conn, items, stamp)

    ids = [p["proposal_id"] for p in items]
    todo = [
        r["proposal_id"]
        for pid in ids
        for r in conn.execute(
            "SELECT proposal_id FROM proposals WHERE proposal_id = ? AND cover_letter IS NULL",
            (pid,),
        )
    ]
    batch = max(1, int(settings.get("claude.fetch_batch_size")))
    for start in range(0, len(todo), batch):
        chunk = todo[start : start + batch]
        answers = bridge.call_many([get_call(org, pid) for pid in chunk])
        for pid, res in zip(chunk, answers, strict=True):
            try:
                detail = parse_detail(res.unwrap())
            except CallBudgetExceeded:
                left = len(todo) - start
                result.notes.append(f"MCP call limit reached: {left} proposal text(s) wait.")
                return result
            except UpworkToolError as e:
                result.notes.append(f"Proposal {pid} could not be read: {e}"[:300])
                continue
            if detail is None:
                result.notes.append(f"Proposal {pid} had no usable data.")
                continue
            detail["proposal_id"] = pid
            note = _store_detail(conn, detail, stamp)
            result.fetched += 1
            if note:
                result.draft_notes.append(note)
    return result


# ---------------------------------------------------------------- review


def _job_for(conn: sqlite3.Connection, row: sqlite3.Row, questions: list[str]) -> dict[str, Any]:
    """The job for the review: the stored job when there is one, else the title only.

    Questions go into the job input, which the runner wraps in untrusted tags."""
    stored = service.load_job(conn, row["job_id"] or "")
    if stored:
        job = dict(stored)
        job["input"] = dict(stored["input"])
    else:
        title = row["job_title"] or row["job_id"] or "Unknown job"
        job = {
            "job_id": None,
            "title": title,
            "input": {"title": title, "job_text_available": False, "note": NO_JOB_TEXT},
            "questions": [],
            "budget": None,
        }
    job["questions"] = questions
    job["input"]["screening_questions"] = questions
    return job


def _rate_text(rate: float | None) -> str:
    return "" if rate is None else f"{rate:g}"


def top_issue(result: dict[str, Any]) -> str:
    issues = result.get("issues") if isinstance(result, dict) else None
    if isinstance(issues, list) and issues and isinstance(issues[0], dict):
        first = issues[0]
        return str(first.get("problem") or first.get("check") or "").strip()
    return ""


def review_new(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> ReviewBatch:
    """Review proposals that have text and no review, newest first. One Claude call each.

    A cap, a rate limit or a missing USER.md ends the step; the temporary review row is removed
    so the proposal is reviewed in a later run.
    """
    batch = ReviewBatch()
    now = now or datetime.now(UTC)
    cutoff = _iso_now(now - timedelta(days=int(settings.get("review.after_send_max_age_days"))))
    cap = max(0, int(settings.get("review.after_send_max_per_run")))
    rows = conn.execute(
        "SELECT * FROM proposals WHERE review_id IS NULL AND cover_letter IS NOT NULL "
        "AND cover_letter != '' AND created_at IS NOT NULL AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, cap),
    ).fetchall()
    for row in rows:
        try:
            qa = json.loads(row["answers_json"] or "[]")
        except ValueError:
            qa = []
        qa = [a for a in qa if isinstance(a, dict)]
        questions = [str(a.get("question") or "") for a in qa]
        answers = [
            {"question": q, "answer": str(a.get("answer") or "")}
            for q, a in zip(questions, qa, strict=True)
        ]
        for_claude = [
            {"question": f"screening_questions[{i}]", "answer": a["answer"]}
            for i, a in enumerate(answers)
        ]
        title = row["job_title"] or row["proposal_id"]
        review_id = service.run_review(
            conn,
            config,
            settings,
            job=_job_for(conn, row, [q for q in questions if q]),
            cover_letter=row["cover_letter"],
            answers=answers,
            rate=_rate_text(row["rate"]),
            claude_answers=for_claude,
            run_id=run_id,
        )
        saved = conn.execute(
            "SELECT score, result FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        result = json.loads(saved["result"] or "{}")
        kind = result.get("error_kind") if isinstance(result, dict) else None
        if kind in service.TEMPORARY_ERRORS:
            conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
            batch.stopped = str(result.get("error") or kind)[:200]
            batch.notes.append(f"After-send reviews stopped: {batch.stopped}")
            log.warning("After-send reviews stopped (%s)", kind)
            break
        conn.execute(
            "UPDATE proposals SET review_id = ? WHERE proposal_id = ?",
            (review_id, row["proposal_id"]),
        )
        if saved["score"] is None:
            batch.notes.append(f"Review of '{title}' has no score: {result.get('error')}"[:300])
            continue
        batch.reviewed.append(
            {
                "proposal_id": row["proposal_id"],
                "review_id": review_id,
                "title": title,
                "score": int(saved["score"]),
                "top_issue": top_issue(result),
            }
        )
    return batch


# ---------------------------------------------------------------- email


def notify_reviews(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    reviewed: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> str | None:
    """One short email for the reviews of this run. Never raises. Returns the send result."""
    if not reviewed:
        return None
    try:
        base = get_base_url(settings, config)
        items = []
        for r in reviewed:
            links = [("Open the review", f"{base}/review/{r['review_id']}")] if base else []
            items.append(
                {
                    "title": r["title"],
                    "meta": f"Score {r['score']} / 100",
                    "lines": [f"Top issue: {r['top_issue']}"] if r["top_issue"] else [],
                    "links": links,
                }
            )
        count = len(reviewed)
        title = f"{count} sent proposal{'s' if count != 1 else ''} reviewed"
        text, html = mail.build_email(
            title,
            ["Claude reviewed the proposals below after you sent them. Nothing was changed."],
            items=items,
            link_path="/review",
            base_url=base,
            button_label="Open all reviews",
        )
        ref = ",".join(sorted(str(r["proposal_id"]) for r in reviewed))
        return mail.send_event(
            conn, EVENT, ref, title, text, html, config=config, settings=settings, now=now
        )
    except Exception:
        log.exception("After-send review email failed")
        return mail.FAILED


# ---------------------------------------------------------------- the daily part


def run_after_send(
    bridge: Any,
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> AfterSendResult:
    """Sync, review, email. Cap and rate limit end the step quietly (a note); other errors
    are raised for the caller (daily.py logs them and sets an alert)."""
    out = AfterSendResult()
    try:
        out.sync = sync_proposals(bridge, conn, settings, now=now)
    except (BudgetCapReached, ClaudeRateLimited) as e:
        out.sync.notes.append(f"Proposals not synced (Claude limit): {e}"[:300])
        return out
    out.reviews = review_new(conn, config, settings, now=now, run_id=run_id)
    out.email = notify_reviews(conn, config, settings, out.reviews.reviewed, now=now)
    return out
