"""Proposal review: prepare input, call the writing model once, check and save (plan R2).

Python does not trust the model. Its answer is cleaned (no dashes, at most 5 issues, score
0-100), the fixed version is checked again by the same Python pre-checks, and numbers in the
fixed version that appear nowhere in the draft, USER.md or the job post are flagged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.config import Config
from app.llm import judging, prompting
from app.llm.runner import (
    BudgetCapReached,
    ClaudeFailed,
    ClaudeRateLimited,
    run_claude,
)
from app.pages import strip_untrusted
from app.review import prechecks as pc
from app.settings import Settings

log = logging.getLogger("review")

PROMPT = "prompts/review_proposal.md"
TASK = "review"
DRAFT_TAG = "draft"
MAX_ISSUES = 5
MAX_LIST = 12

MSG_NO_USER = (
    "USER.md is empty, so Claude cannot review the draft without inventing facts about you. "
    "Add your profile facts on the Profile page, then review again. The checks below ran "
    "without Claude."
)
MSG_RATE_LIMIT = (
    "Claude is rate limited right now. Try again later. The checks below ran without Claude."
)
MSG_BAD_OUTPUT = (
    "Claude's answer was not a usable review. Try again. The checks below ran without Claude."
)


# `error_kind` in a saved review that has no score. no_user, cap and rate_limit are temporary:
# the after-send task does not count them as a review and tries again in a later run.
ERR_NO_USER, ERR_CAP, ERR_RATE_LIMIT = "no_user", "cap", "rate_limit"
ERR_FAILED, ERR_BAD_OUTPUT = "failed", "bad_output"
TEMPORARY_ERRORS = (ERR_NO_USER, ERR_CAP, ERR_RATE_LIMIT)


class ReviewError(RuntimeError):
    """The model's answer cannot be used."""


