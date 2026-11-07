"""Find jobs on Upwork (SPEC 8.1), cut each one down to about 15 fields (SPEC 7.3 rule 2).

Real response shapes are in tests/fixtures/upwork/. Money is a string like "$13,692.95".
Many fields can be missing. Client text is wrapped in <untrusted_participant_content> tags
and the tags are kept on purpose.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from app.filters.activity import posted_text
from app.settings import Settings, load_defaults
from app.upwork.client import CallBudgetExceeded, ToolResult, UpworkToolError

log = logging.getLogger(__name__)

# --- Names of the MCP tool, actions, and params. Keep them here so they are easy to fix. ---
TOOL = "find_jobs"
ACTION_SMART = "smart_search"
ACTION_SEARCH = "search"
ACTION_GET = "get"
P_MODE = "mode"
P_DAYS = "days_posted"
P_VERIFIED = "verified_payment_only"
P_LIMIT = "limit"
P_TITLE = "title"  # keyword for action=search (name unverified)
P_JOB_ID = "job_id"
MODE_RECENT = "most_recent"
MODE_BEST = "best_match"

# Upwork error text (lower case) -> error kind, for the job alert task. First match wins.
KIND_RATE_LIMITED = "rate_limited"
KIND_COOLDOWN = "cooldown"
KIND_BUDGET = "budget"  # our own per-run call limit, not an Upwork error
KIND_ERROR = "error"  # any other failed call
COOLDOWN_PATTERNS = ("job_browse_cooldown",)
RATE_LIMIT_PATTERNS = ("too many requests", "429", "rate limit", "rate-limit", "rate_limit")

# Possible keys for the job budget (shape not yet seen in a real sample). First hit wins.
HOURLY_MIN_KEYS = ("hourly_budget_min", "hourly_rate_min", "hourlyBudgetMin")
HOURLY_MAX_KEYS = ("hourly_budget_max", "hourly_rate_max", "hourlyBudgetMax")

OPEN_TAG = "<untrusted_participant_content>"
CLOSE_TAG = "</untrusted_participant_content>"

_MONEY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


class JobsError(RuntimeError):
    pass


# ---------------------------------------------------------------- small parsers


def parse_money(value: Any) -> float | None:
    """'$13,692.95' -> 13692.95. Numbers pass through. Anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "rawValue", "displayValue", "value"):
            if key in value:
                return parse_money(value[key])
        return None
    if isinstance(value, str):
        m = _MONEY_RE.search(value)
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", ""))
        except ValueError:
            return None
    return None


def parse_budget(value: Any) -> tuple[float | None, float | None]:
    """'$8.00-$20.00' -> (8, 20); '$300.00' -> (300, 300); other -> (None, None)."""
    if isinstance(value, str):
        nums = [float(n.replace(",", "")) for n in _MONEY_RE.findall(value)]
        if len(nums) >= 2:
            return min(nums[0], nums[1]), max(nums[0], nums[1])
        if len(nums) == 1:
            return nums[0], nums[0]
        return None, None
    one = parse_money(value)
    return (one, one) if one is not None else (None, None)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_money(value)


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _setting(settings: Settings | None, key: str) -> Any:
    if settings is not None:
        return settings.get(key)
    return load_defaults()[key]


