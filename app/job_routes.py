"""Job page write routes (plan H2). Login and CSRF come from the app-wide guard.

- POST /jobs/<id>/draft    save the edited draft (an em dash is never saved silently)
- POST /jobs/<id>/prepare  click -> check existing -> preview (nothing is sent)
- POST /jobs/<id>/answers  the attachments and highlights questions -> mark_asked
- POST /jobs/<id>/approve  click -> confirm_preview (dry-run blocks it and says so)
- POST /jobs/<id>/remake   click -> remake_preview for an expired preview
- POST /jobs/<id>/skip, /unskip

Every Upwork write goes through `app/upwork/writes.py`. A `UserClick` is made here, inside the
POST handler, and nowhere else. Messages after a redirect are fixed texts (`JOB_MESSAGES`).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import job_actions as ja
from app.llm import prompting
from app.llm.runner import ClaudeError
from app.pages import job_page
from app.upwork import writes
from app.upwork.bridge import ClaudeUpworkBridge
from app.upwork.client import UpworkMCPError
from app.worker import WorkerError, ensure_writes_open

log = logging.getLogger("job.actions")

router = APIRouter()


# ---------------------------------------------------------------- helpers


def _job(request: Request, job_id: str):
    row = request.app.state.db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _draft(request: Request, job_id: str):
    return request.app.state.db.execute(
        "SELECT * FROM drafts WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


def _done(job_id: str, code: str) -> Response:
    return RedirectResponse(f"/jobs/{job_id}?msg={code}", status_code=303)


def _fail(request: Request, job_id: str, text: str, kind: str = "error", **kw: Any) -> Response:
    return job_page(request, job_id, notice=(kind, text), status=kw.pop("status", 200), **kw)


def _close(reader: Any) -> None:
    try:
        getattr(reader, "close", lambda: None)()
    except Exception:
        log.exception("Could not close the Upwork bridge")


def _guard(request: Request, job_id: str, key: str) -> Response | None:
    """The same button rules the page shows, checked again on the server."""
    job = _job(request, job_id)
    view = ja.build_view(
        request.app.state.db, request.app.state.settings, job, _draft(request, job_id)
    )
    reason = view["reasons"].get(key)
    return _fail(request, job_id, reason, "warn", status=409) if reason else None


def _live_preview(request: Request, job_id: str, preview: int) -> tuple[Any, Response | None]:
    """The newest preview of the job, if it is the one the page showed."""
    row = ja.latest_preview(request.app.state.db, job_id)
    if row is None or row["id"] != preview:
        return None, _fail(
            request,
            job_id,
            "The page was out of date. Check the preview below.",
            "warn",
            status=409,
        )
    return row, None


def _error_text(exc: Exception) -> str:
    return str(exc).strip()[:300] or type(exc).__name__


# ---------------------------------------------------------------- edit draft


@router.post("/jobs/{job_id}/draft")
def save_draft(
    request: Request,
    job_id: str,
    cover_letter: str = Form(""),
    answers: Annotated[list[str] | None, Form()] = None,
    rate: str = Form(""),
    clean: str = Form(""),
) -> Response:
    s = request.app.state
    _job(request, job_id)
    draft = _draft(request, job_id)
    if draft is None:
        return _fail(request, job_id, "There is no draft to edit yet.", "warn", status=409)
    cover_letter = cover_letter.replace("\r\n", "\n").rstrip()
    answers = [a.replace("\r\n", "\n").rstrip() for a in answers or []]
    edit = {"cover_letter": cover_letter, "answers": answers, "rate": rate.strip()}
    problems = ja.check_draft_input(s.settings, cover_letter, answers, len(ja.draft_answers(draft)))
    try:
        value = ja.parse_rate(rate)
    except ValueError:
        problems.append("The rate must be a number above zero, or empty.")
        value = None
    if problems:
        return _fail(request, job_id, " ".join(problems), status=422, edit=edit)
    fields = ja.dash_fields(cover_letter, answers)
    code = "saved"
    if fields:
        if not clean:
            return _fail(
                request,
                job_id,
                f"Not saved: {', '.join(fields)} has an em dash or en dash. The rules allow none "
                "in any text. Fix it, or use 'Clean and save' to replace them.",
                "warn",
                status=422,
                edit=edit,
                edit_dashes=True,
            )
        cover_letter = prompting.no_em_dash(cover_letter)
        answers = [prompting.no_em_dash(a) for a in answers]
        code = "saved_clean"
    old = ja.draft_answers(draft)
    merged = [{"question": o["question"], "answer": a} for o, a in zip(old, answers, strict=True)]
    s.db.execute(
        "UPDATE drafts SET cover_letter = ?, answers = ?, suggested_rate = ?, edited_by_user = 1 "
        "WHERE id = ?",
        (cover_letter, json.dumps(merged, ensure_ascii=False), value, draft["id"]),
    )
    return _done(job_id, code)


# ---------------------------------------------------------------- the worker
#
# Prepare, answers, approve and remake are slow, so the route does the fast checks (login and CSRF
# by the app-wide guard, form checks, kill switch, dry-run text) and queues the rest on the worker
# (plan W2). The click is made here, in the POST handler, and travels inside `fn`. The route
# answers with 303 to /jobs/<id>?action=<id>; the page polls until the action is done.


def _queue(request: Request, kind: str, job_id: str, label: str, fn: Any) -> Response:
    action_id = request.app.state.worker.submit(kind, job_id, label, fn, f"{kind}:{job_id}")
    return RedirectResponse(f"/jobs/{job_id}?action={action_id}", status_code=303)


def _outcome(job_id: str, code: str) -> tuple[str, str]:
    """(result_url, message) for an action that ends well, from the fixed texts."""
    return f"/jobs/{job_id}?msg={code}", ja.JOB_MESSAGES[code][1]


def _make_bridge(factory: Any, config: Any, ctx: Any) -> Any:
    return factory() if factory else ClaudeUpworkBridge(config, ctx.settings, ctx.conn)


def _make_writer(config: Any, ctx: Any) -> writes.ClaudeUpworkWriter:
    return writes.ClaudeUpworkWriter(config, ctx.settings, ctx.conn)


def _set_status_conn(conn: Any, job_id: str, status: str, only_from: tuple[str, ...]) -> None:
    marks = ",".join("?" for _ in only_from)
    conn.execute(
        f"UPDATE jobs SET status = ? WHERE job_id = ? AND status IN ({marks})",
        (status, job_id, *only_from),
    )


# ---------------------------------------------------------------- prepare


def _fetch_highlights(reader: Any, org: str, limit: int) -> list[dict[str, Any]] | None:
    """`get_profile list_highlights` through the READ bridge. None when it cannot be read."""
    try:
        res = reader.call_many(
            [("get_profile", {"action": "list_highlights", "org_uid": org, "params": {}})]
        )[0]
        if res.is_error:
            return None
        return ja.parse_highlights(res.data(), limit)
    except (UpworkMCPError, ClaudeError):
        log.info("Highlights could not be read", exc_info=True)
        return None


def _boost(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise writes.WriteRefused("The boost must be a whole number of Connects.") from exc


@router.post("/jobs/{job_id}/prepare")
def prepare(request: Request, job_id: str, boost_connects: str = Form("")) -> Response:
    if (blocked := _guard(request, job_id, "prepare")) is not None:
        return blocked
    s = request.app.state
    draft = _draft(request, job_id)
    try:
        boost = _boost(boost_connects)
    except writes.WriteError as exc:
        return _fail(request, job_id, _error_text(exc))
    click = writes.issue_click(request, "preview")
    config = s.config
    factory = getattr(s, "mcp_factory", None)
    cover_letter = draft["cover_letter"]
    rate = draft["suggested_rate"]
    answers = [a for a in ja.draft_answers(draft) if a["answer"].strip() or a["question"]]

    def work(ctx: Any) -> tuple[str, str]:
        reader = None
        try:
            reader = _make_bridge(factory, config, ctx)
            result = writes.preview_proposal(
                click,
                conn=ctx.conn,
                config=config,
                settings=ctx.settings,
                reader=reader,
                writer=_make_writer(config, ctx),
                job_id=job_id,
                cover_letter=cover_letter,
                charged_amount=rate,
                answers=answers,
                boost_connects=boost,
            )
            if result.status == "previewed":
                org = str(ctx.settings.get("upwork.talent_org_uid"))
                items = _fetch_highlights(
                    reader, org, int(ctx.settings.get("writes.highlights_max_shown"))
                )
                ctx.conn.execute(
                    "UPDATE previews SET highlights_json = ? WHERE id = ?",
                    (
                        json.dumps(items, ensure_ascii=False) if items is not None else None,
                        result.row_id,
                    ),
                )
                _set_status_conn(
                    ctx.conn, job_id, "prepared", ("candidate", "judged", "drafted", "seen")
                )
        except writes.ExistingProposal:
            return _outcome(job_id, "applied")
        except (writes.WriteError, UpworkMCPError, ClaudeError) as exc:
            raise WorkerError(_error_text(exc)) from exc
        finally:
            if reader is not None:
                _close(reader)
        if result.status != "previewed":
            raise WorkerError(result.message or "The preview failed. Nothing was sent.")
        return _outcome(job_id, "prepared")

    return _queue(request, "job_prepare", job_id, "Making the preview", work)


# ---------------------------------------------------------------- the two questions


@router.post("/jobs/{job_id}/answers")
def answers(
    request: Request,
    job_id: str,
    preview: int = Form(0),
    attachments: str = Form(""),
    highlight: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Save the answers to the two questions Upwork requires. A highlight the user picks goes into
    a new preview (the proposal changes), which is made with a new preview click."""
    if (blocked := _guard(request, job_id, "asked")) is not None:
        return blocked
    row, err = _live_preview(request, job_id, preview)
    if err is not None:
        return err
    if attachments not in ("none", "want"):
        return _fail(request, job_id, "Answer the attachments question.", "warn", status=422)
    if attachments == "want":
        return _fail(
            request,
            job_id,
            "The Upwork tool cannot attach files. Open the job on Upwork and send this proposal "
            "by hand, or answer 'No files' to go on without files.",
            "warn",
        )
    items = json.loads(row["highlights_json"]) if row["highlights_json"] else []
    picks = [h for h in items if h.get("key") in set(highlight or []) and h.get("param")]
    if not picks:
        # Fast and local (one row update): no worker needed.
        writes.mark_asked(request.app.state.db, row["id"], attachments=True, highlights=True)
        return _done(job_id, "asked")
    return _repreview_with_highlights(request, job_id, row, picks)


