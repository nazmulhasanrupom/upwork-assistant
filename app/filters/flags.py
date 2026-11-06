"""Red flags (SPEC 8.4). Shown to the user, never used to drop a job."""

from __future__ import annotations

from typing import Any

from app.filters.tiers import is_platform_default


def _g(v: float) -> str:
    return f"{v:g}"


def flags(job: dict[str, Any], settings: Any) -> list[str]:
    out: list[str] = []

    rating = job.get("client_rating")
    reviews = job.get("client_reviews")
    if (
        rating is not None
        and reviews is not None
        and reviews >= settings.get("flags.feedback_min_reviews")
        and rating < settings.get("flags.feedback_min")
    ):
        out.append(f"client rating {_g(rating)} ({reviews} reviews)")

    bad_max = settings.get("flags.bad_review_score_max")
    scores = [
        r["score"]
        for r in job.get("reviews") or []
        if isinstance(r, dict) and isinstance(r.get("score"), (int, float))
    ]
    bad = [s for s in scores if s <= bad_max]
    if bad:
        out.append(f"{_g(min(bad))}-star review")

    hire_rate = job.get("hire_rate")
    if hire_rate is not None and hire_rate < settings.get("flags.hire_rate_min"):
        out.append(f"hire rate {_g(hire_rate)}%")

    if job.get("proposals_tier") == settings.get("flags.bad_proposals_tier"):
        out.append(f"{job['proposals_tier']} proposals")

    cost, balance = job.get("connects_cost"), job.get("connects_balance")
    if cost is not None and balance is not None and cost > balance:
        out.append(f"needs {cost} Connects, you have {balance}")

    if is_platform_default(job):
        out.append("platform default rate")
    elif job.get("job_type") == "hourly" and job.get("budget_max") is None:
        out.append("no rate stated")

    if job.get("can_apply") is False:
        out.append("cannot apply")

    return out