def truncate_untrusted(text: Any, max_chars: int) -> str | None:
    """Cut text to max_chars (the text inside the tags). Keeps the tags balanced."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    had_open = s.startswith(OPEN_TAG)
    inner = s
    if had_open:
        inner = s[len(OPEN_TAG) :]
        if inner.rstrip().endswith(CLOSE_TAG):
            inner = inner.rstrip()[: -len(CLOSE_TAG)]
    inner = inner.strip()
    if len(inner) > max_chars:
        inner = inner[:max_chars].rstrip() + "..."
    if had_open:
        return f"{OPEN_TAG}\n{inner}\n{CLOSE_TAG}"
    return inner


def _type(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    if v.startswith("hour"):
        return "hourly"
    if v.startswith("fixed"):
        return "fixed"
    return v


# ---------------------------------------------------------------- detail


def _activity(value: Any) -> dict[str, Any] | None:
    """job_activity as a plain dict, or None when the answer has none."""
    return dict(value) if isinstance(value, dict) else None


def _count(value: Any) -> int | None:
    return None if isinstance(value, bool) else _int(value)


def parse_detail(payload: Any) -> dict[str, Any]:
    """Flatten a `find_jobs get` answer into the few facts we use."""
    if not isinstance(payload, dict):
        raise JobsError("find_jobs get returned no object")
    status = payload.get("status")
    if status is not None and status != "ok":
        raise JobsError(f"find_jobs get status: {status}")
    posting = _dict(_dict(payload.get("data")).get("marketplaceJobPosting"))
    content = _dict(posting.get("content"))
    terms = _dict(posting.get("contractTerms"))
    hourly = _dict(terms.get("hourlyContractTerms"))
    fixed = _dict(terms.get("fixedPriceContractTerms"))
    record = _dict(payload.get("client_record"))
    feedback = _dict(payload.get("client_feedback"))

    hmin = _num(_first(hourly, HOURLY_MIN_KEYS))
    hmax = _num(_first(hourly, HOURLY_MAX_KEYS))
    fixed_amount = _num(fixed.get("amount"))

    reviews = []
    for r in feedback.get("reviews") or []:
        if isinstance(r, dict):
            reviews.append({"score": _num(r.get("score")), "comment": r.get("comment")})

    questions = payload.get("screening_questions")
    can_apply = payload.get("can_apply")
    return {
        "job_id": str(posting["id"]) if posting.get("id") is not None else None,
        "title": content.get("title"),
        "url": posting.get("url"),
        "description": content.get("description"),
        "job_type": _type(terms.get("contractType")),
        "experience_level": terms.get("experienceLevel"),
        "hourly_budget_type": hourly.get("hourlyBudgetType"),
        "budget_min": hmin,
        "budget_max": hmax,
        "fixed_budget": fixed_amount,
        "client_country": _dict(_dict(posting.get("clientCompanyPublic")).get("country")).get(
            "name"
        ),
        "client_spend": parse_money(record.get("spend_total")),
        "client_hours": _num(record.get("hours_total")),
        "client_hires": _int(record.get("contracts_total")),
        "client_rating": _num(record.get("feedback_score")),
        "client_reviews": _int(record.get("feedback_count")),
        "hire_rate": _num(record.get("hire_rate_percent")),
        "connects_cost": _int(payload.get("connects_cost")),
        "connects_balance": _int(payload.get("connects_balance")),
        "can_apply": can_apply if isinstance(can_apply, bool) else None,
        "plan": payload.get("plan"),
        "proposals_tier_unavailable": bool(payload.get("proposals_tier_unavailable")),
        "applied": payload.get("applied") if isinstance(payload.get("applied"), bool) else None,
        "proposals_tier": payload.get("proposals_tier"),
        "job_activity": _activity(payload.get("job_activity")),
        "screening_questions": [q for q in (questions or []) if isinstance(q, str)],
        "reviews": reviews,
    }


def detail_call(job_id: str, org_uid: str) -> tuple[str, dict[str, Any]]:
    return TOOL, {"action": ACTION_GET, "org_uid": org_uid, "params": {P_JOB_ID: str(job_id)}}


def _parse_detail_result(res: ToolResult) -> dict[str, Any] | Exception:
    """A parsed detail, or the exception the caller should handle (never raised here)."""
    try:
        return parse_detail(res.unwrap())
    except (UpworkToolError, CallBudgetExceeded, JobsError) as e:
        return e


def fetch_details(
    client: Any, job_ids: list[str], org_uid: str | None = None
) -> list[dict[str, Any] | Exception]:
    """`find_jobs get` for many jobs in one batch (client.call_many).

    One entry per job id, in order: the parsed detail, or an exception object for that job
    (UpworkToolError, CallBudgetExceeded, JobsError). Auth and Claude errors are raised.
    """
    if not job_ids:
        return []
    org = org_uid or _org_uid_from(client)
    results = client.call_many([detail_call(j, org) for j in job_ids])
    return [_parse_detail_result(r) for r in results]


def fetch_detail(client: Any, job_id: str, org_uid: str | None = None) -> dict[str, Any]:
    """One `find_jobs get` call. Returns the parsed detail or raises its error.

    org_uid defaults to the talent org_uid in the client's settings.
    """
    out = fetch_details(client, [job_id], org_uid)[0]
    if isinstance(out, Exception):
        raise out
    return out


def _org_uid_from(client: Any) -> str:
    org = getattr(getattr(client, "settings", None), "get", lambda k: "")("upwork.talent_org_uid")
    if not org:
        raise JobsError("upwork.talent_org_uid is not set")
    return str(org)


# ---------------------------------------------------------------- cut down


def _budget_type(detail: dict[str, Any], search_job: dict[str, Any]) -> Any:
    """Detail wins, except that a search result saying "platform default" is kept:
    that range is Upwork's, not the client's rate (see tests/fixtures/upwork/README.md)."""
    from_search = search_job.get("hourly_budget_type")
    if isinstance(from_search, str) and from_search.lower().startswith("platform default"):
        return from_search
    return detail.get("hourly_budget_type") or from_search


def cut_down(
    search_job: dict[str, Any],
    detail: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Make the small job dict used everywhere after the search.

    `detail` is the result of fetch_detail (or a raw `get` answer). Detail wins over search.
    """
    if detail and ("client_record" in detail or "data" in detail):
        detail = parse_detail(detail)
    detail = detail or {}
    sj = _dict(search_job)
    client = _dict(sj.get("client"))
    max_chars = int(_setting(settings, "search.description_max_chars"))
    comment_chars = int(_setting(settings, "search.review_comment_chars"))
    max_reviews = int(_setting(settings, "search.max_reviews"))

    job_type = detail.get("job_type") or _type(sj.get("job_type"))

    # Budget. Fixed jobs have `budget` ("$300.00"). Hourly may have a range or none.
    budget_min = budget_max = fixed_budget = None
    if job_type == "fixed":
        fixed_budget = detail.get("fixed_budget")
        if fixed_budget is None:
            fixed_budget = parse_money(sj.get("budget"))
    else:
        budget_min = detail.get("budget_min")
        budget_max = detail.get("budget_max")
        if budget_min is None and budget_max is None:
            budget_min = _num(_first(sj, HOURLY_MIN_KEYS))
            budget_max = _num(_first(sj, HOURLY_MAX_KEYS))
        if budget_min is None and budget_max is None and sj.get("budget") is not None:
            budget_min, budget_max = parse_budget(sj.get("budget"))
        if budget_min is None:
            budget_min = budget_max
        if budget_max is None:
            budget_max = budget_min

    def pick(detail_key: str, search_value: Any) -> Any:
        v = detail.get(detail_key)
        return v if v is not None else search_value

    description = truncate_untrusted(
        detail.get("description") or sj.get("description_snippet"), max_chars
    )

    reviews = []
    for r in (detail.get("reviews") or [])[:max_reviews]:
        reviews.append(
            {
                "score": r.get("score"),
                "comment": truncate_untrusted(r.get("comment"), comment_chars),
            }
        )

    questions = [truncate_untrusted(q, max_chars) for q in detail.get("screening_questions") or []]

    verification = client.get("verification_status")
    payment_verified = None if verification is None else str(verification).upper() == "VERIFIED"

    return {
        "id": str(pick("job_id", sj.get("id"))) if pick("job_id", sj.get("id")) else None,
        "title": pick("title", sj.get("title")),
        "url": pick("url", sj.get("url")),
        "job_type": job_type,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "fixed_budget": fixed_budget,
        "hourly_budget_type": _budget_type(detail, sj),
        "experience_level": pick("experience_level", sj.get("experience_level")),
        "duration": sj.get("duration"),
        "proposals_tier": pick("proposals_tier", sj.get("proposals_tier")),
        "proposal_count": _count(sj.get("proposal_count")),
        "job_activity": pick("job_activity", _activity(sj.get("job_activity"))),
        "published_date": sj.get("published_date"),
        "created_date": sj.get("created_date"),
        "freelancers_to_hire": _count(sj.get("freelancers_to_hire")),
        "applied": detail.get("applied"),
        "skills": [s for s in sj.get("skills") or [] if isinstance(s, str)],
        "payment_verified": payment_verified,
        "client_country": pick("client_country", client.get("country")),
        "client_spend": pick("client_spend", parse_money(client.get("total_spent"))),
        "client_hours": detail.get("client_hours"),
        "client_hires": pick("client_hires", _int(client.get("total_hires"))),
        "client_rating": pick("client_rating", _num(client.get("rating"))),
        "client_reviews": pick("client_reviews", _int(client.get("total_reviews"))),
        "hire_rate": detail.get("hire_rate"),
        "connects_cost": detail.get("connects_cost"),
        "connects_balance": detail.get("connects_balance"),
        "can_apply": detail.get("can_apply"),
        "description": description,
        "screening_questions": questions,
        "reviews": reviews,
    }