def _repreview_with_highlights(
    request: Request, job_id: str, row: Any, picks: list[dict]
) -> Response:
    s = request.app.state
    params = json.loads(row["request_json"])["params"]
    extra = {k: params[k] for k in writes.PASS_THROUGH if k in params}
    for h in picks:
        extra.setdefault(h["param"], [])
        if h["id"] not in extra[h["param"]]:
            extra[h["param"]].append(h["id"])
    click = writes.issue_click(request, "preview")
    config = s.config
    factory = getattr(s, "mcp_factory", None)
    old_id = row["id"]
    old_highlights = row["highlights_json"]
    boost_limits = json.loads(row["boost_json"]) if row["boost_json"] else None

    def work(ctx: Any) -> tuple[str, str]:
        reader = None
        try:
            reader = _make_bridge(factory, config, ctx)
            result = writes.preview_proposal(
                click,
                conn=ctx.conn,
                config=config,
                settings=ctx.settings,
                reader=reader,
                writer=_make_writer(config, ctx),
                job_id=job_id,
                cover_letter=params["cover_letter"],
                charged_amount=params.get("charged_amount", params.get("bid")),
                answers=params.get("answers"),
                boost_connects=params.get("boost_connects"),
                boost_limits=boost_limits,
                extra=extra,
                attachments_asked=True,
                highlights_asked=True,
            )
        except writes.ExistingProposal:
            return _outcome(job_id, "applied")
        except (writes.WriteError, UpworkMCPError, ClaudeError) as exc:
            raise WorkerError(_error_text(exc)) from exc
        finally:
            if reader is not None:
                _close(reader)
        if result.status != "previewed":
            raise WorkerError(result.message or "The preview failed. Nothing was sent.")
        ctx.conn.execute(
            "UPDATE previews SET highlights_json = ? WHERE id = ?",
            (old_highlights, result.row_id),
        )
        ctx.conn.execute(
            "UPDATE previews SET status = 'expired' WHERE id = ? AND confirmed_at IS NULL "
            "AND status IN ('previewed', 'dry_run_blocked')",
            (old_id,),
        )
        return _outcome(job_id, "asked")

    return _queue(request, "job_answers", job_id, "Making a new preview", work)


