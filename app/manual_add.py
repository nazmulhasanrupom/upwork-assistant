"""Manual job add (plan M1): the owner pastes a link, an id or a few title words.

READ only, through the bridge. The job is saved as "candidate" (the owner chose it), judged and
drafted like a daily-run job, and an auto-send countdown may start (gated by dry-run and the
kill switch inside auto_send_flow). Reasons a filter would have dropped it are reported.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import auto_send_flow
from app.config import Config
from app.filters.activity import HIDE, classify
from app.filters.flags import flags as red_flags
from app.filters.tiers import prefilter, tier
from app.llm import judging
from app.upwork import jobs as J

CIPHER_RE = re.compile(r"~0[0-9A-Za-z]{8,}")
DIGITS_RE = re.compile(r"(?<!\d)\d{12,25}(?!\d)")
URL_RE = re.compile(r"https://(?:[\w-]+\.)*upwork\.com/\S*")
ACTION_ID_RE = re.compile(r"[0-9a-f]{16}")
WORDS_FOR_TITLE = 3
KEPT_TIERS = ("HIGH", "MID")


class ManualError(ValueError):
    """A problem the owner can read (bad input, job not found)."""


@dataclass
class Parsed:
    kind: str  # "id" | "url" | "title"
    value: str


def parse_input(text: str, max_chars: int) -> Parsed:
    text = (text or "").strip()
    if not text:
        raise ManualError("Type a job link, a job id or a few words of the title.")
    if len(text) > max_chars:
        raise ManualError(f"That is too long. Use at most {max_chars} characters.")
    if m := CIPHER_RE.search(text):
        return Parsed("id", m.group(0))
    if m := DIGITS_RE.search(text):
        return Parsed("id", m.group(0))
    if m := URL_RE.search(text):
        return Parsed("url", m.group(0).rstrip(").,;"))
    return Parsed("title", " ".join(text.split()))


def manual_dir(config: Config) -> Path:
    return config.data_dir / "manual"


def save_found(config: Config, action_id: str, rows: list[dict[str, Any]]) -> None:
    if not ACTION_ID_RE.fullmatch(action_id):
        raise ManualError("Bad action id.")
    folder = manual_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{action_id}.json").write_text(json.dumps(rows, ensure_ascii=False))


def load_found(config: Config, action_id: str) -> list[dict[str, Any]]:
    if not ACTION_ID_RE.fullmatch(action_id or ""):
        return []
    path = manual_dir(config) / f"{action_id}.json"
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def search(bridge: Any, settings: Any, words: str) -> list[dict[str, Any]]:
    """One find_jobs search by title words (max N rows from settings)."""
    org = str(settings.get("upwork.talent_org_uid") or "")
    if not org:
        raise ManualError("The Upwork connection is not set up yet.")
    parts = words.split()
    key = J.P_TITLE if len(parts) <= WORDS_FOR_TITLE else "query"
    params = {key: words, J.P_LIMIT: int(settings.get("manual.search_limit"))}
    res = bridge.call_many(
        [(J.TOOL, {"action": J.ACTION_SEARCH, "org_uid": org, "params": params})]
    )
    return J._search_jobs(res[0], J.ACTION_SEARCH)


def describe_rows(rows: list[dict[str, Any]], known: set[str]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        job = J.cut_down(r)
        if not job.get("id"):
            continue
        if job["job_type"] == "fixed" and job["fixed_budget"] is not None:
            budget = f"${job['fixed_budget']:g}"
        elif job["budget_min"] is not None:
            lo, hi = job["budget_min"], job["budget_max"]
            budget = f"${lo:g}/hr" if lo == hi else f"${lo:g}-{hi:g}/hr"
        else:
            budget = ""
        out.append(
            {
                "id": job["id"],
                "title": job["title"] or "(no title)",
                "posted": job.get("published_date") or job.get("created_date") or "",
                "budget": budget,
                "proposals": job.get("proposals_tier") or job.get("proposal_count") or "",
                "known": job["id"] in known,
            }
        )
    return out


def _find_search_row(bridge: Any, settings: Any, title: str, job_id: str) -> dict[str, Any]:
    """Optional: a search by the first title words, to get skills and dates. {} if not found."""
    words = " ".join((title or "").split()[:WORDS_FOR_TITLE])
    if not words:
        return {}
    try:
        rows = search(bridge, settings, words)
    except Exception:
        return {}
    for r in rows:
        if str(r.get("id")) == job_id:
            return r
    return {}


def _existing(conn: Any, job_id: str) -> Any:
    return conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def add_job(
    ctx: Any,
    config: Config,
    bridge: Any,
    ref: str,
    search_row: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Fetch, save as candidate, judge and draft. Returns (result_url, message)."""
    conn, settings = ctx.conn, ctx.settings
    org = str(settings.get("upwork.talent_org_uid") or "")
    if not org:
        raise ManualError("The Upwork connection is not set up yet.")
    row = _existing(conn, ref)
    if row is not None and row["status"] != "dropped":
        return f"/jobs/{ref}", "This job is already in Jobs."
    try:
        detail = J.fetch_detail(bridge, ref, org)
    except Exception as exc:
        raise ManualError(f"Upwork did not return that job ({str(exc)[:150]}).") from exc
    job_id = str(detail.get("job_id") or ref)
    row = _existing(conn, job_id)
    if row is not None and row["status"] != "dropped":
        return f"/jobs/{job_id}", "This job is already in Jobs."
    if search_row is None:
        search_row = _find_search_row(bridge, settings, detail.get("title") or "", job_id)
    full = J.cut_down(search_row or {}, detail, settings)
    full["id"] = job_id
    now = datetime.now(UTC)

    reasons: list[str] = []
    label, rule = classify(full, settings, now)
    keep, reason = prefilter(full, settings)
    if not keep:
        reasons.append(reason)
    if label == HIDE:
        reasons.append(f"activity: {rule}")
    t, estimated, rate = tier(full, settings)
    if t not in KEPT_TIERS:
        reasons.append(
            f"tier LOW (rate {rate:.2f}/hr)" if rate is not None else "tier LOW (no rate)"
        )
    flag_list = red_flags(full, settings)
    J.save_job(
        conn,
        full,
        status="candidate",
        tier=t,
        tier_estimated=estimated,
        rate_paid=rate,
        red_flags=flag_list,
        activity_label=label,
    )
    full.update(tier=t, tier_estimated=estimated, rate_paid=rate, flags=flag_list)
    full["status"] = "candidate"

    judged = judging.judge_and_draft(conn, [full], settings=settings, config=config)
    started: list[int] = []
    try:
        started = auto_send_flow.start_countdowns(
            conn, settings, config, bridge, [i["job_id"] for i in judged.judged], now=now
        )
    except Exception:
        started = []
    text = "Job added and judged." if judged.judged else "Job added. It was not judged yet."
    if reasons:
        text += " A filter would have dropped it: " + "; ".join(reasons) + "."
    if started:
        text += " An auto-send countdown started."
    if judged.notes:
        text += " " + judged.notes[0]
    return f"/jobs/{job_id}", text
