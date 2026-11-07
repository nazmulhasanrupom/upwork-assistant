"""One way to start a non-interactive `claude`, and the registry behind the Claude jobs dock (L1).

`run_process` replaces `subprocess.run` for every headless claude call:
- claude starts in its own session (so its whole group can be stopped),
- the `claude_live` table gets a row (the panel and the task processes share the database),
- a live log `<DATA_DIR>/live/<id>.log` gets the trimmed prompt, then the text and one line per
  tool call (when claude runs with stream-json),
- a Cancel click (or the timeout) stops the group: SIGTERM, then SIGKILL after a grace time.

Safety:
- The pid is checked (/proc cmdline and start time) before it is signalled, so a reused pid is
  never killed. Only the machine that owns the pid signals it.
- Environment values, tokens and keys are masked before anything is written to a log.
- A cancelled call raises `ClaudeCancelled`. It is a BaseException on purpose: the broad
  `except Exception` blocks in tasks and actions must not turn a Cancel into "try the next one".
  Nothing here ever retries a cancelled call (a cancelled write may already have been sent).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.settings import Settings, load_defaults

log = logging.getLogger("claude.live")

CANCELLED_TEXT = "Cancelled by you"
WRITE_WARNING = "This may already be sent to Upwork. Check Upwork before you try again."
RUNNING = "running"
KINDS = ("claude", "read", "write", "check")

# The worker sets this around an action, so a claude call knows which action it belongs to.
CURRENT_ACTION: ContextVar[str | None] = ContextVar("claude_live_action", default=None)

_SECRET_NAME_RE = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|AUTH|CREDENTIAL", re.IGNORECASE)
_SECRET_TEXT_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{8,}")
_CLAUDE_ARG_RE = re.compile(r"^claude(\.exe|\.js)?$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_DEFAULTS: dict[str, Any] = {}
_last_prune = 0.0


class ClaudeCancelled(BaseException):
    """The user pressed Cancel. Not an Exception: nothing may swallow it and carry on."""


# ---------- settings ----------


def _setting(settings: Settings | None, key: str) -> Any:
    if settings is not None:
        try:
            return settings.get(key)
        except (KeyError, sqlite3.Error):
            pass
    if not _DEFAULTS:
        _DEFAULTS.update(load_defaults())
    return _DEFAULTS[key]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def live_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "live"


def log_path(data_dir: Path, live_id: str) -> Path:
    return live_dir(data_dir) / f"{live_id}.log"


def valid_id(live_id: str) -> bool:
    return bool(_ID_RE.match(live_id or ""))


# ---------- masking ----------


def mask(text: str, env: dict[str, str] | None = None) -> str:
    """Hide secret values: env values whose name looks secret, and token-shaped text."""
    for name, value in (env or {}).items():
        if value and len(value) >= 8 and _SECRET_NAME_RE.search(name):
            text = text.replace(value, "[hidden]")
    return _SECRET_TEXT_RE.sub("[hidden]", text)


# ---------- the pid check ----------


def _proc_start(pid: int) -> str:
    """Start time of a process in clock ticks since boot, '' when it is not readable."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    rest = raw[raw.rfind(")") + 2 :].split()
    return rest[19] if len(rest) > 19 else ""


def _proc_state(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    return raw[raw.rfind(")") + 2 :][:1]


def pid_alive(pid: int) -> bool:
    return _proc_state(pid) not in ("", "Z", "X")


def verify_pid(pid: int, pid_start: str) -> bool:
    """True only when `pid` is still the claude process we started: the start time matches and
    one of the first words of its command line is `claude`."""
    if pid <= 1 or not pid_start or _proc_start(pid) != pid_start:
        return False
    try:
        words = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:3]
    except OSError:
        return False
    return any(
        _CLAUDE_ARG_RE.match(os.path.basename(w.decode(errors="replace"))) for w in words if w
    )


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def stop_group(pid: int, pgid: int, grace: float) -> None:
    """SIGTERM the group, wait up to `grace` seconds for the leader, then SIGKILL the group."""
    _signal_group(pgid, signal.SIGTERM)
    end = time.monotonic() + max(0.0, float(grace))
    while time.monotonic() < end and pid_alive(pid):
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)  # also sweeps children that ignored SIGTERM


# ---------- registry (rows in claude_live) ----------