def apply_detail(
    job: dict[str, Any], detail: dict[str, Any], settings: Settings | None = None
) -> dict[str, Any]:
    """Overlay a parsed `find_jobs get` answer on a cut-down search job. Returns a new dict.

    Same rules as cut_down: detail wins when it has a value; text is cut to the same sizes.
    """
    max_chars = int(_setting(settings, "search.description_max_chars"))
    comment_chars = int(_setting(settings, "search.review_comment_chars"))
    max_reviews = int(_setting(settings, "search.max_reviews"))
    out = dict(job)
    for key in (
        "title",
        "url",
        "job_type",
        "experience_level",
        "client_country",
        "client_spend",
        "client_hours",
        "client_hires",
        "client_rating",
        "client_reviews",
        "hire_rate",
        "connects_cost",
        "connects_balance",
        "can_apply",
        "fixed_budget",
        "applied",
        "proposals_tier",
        "job_activity",
    ):
        if detail.get(key) is not None:
            out[key] = detail[key]
    if detail.get("budget_min") is not None or detail.get("budget_max") is not None:
        bmin, bmax = detail.get("budget_min"), detail.get("budget_max")
        out["budget_min"] = bmin if bmin is not None else bmax
        out["budget_max"] = bmax if bmax is not None else bmin
    out["hourly_budget_type"] = _budget_type(
        detail, {"hourly_budget_type": job.get("hourly_budget_type")}
    )
    description = truncate_untrusted(detail.get("description"), max_chars)
    if description:
        out["description"] = description
    if detail.get("screening_questions"):
        out["screening_questions"] = [
            truncate_untrusted(q, max_chars) for q in detail["screening_questions"]
        ]
    if detail.get("reviews"):
        out["reviews"] = [
            {
                "score": r.get("score"),
                "comment": truncate_untrusted(r.get("comment"), comment_chars),
            }
            for r in detail["reviews"][:max_reviews]
        ]
    return out