# ---------------------------------------------------------------- approve / remake


@router.post("/jobs/{job_id}/approve")
def approve(request: Request, job_id: str, preview: int = Form(0)) -> Response:
    """Confirm the previewed proposal. With dry-run on (the default) nothing is sent."""
    if (blocked := _guard(request, job_id, "approve")) is not None:
        return blocked
    s = request.app.state
    row, err = _live_preview(request, job_id, preview)
    if err is not None:
        return err
    click = writes.issue_click(request, "confirm")  # the user's click, made here only
    config = s.config
    row_id = row["id"]

    def work(ctx: Any) -> tuple[str, str]:
        # Settings can change while the action waits: the kill switch is read again right before
        # the write, and confirm_preview reads dry-run again (and records the block).
        ensure_writes_open(ctx.settings)
        try:
            result = writes.confirm_preview(
                click,
                conn=ctx.conn,
                config=config,
                settings=ctx.settings,
                writer=_make_writer(config, ctx),
                row_id=row_id,
            )
        except writes.PreviewExpired as exc:
            raise WorkerError("Preview expired. Re-make the preview below.") from exc
        except (writes.WriteError, UpworkMCPError, ClaudeError) as exc:
            raise WorkerError(_error_text(exc)) from exc
        if result.status == "dry_run_blocked":
            log.warning("dry-run: approve for job %s blocked (preview row %s)", job_id, row_id)
            return _outcome(job_id, "dry_run")
        if result.sent:
            _set_status_conn(
                ctx.conn, job_id, "sent", ("prepared", "candidate", "judged", "drafted", "seen")
            )
            return _outcome(job_id, "sent")
        raise WorkerError(result.message or "Upwork did not accept it. Nothing was sent.")

    return _queue(request, "job_approve", job_id, "Approving the proposal", work)


