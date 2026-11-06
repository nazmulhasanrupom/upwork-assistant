"""Saved weekly and monthly reports (plan I1, I2, I3). One row per kind and period."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

KINDS = ("weekly", "monthly")


def save(
    conn: sqlite3.Connection,
    kind: str,
    period: str,
    title: str,
    body: str,
    stats: Any,
    run_id: str | None,
    now: datetime | None = None,
) -> int:
    """Insert, or replace the report of the same kind and period. Returns the id."""
    if kind not in KINDS:
        raise ValueError(f"unknown report kind {kind!r}")
    created = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO reports(kind, period, run_id, created_at, title, body, stats_json) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, period) DO UPDATE SET run_id = excluded.run_id, "
        "created_at = excluded.created_at, title = excluded.title, body = excluded.body, "
        "stats_json = excluded.stats_json",
        (kind, period, run_id, created, title, body, json.dumps(stats, ensure_ascii=False)),
    )
    return int(
        conn.execute(
            "SELECT id FROM reports WHERE kind = ? AND period = ?", (kind, period)
        ).fetchone()["id"]
    )


def list_reports(conn: sqlite3.Connection, kind: str | None = None, limit: int = 100):
    sql = "SELECT id, kind, period, created_at, title FROM reports"
    args: list[Any] = []
    if kind in KINDS:
        sql += " WHERE kind = ?"
        args.append(kind)
    return conn.execute(
        sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*args, limit)
    ).fetchall()


def get(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()


def stats_of(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["stats_json"] or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def start_run(conn: sqlite3.Connection, task: str, now: datetime) -> str:
    import uuid

    run_id = f"{task}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES (?,?,?, 'running')",
        (run_id, task, now.astimezone(UTC).isoformat(timespec="seconds")),
    )
    return run_id


def end_run(conn: sqlite3.Connection, run_id: str, status: str, error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET ended = ?, status = ?, error = ? WHERE run_id = ?",
        (datetime.now(UTC).isoformat(timespec="seconds"), status, error, run_id),
    )


def wrap(tag: str, value: Any) -> str:
    """JSON inside <tag>...</tag>. A closing/opening tag of the same name inside is defanged."""
    import re

    text = json.dumps(value, ensure_ascii=False, indent=1, default=str)
    text = re.sub(rf"<(/?)\s*{tag}", r"&lt;\1" + tag, text, flags=re.IGNORECASE)
    return f"<{tag}>\n{text}\n</{tag}>"