# ---------------------------------------------------------------- search


def _search_jobs(res: ToolResult, action: str) -> list[dict[str, Any]]:
    try:
        payload = res.unwrap()
    except (UpworkToolError, CallBudgetExceeded) as e:
        log.warning("find_jobs %s failed: %s", action, e)
        return []
    if not isinstance(payload, dict):
        return []
    status = payload.get("status")
    if status is not None and status != "ok":
        log.warning("find_jobs %s status: %s", action, status)
        return []
    jobs = payload.get("jobs")
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def tool_error_kind(text: Any) -> str | None:
    """ "cooldown" or "rate_limited" if Upwork error text says so, else None."""
    low = str(text or "").lower()
    if any(p in low for p in COOLDOWN_PATTERNS):
        return KIND_COOLDOWN
    if any(p in low for p in RATE_LIMIT_PATTERNS):
        return KIND_RATE_LIMITED
    return None


def error_kind_of(err: BaseException) -> str:
    """Kind for an exception from a call: cooldown, rate_limited, budget or error."""
    if isinstance(err, CallBudgetExceeded):
        return KIND_BUDGET
    return tool_error_kind(err) or KIND_ERROR


def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT job_id FROM jobs WHERE job_id IN ({marks})", chunk):
            found.add(row["job_id"])
    return found


