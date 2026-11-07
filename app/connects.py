"""Keep the stored Connects balance fresh (plan N1).

The `connects` table is read by Home, the job page, judging and auto-send. Besides the daily
run it is now also written after a send, when a preview shows a balance, when metrics reads the
dashboard, and by the Refresh button. `refresh` is ONE read call; it never writes to Upwork.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app import metrics
from app.llm.live import ClaudeCancelled
from app.settings import Settings

log = logging.getLogger(__name__)


class ConnectsError(Exception):
    """The balance could not be read. The text is safe to show."""


def record(
    conn: sqlite3.Connection,
    balance: int | None,
    source: str = "",
    run_id: str | None = None,
) -> bool:
    """Store a balance already known. Returns False when there is no number to store."""
    if balance is None or isinstance(balance, bool):
        return False
    conn.execute(
        "INSERT INTO connects(balance, needed, seen_at, run_id) VALUES (?, NULL, ?, ?)",
        (int(balance), datetime.now(UTC).isoformat(timespec="seconds"), run_id),
    )
    log.info("Connects balance %s recorded (%s)", balance, source or "unknown")
    return True


def refresh(
    bridge: Any, conn: sqlite3.Connection, settings: Settings, run_id: str | None = None
) -> int:
    """Read the dashboard once and store the balance. Raises ConnectsError."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise ConnectsError("No freelancer account is chosen yet (see the Upwork page).")
    try:
        results = bridge.call_many(
            [(metrics.DASHBOARD_TOOL, {"action": "check", "org_uid": org_uid, "params": {}})]
        )
        balance = metrics.parse_dashboard(results[0].unwrap())["connects"]
    except Exception as exc:
        raise ConnectsError(f"Could not read the Connects balance ({type(exc).__name__}).") from exc
    if balance is None:
        raise ConnectsError("Upwork did not show a Connects balance.")
    record(conn, balance, "refresh", run_id)
    return balance


def refresh_quietly(
    bridge: Any, conn: sqlite3.Connection, settings: Settings, run_id: str | None = None
) -> int | None:
    """`refresh` for use after a send: any failure is only logged, never raised."""
    try:
        return refresh(bridge, conn, settings, run_id)
    except (Exception, ClaudeCancelled):  # a cancel is a BaseException
        log.warning("Connects refresh after a send failed", exc_info=True)
        return None
