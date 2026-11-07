"""Daily metrics snapshot (plan X1, SPEC2 4.C phase 1 and 4.D).

Once per daily run (behind `metrics.enabled`) `snapshot` reads the Upwork freelancer dashboard
through the bridge (one batch, `get_freelancer_dashboard action=check`) and stores ONE row per
local day in `metrics_daily` (a later run on the same day updates the row).

- Connects, invites, offers, messages waiting for me and active contracts come from the dashboard.
- The proposal funnel (sent / viewed / interviews / hires) is counted from the `proposals` table
  that the after-send step keeps up to date. It needs no extra Upwork call. It counts proposals
  sent in the last `metrics.funnel_days` days.
- JSS has no source in the dashboard or in `get_profile get`. It is an OPTIONAL second read:
  `find_freelancers action=get_profile {profile_key}`. My own key comes from `get_profile get`
  (data.identity.ciphertext) and is kept in `metrics.profile_key`. The shape of that answer is
  NOT verified live. No score found = NULL, and that is never an error.
- Profile views are not available from Upwork. The owner types them once a week
  (`metrics_weekly`).

Nothing here writes to Upwork. The raw_json column holds a small summary (numbers and ids only),
never client text.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.llm.runner import BudgetCapReached, ClaudeRateLimited
from app.settings import Settings
from app.upwork.client import UpworkAuthError

log = logging.getLogger("metrics")

ALERT_KEY = "metrics_failed"
DASHBOARD_TOOL = "get_freelancer_dashboard"
FREELANCERS_TOOL = "find_freelancers"
JSS_KEYS = ("job_success_score", "jobSuccessScore")
MAX_SEARCH_DEPTH = 6

# Columns the average helper may read (never build SQL from outside text).
NUMERIC_COLUMNS = (
    "connects",
    "jss",
    "invites_open",
    "invites_new",
    "offers_open",
    "messages_awaiting_me",
    "active_contracts",
    "proposals_sent",
    "proposals_viewed",
    "interviews",
    "hires",
)


class MetricsError(Exception):
    """The snapshot could not be made (no account, or the dashboard call failed)."""


# ---------------------------------------------------------------- helpers


def _zone(tz: tzinfo | str | None, settings: Settings) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(tz or str(settings.get("general.timezone")))
    except Exception:
        return UTC


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip().replace(",", "")))
        except ValueError:
            return None
    return None


def week_start(day: date) -> date:
    """Monday of the week that holds `day`."""
    return day - timedelta(days=day.weekday())


def _parse_when(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------- parsing


def parse_jss(payload: Any, _depth: int = 0) -> float | None:
    """The first job success score (0 to 100) found in a `find_freelancers get_profile` answer.
    Shape is unverified, so the answer is searched, not indexed. None when there is none."""
    if _depth > MAX_SEARCH_DEPTH:
        return None
    if isinstance(payload, dict):
        for key in JSS_KEYS:
            if key in payload:
                value = payload[key]
                if isinstance(value, str):
                    value = value.strip().rstrip("%").strip()
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, bool) or not 0 <= number <= 100:
                    continue
                return number
        for value in payload.values():
            found = parse_jss(value, _depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = parse_jss(value, _depth + 1)
            if found is not None:
                return found
    return None


def own_profile_key(payload: Any) -> str | None:
    """My profile key from `get_profile get`: data.identity.ciphertext (starts with ~)."""
    key = _get(payload, "data", "identity", "ciphertext")
    return key if isinstance(key, str) and key.startswith("~") else None


def _ids(items: Any) -> list[str]:
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            ident = item.get("id") or item.get("invitation_uid") or item.get("uid")
            if ident not in (None, ""):
                out.append(str(ident))
    return out


def _invite_items(section: Any) -> list[dict[str, str]]:
    """Pending invitations as {id, job_id, title}. The shape of the dashboard items is the one
    of `list_freelancer_proposals invitations` (id = invitation_uid, jobPosting{id,title}); a
    flat job_id / title is read too. Items without an id are skipped."""
    out = []
    items = _get(section, "items")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        ident = item.get("id") or item.get("invitation_uid") or item.get("uid")
        if ident in (None, ""):
            continue
        posting = item.get("jobPosting") if isinstance(item.get("jobPosting"), dict) else {}
        job_id = posting.get("id") or item.get("job_id") or item.get("jobId")
        title = posting.get("title") or item.get("title") or item.get("job_title")
        out.append(
            {
                "id": str(ident),
                "job_id": "" if job_id in (None, "") else str(job_id),
                "title": "" if title in (None, "") else str(title),
            }
        )
    return out


def _open_count(section: Any) -> int | None:
    count = _int(_get(section, "count"))
    if count is not None:
        return count
    items = _get(section, "items")
    return len(items) if isinstance(items, list) else None


def parse_dashboard(payload: Any) -> dict[str, Any]:
    """Numbers from a `get_freelancer_dashboard check` answer. Missing parts become None."""
    data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    rooms = _get(data, "messages", "rooms")
    rooms = [r for r in rooms if isinstance(r, dict)] if isinstance(rooms, list) else []
    return {
        "connects": _int(_get(data, "connects", "balance", "connectsBalance")),
        "connects_paid": _int(_get(data, "connects", "balance", "connectsBalancePaid")),
        "invites_open": _open_count(_get(data, "invitations")),
        "invite_ids": _ids(_get(data, "invitations", "items")),
        "invites": _invite_items(_get(data, "invitations")),
        # the dashboard may show only a preview: with fewer items than the count, an invite
        # that is missing from the list is NOT known to be answered
        "invites_complete": (
            isinstance(_get(data, "invitations", "items"), list)
            and len(_get(data, "invitations", "items"))
            >= (_open_count(_get(data, "invitations")) or 0)
        ),
        "offers_open": _open_count(_get(data, "offers")),
        "offer_ids": _ids(_get(data, "offers", "items")),
        "messages_awaiting_me": (
            sum(1 for r in rooms if r.get("awaiting_reply_from") == "you")
            if isinstance(_get(data, "messages", "rooms"), list)
            else None
        ),
        "rooms_truncated": bool(_get(data, "messages", "truncated")),
        "interview_room_dates": [
            r.get("createdAtDateTime") for r in rooms if r.get("roomType") == "INTERVIEW"
        ],
        "active_contracts": _int(_get(data, "active_contracts", "total_count")),
    }


# ---------------------------------------------------------------- funnel from the proposals table


def funnel(
    conn: sqlite3.Connection, settings: Settings, now: datetime, room_dates: list[Any]
) -> dict[str, int]:
    """sent / viewed / interviews / hires of the proposals sent in the last funnel_days days."""
    days = int(settings.get("metrics.funnel_days"))
    cutoff = now.astimezone(UTC) - timedelta(days=days)
    rows = conn.execute(
        "SELECT status, viewed_by_client FROM proposals WHERE created_at >= ?", (_iso(cutoff),)
    ).fetchall()
    interview = {str(s) for s in settings.get("metrics.interview_statuses")}
    hired = {str(s) for s in settings.get("metrics.hire_statuses")}
    by_status = sum(1 for r in rows if r["status"] in interview)
    in_rooms = sum(1 for d in room_dates if (w := _parse_when(d)) is not None and w >= cutoff)
    return {
        "proposals_sent": len(rows),
        "proposals_viewed": sum(1 for r in rows if r["viewed_by_client"]),
        "interviews": max(by_status, in_rooms),
        "hires": sum(1 for r in rows if r["status"] in hired),
    }


# ---------------------------------------------------------------- invitations


def _known_invites(conn: sqlite3.Connection, today: date, days: int) -> set[str]:
    """Invitation ids seen on earlier days (inside the memory window)."""
    first = (today - timedelta(days=days)).isoformat()
    known: set[str] = set()
    for row in conn.execute(
        "SELECT raw_json FROM metrics_daily WHERE date < ? AND date >= ?",
        (today.isoformat(), first),
    ):
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except ValueError:
            continue
        for key in ("invite_ids", "new_invite_ids"):
            ids = raw.get(key)
            if isinstance(ids, list):
                known.update(str(x) for x in ids)
    return known


# ---------------------------------------------------------------- the snapshot


def _calls(org_uid: str, key: str | None) -> list[tuple[str, dict[str, Any]]]:
    calls = [(DASHBOARD_TOOL, {"action": "check", "org_uid": org_uid, "params": {}})]
    if key:
        calls.append(jss_call(org_uid, key))
    return calls


def jss_call(org_uid: str, key: str) -> tuple[str, dict[str, Any]]:
    return (
        FREELANCERS_TOOL,
        {"action": "get_profile", "org_uid": org_uid, "params": {"profile_key": key}},
    )


def _read_jss(bridge: Any, org_uid: str, key: str) -> float | None:
    """The optional JSS read as its own batch. Only a login problem is raised."""
    try:
        [result] = bridge.call_many([jss_call(org_uid, key)])
        return parse_jss(result.unwrap())
    except UpworkAuthError:
        raise
    except (BudgetCapReached, ClaudeRateLimited):
        return None
    except Exception as e:  # an unknown answer or a failed call must not fail the snapshot
        log.info("JSS read failed: %s: %s", type(e).__name__, e)
        return None


def ensure_profile_key(bridge: Any, settings: Settings) -> str | None:
    """My profile key (metrics.profile_key). When it is not known yet, one `get_profile get`
    learns it and saves it. None when it cannot be learned. Only a login problem is raised."""
    key = str(settings.get("metrics.profile_key") or "").strip()
    if key:
        return key
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        return None
    try:
        [result] = bridge.call_many(
            [("get_profile", {"action": "get", "org_uid": org_uid, "params": {}})]
        )
        found = own_profile_key(result.unwrap())
    except UpworkAuthError:
        raise
    except (BudgetCapReached, ClaudeRateLimited):
        return None
    except Exception as e:
        log.info("Own profile read failed: %s: %s", type(e).__name__, e)
        return None
    if found:
        settings.set("metrics.profile_key", found)
    return found


def snapshot(
    bridge: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    tz: tzinfo | str | None = None,
) -> dict[str, Any]:
    """Read the dashboard (and the optional JSS), store today's row, return it as a dict."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise MetricsError("No freelancer account is chosen yet (see the Upwork page).")
    zone = _zone(tz, settings)
    today = now.astimezone(zone).date()

    want_jss = bool(settings.get("metrics.jss_enabled"))
    key = str(settings.get("metrics.profile_key") or "") or None
    calls = _calls(org_uid, key if want_jss else None)
    # The key is needed by the JSS read AND by the rank check, so it is learned whenever it is
    # missing and either of them is on (plan X2 fix B), whatever metrics.jss_enabled says.
    learn_key = key is None and (want_jss or bool(settings.get("rank.enabled")))
    if learn_key:  # first run: the same batch also fetches my own profile for its key
        calls.append(("get_profile", {"action": "get", "org_uid": org_uid, "params": {}}))
    results = bridge.call_many(calls)

    try:
        dash = parse_dashboard(results[0].unwrap())
    except UpworkAuthError:
        raise
    except Exception as e:
        raise MetricsError(f"The dashboard read failed: {type(e).__name__}: {e}"[:300]) from e

    if dash["connects"] is not None:
        from app import connects  # late import: connects uses this module

        connects.record(conn, dash["connects"], "metrics")

    jss: float | None = None
    if want_jss and key is not None:
        try:
            jss = parse_jss(results[1].unwrap())
        except Exception as e:
            log.info("JSS read failed: %s: %s", type(e).__name__, e)
    elif learn_key:
        try:
            key = own_profile_key(results[1].unwrap())
        except Exception as e:
            log.info("Own profile read failed: %s: %s", type(e).__name__, e)
        if key:
            settings.set("metrics.profile_key", key)
            if want_jss:
                jss = _read_jss(bridge, org_uid, key)

    # invitations first seen today (a union, so one answered within the day still counts)
    existing = conn.execute(
        "SELECT raw_json FROM metrics_daily WHERE date = ?", (today.isoformat(),)
    )
    row = existing.fetchone()
    try:
        before = json.loads(row["raw_json"]) if row else {}
    except ValueError:
        before = {}
    new_today = {str(x) for x in before.get("new_invite_ids", [])}
    known = _known_invites(conn, today, int(settings.get("metrics.invite_memory_days")))
    new_today |= {i for i in dash["invite_ids"] if i not in known}

    values = {
        "connects": dash["connects"],
        "jss": jss,
        "invites_open": dash["invites_open"],
        "invites_new": len(new_today),
        "offers_open": dash["offers_open"],
        "messages_awaiting_me": dash["messages_awaiting_me"],
        "active_contracts": dash["active_contracts"],
        **funnel(conn, settings, now, dash["interview_room_dates"]),
    }
    raw = {
        "invite_ids": dash["invite_ids"],
        "new_invite_ids": sorted(new_today),
        "offer_ids": dash["offer_ids"],
        "connects_paid": dash["connects_paid"],
        "rooms_truncated": dash["rooms_truncated"],
        "jss_found": jss is not None,
        "jss_read": bool(want_jss),
    }
    conn.execute(
        "INSERT INTO metrics_daily(date, connects, jss, invites_open, invites_new, offers_open, "
        "messages_awaiting_me, active_contracts, proposals_sent, proposals_viewed, interviews, "
        "hires, raw_json, created_at) "
        "VALUES (:date, :connects, :jss, :invites_open, :invites_new, :offers_open, "
        ":messages_awaiting_me, :active_contracts, :proposals_sent, :proposals_viewed, "
        ":interviews, :hires, :raw_json, :created_at) "
        "ON CONFLICT(date) DO UPDATE SET connects = excluded.connects, "
        "jss = COALESCE(excluded.jss, metrics_daily.jss), "
        "invites_open = excluded.invites_open, invites_new = excluded.invites_new, "
        "offers_open = excluded.offers_open, "
        "messages_awaiting_me = excluded.messages_awaiting_me, "
        "active_contracts = excluded.active_contracts, proposals_sent = excluded.proposals_sent, "
        "proposals_viewed = excluded.proposals_viewed, interviews = excluded.interviews, "
        "hires = excluded.hires, raw_json = excluded.raw_json",
        {
            "date": today.isoformat(),
            **values,
            "raw_json": json.dumps(raw, sort_keys=True),
            "created_at": _iso(now),
        },
    )
    stored = conn.execute("SELECT * FROM metrics_daily WHERE date = ?", (today.isoformat(),))
    out = dict(stored.fetchone())
    # not stored (client text): the pending invitations as read now, for the invite alerts
    out["invites"] = dash["invites"]
    out["invites_complete"] = dash["invites_complete"]
    return out