def fetch_candidates(
    client: Any, settings: Settings, conn: sqlite3.Connection
) -> list[dict[str, Any]]:
    """SPEC 8.1: recent + best match + one search per keyword. Merge, dedupe, skip seen.

    Returns cut-down jobs (search data only, no detail).
    """
    org_uid = settings.get("upwork.talent_org_uid")
    if not org_uid:
        raise JobsError("upwork.talent_org_uid is not set")
    verified = bool(settings.get("search.verified_payment_only"))

    plans: list[tuple[str, dict[str, Any]]] = [
        (
            ACTION_SMART,
            {
                P_MODE: MODE_RECENT,
                P_DAYS: int(settings.get("search.recent_days_posted")),
                P_VERIFIED: verified,
                P_LIMIT: int(settings.get("search.recent_limit")),
            },
        ),
        (
            ACTION_SMART,
            {
                P_MODE: MODE_BEST,
                P_VERIFIED: verified,
                P_LIMIT: int(settings.get("search.best_match_limit")),
            },
        ),
    ]
    keywords = [k for k in settings.get("search.keywords") if isinstance(k, str) and k.strip()]
    for kw in keywords[: int(settings.get("search.max_keywords"))]:
        plans.append(
            (
                ACTION_SEARCH,
                {
                    P_TITLE: kw.strip(),
                    P_VERIFIED: verified,
                    P_LIMIT: int(settings.get("search.keyword_limit")),
                },
            )
        )

    merged: dict[str, dict[str, Any]] = {}
    results = client.call_many(  # all searches in one batch
        [
            (TOOL, {"action": action, "org_uid": str(org_uid), "params": params})
            for action, params in plans
        ]
    )
    for (action, _), res in zip(plans, results, strict=True):
        for job in _search_jobs(res, action):
            jid = job.get("id")
            if jid is None:
                continue
            merged.setdefault(str(jid), job)

    seen = known_ids(conn, list(merged))
    return [cut_down(j, settings=settings) for jid, j in merged.items() if jid not in seen]


