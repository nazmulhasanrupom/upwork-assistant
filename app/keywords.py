"""Keyword discovery (plan KW1, SPEC2 4.C phase 0): which 3 to 5 keywords to rank for?

Run by hand ("Find keywords" on /approvals), never scheduled. Steps:

1. Seeds: the setting `keywords.seeds` (cleaned, capped by `keywords.max_seeds`).
2. Numbers per seed, only READ tools through the bridge, all inside the call budget
   `keywords.max_mcp_calls`:
   - demand = `find_jobs search` -> `total_matching` (open jobs that match),
   - competition = `find_freelancers search` -> the total of the first page,
   - my rank = `app.rank.check_ranks` (reused, nothing stored in rank_daily).
   A seed whose call failed keeps `None` for that number. A seed with no number at all is left
   out of the Claude input.
3. One Claude call (think model, `prompts/pick_keywords.md`) picks `keywords.min_picks` to
   `keywords.max_picks` of them with a short reason each. Python does not trust the answer: only
   keywords from the input list, no duplicates, em dashes cleaned, reasons cut, and fewer than the
   minimum is an error (no approval item).
4. The pick becomes ONE item in the approvals table (kind `keywords`) and one email
   (`keywords_ready`, once per item). Approving it (handler below) saves the picks to
   `rank.keywords` and starts the baseline (`keywords.baseline_started` = today). Rejecting only
   records the decision.

During the baseline (`keywords.baseline_days`, default 14) the profile must not change, so the
numbers stay comparable. Phase O asks `baseline_active` / `profile_changes_blocked`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app import approvals, mail, metrics, rank
from app.config import Config
from app.llm import prompting
from app.llm.runner import (
    BudgetCapReached,
    ClaudeFailed,
    ClaudeRateLimited,
    run_claude,
)
from app.settings import Settings
from app.upwork import jobs
from app.upwork.client import UpworkAuthError

log = logging.getLogger("keywords")

TASK = "keywords"
KIND = "keywords"  # the approvals kind
EVENT = "keywords_ready"
CLAUDE_TASK = "pick_keywords"
PROMPT = "prompts/pick_keywords.md"
DATA_TAG = "keyword_data"

TOTAL_JOB_KEYS = ("total_matching", "totalMatching", "total_count", "totalCount", "total")


class KeywordsError(RuntimeError):
    """A problem with a message that is safe to show to the owner."""


# ---------------------------------------------------------------- the baseline


def _zone(settings: Settings, tz: tzinfo | str | None = None) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(tz or str(settings.get("general.timezone")))
    except Exception:
        return UTC


def _started(settings: Settings) -> date | None:
    raw = str(settings.get("keywords.baseline_started") or "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def baseline_days_left(settings: Settings, today: date | None = None) -> int | None:
    """Days until the baseline ends (0 = over), or None when no baseline was started."""
    started = _started(settings)
    if started is None:
        return None
    today = today or datetime.now(_zone(settings)).date()
    length = max(0, int(settings.get("keywords.baseline_days")))
    return max(0, length - (today - started).days)


def baseline_active(settings: Settings, today: date | None = None) -> bool:
    """True while the baseline runs: the keywords were approved less than
    `keywords.baseline_days` days ago. Profile changes must wait (Phase O)."""
    left = baseline_days_left(settings, today)
    return left is not None and left > 0


def profile_changes_blocked(settings: Settings, today: date | None = None) -> bool:
    """True when Phase O must not change the profile: the baseline is running, or it was never
    started (no approved keywords means nothing to measure against)."""
    left = baseline_days_left(settings, today)
    return left is None or left > 0


# ---------------------------------------------------------------- seeds and numbers


def seed_list(settings: Settings) -> list[str]:
    """The seeds, cleaned: strings, trimmed, no duplicates (case-insensitive), capped."""
    raw = settings.get("keywords.seeds")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        word = " ".join(item.split())
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            out.append(word)
    return out[: max(0, int(settings.get("keywords.max_seeds")))]


def jobs_total(data: Any) -> int | None:
    """`total_matching` of a find_jobs answer (top level, or under `data`). None if missing."""
    if not isinstance(data, dict):
        return None
    inner = data.get("data")
    for holder in (data, inner if isinstance(inner, dict) else {}):
        for key in TOTAL_JOB_KEYS:
            value = holder.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
    return None


def _jobs_call(org_uid: str, keyword: str, limit: int) -> tuple[str, dict[str, Any]]:
    return (
        jobs.TOOL,
        {
            "action": jobs.ACTION_SEARCH,
            "org_uid": org_uid,
            "params": {jobs.P_TITLE: keyword, jobs.P_LIMIT: limit},
        },
    )


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:160]


def gather(
    bridge: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    tz: tzinfo | str | None = None,
) -> list[dict[str, Any]]:
    """The numbers for every seed. One dict per seed:
    keyword, demand, competition, my_rank, my_organic_rank, rank_status (found | not_found |
    unknown), rank_pages_seen, errors. Raises KeywordsError (no account / no seeds) and what the
    bridge raises for a whole batch (login problem, Claude cap, rate limit)."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise KeywordsError("No freelancer account is chosen yet (see the Upwork page).")
    seeds = seed_list(settings)
    if not seeds:
        raise KeywordsError("The seed list is empty. Set the setting keywords.seeds.")

    limit = max(1, int(settings.get("keywords.jobs_limit")))
    rows = {
        k: {
            "keyword": k,
            "demand": None,
            "competition": None,
            "my_rank": None,
            "my_organic_rank": None,
            "rank_status": "unknown",
            "rank_pages_seen": 0,
            "errors": [],
        }
        for k in seeds
    }
    budget = getattr(bridge, "call_budget", None)
    with budget("keywords.max_mcp_calls") if budget else nullcontext():
        answers = bridge.call_many([_jobs_call(org_uid, k, limit) for k in seeds])
        for keyword, answer in zip(seeds, answers, strict=True):
            try:
                total = jobs_total(answer.unwrap())
            except UpworkAuthError:
                raise
            except Exception as e:  # one failed seed must not stop the others
                rows[keyword]["errors"].append(f"jobs: {_short(e)}")
                log.info("Demand search failed for %r: %s", keyword, e)
                continue
            rows[keyword]["demand"] = total
            if total is None:
                rows[keyword]["errors"].append("jobs: the answer has no total_matching")

        key_known = bool(rank_key(bridge, settings))
        result = rank.check_ranks(
            bridge,
            conn,
            settings,
            now,
            tz=tz,
            keywords=seeds,
            store=False,
            max_pages=int(settings.get("keywords.rank_pages")),
            allow_no_key=True,
        )
    for r in result.rows:
        row = rows.get(r["keyword"])
        if row is None:
            continue
        row["competition"] = r.get("total")
        row["rank_pages_seen"] = r["total_seen"]
        if key_known:
            row["my_rank"] = r["position"]
            row["my_organic_rank"] = r["organic_position"]
            row["rank_status"] = "found" if r["position"] is not None else "not_found"
        if r.get("total") is None:
            row["errors"].append("freelancers: the answer has no total")
    for keyword, reason in result.failed.items():
        if keyword in rows:
            rows[keyword]["errors"].append(f"freelancers: {reason}")
    return [rows[k] for k in seeds]