@router.post("/jobs/{job_id}/remake")
def remake(request: Request, job_id: str, preview: int = Form(0)) -> Response:
    if (blocked := _guard(request, job_id, "remake")) is not None:
        return blocked
    s = request.app.state
    row, err = _live_preview(request, job_id, preview)
    if err is not None:
        return err
    view = ja.build_view(s.db, s.settings, _job(request, job_id), _draft(request, job_id))
    if not view["show_remake"]:
        return _fail(request, job_id, "This preview has not expired.", "warn", status=409)
    click = writes.issue_click(request, "preview")
    config = s.config
    factory = getattr(s, "mcp_factory", None)
    row_id = row["id"]
    old_highlights = row["highlights_json"]

    def work(ctx: Any) -> tuple[str, str]:
        reader = None
        try:
            reader = _make_bridge(factory, config, ctx)
            result = writes.remake_preview(
                click,
                conn=ctx.conn,
                config=config,
                settings=ctx.settings,
                reader=reader,
                writer=_make_writer(config, ctx),
                row_id=row_id,
            )
        except writes.ExistingProposal:
            return _outcome(job_id, "applied")
        except (writes.WriteError, UpworkMCPError, ClaudeError) as exc:
            raise WorkerError(_error_text(exc)) from exc
        finally:
            if reader is not None:
                _close(reader)
        if result.status != "previewed":
            raise WorkerError(result.message or "The preview failed. Nothing was sent.")
        ctx.conn.execute(
            "UPDATE previews SET highlights_json = ? WHERE id = ?",
            (old_highlights, result.row_id),
        )
        return _outcome(job_id, "remade")

    return _queue(request, "job_remake", job_id, "Making a fresh preview", work)


# ---------------------------------------------------------------- skip


@router.post("/jobs/{job_id}/skip")
def skip(request: Request, job_id: str) -> Response:
    job = _job(request, job_id)
    if job["status"] == "sent":
        return _fail(request, job_id, "This job was already sent.", "warn", status=409)
    request.app.state.db.execute("UPDATE jobs SET status = 'skipped' WHERE job_id = ?", (job_id,))
    return _done(job_id, "skipped")


@router.post("/jobs/{job_id}/unskip")
def unskip(request: Request, job_id: str) -> Response:
    job = _job(request, job_id)
    if job["status"] == "skipped":
        back = "drafted" if _draft(request, job_id) is not None else "judged"
        request.app.state.db.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (back, job_id))
    return _done(job_id, "unskipped")
