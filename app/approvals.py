"""Approvals queue (plan H3): everything that waits for a user click.

Two kinds of item are shown on /approvals:
- `proposal`: an open proposal preview (previews table: previewed / dry_run_blocked, not expired).
  It is decided on the job page (Approve / Re-make / Skip from H2). This module only lists it and
  links there; it never confirms a preview.
- generic items from the `approvals` table (kind, ref_id, title, before/after JSON). Later phases
  add them with `add()`: keywords, profile changes, the playbook. Approve and Reject only record
  the decision, then run the handler registered for that kind, if there is one. There are no
  handlers yet, so an unknown kind records the decision and nothing else.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from app.settings import Settings
from app.upwork import writes

log = logging.getLogger("approvals")

PROPOSAL = "proposal"
OPEN_PREVIEW_STATUSES = ("previewed", "dry_run_blocked")

# kind -> handler(item: sqlite3.Row, decision: "approved" | "rejected", conn) -> None.
# A handler that raises leaves the item pending. `keywords` registers its own (app/keywords.py);
# O and L5 add theirs.
Handler = Callable[[sqlite3.Row, str, sqlite3.Connection], None]
HANDLERS: dict[str, Handler] = {}


# Extra things a handler may need from the route that decided (a `writes.UserClick`, a reader and
# a writer factory). Only set by `decide(context=...)`; a handler that needs it raises when absent.
_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("approvals_context", default=None)


def context() -> dict[str, Any]:
    return _CONTEXT.get() or {}


def register(kind: str, handler: Handler) -> None:
    HANDLERS[kind] = handler


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------- generic items


def add(
    conn: sqlite3.Connection,
    kind: str,
    title: str,
    *,
    ref_id: str | None = None,
    before: Any = None,
    after: Any = None,
    now: datetime | None = None,
) -> int:
    """Put one item in the queue. Returns its id."""
    cur = conn.execute(
        "INSERT INTO approvals(kind, ref_id, title, before_json, after_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (
            kind,
            ref_id,
            title,
            None if before is None else json.dumps(before, ensure_ascii=False),
            None if after is None else json.dumps(after, ensure_ascii=False),
            _iso(now or _now()),
        ),
    )
    return int(cur.lastrowid or 0)


def _cutoff(settings: Settings, now: datetime) -> str:
    return _iso(now - timedelta(days=float(settings.get("approvals.expire_days"))))


def expire_old(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> int:
    """Mark pending items older than approvals.expire_days as expired. Returns how many."""
    cur = conn.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ? "
        "WHERE status = 'pending' AND created_at < ?",
        (_iso(now or _now()), _cutoff(settings, now or _now())),
    )
    return cur.rowcount


def _show(text: str | None) -> str:
    """JSON text from the table, shown as pretty text (strings stay plain)."""
    if text is None:
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _data(text: str | None) -> Any:
    """The parsed JSON of a before/after column, or None (for kinds that have their own view)."""
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def pending_items(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = now or _now()
    rows = conn.execute(
        "SELECT * FROM approvals WHERE status = 'pending' AND created_at >= ? "
        "ORDER BY created_at DESC, id DESC",
        (_cutoff(settings, now),),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "ref_id": r["ref_id"],
            "title": r["title"],
            "before": _show(r["before_json"]),
            "after": _show(r["after_json"]),
            "before_data": _data(r["before_json"]),
            "after_data": _data(r["after_json"]),
            "created_at": r["created_at"],
            "has_handler": r["kind"] in HANDLERS,
        }
        for r in rows
    ]


def recent_decided(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM approvals WHERE status != 'pending' "
        "ORDER BY COALESCE(decided_at, created_at) DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------- proposal previews


def open_previews(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Newest preview per job (or invitation) that still waits for a click and has not expired."""
    now = now or _now()
    rows = conn.execute(
        "SELECT p.*, j.title AS job_title FROM previews p LEFT JOIN jobs j ON j.job_id = p.job_id "
        "ORDER BY p.id DESC"
    ).fetchall()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        key = r["job_id"] or r["invitation_uid"] or f"id{r['id']}"
        if key in seen:
            continue
        seen.add(key)  # only the newest preview of a job counts, whatever its status
        if r["status"] not in OPEN_PREVIEW_STATUSES or r["confirmed_at"]:
            continue
        if writes.is_expired(r, settings, now):
            continue
        out.append(
            {
                "kind": PROPOSAL,
                "id": r["id"],
                "title": r["job_title"] or (f"Job {r['job_id']}" if r["job_id"] else "Invitation"),
                "job_id": r["job_id"],
                "link": f"/jobs/{r['job_id']}" if r["job_id"] else None,
                "connects_cost": r["connects_cost"],
                "connects_balance": r["connects_balance"],
                "dry_run_blocked": r["status"] == "dry_run_blocked",
                "created_at": r["created_at"],
            }
        )
    return out


def pending_count(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> int:
    """Generic pending items plus open proposal previews (the number in the nav)."""
    return len(pending_items(conn, settings, now)) + len(open_previews(conn, settings, now))


# ---------------------------------------------------------------- decide


class ApprovalError(RuntimeError):
    """A message that is safe to show to the user."""


def decide(
    conn: sqlite3.Connection,
    item_id: int,
    decision: str,
    now: datetime | None = None,
    context: dict[str, Any] | None = None,
) -> sqlite3.Row:
    """Record approve / reject for a pending generic item, then run its handler (if any).

    `decision` is "approved" or "rejected". The status changes with one UPDATE that only matches
    a pending row, so a double click decides once. A handler that raises puts the item back to
    pending and raises ApprovalError. Returns the item row."""
    if decision not in ("approved", "rejected"):
        raise ValueError(decision)
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ApprovalError("That item does not exist.")
    cur = conn.execute(
        "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
        (decision, _iso(now or _now()), item_id),
    )
    if cur.rowcount != 1:
        raise ApprovalError(f"That item was already decided ({row['status']}).")
    handler = HANDLERS.get(row["kind"])
    if handler is not None:
        token = _CONTEXT.set(context)
        try:
            handler(row, decision, conn)
        except Exception as e:
            log.exception("Approval handler for %s failed", row["kind"])
            conn.execute(
                "UPDATE approvals SET status = 'pending', decided_at = NULL WHERE id = ?",
                (item_id,),
            )
            raise ApprovalError(f"The change could not be applied: {str(e)[:200]}") from e
        finally:
            _CONTEXT.reset(token)
    return conn.execute("SELECT * FROM approvals WHERE id = ?", (item_id,)).fetchone()
