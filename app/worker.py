"""In-app background worker (plan W1).

Long clicks call `submit(...)` and return at once; a small thread pool runs the work inside the
same uvicorn process. One row per action in the `actions` table.

- Each worker thread opens its own DB connection (and its own Settings on it).
- `submit` with a dedup_key returns the id of a queued/running action with the same key.
- The kill switch is read right before `fn` runs. If it is on, the action fails.
- `fn(ctx)` (or `fn()`) returns None or (result_url, message). A `WorkerError` message is shown
  to the user. Any other exception shows a fixed safe text; the full trace goes to the log only.
- On start, queued/running rows from an earlier process become 'stopped'. They are never re-run.
- Tests set `worker.sync = True` to run each action inside `submit`.
"""

from __future__ import annotations

import inspect
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app import db
from app.llm.live import CANCELLED_TEXT, CURRENT_ACTION, ClaudeCancelled
from app.settings import Settings

log = logging.getLogger("worker")

ACTIVE = ("queued", "running")
FINISHED = ("done", "failed", "stopped")
STOPPED_BY_RESTART = "Stopped by restart"
STOPPED_BY_KILL_SWITCH = "Stopped by kill switch"
GENERIC_ERROR = "Something went wrong. Details are in the log."
MAX_TEXT = 300


class WorkerError(Exception):
    """An error whose text is safe and short enough to show to the user."""


@dataclass
class ActionContext:
    """What an action function can use. conn and settings belong to the worker thread."""

    action_id: str
    conn: sqlite3.Connection
    settings: Settings


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text[:MAX_TEXT]


def _takes_arg(fn: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in params
    )