def prune(conn: sqlite3.Connection, data_dir: Path, keep_days: int) -> None:
    """Delete finished rows and live logs older than `keep_days`."""
    cutoff = datetime.now(UTC) - timedelta(days=int(keep_days))
    conn.execute(
        "DELETE FROM claude_live WHERE ended IS NOT NULL AND ended < ?",
        (cutoff.isoformat(timespec="seconds"),),
    )
    folder = live_dir(data_dir)
    if folder.is_dir():
        for path in folder.glob("*.log"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff.timestamp():
                    path.unlink()


def reap_stale(conn: sqlite3.Connection, stale_seconds: int) -> int:
    """A running row with no sign of life for a while (its process died hard) becomes failed."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=int(stale_seconds))).isoformat(
        timespec="seconds"
    )
    cur = conn.execute(
        "UPDATE claude_live SET status='failed', ended=? WHERE status=? "
        "AND COALESCE(beat, started) < ?",
        (_now(), RUNNING, cutoff),
    )
    return cur.rowcount


def mark_cancelled(conn: sqlite3.Connection, live_id: str) -> None:
    """Row -> cancelled. The linked action fails with "Cancelled by you"; the linked task run
    ends as cancelled. Safe to call twice."""
    row = conn.execute(
        "SELECT action_id, run_id FROM claude_live WHERE id = ?", (live_id,)
    ).fetchone()
    if row is None:
        return
    now = _now()
    conn.execute(
        "UPDATE claude_live SET status='cancelled', ended=COALESCE(ended, ?), cancel_requested=1 "
        "WHERE id=? AND status=?",
        (now, live_id, RUNNING),
    )
    if row["action_id"]:
        conn.execute(
            "UPDATE actions SET status='failed', ended=?, error=? "
            "WHERE id=? AND status IN ('queued','running')",
            (now, CANCELLED_TEXT, row["action_id"]),
        )
    if row["run_id"]:
        conn.execute(
            "UPDATE runs SET status='cancelled', ended=?, error=? "
            "WHERE run_id=? AND status='running'",
            (now, CANCELLED_TEXT, row["run_id"]),
        )


def request_cancel(conn: sqlite3.Connection, data_dir: Path, live_id: str, grace: float) -> str:
    """The Cancel click. Returns 'stopped', 'requested', 'gone' or 'unknown'.

    stopped    the pid was verified on this machine and the group was stopped
    requested  the owner process (maybe another container) was asked to stop it; it will
    gone       the row was not running any more (nothing to do)
    """
    row = conn.execute("SELECT * FROM claude_live WHERE id = ?", (live_id,)).fetchone()
    if row is None:
        return "unknown"
    if row["status"] != RUNNING:
        return "gone"
    conn.execute("UPDATE claude_live SET cancel_requested=1 WHERE id=?", (live_id,))
    pid, pgid = int(row["pid"]), int(row["pgid"])
    if row["host"] == socket.gethostname() and verify_pid(pid, row["pid_start"]):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if os.getpgid(pid) != pgid:
                return "requested"  # not the group we started: leave it to the owner
        _note(data_dir, live_id, "Cancelled by you.")
        stop_group(pid, pgid, grace)
        mark_cancelled(conn, live_id)
        return "stopped"
    return "requested"


# ---------- the log ----------


def _note(data_dir: Path, live_id: str, text: str, env: dict[str, str] | None = None) -> None:
    try:
        with open(log_path(data_dir, live_id), "a", encoding="utf-8") as f:
            f.write(mask(text, env).rstrip("\n") + "\n")
    except OSError:
        pass


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(1, limit - 3)] + "..."


def trim_prompt(cmd: list[str], limit: int) -> str:
    try:
        text = cmd[cmd.index("-p") + 1]
    except (ValueError, IndexError):
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ... [cut]"


def stream_lines(line: str, limit: int) -> list[str]:
    """Plain log lines for one stream-json line. Non-JSON lines give nothing."""
    line = line.strip()
    if not line.startswith("{"):
        return []
    try:
        event = json.loads(line)
    except ValueError:
        return []
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    out: list[str] = []
    if kind == "assistant":
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                for part in str(block["text"]).splitlines():
                    if part.strip():
                        out.append(_clip(part, limit))
            elif block.get("type") == "tool_use":
                args = json.dumps(block.get("input", {}), ensure_ascii=False)
                out.append(_clip(f"> tool: {block.get('name', '?')} {args}", limit))
    elif kind == "user":
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                bad = " (error)" if block.get("is_error") else ""
                out.append(f"< tool result{bad}")
    elif kind == "result":
        cost = event.get("total_cost_usd")
        out.append(
            f"[finished] cost ${float(cost):.3f}"
            if isinstance(cost, (int, float))
            else "[finished]"
        )
    return out


def tail(data_dir: Path, live_id: str, lines: int) -> str:
    """The last `lines` lines of a live log ('' when there is none)."""
    if not valid_id(live_id):
        return ""
    try:
        size = log_path(data_dir, live_id).stat().st_size
        with open(log_path(data_dir, live_id), "rb") as f:
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-max(1, int(lines)) :])


# ---------- running claude ----------


class _Pump(threading.Thread):
    """Reads a pipe to the end. For stdout it also appends live lines to the log."""

    def __init__(self, stream, sink: list[str], on_line=None):
        super().__init__(daemon=True)
        self.stream, self.sink, self.on_line = stream, sink, on_line

    def run(self) -> None:
        try:
            for line in self.stream:
                self.sink.append(line)
                if self.on_line is not None:
                    self.on_line(line)
        except (OSError, ValueError):
            pass


def _db_try(fn, *args) -> Any:
    """The registry must never break a claude call."""
    try:
        return fn(*args)
    except sqlite3.Error:
        log.exception("claude_live registry error")
        return None


def run_process(
    cmd: list[str],
    *,
    cwd: str | Path | None,
    env: dict[str, str],
    timeout: float,
    conn: sqlite3.Connection | None = None,
    data_dir: Path | None = None,
    settings: Settings | None = None,
    kind: str = "claude",
    label: str = "",
    model: str | None = None,
    run_id: str | None = None,
    stream: bool = False,
) -> subprocess.CompletedProcess:
    """Run `cmd` like `subprocess.run(capture_output=True, text=True, stdin=DEVNULL)`.

    Raises FileNotFoundError and subprocess.TimeoutExpired like subprocess.run, and
    ClaudeCancelled after a Cancel. Without `conn` and `data_dir` nothing is registered.

    `stream=True` on a `--output-format json` command asks for stream-json instead (live text);
    the returned stdout is then the final `result` event, so callers parse it as before.
    """
    cmd = list(cmd)
    collapse = False
    if stream and bool(_setting(settings, "live.stream_claude")) and "--output-format" in cmd:
        i = cmd.index("--output-format") + 1
        if i < len(cmd) and cmd[i] == "json":
            cmd[i] = "stream-json"
            if "--verbose" not in cmd:
                cmd.append("--verbose")
            collapse = True
    if model is None and "--model" in cmd and cmd.index("--model") + 1 < len(cmd):
        model = cmd[cmd.index("--model") + 1]
    registered = conn is not None and data_dir is not None
    line_chars = int(_setting(settings, "live.line_chars"))

    proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    live_id = secrets.token_hex(8)
    action_id = CURRENT_ACTION.get()
    if registered:
        _start_row(conn, data_dir, settings, live_id, proc, kind, label, action_id, run_id, model)
        _db_try(_head, data_dir, settings, live_id, cmd, label, model, env)

    log_max = int(_setting(settings, "live.log_max_kb")) * 1024
    written = [0]

    def on_line(line: str) -> None:
        if not registered or written[0] > log_max:
            return
        for text in stream_lines(line, line_chars):
            written[0] += len(text) + 1
            _note(data_dir, live_id, text, env)

    out_lines: list[str] = []
    err_lines: list[str] = []
    pumps = [_Pump(proc.stdout, out_lines, on_line), _Pump(proc.stderr, err_lines)]
    for p in pumps:
        p.start()

    grace = float(_setting(settings, "live.cancel_grace_seconds"))
    check = max(0.05, float(_setting(settings, "live.check_seconds")))
    beat_every = float(_setting(settings, "live.heartbeat_seconds"))
    deadline = time.monotonic() + float(timeout)
    next_beat = time.monotonic() + beat_every
    cancelled = False
    timed_out = False
    try:
        while True:
            try:
                proc.wait(timeout=check)
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                stop_group(proc.pid, proc.pid, grace)
                break
            if registered:
                if now >= next_beat:
                    next_beat = now + beat_every
                    _db_try(_beat, conn, live_id)
                if _db_try(_wants_cancel, conn, live_id):
                    cancelled = True
                    _note(data_dir, live_id, "Cancelled by you.")
                    stop_group(proc.pid, proc.pid, grace)
                    break
    except BaseException:
        stop_group(proc.pid, proc.pid, 0)
        proc.wait()
        if registered:
            _db_try(_finish_row, conn, live_id, "failed")
        raise
    finally:
        with contextlib.suppress(Exception):
            proc.wait(timeout=grace + 5)
        for p in pumps:
            p.join(timeout=5)

    if registered and not cancelled:
        cancelled = bool(_db_try(_was_cancelled, conn, live_id))
    if cancelled:
        if registered:
            _db_try(mark_cancelled, conn, live_id)
        raise ClaudeCancelled(CANCELLED_TEXT)
    if timed_out:
        if registered:
            _db_try(_finish_row, conn, live_id, "failed")
            _note(data_dir, live_id, "Timed out.")
        raise subprocess.TimeoutExpired(cmd, timeout)

    stdout = "".join(out_lines)
    if collapse:
        stdout = _final_result(stdout) or stdout
    rc = proc.returncode
    if registered:
        _db_try(_finish_row, conn, live_id, "done" if rc == 0 else "failed")
        if rc != 0:
            _note(data_dir, live_id, f"[exit code {rc}]")
    return subprocess.CompletedProcess(cmd, rc, stdout, "".join(err_lines))


def _final_result(stdout: str) -> str:
    """The last `result` event of a stream-json output ('' if there is none)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"result"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return line
    return ""


def _start_row(conn, data_dir, settings, live_id, proc, kind, label, action_id, run_id, model):
    def work() -> None:
        global _last_prune
        live_dir(data_dir).mkdir(parents=True, exist_ok=True)
        now = _now()
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        conn.execute(
            "INSERT INTO claude_live(id, pid, pgid, pid_start, host, kind, label, action_id, "
            "run_id, model, started, status, beat) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                live_id, proc.pid, pgid, _proc_start(proc.pid), socket.gethostname(),
                kind if kind in KINDS else "claude", _clip(label or kind, 120),
                action_id, run_id, model, now, RUNNING, now,
            ),
        )  # fmt: skip
        if time.monotonic() - _last_prune > 3600:
            _last_prune = time.monotonic()
            prune(conn, data_dir, int(_setting(settings, "live.log_keep_days")))

    _db_try(work)


