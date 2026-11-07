"""Job page actions, the logic side (plan H2): what the Apply card shows, and the draft edit rules.

No routes here (see `app/job_routes.py`) and no Upwork or Claude call. Every Upwork write goes
through `app/upwork/writes.py`; this module only reads the `previews` rows it leaves behind and
decides which buttons are enabled, with a short reason for each disabled one.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.llm import prompting
from app.llm.runner import ClaudeError
from app.settings import Settings
from app.upwork import writes
from app.upwork.client import UpworkMCPError
from app.upwork.jobs import CLOSE_TAG, OPEN_TAG

# Fixed texts for the notice after a redirect (never echo the query string).
JOB_MESSAGES: dict[str, tuple[str, str]] = {
    "saved": ("ok", "Draft saved."),
    "saved_clean": ("ok", "Draft saved. Em dashes were replaced with plain punctuation."),
    "prepared": ("ok", "Preview made. Nothing was sent. Answer the two questions, then approve."),
    "asked": ("ok", "Answers saved. You can approve now."),
    "dry_run": (
        "info",
        "Dry-run: not sent. The approve click was logged and nothing went to Upwork.",
    ),
    "sent": ("ok", "Sent to Upwork."),
    "remade": ("ok", "Fresh preview made. Nothing was sent."),
    "applied": ("warn", "Already applied: Upwork shows a proposal from you for this job."),
    "auto_stopped": ("ok", "Stopped. Nothing will be sent automatically for this job."),
    "auto_late": ("warn", "Too late: this countdown was already stopped, sent or cancelled."),
    "skipped": ("info", "Job skipped."),
    "unskipped": ("info", "Job is back in the list."),
}

HIGHLIGHT_PARAMS = {  # kind -> manage_proposals parameter (writes.PASS_THROUGH)
    "portfolio": "portfolio_project_ids",
    "certificate": "certificate_ids",
    "work_history": "work_history_ids",
}
_KIND_HINTS = (
    ("portfolio", "portfolio"),
    ("certif", "certificate"),
    ("work", "work_history"),
    ("employment", "work_history"),
    ("job", "work_history"),
)
_ID_KEYS = ("id", "uid", "ciphertext", "project_id", "certificate_id", "work_history_id")
_DONE_STATUSES = ("sent",)
_OPEN_STATUSES = ("previewed", "dry_run_blocked")


def _jloads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def untag(text: Any) -> str:
    return str(text or "").replace(OPEN_TAG, "").replace(CLOSE_TAG, "").strip()


# ---------------------------------------------------------------- the draft


def draft_answers(row: sqlite3.Row | None) -> list[dict[str, str]]:
    items = _jloads(row["answers"], []) if row is not None else []
    out = []
    for a in items if isinstance(items, list) else []:
        if isinstance(a, dict):
            out.append(
                {"question": str(a.get("question") or ""), "answer": str(a.get("answer") or "")}
            )
    return out


def parse_rate(text: str) -> float | None:
    """Empty -> None. Otherwise a finite number above zero, else ValueError."""
    text = (text or "").strip().replace(",", ".").lstrip("$")
    if not text:
        return None
    value = float(text)  # ValueError for words
    if not math.isfinite(value) or value <= 0:
        raise ValueError("The rate must be a number above zero.")
    return value


def dash_fields(cover_letter: str, answers: list[str]) -> list[str]:
    """Names of the fields that hold an em dash or en dash (hard rule: none in any text)."""
    found = []
    if prompting.has_em_dash(cover_letter):
        found.append("Cover letter")
    for i, a in enumerate(answers, 1):
        if prompting.has_em_dash(a):
            found.append(f"Answer {i}")
    return found


def check_draft_input(
    settings: Settings, cover_letter: str, answers: list[str], old_count: int
) -> list[str]:
    """Problems that stop a save (the em dash check is separate: it offers a fix)."""
    problems = []
    max_letter = int(settings.get("writes.cover_letter_max_chars"))
    max_answer = int(settings.get("writes.answer_max_chars"))
    if not cover_letter.strip():
        problems.append("The cover letter is empty.")
    elif len(cover_letter) > max_letter:
        problems.append(
            f"The cover letter has {len(cover_letter)} characters; the limit is {max_letter}."
        )
    if len(answers) != old_count:
        problems.append("The number of answers does not match the draft. Reload the page.")
    for i, a in enumerate(answers, 1):
        if len(a) > max_answer:
            problems.append(f"Answer {i} has {len(a)} characters; the limit is {max_answer}.")
    return problems


# ---------------------------------------------------------------- highlights


log = logging.getLogger("job.actions")


def fetch_highlights(reader: Any, org: str, limit: int) -> list[dict[str, Any]] | None:
    """`get_profile list_highlights` through the READ bridge. None when it cannot be read."""
    try:
        res = reader.call_many(
            [("get_profile", {"action": "list_highlights", "org_uid": org, "params": {}})]
        )[0]
        if res.is_error:
            return None
        return parse_highlights(res.data(), limit)
    except (UpworkMCPError, ClaudeError):
        log.info("Highlights could not be read", exc_info=True)
        return None


def highlight_extra(items: list[dict[str, Any]] | None, count: int) -> dict[str, list[str]]:
    """manage_proposals extras for the first `count` highlights that can be added."""
    extra: dict[str, list[str]] = {}
    taken = 0
    for h in items or []:
        if taken >= count:
            break
        if h.get("param") and h.get("id"):
            extra.setdefault(h["param"], []).append(h["id"])
            taken += 1
    return extra


def parse_highlights(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Highlights from a `get_profile list_highlights` answer. The live shape was not captured, so
    this walks the answer: a dict with a `title` is a highlight, its kind comes from a `type`
    field or from the key of the list it sits in. Without a known kind and an id it is listed but
    cannot be added (`param` is None)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def kind_of(name: str) -> str:
        low = name.lower()
        return next((k for hint, k in _KIND_HINTS if hint in low), "")

    def walk(node: Any, hint: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, hint)
        elif isinstance(node, dict):
            title = untag(node.get("title"))
            ident = next((str(node[k]) for k in _ID_KEYS if node.get(k) not in (None, "")), "")
            if title:
                own = ""
                for key in ("type", "kind", "category"):
                    if isinstance(node.get(key), str):
                        own = kind_of(node[key]) or own
                kind = own or hint
                sig = (kind, ident, title)
                if sig not in seen:
                    seen.add(sig)
                    param = HIGHLIGHT_PARAMS.get(kind) if ident else None
                    out.append(
                        {
                            "key": f"h{len(out)}",
                            "kind": kind or "other",
                            "id": ident,
                            "title": title,
                            "param": param,
                        }
                    )
            for k, v in node.items():
                if isinstance(v, (list, dict)):
                    walk(v, kind_of(str(k)) or hint)

    walk(payload, "")
    return out[:limit]


# ---------------------------------------------------------------- the Apply card


def latest_preview(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM previews WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


def _label(item: Any) -> str:
    if isinstance(item, str):
        return untag(item)
    if isinstance(item, dict):
        for key in ("name", "title", "description", "label", "qualification"):
            if item.get(key):
                return untag(item[key])
        return untag(json.dumps(item, ensure_ascii=False))[:200]
    return untag(item)


def _label_list(value: Any) -> list[str]:
    return [x for x in (_label(i) for i in value) if x] if isinstance(value, list) else []


def _same_request(row: sqlite3.Row, draft: sqlite3.Row | None) -> bool:
    """True when the stored preview request still matches the draft (letter, answers, rate)."""
    if draft is None:
        return False
    req = _jloads(row["request_json"], {})
    params = req.get("params") if isinstance(req, dict) else None
    if not isinstance(params, dict):
        return False
    amount = params.get("charged_amount", params.get("bid"))
    rate = draft["suggested_rate"]
    if rate is None or amount is None or abs(float(amount) - float(rate)) > 1e-9:
        return False
    sent = [(a.get("question"), a.get("answer")) for a in params.get("answers") or []]
    mine = [(a["question"], a["answer"]) for a in draft_answers(draft)]
    return params.get("cover_letter") == draft["cover_letter"] and sent == mine


def preview_view(
    row: sqlite3.Row, draft: sqlite3.Row | None, settings: Settings, now: datetime
) -> dict[str, Any]:
    response = _jloads(row["response_json"], row["response_json"])
    req = _jloads(row["request_json"], {})
    params = req.get("params", {}) if isinstance(req, dict) else {}
    boost = _jloads(row["boost_json"], None)
    boost = boost if isinstance(boost, dict) else None
    cost, balance = row["connects_cost"], row["connects_balance"]
    expired = row["status"] == "expired" or (
        row["status"] in _OPEN_STATUSES and writes.is_expired(row, settings, now)
    )
    can_apply = (
        writes._find(response, "can_apply", "canApply") if isinstance(response, dict) else None
    )
    highlights = _jloads(row["highlights_json"], None)
    chosen = {
        p: set(map(str, params.get(p) or [])) for p in HIGHLIGHT_PARAMS.values() if params.get(p)
    }
    return {
        "id": row["id"],
        "status": row["status"],
        "kind": row["kind"],
        "expired": expired,
        "stale": row["status"] in (*_OPEN_STATUSES, "expired") and not _same_request(row, draft),
        "created_at": row["created_at"],
        "connects_cost": cost,
        "connects_balance": balance,
        "can_afford": None if cost is None or balance is None else cost <= balance,
        "can_apply": can_apply if isinstance(can_apply, bool) else None,
        "boost": boost,
        "boost_range": (
            [boost.get("recommended_connects"), boost.get("max_boost_connects")] if boost else None
        ),
        "boost_chosen": params.get("boost_connects"),
        "unmet": _label_list(
            writes._find(response, "unmet_preferred_qualifications", "unmet_qualifications")
        ),
        "extra_prefs": _label_list(writes._find(response, "additional_preferences")),
        "attachments_asked": bool(row["attachments_asked"]),
        "highlights_asked": bool(row["highlights_asked"]),
        "highlights": highlights if isinstance(highlights, list) else None,
        "highlights_read": isinstance(highlights, list),
        "chosen_ids": chosen,
        "error": (
            str(response)[:300] if row["status"] == "failed" and row["confirmed_at"] is None else ""
        ),
        "confirm_failed": row["status"] == "failed" and row["confirmed_at"] is not None,
        "dry_run_blocked": row["status"] == "dry_run_blocked",
        "confirmed": row["status"] == "confirmed",
    }


def build_view(
    conn: sqlite3.Connection,
    settings: Settings,
    job: sqlite3.Row,
    draft: sqlite3.Row | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the Apply card needs. `reasons` maps each button to why it is disabled."""
    now = now or datetime.now(UTC)
    kill = bool(settings.get("general.kill_switch"))
    row = latest_preview(conn, job["job_id"])
    p = preview_view(row, draft, settings, now) if row is not None else None
    live = p is not None and p["status"] in _OPEN_STATUSES and not p["expired"]

    reasons: dict[str, str] = {}
    letter = (draft["cover_letter"] or "").strip() if draft is not None else ""
    blocked = ""
    if job["status"] in _DONE_STATUSES:
        blocked = "This job was already sent."
    elif job["status"] == "skipped":
        blocked = "This job is skipped."
    elif draft is None or not letter:
        blocked = "There is no draft yet."
    elif draft["suggested_rate"] is None:
        blocked = "The draft has no rate. Edit the draft and add one."
    elif not str(settings.get("upwork.talent_org_uid") or "").strip():
        blocked = "No Upwork account chosen yet. Open the Upwork page."
    if kill:
        reasons["prepare"] = reasons["remake"] = reasons["asked"] = reasons["approve"] = (
            "The kill switch is on, so nothing is written to Upwork."
        )
    else:
        if blocked:
            reasons["prepare"] = reasons["remake"] = reasons["asked"] = reasons["approve"] = blocked
        if p is not None and p["stale"] and "remake" not in reasons:
            reasons["remake"] = "The draft changed after this preview. Prepare again."
        if p is not None and not live and not blocked:
            reasons["asked"] = reasons["approve"] = (
                "Preview expired." if p["expired"] else "There is no live preview."
            )
        if live and "approve" not in reasons:
            need = settings.get("writes.require_asked_flags")
            if p["stale"]:
                reasons["approve"] = "The draft changed after the preview. Prepare again."
            elif p["can_apply"] is False:
                reasons["approve"] = "Upwork says this proposal cannot be sent."
            elif p["can_afford"] is False:
                reasons["approve"] = (
                    f"Not enough Connects: it costs {p['connects_cost']}, you have "
                    f"{p['connects_balance']}."
                )
            elif need and not (p["attachments_asked"] and p["highlights_asked"]):
                reasons["approve"] = "Answer the attachments and highlights questions first."
        if live and p["stale"]:
            reasons["asked"] = "The draft changed after the preview. Prepare again."
    return {
        "preview": p,
        "live": live,
        "blocked": blocked,
        "kill_switch": kill,
        "dry_run": bool(settings.get("general.dry_run")),
        "reasons": reasons,
        "can_prepare": "prepare" not in reasons,
        "can_remake": "remake" not in reasons,
        "can_ask": "asked" not in reasons,
        "can_approve": "approve" not in reasons,
        "show_remake": p is not None and p["expired"] and p["status"] != "confirmed",
        "boost_limits": writes.latest_boost_limits(conn, job["job_id"]),
        "skipped": job["status"] == "skipped",
    }