class Worker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, db_path: str | Path):
        self.conn = conn  # the app's own connection: used by submit and the page reads
        self.settings = settings
        self.db_path = str(db_path)
        self.sync = False
        self._lock = threading.Lock()
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False

    # ---------- life cycle ----------

    def start(self) -> int:
        """Mark actions left from an earlier process as stopped. Returns how many."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE actions SET status='stopped', ended=?, error=? WHERE status IN (?, ?)",
                (_now(), STOPPED_BY_RESTART, *ACTIVE),
            )
            return cur.rowcount

    def stop(self) -> None:
        """Shutdown hook: wait a little for running actions, then stop the rest."""
        self._closed = True
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            deadline = time.monotonic() + float(self.settings.get("worker.shutdown_wait_seconds"))
            while time.monotonic() < deadline and self.running_count():
                time.sleep(0.05)
        with self._lock:
            self.conn.execute(
                "UPDATE actions SET status='stopped', ended=?, error=? WHERE status IN (?, ?)",
                (_now(), STOPPED_BY_RESTART, *ACTIVE),
            )
        if not self.running_count():
            for c in self._conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()

    # ---------- submit ----------

    def submit(
        self,
        kind: str,
        target: str,
        label: str,
        fn: Callable[..., Any],
        dedup_key: str | None = None,
    ) -> str:
        """Queue `fn` and return the action id at once."""
        with self._lock:
            if dedup_key:
                row = self.conn.execute(
                    "SELECT id FROM actions WHERE dedup_key = ? AND status IN (?, ?) "
                    "ORDER BY created LIMIT 1",
                    (dedup_key, *ACTIVE),
                ).fetchone()
                if row is not None:
                    return row["id"]
            action_id = secrets.token_hex(8)
            self.conn.execute(
                "INSERT INTO actions(id, kind, target, label, status, created, dedup_key) "
                "VALUES (?,?,?,?,?,?,?)",
                (action_id, kind, target or "", _clip(label) or kind, "queued", _now(), dedup_key),
            )
        if self.sync:
            self._run(action_id, fn)
        else:
            self._pool().submit(self._run, action_id, fn)
        return action_id

    def _pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                threads = max(1, int(self.settings.get("worker.threads")))
                self._executor = ThreadPoolExecutor(
                    max_workers=threads, thread_name_prefix="worker"
                )
            return self._executor

    # ---------- run (in a worker thread) ----------

    def _thread_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.db_path)
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    def _run(self, action_id: str, fn: Callable[..., Any]) -> None:
        conn = self.conn if self.sync else self._thread_conn()
        settings = self.settings if self.sync else Settings(conn)
        try:
            if self._closed:
                self._finish(conn, action_id, "stopped", error=STOPPED_BY_RESTART)
                return
            if settings.get("general.kill_switch"):
                self._finish(conn, action_id, "failed", error=STOPPED_BY_KILL_SWITCH)
                return
            conn.execute(
                "UPDATE actions SET status='running', started=? WHERE id=? AND status='queued'",
                (_now(), action_id),
            )
            ctx = ActionContext(action_id, conn, settings)
            token = CURRENT_ACTION.set(action_id)  # claude calls in fn link to this action
            try:
                result = fn(ctx) if _takes_arg(fn) else fn()
            finally:
                CURRENT_ACTION.reset(token)
            url, message = self._unpack(result)
            self._finish(conn, action_id, "done", url=url, message=message)
        except ClaudeCancelled:
            log.warning("Action %s cancelled by the user", action_id)
            self._finish(conn, action_id, "failed", error=CANCELLED_TEXT)
        except WorkerError as exc:
            log.warning("Action %s failed: %s", action_id, exc)
            self._finish(conn, action_id, "failed", error=_clip(str(exc)) or GENERIC_ERROR)
        except Exception:
            log.exception("Action %s failed", action_id)  # full trace: log only
            self._finish(conn, action_id, "failed", error=GENERIC_ERROR)

    @staticmethod
    def _unpack(result: Any) -> tuple[str | None, str | None]:
        if result is None:
            return None, None
        if isinstance(result, (tuple, list)) and len(result) == 2:
            return result[0] or None, _clip(result[1])
        if isinstance(result, str):
            return None, _clip(result)
        return None, None

    def _finish(
        self,
        conn: sqlite3.Connection,
        action_id: str,
        status: str,
        url: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        try:
            conn.execute(
                "UPDATE actions SET status=?, ended=?, result_url=?, message=?, error=? "
                "WHERE id=? AND status IN (?, ?)",
                (status, _now(), url, message, error, action_id, *ACTIVE),
            )
        except sqlite3.Error:
            log.exception("Could not save the result of action %s", action_id)

    # ---------- reads ----------

    def running_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM actions WHERE status='running'").fetchone()
        return int(row[0])

    def get(self, action_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return dict(row) if row else None

    def summary(self) -> dict[str, Any]:
        """For the top bar: working actions, recently finished ones (toasts) and the poll time."""
        poll = max(1, int(self.settings.get("worker.poll_seconds")))
        toast = int(self.settings.get("worker.toast_seconds"))
        cutoff = (datetime.now(UTC) - timedelta(seconds=toast)).isoformat(timespec="seconds")
        working = self.conn.execute(
            "SELECT * FROM actions WHERE status IN (?, ?) ORDER BY created", ACTIVE
        ).fetchall()
        recent = self.conn.execute(
            "SELECT * FROM actions WHERE status IN (?, ?, ?) AND ended >= ? ORDER BY ended",
            (*FINISHED, cutoff),
        ).fetchall()
        return {
            "working": [dict(r) for r in working],
            "recent": [dict(r) for r in recent],
            "count": len(working),
            "poll": poll,
        }


def safe_local_url(url: str | None) -> str | None:
    """Only a path on this site. Anything else is dropped."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


def known_action(worker: Worker, action_id: str | None) -> str:
    """The id from a `?action=` query when it names a real action, else an empty string."""
    if action_id and worker.get(action_id) is not None:
        return action_id
    return ""


def finished_url(worker: Worker, action_id: str) -> str | None:
    """The result page of a finished action, for a page that is opened with `?action=`. Only
    a path on this site; None while the action is still working or failed."""
    row = worker.get(action_id) if action_id else None
    if row is not None and row["status"] == "done":
        return safe_local_url(row["result_url"])
    return None


def ensure_writes_open(settings: Settings) -> None:
    """Call inside a work function right before an Upwork write: settings can change while the
    action waits in the queue. (Dry-run is checked again by the write code itself, which reads
    the setting afresh and records the block.)"""
    if settings.get("general.kill_switch"):
        raise WorkerError("The kill switch is on. No writes.")
