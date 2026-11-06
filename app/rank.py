"""Keyword rank (plan X2, SPEC2 4.C). Where does my profile show up in Upwork's freelancer search?

Once per local day (behind `rank.enabled` and `rank.run_hour_local`) `check_ranks` runs
`find_freelancers action=search {query, limit, offset}` for every keyword and looks for my
profile key. All keywords share the bridge batches: page 1 for every keyword in one batch, then
page 2 only for the keywords not found yet, and so on up to `rank.max_pages`.

- `position`: my 1-based place, every row counted (a boosted row is a paid ad but it still
  takes a place on the page).
- `organic_position`: the same, with boosted rows skipped.
- NULL for both = not found in the pages checked (`pages_checked`, `total_seen` say how far we
  looked). That is not an error and it is not "rank 0".

The profile key is the one the metrics snapshot keeps in `metrics.profile_key`. It is never
guessed here. Only read tools are used. Nothing goes to Upwork except search queries.

Keyword source: `approved_keywords`. Today it returns the setting `rank.keywords`; Phase KW
swaps in the keywords the owner approved.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.settings import Settings

log = logging.getLogger("rank")

ALERT_KEY = "rank_failed"
TOOL = "find_freelancers"
AVERAGE_DAYS = 7  # the days in the name of avg_7d; the dashboard passes rank.average_days


class RankSkipped(Exception):
    """The check cannot run (no account, no profile key). The message is for the owner."""


# ---------------------------------------------------------------- small helpers


def _zone(tz: tzinfo | str | None, settings: Settings) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(tz or str(settings.get("general.timezone")))
    except Exception:
        return UTC


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def approved_keywords(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """The keywords to check. HOOK for Phase KW1: swap the body for the approved keywords.
    Today: the setting `rank.keywords` (cleaned: strings, trimmed, no duplicates, capped)."""
    raw = settings.get("rank.keywords")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        word = " ".join(item.split())
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            out.append(word)
    return out[: max(0, int(settings.get("rank.max_keywords")))]


# ---------------------------------------------------------------- when to run


def is_due(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    tz: tzinfo | str | None = None,
) -> bool:
    """True once per local day: enabled, the local hour is reached, no row for today yet."""
    if not settings.get("rank.enabled"):
        return False
    local = now.astimezone(_zone(tz, settings))
    if local.hour < int(settings.get("rank.run_hour_local")):
        return False
    row = conn.execute(
        "SELECT 1 FROM rank_daily WHERE date = ? LIMIT 1", (local.date().isoformat(),)
    ).fetchone()
    return row is None


# ---------------------------------------------------------------- parsing one page


@dataclass
class _State:
    keyword: str
    offset: int = 0
    pages: int = 0
    seen: int = 0
    boosted: int = 0
    done: bool = False
    position: int | None = None
    organic: int | None = None


def _scan(state: _State, rows: list[Any], my_key: str) -> bool:
    """Walk one page of results. Returns True when my profile is in it."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        state.seen += 1
        boosted = row.get("boosted") is True
        if str(row.get("profile_key") or "").strip() == my_key:
            state.position = state.seen
            state.organic = state.seen - state.boosted
            return True
        if boosted:
            state.boosted += 1
    return False


def _next_offset(data: dict[str, Any], state: _State, rows: list[Any]) -> int | None:
    """Where the next page starts, or None when there is no next page."""
    if data.get("hasMore") is not True or not rows:
        return None
    nxt = data.get("next_offset")
    if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt > state.offset:
        return nxt
    return state.offset + len(rows)


# ---------------------------------------------------------------- the check


@dataclass
class RankResult:
    date: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # keyword -> short reason

    def lines(self) -> list[str]:
        if not self.rows and not self.failed:
            return []
        parts = []
        for r in self.rows:
            if r["position"] is None:
                parts.append(f"{r['keyword']}: not in the first {r['total_seen']}")
            else:
                parts.append(f"{r['keyword']}: #{r['position']} (organic #{r['organic_position']})")
        line = f"Rank {self.date}: " + "; ".join(parts) if parts else f"Rank {self.date}: none"
        if self.failed:
            line += f". Not checked: {', '.join(self.failed)}"
        return [line]


def _search_call(org_uid: str, state: _State, page_size: int) -> tuple[str, dict[str, Any]]:
    return (
        TOOL,
        {
            "action": "search",
            "org_uid": org_uid,
            "params": {"query": state.keyword, "limit": page_size, "offset": state.offset},
        },
    )


def _store(conn: sqlite3.Connection, day: str, state: _State, now: datetime) -> dict[str, Any]:
    row = {
        "date": day,
        "keyword": state.keyword,
        "position": state.position,
        "organic_position": state.organic,
        "pages_checked": state.pages,
        "total_seen": state.seen,
        "created_at": _iso(now),
    }
    conn.execute(
        "INSERT INTO rank_daily(date, keyword, position, organic_position, pages_checked, "
        "total_seen, created_at) VALUES (:date, :keyword, :position, :organic_position, "
        ":pages_checked, :total_seen, :created_at) "
        "ON CONFLICT(date, keyword) DO UPDATE SET position = excluded.position, "
        "organic_position = excluded.organic_position, pages_checked = excluded.pages_checked, "
        "total_seen = excluded.total_seen",
        row,
    )
    return row