# ---------------------------------------------------------------- averages and weekly input


def average(
    conn: sqlite3.Connection,
    column: str,
    *,
    end: date,
    days: int = 7,
) -> float | None:
    """Average of a metrics_daily column over the `days` days ending on `end` (inclusive).
    Days without a row or with NULL are skipped. None when there is nothing to average."""
    if column not in NUMERIC_COLUMNS:
        raise ValueError(f"Unknown metrics column: {column!r}")
    first = end - timedelta(days=max(1, int(days)) - 1)
    value = conn.execute(
        f"SELECT AVG({column}) FROM metrics_daily WHERE date >= ? AND date <= ?",
        (first.isoformat(), end.isoformat()),
    ).fetchone()[0]
    return None if value is None else float(value)


def seven_day_average(
    conn: sqlite3.Connection, settings: Settings, column: str, *, end: date
) -> float | None:
    return average(conn, column, end=end, days=int(settings.get("metrics.average_days")))


def save_profile_views(
    conn: sqlite3.Connection,
    settings: Settings,
    views: int,
    now: datetime,
    tz: tzinfo | str | None = None,
) -> str:
    """Store the profile views typed for the current week. Returns the week start (Monday)."""
    if isinstance(views, bool) or not isinstance(views, int):
        raise ValueError("Profile views must be a whole number.")
    if not 0 <= views <= int(settings.get("metrics.profile_views_max")):
        raise ValueError("Profile views must be between 0 and the allowed maximum.")
    start = week_start(now.astimezone(_zone(tz, settings)).date()).isoformat()
    conn.execute(
        "INSERT INTO metrics_weekly(week_start, profile_views, entered_at) VALUES (?,?,?) "
        "ON CONFLICT(week_start) DO UPDATE SET profile_views = excluded.profile_views, "
        "entered_at = excluded.entered_at",
        (start, views, _iso(now)),
    )
    return start