# ---------------------------------------------------------------- jobs


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def job_options(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT job_id, title, tier, status, first_seen FROM jobs ORDER BY first_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """A stored job for the review: Claude input, screening questions, budget."""
    if not job_id:
        return None
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    details = _json(row["details"], {})
    raw_questions = details.get("screening_questions") if isinstance(details, dict) else None
    questions = [strip_untrusted(q) for q in raw_questions or [] if isinstance(q, str)]
    return {
        "job_id": row["job_id"],
        "title": row["title"] or row["job_id"],
        "input": judging.job_input(row),
        "questions": [q for q in questions if q],
        "budget": {
            "min": row["budget_min"],
            "max": row["budget_max"],
            "job_type": row["job_type"],
            "kind": row["hourly_budget_type"],
        },
    }


def pasted_job(text: str, max_chars: int) -> dict[str, Any]:
    return {
        "job_id": None,
        "title": "Pasted job text",
        "input": {"pasted_job_text": text.strip()[:max_chars]},
        "questions": [],
        "budget": None,
    }


# ---------------------------------------------------------------- Claude input


def _neutral(text: str) -> str:
    """Stop text from closing or opening the draft tag (or the job tag)."""
    text = re.sub(rf"<(/?)\s*{DRAFT_TAG}\b", rf"&lt;\1{DRAFT_TAG}", text, flags=re.I)
    return re.sub(r"<(/?)\s*untrusted_job_post", r"&lt;\1untrusted_job_post", text, flags=re.I)


def draft_block(cover_letter: str, answers: list[dict[str, str]], rate: str) -> str:
    body = json.dumps(
        {"cover_letter": cover_letter, "answers": answers, "rate": rate or None},
        ensure_ascii=False,
        indent=1,
    )
    return f"<{DRAFT_TAG}>\n{_neutral(body)}\n</{DRAFT_TAG}>"


def job_hash(job_input: dict[str, Any]) -> str:
    text = json.dumps(job_input, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- numbers the model added

_NUM_RE = re.compile(r"[$]?\d[\d,]*(?:\.\d+)?%?")


def _num_key(token: str) -> str:
    return token.strip("$%").replace(",", "").rstrip(".")


def numbers_in(text: str) -> set[str]:
    return {_num_key(t) for t in _NUM_RE.findall(text or "")}


def new_numbers(fixed_texts: list[str], source_text: str) -> list[str]:
    """Numbers in the fixed version that are in none of the sources (draft, USER.md, job)."""
    known = numbers_in(source_text)
    found: list[str] = []
    for text in fixed_texts:
        for tok in _NUM_RE.findall(text or ""):
            key = _num_key(tok)
            if len(key.replace(".", "")) <= 1 and not tok.endswith("%") and not tok.startswith("$"):
                continue  # "2 lines", "1 proof": small counting words, not claims
            if key not in known and tok not in found:
                found.append(tok)
    return found[:8]


# ---------------------------------------------------------------- the model's answer


def _text(value: Any, limit: int = 600) -> str:
    return prompting.no_em_dash(str(value if value is not None else "")).strip()[:limit]


def _str_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else []
    out = [_text(x, 400) for x in items if isinstance(x, (str, int, float)) and str(x).strip()]
    return out[:MAX_LIST]


def normalize_result(
    raw: Any,
    *,
    sources_text: str,
    limits: pc.Limits,
) -> dict[str, Any]:
    """The model's JSON as a clean dict. Raises ReviewError when there is no usable score."""
    if not isinstance(raw, dict):
        raise ReviewError("not an object")
    try:
        score = int(round(float(raw.get("score"))))
    except (TypeError, ValueError):
        raise ReviewError("no score") from None
    score = max(0, min(100, score))

    issues = []
    for item in raw.get("issues") if isinstance(raw.get("issues"), list) else []:
        if not isinstance(item, dict):
            continue
        issue = {k: _text(item.get(k)) for k in ("check", "problem", "fix")}
        if issue["problem"] or issue["fix"]:
            issues.append(issue)
    issues = issues[:MAX_ISSUES]

    first_ok = raw.get("first_two_lines_ok")
    proof = raw.get("proof_found")
    proof = _text(proof, 400) if isinstance(proof, str) and proof.strip() else None

    fixed = None
    fv = raw.get("fixed_version")
    if (
        isinstance(fv, dict)
        and isinstance(fv.get("cover_letter"), str)
        and fv["cover_letter"].strip()
    ):
        answers = []
        for a in fv.get("answers") if isinstance(fv.get("answers"), list) else []:
            if isinstance(a, dict):
                answers.append(
                    {
                        "question": _text(a.get("question"), 2000),
                        "answer": _text(a.get("answer"), 20000),
                    }
                )
        fixed = {"cover_letter": _text(fv["cover_letter"], 20000), "answers": answers}

    warnings: list[str] = []
    if fixed:
        texts = [fixed["cover_letter"]] + [a["answer"] for a in fixed["answers"]]
        added = new_numbers(texts, sources_text)
        if added:
            warnings.append(
                "The fixed version has numbers that are not in your draft, USER.md or the job "
                "post: " + ", ".join(added) + ". Check them before you use the text."
            )
        problems = pc.run_prechecks(fixed["cover_letter"], fixed["answers"], limits=limits)
        for p in problems:
            if p["severity"] == pc.ERROR and p["check"] in ("length", "na_answer", "dash"):
                warnings.append("Fixed version: " + p["message"])
    return {
        "score": score,
        "issues": issues,
        "instructions_found": _str_list(raw.get("instructions_found")),
        "instructions_missed": _str_list(raw.get("instructions_missed")),
        "first_two_lines_ok": first_ok if isinstance(first_ok, bool) else None,
        "proof_found": proof,
        "fixed_version": fixed,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- run + save


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def save_review(
    conn: sqlite3.Connection,
    *,
    job_id: str | None,
    job_text_hash: str,
    original: dict[str, Any],
    prechecks: list[dict[str, Any]],
    result: dict[str, Any],
    score: int | None,
    model: str | None,
    cost: float,
) -> int:
    cur = conn.execute(
        "INSERT INTO reviews(job_id, job_text_hash, original, prechecks, result, score, model, "
        "cost, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            job_text_hash,
            json.dumps(original, ensure_ascii=False),
            json.dumps(prechecks, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            score,
            model,
            float(cost),
            _iso_now(),
        ),
    )
    return int(cur.lastrowid)


def run_review(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    *,
    job: dict[str, Any],
    cover_letter: str,
    answers: list[dict[str, str]],
    rate: str,
    claude_answers: list[dict[str, str]] | None = None,
    run_id: str | None = None,
) -> int:
    """Pre-checks, then one writing-model call. Saves and returns the review id.

    `claude_answers` replaces `answers` in the draft block that goes to Claude (the after-send
    review keeps the questions, which are Upwork text, inside the untrusted job tags instead).
    `run_id` is the `runs` row the Claude cost is recorded under.

    Cap reached, rate limit, a missing USER.md or a bad answer do not raise: the review is saved
    with the pre-checks and an `error` text, so the page still shows what Python found.
    """
    limits = pc.limits_from_settings(settings)
    findings = pc.run_prechecks(
        cover_letter,
        answers,
        rate=rate,
        budget=job["budget"],
        questions=job["questions"],
        limits=limits,
    )
    original = {
        "cover_letter": cover_letter,
        "answers": answers,
        "rate": rate,
        "job_title": job["title"],
        "job_text": job["input"].get("pasted_job_text") if not job["job_id"] else None,
    }
    common = {
        "job_id": job["job_id"],
        "job_text_hash": job_hash(job["input"]),
        "original": original,
        "prechecks": findings,
    }
    model = str(settings.get("models.write"))

    def failed(message: str, kind: str) -> int:
        return save_review(
            conn,
            **common,
            result={"error": message, "error_kind": kind},
            score=None,
            model=None,
            cost=0.0,
        )

    user_facts = prompting.user_facts(config)
    if not user_facts:
        return failed(MSG_NO_USER, ERR_NO_USER)

    job_payload = json.loads(_neutral(json.dumps(job["input"], ensure_ascii=False)))
    input_obj = {
        "job": job_payload,
        "draft": draft_block(cover_letter, claude_answers or answers, rate),
        "prechecks": [{k: f[k] for k in ("check", "severity", "message")} for f in findings],
    }
    try:
        res = run_claude(
            TASK,
            PROMPT,
            input_obj,
            model,
            int(settings.get("claude.review_max_turns")),
            float(settings.get("claude.review_max_budget_usd")),
            config=config,
            conn=conn,
            settings=settings,
            effort=settings.get("models.write_effort") or None,
            timeout=int(settings.get("review.timeout_seconds")),
            prompt_text=prompting.build_prompt(PROMPT, config=config),
            record_run_id=run_id,
        )
    except BudgetCapReached as e:
        return failed(f"{e} The checks below ran without Claude.", ERR_CAP)
    except ClaudeRateLimited:
        return failed(MSG_RATE_LIMIT, ERR_RATE_LIMIT)
    except ClaudeFailed as e:
        log.warning("review call failed: %s", e)
        return failed(f"Claude could not finish the review: {str(e)[:200]}", ERR_FAILED)

    sources = "\n".join(
        [
            cover_letter,
            *(a.get("answer", "") for a in answers),
            str(rate or ""),
            user_facts,
            json.dumps(job["input"], ensure_ascii=False, default=str),
        ]
    )
    try:
        result = normalize_result(
            res["output"],
            sources_text=sources,
            limits=limits,
        )
    except ReviewError as e:
        log.warning("review output unusable: %s", e)
        return save_review(
            conn,
            **common,
            result={"error": MSG_BAD_OUTPUT, "error_kind": ERR_BAD_OUTPUT},
            score=None,
            model=model,
            cost=float(res["cost_usd"]),
        )
    return save_review(
        conn,
        **common,
        result=result,
        score=result["score"],
        model=model,
        cost=float(res["cost_usd"]),
    )