def _head(data_dir, settings, live_id, cmd, label, model, env) -> None:
    prompt = trim_prompt(cmd, int(_setting(settings, "live.prompt_chars")))
    path = log_path(data_dir, live_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(mask(f"# {label or 'claude'} ({model or 'default model'})\n", env))
        if prompt:
            f.write(mask("# prompt:\n" + prompt + "\n# live output:\n", env))


def _beat(conn, live_id: str) -> None:
    conn.execute(
        "UPDATE claude_live SET beat=? WHERE id=? AND status=?", (_now(), live_id, RUNNING)
    )


def _wants_cancel(conn, live_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested, status FROM claude_live WHERE id=?", (live_id,)
    ).fetchone()
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))


def _was_cancelled(conn, live_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested, status FROM claude_live WHERE id=?", (live_id,)
    ).fetchone()
    return bool(row and (row["status"] == "cancelled" or row["cancel_requested"]))


def _finish_row(conn, live_id: str, status: str) -> None:
    conn.execute(
        "UPDATE claude_live SET status=?, ended=? WHERE id=? AND status=?",
        (status, _now(), live_id, RUNNING),
    )


# ---------- reads for the dock ----------


def running_rows(
    conn: sqlite3.Connection, data_dir: Path, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Running jobs for the dock, oldest first, each with its live log tail."""
    reap_stale(conn, int(_setting(settings, "live.stale_seconds")))
    rows = conn.execute(
        "SELECT l.*, a.label AS action_label FROM claude_live l "
        "LEFT JOIN actions a ON a.id = l.action_id WHERE l.status = ? ORDER BY l.started",
        (RUNNING,),
    ).fetchall()
    lines = int(_setting(settings, "live.tail_lines"))
    now = datetime.now(UTC)
    out = []
    for r in rows:
        try:
            secs = int((now - datetime.fromisoformat(r["started"])).total_seconds())
        except ValueError:
            secs = 0
        out.append(
            {
                "id": r["id"],
                "label": r["action_label"] or r["label"],
                "model": r["model"] or "",
                "kind": r["kind"],
                "is_write": r["kind"] == "write",
                "seconds": max(0, secs),
                "elapsed": elapsed_text(secs),
                "stopping": bool(r["cancel_requested"]),
                "tail": tail(data_dir, r["id"], lines),
            }
        )
    return out


def elapsed_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}m {sec:02d}s" if minutes else f"{sec}s"
