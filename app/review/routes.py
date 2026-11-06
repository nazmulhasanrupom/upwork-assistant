"""Proposal reviewer pages (plan R2, SPEC2 4.B).

- GET  /review            the form (pick a stored job or paste the job text) and past reviews
- POST /review            pre-checks + one writing-model call, then redirect to the result
- GET  /review/<id>       score, pre-checks, issues, side-by-side original and fixed version

Login and CSRF come from the app-wide guard. Everything the user pasted is escaped by Jinja; the
only markup made here is the diff, which escapes every word first (app/review/diff.py).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response

from app.auth_routes import render
from app.pages import _json
from app.review import after_send, service
from app.review import prechecks as pc
from app.review.diff import word_diff

log = logging.getLogger("review")

router = APIRouter()


def _int_setting(request: Request, key: str) -> int:
    return int(request.app.state.settings.get(key))


def score_class(request: Request, score: int | None) -> str:
    if score is None:
        return "none"
    if score >= _int_setting(request, "review.score_good"):
        return "good"
    return "ok" if score >= _int_setting(request, "review.score_ok") else "bad"


# ---------------------------------------------------------------- form + history


def history(request: Request) -> list[dict[str, Any]]:
    conn: sqlite3.Connection = request.app.state.db
    rows = conn.execute(
        "SELECT id, job_id, original, prechecks, result, score, cost, created_at FROM reviews "
        "ORDER BY id DESC LIMIT ?",
        (_int_setting(request, "review.history_limit"),),
    ).fetchall()
    out = []
    for r in rows:
        original = _json(r["original"], {})
        result = _json(r["result"], {})
        findings = _json(r["prechecks"], [])
        out.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "title": (original.get("job_title") if isinstance(original, dict) else "") or "",
                "job_id": r["job_id"],
                "score": r["score"],
                "score_class": score_class(request, r["score"]),
                "error": result.get("error") if isinstance(result, dict) else None,
                "problems": sum(1 for f in findings if f.get("severity") in (pc.ERROR, pc.WARN)),
                "cost": r["cost"],
            }
        )
    return out


def sent_proposals(request: Request) -> list[dict[str, Any]]:
    """Synced proposals (plan R3) with the score of their after-send review."""
    conn: sqlite3.Connection = request.app.state.db
    rows = conn.execute(
        "SELECT p.*, r.score AS score, r.result AS review_result FROM proposals p "
        "LEFT JOIN reviews r ON r.id = p.review_id ORDER BY p.created_at DESC LIMIT ?",
        (_int_setting(request, "review.after_send_page_limit"),),
    ).fetchall()
    out = []
    for r in rows:
        diff_data = _json(r["draft_diff"], None) if r["draft_diff"] else None
        cover = diff_data.get("cover_letter") if isinstance(diff_data, dict) else None
        result = _json(r["review_result"], {}) if r["review_result"] else {}
        out.append(
            {
                "id": r["proposal_id"],
                "job_id": r["job_id"],
                "title": r["job_title"] or r["proposal_id"],
                "created_at": r["created_at"],
                "rate": r["rate"],
                "status": after_send.STATUS_LABELS.get(r["status"] or "", r["status"] or ""),
                "viewed": bool(r["viewed_by_client"]),
                "boosted": bool(r["boosted"]),
                "text_stored": r["cover_letter"] is not None,
                "review_id": r["review_id"],
                "score": r["score"],
                "score_class": score_class(request, r["score"]),
                "error": result.get("error") if isinstance(result, dict) else None,
                "top_issue": after_send.top_issue(result) if isinstance(result, dict) else "",
                "similarity": round(cover["similarity"] * 100) if cover else None,
                "draft_changed": bool(cover and cover.get("changed")),
                "has_draft": bool(cover),
            }
        )
    return out


def _form_page(
    request: Request,
    status: int = 200,
    *,
    job_id: str = "",
    error: str = "",
    values: dict[str, Any] | None = None,
) -> Response:
    s = request.app.state
    job = service.load_job(s.db, job_id) if job_id else None
    values = dict(values or {})
    questions = job["questions"] if job else []
    answers = values.get("answers") or []
    return render(
        request,
        "review.html",
        status,
        jobs=service.job_options(s.db, _int_setting(request, "review.job_list_limit")),
        job_id=job["job_id"] if job else "",
        job_title=job["title"] if job else "",
        budget=job["budget"] if job else None,
        questions=questions,
        answers=[answers[i] if i < len(answers) else "" for i in range(len(questions))],
        cover_letter=values.get("cover_letter", ""),
        job_text=values.get("job_text", ""),
        answers_free=values.get("answers_free", ""),
        rate=values.get("rate", ""),
        input_max=_int_setting(request, "review.input_max_chars"),
        error=error,
        history=history(request),
        proposals=sent_proposals(request),
    )


@router.get("/review")
def review_index(request: Request, job_id: str = "") -> Response:
    return _form_page(request, job_id=job_id.strip())


def _too_long(request: Request, *texts: str) -> bool:
    limit = _int_setting(request, "review.input_max_chars")
    return any(len(t) > limit for t in texts)


@router.post("/review")
async def review_submit(request: Request) -> Response:
    s = request.app.state
    form = await request.form()

    def field(name: str) -> str:
        value = form.get(name)
        return value.replace("\r\n", "\n").replace("\r", "\n") if isinstance(value, str) else ""

    job_id = field("job_id").strip()
    job = service.load_job(s.db, job_id) if job_id else None
    cover = field("cover_letter").strip()
    rate = field("rate").strip()[:40]
    job_text = field("job_text")
    answers_free = field("answers_free")
    if job and job["questions"]:
        raw_answers = [field(f"answer_{i}") for i in range(len(job["questions"]))]
    else:
        raw_answers = []
    values = {
        "cover_letter": cover,
        "rate": rate,
        "job_text": job_text,
        "answers_free": answers_free,
        "answers": raw_answers,
    }

    def again(message: str, status: int = 400) -> Response:
        return _form_page(
            request, status, job_id=job["job_id"] if job else "", error=message, values=values
        )

    if _too_long(request, cover, job_text, answers_free, *raw_answers):
        return again("One of the texts is too long.", 413)
    if job is None:
        if not job_text.strip():
            return again("Choose a job from the list or paste the job text.")
        job = service.pasted_job(job_text, _int_setting(request, "review.job_text_max_chars"))
    if not cover:
        return again("Paste your cover letter.")

    if job["questions"]:
        answers = [
            {"question": q, "answer": a.strip()}
            for q, a in zip(job["questions"], raw_answers, strict=True)
        ]
    elif answers_free.strip():
        answers = [{"question": "", "answer": answers_free.strip()}]
    else:
        answers = []

    try:
        review_id = await run_in_threadpool(
            service.run_review,
            s.db,
            s.config,
            s.settings,
            job=job,
            cover_letter=cover,
            answers=answers,
            rate=rate,
        )
    except Exception:
        log.exception("Review failed")
        return again("The review failed. See the log.", 500)
    return RedirectResponse(f"/review/{review_id}", status_code=303)


# ---------------------------------------------------------------- result


def result_view(request: Request, row: sqlite3.Row) -> dict[str, Any]:
    original = _json(row["original"], {})
    original = original if isinstance(original, dict) else {}
    result = _json(row["result"], {})
    result = result if isinstance(result, dict) else {}
    findings = _json(row["prechecks"], [])
    fixed = result.get("fixed_version") if isinstance(result.get("fixed_version"), dict) else None

    cover_left = cover_right = None
    answer_rows: list[dict[str, Any]] = []
    orig_answers = original.get("answers") if isinstance(original.get("answers"), list) else []
    if fixed:
        cover_left, cover_right = word_diff(original.get("cover_letter", ""), fixed["cover_letter"])
        fixed_answers = fixed.get("answers") or []
        for i in range(max(len(orig_answers), len(fixed_answers))):
            oa = orig_answers[i] if i < len(orig_answers) else {}
            fa = fixed_answers[i] if i < len(fixed_answers) else {}
            left, right = word_diff(oa.get("answer", ""), fa.get("answer", ""))
            answer_rows.append(
                {
                    "n": i + 1,
                    "question": oa.get("question") or fa.get("question") or "",
                    "left": left,
                    "right": right,
                    "original": oa.get("answer", ""),
                    "fixed": fa.get("answer", ""),
                }
            )
    else:
        for i, oa in enumerate(orig_answers):
            answer_rows.append(
                {"n": i + 1, "question": oa.get("question", ""), "original": oa.get("answer", "")}
            )
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "job_id": row["job_id"],
        "job_title": original.get("job_title") or "",
        "original": original,
        "prechecks": findings,
        "pre_counts": pc.counts(findings),
        "result": result,
        "error": result.get("error"),
        "score": row["score"],
        "score_class": score_class(request, row["score"]),
        "model": row["model"],
        "cost": row["cost"],
        "fixed": fixed,
        "cover_left": cover_left,
        "cover_right": cover_right,
        "answer_rows": answer_rows,
        "field_label": pc.field_label,
    }


@router.get("/review/{review_id}")
def review_result(request: Request, review_id: int) -> Response:
    row = request.app.state.db.execute(
        "SELECT * FROM reviews WHERE id = ?", (review_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return render(request, "review_result.html", review=result_view(request, row))