def dashboard_summary(
    conn: sqlite3.Connection, settings: Settings, tz: tzinfo, now: datetime
) -> dict[str, Any]:
    """Data for the dashboard "This week" card. `row` is None before the first snapshot."""
    today = now.astimezone(tz).date()
    row = conn.execute("SELECT * FROM metrics_daily ORDER BY date DESC LIMIT 1").fetchone()
    card_days = max(1, int(settings.get("metrics.card_days")))
    first = (today - timedelta(days=card_days - 1)).isoformat()
    new_invites = conn.execute(
        "SELECT COALESCE(SUM(invites_new), 0) FROM metrics_daily WHERE date >= ?", (first,)
    ).fetchone()[0]
    this_week = week_start(today)
    views = {
        r["week_start"]: r["profile_views"]
        for r in conn.execute(
            "SELECT week_start, profile_views FROM metrics_weekly WHERE week_start IN (?, ?)",
            (this_week.isoformat(), (this_week - timedelta(days=7)).isoformat()),
        )
    }
    return {
        "enabled": bool(settings.get("metrics.enabled")),
        "row": dict(row) if row else None,
        "new_invites": int(new_invites or 0),
        "card_days": card_days,
        "funnel_days": int(settings.get("metrics.funnel_days")),
        "views_this_week": views.get(this_week.isoformat()),
        "views_last_week": views.get((this_week - timedelta(days=7)).isoformat()),
        "week_start": this_week.isoformat(),
        "views_max": int(settings.get("metrics.profile_views_max")),
    }