def rank_key(bridge: Any, settings: Settings) -> str | None:
    """My profile key, learned once from `get_profile get` when it is not known yet. A failed
    read is not an error here: the rank stays unknown, the other numbers are still gathered."""
    try:
        return metrics.ensure_profile_key(bridge, settings)
    except UpworkAuthError:
        raise
    except Exception as e:
        log.info("Own profile key not read: %s", e)
        return None


def usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Seeds with at least one real number (the others cannot be judged)."""
    return [r for r in rows if r["demand"] is not None or r["competition"] is not None]


# ---------------------------------------------------------------- the Claude pick


def _wrap(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    text = re.sub(rf"<(/?)\s*{DATA_TAG}", r"&lt;\1" + DATA_TAG, text, flags=re.IGNORECASE)
    return f"<{DATA_TAG}>\n{text}\n</{DATA_TAG}>"


def claude_input(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    fields = (
        "keyword",
        "demand",
        "competition",
        "my_rank",
        "rank_status",
        "rank_pages_seen",
    )
    return {
        "keywords": _wrap([{k: r[k] for k in fields} for r in rows]),
        "min_picks": int(settings.get("keywords.min_picks")),
        "max_picks": int(settings.get("keywords.max_picks")),
        "reason_max_chars": int(settings.get("keywords.reason_max_chars")),
    }


def validate_picks(output: Any, allowed: list[str], settings: Settings) -> list[dict[str, str]]:
    """Check Claude's answer. Returns [{keyword, reason}] (keyword spelled as in `allowed`).
    Unknown keywords and duplicates are dropped, extras beyond max_picks are cut, em dashes are
    cleaned. Fewer than min_picks left raises KeywordsError."""
    lookup = {a.casefold(): a for a in allowed}
    items = output.get("keywords") if isinstance(output, dict) else None
    if not isinstance(items, list):
        raise KeywordsError("Claude's answer had no keyword list.")
    min_picks = int(settings.get("keywords.min_picks"))
    max_picks = int(settings.get("keywords.max_picks"))
    reason_max = int(settings.get("keywords.reason_max_chars"))
    picks: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("keyword"), str):
            continue
        canon = lookup.get(" ".join(item["keyword"].split()).casefold())
        if canon is None or canon in seen:
            continue
        seen.add(canon)
        reason = item.get("reason")
        reason = prompting.no_em_dash(" ".join(reason.split())) if isinstance(reason, str) else ""
        picks.append({"keyword": canon, "reason": reason[:reason_max]})
        if len(picks) >= max_picks:
            break
    if len(picks) < min_picks:
        raise KeywordsError(
            f"Claude picked only {len(picks)} usable keyword(s); at least {min_picks} are needed. "
            "No approval item was made. Run it again."
        )
    return picks


def pick(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    rows: list[dict[str, Any]],
    run_id: str,
) -> list[dict[str, str]]:
    """One Claude call (think model). Cost goes to claude_calls under `run_id`."""
    if len(rows) < int(settings.get("keywords.min_picks")):
        raise KeywordsError(
            f"Only {len(rows)} seed(s) could be measured; at least "
            f"{int(settings.get('keywords.min_picks'))} are needed."
        )
    try:
        res = run_claude(
            CLAUDE_TASK,
            PROMPT,
            claude_input(rows, settings),
            str(settings.get("models.think")),
            int(settings.get("keywords.pick_max_turns")),
            float(settings.get("keywords.pick_max_budget_usd")),
            config=config,
            conn=conn,
            settings=settings,
            effort=settings.get("models.think_effort") or None,
            timeout=int(settings.get("keywords.pick_timeout_seconds")),
            prompt_text=prompting.build_prompt(PROMPT, config=config),
            record_run_id=run_id,
        )
    except ClaudeFailed as e:
        raise KeywordsError(f"Claude could not pick the keywords: {str(e)[:200]}") from e
    return validate_picks(res["output"], [r["keyword"] for r in rows], settings)


# ---------------------------------------------------------------- the approval item


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def create_item(
    conn: sqlite3.Connection,
    settings: Settings,
    run_id: str,
    picks: list[dict[str, str]],
    rows: list[dict[str, Any]],
    now: datetime,
) -> tuple[int, list[dict[str, Any]]]:
    """ONE approvals item. An older keywords item that is still pending is replaced (expired).
    Returns (item id, the picks with their numbers)."""
    by_key = {r["keyword"]: r for r in rows}
    shown = []
    for p in picks:
        r = by_key[p["keyword"]]
        shown.append(
            {
                "keyword": p["keyword"],
                "reason": p["reason"],
                "demand": r["demand"],
                "competition": r["competition"],
                "my_rank": r["my_rank"],
                "rank_status": r["rank_status"],
                "rank_pages_seen": r["rank_pages_seen"],
            }
        )
    conn.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ? "
        "WHERE kind = ? AND status = 'pending'",
        (_iso(now), KIND),
    )
    item_id = approvals.add(
        conn,
        KIND,
        f"Keywords to rank for ({len(picks)})",
        ref_id=run_id,
        before=rank.approved_keywords(conn, settings),
        after={
            "picks": shown,
            "numbers": [dict(r) for r in rows],
            "measured_at": _iso(now),
        },
        now=now,
    )
    return item_id, shown


def apply_decision(row: sqlite3.Row, decision: str, conn: sqlite3.Connection) -> None:
    """Approvals handler for kind 'keywords'. Approve saves the picks to `rank.keywords` and
    starts the baseline (today's date in the panel time zone). Reject records only (nothing to
    do here, the decision is already stored). A bad item raises, so it stays pending."""
    if decision != "approved":
        return
    try:
        after = json.loads(row["after_json"] or "")
        words = [p["keyword"] for p in after["picks"]]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("the item has no keyword list") from e
    words = [w for w in words if isinstance(w, str) and w.strip()]
    if not words:
        raise ValueError("the item has no keyword list")
    settings = Settings(conn)
    settings.set("rank.keywords", words)
    settings.set("keywords.baseline_started", datetime.now(_zone(settings)).date().isoformat())


approvals.register(KIND, apply_decision)


# ---------------------------------------------------------------- the email


def notify(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    item_id: int,
    picks: list[dict[str, Any]],
    now: datetime,
) -> str:
    """One email per approval item (deduped on the item id). Returns the mail.send_event result."""

    def num(v: Any) -> str:
        return "n/a" if v is None else str(v)

    items = []
    for p in picks:
        rank_text = (
            f"#{p['my_rank']}"
            if p["my_rank"] is not None
            else ("not in the first " + str(p["rank_pages_seen"]))
            if p["rank_status"] == "not_found"
            else "unknown"
        )
        items.append(
            {
                "title": p["keyword"],
                "meta": (
                    f"{num(p['demand'])} jobs, {num(p['competition'])} freelancers, "
                    f"your place: {rank_text}"
                ),
                "lines": [p["reason"]] if p["reason"] else [],
            }
        )
    text, html = mail.build_email(
        "Keywords picked for your profile",
        [
            f"I picked {len(picks)} keywords. Open the panel to approve or reject them.",
            "Approving starts a baseline: no profile change for "
            f"{int(settings.get('keywords.baseline_days'))} days, so the numbers stay comparable.",
        ],
        items=items,
        link_path="/approvals",
        base_url=config.public_base_url,
        button_label="Open approvals",
    )
    return mail.send_event(
        conn,
        EVENT,
        str(item_id),
        f"Keywords ready: {len(picks)} picked"[:150],
        text,
        html,
        config=config,
        settings=settings,
        now=now,
    )


# ---------------------------------------------------------------- the run


@dataclass
class KeywordsResult:
    run_id: str
    status: str  # ok | skipped | error | auth_expired
    item_id: int | None = None
    picks: list[dict[str, str]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    email: str | None = None
    error: str | None = None


def _start_run(conn: sqlite3.Connection, now: datetime) -> str:
    import uuid

    run_id = f"{TASK}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES (?,?,?, 'running')",
        (run_id, TASK, _iso(now)),
    )
    return run_id


def _end_run(conn: sqlite3.Connection, run_id: str, status: str, error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET ended = ?, status = ?, error = ? WHERE run_id = ?",
        (_iso(datetime.now(UTC)), status, error, run_id),
    )


def run_keywords(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    bridge: Any = None,
    *,
    now: datetime | None = None,
) -> KeywordsResult:
    """The whole task. Never raises: the outcome is in the result and in the runs row."""
    from app.upwork.bridge import ClaudeUpworkBridge

    now = now or datetime.now(UTC)
    run_id = _start_run(conn, now)
    result = KeywordsResult(run_id=run_id, status="running")
    if settings.get("general.kill_switch"):
        result.status, result.error = "skipped", "kill switch is on"
        _end_run(conn, run_id, result.status, result.error)
        return result

    owns = bridge is None
    try:
        if bridge is None:
            bridge = ClaudeUpworkBridge(config, settings, conn, run_id)
        rows = gather(bridge, conn, settings, now, tz=config.tz)
        result.rows = rows
        measured = usable(rows)
        picks = pick(conn, config, settings, measured, run_id)
        result.picks = picks
        result.item_id, shown = create_item(conn, settings, run_id, picks, measured, now)
        result.email = notify(conn, config, settings, result.item_id, shown, now)
        result.status = "ok"
    except UpworkAuthError as e:
        result.status, result.error = "auth_expired", str(e)
    except (BudgetCapReached, ClaudeRateLimited, KeywordsError) as e:
        result.status, result.error = "error", str(e)[:500]
    except Exception as e:
        log.exception("Keyword discovery failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:500]
    finally:
        if owns and bridge is not None:
            bridge.close()
    _end_run(conn, run_id, result.status, result.error)
    log.info("keywords %s: picks=%d item=%s", result.status, len(result.picks), result.item_id)
    return result
