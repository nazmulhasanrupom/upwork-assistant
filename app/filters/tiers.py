"""Pre-filter (SPEC 8.2) and tier rule (SPEC 8.3). Pure functions; all numbers from settings.

`settings` is anything with `.get("section.key")` (the Settings class).
Fields come from app.upwork.jobs.cut_down.
"""

from __future__ import annotations

from typing import Any

PLATFORM_DEFAULT = "platform default"  # seen as "platform default range" in search results


def is_platform_default(job: dict[str, Any]) -> bool:
    """True when the hourly range is Upwork's default, not the client's own rate."""
    kind = job.get("hourly_budget_type")
    return (
        job.get("job_type") == "hourly"
        and isinstance(kind, str)
        and kind.strip().lower().replace("_", " ").startswith(PLATFORM_DEFAULT)
    )


def known_budget_max(job: dict[str, Any]) -> float | None:
    """Hourly budget max the client really chose. None when unknown or Upwork's default.

    The numbers stay on the job for display; filters and tiers must use this instead.
    """
    if is_platform_default(job):
        return None
    return job.get("budget_max")


def prefilter(job: dict[str, Any], settings: Any) -> tuple[bool, str]:
    """Search data only. Returns (keep, reason). 'Already seen' is handled in fetch_candidates."""
    if job.get("payment_verified") is False:
        return False, "payment not verified"
    spend = job.get("client_spend")
    if spend is None:
        return False, "no spend data"
    good = settings.get("pay.good_spending")
    if spend < good:
        return False, f"client spend {spend:g} below {good}"
    if job.get("job_type") == "hourly":
        bmax = known_budget_max(job)
        floor = settings.get("pay.hourly_budget_min")
        if bmax is not None and bmax < floor:
            return False, f"hourly budget max {bmax:g} below {floor}"
        # no rate stated: keep (the rate is judged later)
    elif job.get("job_type") == "fixed":
        fixed = job.get("fixed_budget")
        floor = settings.get("pay.fixed_budget_min")
        if fixed is not None and fixed < floor:
            return False, f"fixed budget {fixed:g} below {floor}"
    return True, "ok"


def _by_rate(rate: float, settings: Any) -> str:
    # No gaps: LOW < mid_min <= MID < high_min <= HIGH
    if rate < settings.get("pay.mid_rate_min"):
        return "LOW"
    if rate < settings.get("pay.high_rate_min"):
        return "MID"
    return "HIGH"


def tier(job: dict[str, Any], settings: Any) -> tuple[str, bool, float | None]:
    """Returns (tier, estimated, rate_paid).

    rate_paid = spend / hours. When hours is 0 or unknown, the job's own budget is used
    and estimated is True. If even the budget is unknown, the answer is LOW (estimated).
    """
    spend = job.get("client_spend")
    hours = job.get("client_hours")
    if spend is None or spend < settings.get("pay.good_spending"):
        rate = spend / hours if spend is not None and hours else None
        return "LOW", False, rate

    is_hourly = job.get("job_type") == "hourly"
    bmax = known_budget_max(job)
    if is_hourly and bmax is not None and bmax < settings.get("pay.hourly_budget_min"):
        rate = spend / hours if hours else None
        return "LOW", not hours, rate

    if hours and hours > 0:
        rate = spend / hours
        return _by_rate(rate, settings), False, rate

    # No hours yet (for example a fixed-price-only client): use the job budget.
    if is_hourly:
        if bmax is None:
            return "LOW", True, None
        return _by_rate(bmax, settings), True, bmax
    fixed = job.get("fixed_budget")
    if fixed is None:
        return "LOW", True, None
    if fixed >= settings.get("pay.fixed_budget_min"):
        return "MID", True, None
    return "LOW", True, None
