"""Job activity filter (plan F1). Pure functions; all numbers from settings.

Reads what search and `get` already gave us (job_activity, applied, dates, proposals).
Never raises on missing or odd data: unknown parts are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

HIDE = "HIDE"
SUSPICIOUS = "SUSPICIOUS"
POTENTIAL = "POTENTIAL"
GOOD = "GOOD"
NEUTRAL = "NEUTRAL"
UNKNOWN = "UNKNOWN"

# Best first. SUSPICIOUS goes last; HIDE jobs are dropped, so it is not listed.
ORDER = [POTENTIAL, GOOD, NEUTRAL, UNKNOWN, SUSPICIOUS]
LABELS = [HIDE, *ORDER]
RANK = {label: i for i, label in enumerate(ORDER)}

RULE_APPLIED = "applied"
RULE_FILLED = "positions filled"
RULE_CROWDED = "old job with many proposals"


def order_rank(label: str | None) -> int:
    """Sort key for a label. Unknown or empty text sorts like UNKNOWN. HIDE sorts last."""
    if label in RANK:
        return RANK[label]
    if label == HIDE:
        return len(ORDER)
    return RANK[UNKNOWN]


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def tier_minimums(settings: Any) -> dict[str, int]:
    """Parse the "tier text=minimum" list from settings into {normalized tier: minimum}."""
    out: dict[str, int] = {}
    raw = settings.get("activity.tier_min")
    for item in raw if isinstance(raw, list) else []:
        text, sep, number = str(item).rpartition("=")
        low = _int(number)
        if sep and low is not None and text.strip():
            out[text.strip().lower()] = low
    return out


def proposals(job: dict[str, Any], settings: Any) -> int:
    """Exact count when present, else the low end of the proposals tier, else 0."""
    activity = job.get("job_activity")
    for source in (job, activity if isinstance(activity, dict) else {}):
        count = _int(source.get("proposal_count"))
        if count is not None:
            return max(count, 0)
    tier = job.get("proposals_tier")
    if isinstance(tier, str):
        return tier_minimums(settings).get(" ".join(tier.split()).lower(), 0)
    return 0


def posted_datetime(job: dict[str, Any]) -> datetime | None:
    """published_date, else created_date, as an aware UTC datetime. None when missing or bad."""
    for key in ("published_date", "created_date"):
        raw = job.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=UTC)
    return None


def age_hours(job: dict[str, Any], now: datetime | None = None) -> float | None:
    when = posted_datetime(job)
    if when is None:
        return None
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - when).total_seconds() / 3600


def classify(job: dict[str, Any], settings: Any, now: datetime | None = None) -> tuple[str, str]:
    """Returns (label, rule). The rule text is only filled for HIDE."""
    if job.get("applied") is True:
        return HIDE, RULE_APPLIED
    activity = job.get("job_activity")
    if not isinstance(activity, dict):
        return UNKNOWN, ""
    hide_age = float(settings.get("activity.hide_age_hours"))
    fresh = float(settings.get("activity.fresh_hours"))
    crowd = int(settings.get("activity.hide_min_proposals"))

    seats = _int(job.get("freelancers_to_hire")) or 1
    taken = (_int(activity.get("total_hired")) or 0) + (_int(activity.get("total_offered")) or 0)
    if taken >= seats:
        return HIDE, RULE_FILLED
    count = proposals(job, settings)
    age = age_hours(job, now)
    if age is not None and age > hide_age and count >= crowd:
        return HIDE, RULE_CROWDED
    if (_int(activity.get("invited_to_interview")) or 0) > 0:
        return POTENTIAL, ""
    if age is not None and age > hide_age and count > 0:
        return SUSPICIOUS, ""
    if age is not None and age < fresh and count < crowd:
        return GOOD, ""
    return NEUTRAL, ""


def label_job(job: dict[str, Any], settings: Any, now: datetime | None = None) -> str:
    return classify(job, settings, now)[0]


def posted_text(job: dict[str, Any]) -> str | None:
    """posted date as stored text (ISO, UTC) or None."""
    when = posted_datetime(job)
    return when.isoformat(timespec="seconds") if when else None