def check_ranks(
    bridge: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    tz: tzinfo | str | None = None,
) -> RankResult:
    """Look up my place for every keyword and store one row per keyword for the local day.

    A keyword whose search fails is NOT stored (no row is better than a false "not found") and
    is listed in `failed`. Raises RankSkipped (no account / no profile key) and whatever the
    bridge raises for the whole batch (login problem, Claude cap, rate limit); keywords that
    finished before that are already stored."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise RankSkipped("No freelancer account is chosen yet (see the Upwork page).")
    my_key = str(settings.get("metrics.profile_key") or "").strip()
    if not my_key:
        raise RankSkipped(
            "Your profile key is not known yet and could not be read from your Upwork "
            "profile. The next daily run tries again."
        )
    keywords = approved_keywords(conn, settings)
    day = now.astimezone(_zone(tz, settings)).date().isoformat()
    result = RankResult(date=day)
    if not keywords:
        return result

    page_size = max(1, int(settings.get("rank.page_size")))
    max_pages = max(1, int(settings.get("rank.max_pages")))
    states = [_State(k) for k in keywords]

    def finish(state: _State) -> None:
        state.done = True
        result.rows.append(_store(conn, day, state, now))

    for _page in range(max_pages):
        active = [s for s in states if not s.done]
        if not active:
            break
        answers = bridge.call_many([_search_call(org_uid, s, page_size) for s in active])
        for state, answer in zip(active, answers, strict=True):
            state.pages += 1
            try:
                data = answer.unwrap()
                rows = data.get("freelancers") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("the search answer has no freelancers list")
            except Exception as e:  # one bad keyword must not stop the others
                state.done = True
                result.failed[state.keyword] = f"{type(e).__name__}: {e}"[:200]
                log.info("Rank search failed for %r: %s", state.keyword, e)
                continue
            if _scan(state, rows, my_key):
                finish(state)
                continue
            nxt = _next_offset(data, state, rows)
            if nxt is None or state.pages >= max_pages:
                finish(state)  # not found in what exists / what we are willing to read
            else:
                state.offset = nxt
    return result


# ---------------------------------------------------------------- averages


def avg_7d(
    conn: sqlite3.Connection,
    keyword: str,
    end_date: date,
    *,
    days: int = AVERAGE_DAYS,
    organic: bool = False,
) -> float | None:
    """Average position of a keyword over the `days` days ending on `end_date` (inclusive).
    NULL days (not found) are ignored. None when there is no position at all."""
    column = "organic_position" if organic else "position"
    first = end_date - timedelta(days=max(1, int(days)) - 1)
    value = conn.execute(
        f"SELECT AVG({column}) FROM rank_daily WHERE keyword = ? AND date >= ? AND date <= ?",
        (keyword, first.isoformat(), end_date.isoformat()),
    ).fetchone()[0]
    return None if value is None else float(value)


def not_found_count(
    conn: sqlite3.Connection, keyword: str, end_date: date, *, days: int = AVERAGE_DAYS
) -> int:
    """Days in the window where the keyword was checked and my profile was not found."""
    first = end_date - timedelta(days=max(1, int(days)) - 1)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM rank_daily "
            "WHERE keyword = ? AND date >= ? AND date <= ? AND position IS NULL",
            (keyword, first.isoformat(), end_date.isoformat()),
        ).fetchone()[0]
    )


# ---------------------------------------------------------------- dashboard


def dashboard_summary(
    conn: sqlite3.Connection, settings: Settings, tz: tzinfo, now: datetime
) -> dict[str, Any]:
    """Data for the dashboard "Keyword rank" table: one entry per keyword."""
    today = now.astimezone(tz).date()
    days = max(1, int(settings.get("rank.average_days")))
    entries = []
    for keyword in approved_keywords(conn, settings):
        latest = conn.execute(
            "SELECT * FROM rank_daily WHERE keyword = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (keyword, today.isoformat()),
        ).fetchone()
        entries.append(
            {
                "keyword": keyword,
                "checked": latest["date"] if latest else None,
                "position": latest["position"] if latest else None,
                "organic": latest["organic_position"] if latest else None,
                "seen": latest["total_seen"] if latest else None,
                "average": avg_7d(conn, keyword, today, days=days),
                "not_found_days": not_found_count(conn, keyword, today, days=days),
            }
        )
    return {
        "enabled": bool(settings.get("rank.enabled")),
        "entries": entries,
        "average_days": days,
        "run_hour": int(settings.get("rank.run_hour_local")),
        "any": any(e["checked"] for e in entries),
    }