def fetch_recent(
    client: Any, settings: Settings, conn: sqlite3.Connection
) -> tuple[list[dict[str, Any]], str | None]:
    """One most-recent smart_search for the job alert task. Returns (new jobs, error kind).

    Unlike fetch_candidates it does not swallow errors. The kind is "rate_limited" or
    "cooldown" (Upwork said so), "budget" (our per-run call limit stopped the call) or
    "error" (any other tool error or a non-ok status). On any error the job list is empty.
    New jobs are cut down (search data only) and skip ids already in the jobs table.
    """
    org_uid = settings.get("upwork.talent_org_uid")
    if not org_uid:
        raise JobsError("upwork.talent_org_uid is not set")
    params = {
        P_MODE: MODE_RECENT,
        P_DAYS: int(settings.get("job_alerts.days_posted")),
        P_VERIFIED: bool(settings.get("search.verified_payment_only")),
        P_LIMIT: int(settings.get("job_alerts.search_limit")),
    }
    res = client.call_many(
        [(TOOL, {"action": ACTION_SMART, "org_uid": str(org_uid), "params": params})]
    )[0]
    try:
        payload = res.unwrap()
    except (UpworkToolError, CallBudgetExceeded) as e:
        kind = error_kind_of(e)
        log.warning("find_jobs %s failed (%s): %s", ACTION_SMART, kind, e)
        return [], kind
    if not isinstance(payload, dict):
        return [], tool_error_kind(payload) or (KIND_ERROR if payload else None)
    status = payload.get("status")
    if status is not None and status != "ok":
        kind = tool_error_kind(json.dumps(payload, default=str)) or KIND_ERROR
        log.warning("find_jobs %s status %s (%s)", ACTION_SMART, status, kind)
        return [], kind
    merged: dict[str, dict[str, Any]] = {}
    jobs = payload.get("jobs")
    for job in [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []:
        jid = job.get("id")
        if jid is not None:
            merged.setdefault(str(jid), job)
    seen = known_ids(conn, list(merged))
    new = [cut_down(j, settings=settings) for jid, j in merged.items() if jid not in seen]
    return new, None


def fetch_details_until_stop(
    client: Any, job_ids: list[str], org_uid: str | None = None
) -> tuple[list[dict[str, Any] | Exception], str | None]:
    """Detail reads one at a time, stopping after the first cooldown / rate limit / budget stop.

    Returns (results, stop_kind). `results` has one entry per job tried, in order (a parsed
    detail or the exception for that job), so it may be shorter than `job_ids`. `stop_kind`
    is "cooldown", "rate_limited" or "budget" when that ended the loop, else None. Other
    per-job errors are in `results` and do not stop the loop.
    """
    out: list[dict[str, Any] | Exception] = []
    for jid in job_ids:
        res = fetch_details(client, [jid], org_uid)[0]
        out.append(res)
        if isinstance(res, Exception):
            kind = error_kind_of(res)
            if kind in (KIND_COOLDOWN, KIND_RATE_LIMITED, KIND_BUDGET):
                return out, kind
    return out, None


# ---------------------------------------------------------------- save


def iso_week(day: date | datetime | None = None) -> str:
    """ISO week label, for example 2026-W45."""
    if day is None:
        day = datetime.now(UTC)
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def save_job(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    *,
    status: str = "seen",
    tier: str | None = None,
    tier_estimated: bool = False,
    rate_paid: float | None = None,
    red_flags: list[str] | None = None,
    drop_reason: str | None = None,
    activity_label: str | None = None,
) -> None:
    """Insert or update one row in `jobs`. first_seen is kept on update.

    Fixed jobs store their price in both budget_min and budget_max.
    """
    if job.get("job_type") == "fixed" and job.get("fixed_budget") is not None:
        bmin = bmax = job["fixed_budget"]
    else:
        bmin, bmax = job.get("budget_min"), job.get("budget_max")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO jobs(job_id, first_seen, title, url, job_type, budget_min, budget_max,
            client_spend, client_hours, client_hires, client_rating, client_reviews,
            rate_paid, tier, tier_estimated, proposals_tier, connects_cost, red_flags, status,
            drop_reason, hourly_budget_type, details, activity_label, posted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_id) DO UPDATE SET
            title=excluded.title, url=excluded.url, job_type=excluded.job_type,
            budget_min=excluded.budget_min, budget_max=excluded.budget_max,
            client_spend=excluded.client_spend, client_hours=excluded.client_hours,
            client_hires=excluded.client_hires, client_rating=excluded.client_rating,
            client_reviews=excluded.client_reviews, rate_paid=excluded.rate_paid,
            tier=excluded.tier, tier_estimated=excluded.tier_estimated,
            proposals_tier=excluded.proposals_tier, connects_cost=excluded.connects_cost,
            red_flags=excluded.red_flags, status=excluded.status,
            drop_reason=excluded.drop_reason, hourly_budget_type=excluded.hourly_budget_type,
            details=excluded.details,
            activity_label=COALESCE(excluded.activity_label, jobs.activity_label),
            posted_at=COALESCE(excluded.posted_at, jobs.posted_at)
        """,
        (
            str(job["id"]),
            now,
            job.get("title"),
            job.get("url"),
            job.get("job_type"),
            bmin,
            bmax,
            job.get("client_spend"),
            job.get("client_hours"),
            job.get("client_hires"),
            job.get("client_rating"),
            job.get("client_reviews"),
            rate_paid,
            tier,
            1 if tier_estimated else 0,
            job.get("proposals_tier"),
            job.get("connects_cost"),
            json.dumps(red_flags or []),
            status,
            drop_reason,
            job.get("hourly_budget_type"),
            json.dumps(job, ensure_ascii=False),
            activity_label,
            posted_text(job),
        ),
    )


def save_skills(
    conn: sqlite3.Connection,
    job_id: str,
    skills: list[str],
    week: str | None = None,
) -> int:
    """Write skills_seen rows (week = ISO week). Returns how many were new."""
    week = week or iso_week()
    new = 0
    for skill in dict.fromkeys(s.strip() for s in skills if isinstance(s, str) and s.strip()):
        cur = conn.execute(
            "INSERT OR IGNORE INTO skills_seen(job_id, skill, week) VALUES (?,?,?)",
            (str(job_id), skill, week),
        )
        new += cur.rowcount
    return new
