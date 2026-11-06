"""Runs `claude -p` headless in a run folder and records usage (SPEC 7.2, 7.3, 14.3).

Claude gets no tools except Read/Write and no MCP servers. It reads `input.json`
and writes `output.json` inside `<DATA_DIR>/runs/<run_id>/`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Config
from app.settings import Settings

log = logging.getLogger("claude")

APP_ROOT = Path(__file__).resolve().parent.parent.parent
EMPTY_MCP_CONFIG = APP_ROOT / "config" / "empty-mcp.json"

# `claude --help` (2.1.318) lists: --effort <level>  (low, medium, high, xhigh, max)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

RUN_FOOTER = (
    "\n\nRead the file input.json in the current folder. "
    "Write your final answer as one JSON object to output.json in the current folder."
)

UNTRUSTED_TAG = "untrusted_job_post"

_RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|usage limit|session limit|limit reached|too many requests",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"\b401\b|authentication|invalid api key|invalid x-api-key|not logged in|/login"
    r"|oauth token|invalid.{0,20}token|token.{0,20}(expired|invalid|revoked)",
    re.IGNORECASE,
)


class ClaudeError(RuntimeError):
    """Base class for Claude runner problems."""


class BudgetCapReached(ClaudeError):
    """The monthly Claude cost cap is reached. No call was made."""


class ClaudeRateLimited(ClaudeError):
    """Claude returned a 429 or a session/usage limit. Try again next cycle."""


class ClaudeFailed(ClaudeError):
    """The call failed for another reason (bad JSON, no output, timeout, ...)."""


def looks_rate_limited(text: str) -> bool:
    return bool(_RATE_LIMIT_RE.search(text or ""))


def looks_like_auth_error(text: str) -> bool:
    return bool(_AUTH_RE.search(text or ""))


def claude_env(config: Config | None = None) -> dict[str, str]:
    """Environment for the claude child process. Passes the token or key through."""
    env = dict(os.environ)
    if config is not None:
        if config.claude_code_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = config.claude_code_oauth_token
        if config.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    return env


# ---------- monthly cap ----------


def _zone(config: Config, settings: Settings) -> ZoneInfo:
    for name in (settings.get("general.timezone"), config.tz, "UTC"):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError, KeyError):
            continue
    return ZoneInfo("UTC")


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def month_bounds_utc(tz: ZoneInfo, now: datetime | None = None) -> tuple[str, str]:
    """Start (inclusive) and end (exclusive) of the current month in `tz`, as UTC text."""
    now = (now or datetime.now(UTC)).astimezone(tz)
    start = datetime(now.year, now.month, 1, tzinfo=tz)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=tz)
    return _utc_iso(start), _utc_iso(end)


def monthly_spend(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    now: datetime | None = None,
) -> float:
    """Total `cost_usd` in claude_calls for the current month in the configured zone."""
    start, end = month_bounds_utc(_zone(config, settings), now)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM claude_calls "
        "WHERE created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone()
    return float(row[0])


def check_monthly_cap(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    cap = float(settings.get("claude.monthly_cap_usd"))
    spent = monthly_spend(conn, config, settings, now)
    if spent >= cap:
        raise BudgetCapReached(
            f"Monthly Claude cap reached: ${spent:.2f} spent, cap is ${cap:.2f}."
        )


# ---------- input ----------


def _neutralize(text: str) -> str:
    """Stop job text from closing or opening our untrusted tag."""
    return re.sub(rf"<(/?)\s*{UNTRUSTED_TAG}", r"&lt;\1" + UNTRUSTED_TAG, text, flags=re.I)


def _wrap_job(job: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in job.items() if k != "job_id"}
    text = _neutralize(json.dumps(body, ensure_ascii=False, indent=1))
    wrapped: dict[str, Any] = {UNTRUSTED_TAG: f"<{UNTRUSTED_TAG}>\n{text}\n</{UNTRUSTED_TAG}>"}
    if "job_id" in job:
        wrapped = {"job_id": job["job_id"], **wrapped}
    return wrapped


def prepare_input(input_obj: dict[str, Any]) -> dict[str, Any]:
    """Wrap job posts (`job` or `jobs` keys) in <untrusted_job_post> tags (SPEC 14.3)."""
    out = dict(input_obj)
    if isinstance(out.get("job"), dict):
        out["job"] = _wrap_job(out["job"])
    if isinstance(out.get("jobs"), list):
        out["jobs"] = [_wrap_job(j) if isinstance(j, dict) else j for j in out["jobs"]]
    return out


# ---------- run ----------


def _new_run_id(task: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", task) or "task"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{safe}-{stamp}-{uuid.uuid4().hex[:6]}"


def build_command(
    prompt: str,
    model: str,
    max_turns: int,
    max_budget_usd: float,
    effort: str | None = None,
) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--strict-mcp-config",
        "--mcp-config",
        str(EMPTY_MCP_CONFIG),
        "--tools",
        "Read,Write",
        "--permission-mode",
        "acceptEdits",  # lets Write create output.json in the run folder without a prompt
        "--max-turns",
        str(int(max_turns)),
        "--max-budget-usd",
        f"{float(max_budget_usd):.2f}",
    ]
    if effort:
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"Unknown effort level {effort!r}. Use one of {EFFORT_LEVELS}.")
        cmd += ["--effort", effort]
    return cmd


def _save_call(
    conn: sqlite3.Connection, run_id: str, model: str, data: dict[str, Any]
) -> tuple[int, int, float]:
    usage = data.get("usage") or {}
    n_in = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )
    n_out = int(usage.get("output_tokens") or 0)
    cost = float(data.get("total_cost_usd") or 0.0)
    if n_in or n_out or cost:
        conn.execute(
            "INSERT INTO claude_calls(run_id, model, input_tokens, output_tokens, cost_usd, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, model, n_in, n_out, cost, _utc_iso(datetime.now(UTC))),
        )
    return n_in, n_out, cost


def run_claude(
    task: str,
    prompt_file: str | Path,
    input_obj: dict[str, Any],
    model: str,
    max_turns: int,
    max_budget_usd: float,
    *,
    config: Config,
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    effort: str | None = None,
    run_id: str | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    """Run one headless Claude call. Returns a dict:

    `output` (parsed output.json), `run_id`, `run_dir`, `model`, `input_tokens`,
    `output_tokens`, `cost_usd`, `num_turns`.

    Raises BudgetCapReached (before any call), ClaudeRateLimited, ClaudeFailed.
    """
    settings = settings or Settings(conn)
    check_monthly_cap(conn, config, settings)

    prompt_path = Path(prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = APP_ROOT / prompt_path
    prompt = prompt_path.read_text(encoding="utf-8") + RUN_FOOTER

    run_id = run_id or _new_run_id(task)
    run_dir = config.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(
        json.dumps(prepare_input(input_obj), ensure_ascii=False, indent=1), encoding="utf-8"
    )

    cmd = build_command(prompt, model, max_turns, max_budget_usd, effort)
    log.info("claude call: task=%s model=%s run=%s", task, model, run_id)
    try:
        proc = subprocess.run(
            cmd,
            cwd=run_dir,
            env=claude_env(config),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ClaudeFailed("The `claude` command was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeFailed(f"claude timed out after {timeout} seconds.") from exc

    (run_dir / "result.json").write_text(proc.stdout or "", encoding="utf-8")
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"

    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError):
        if looks_rate_limited(combined):
            raise ClaudeRateLimited("Claude is rate limited (429 or session limit).") from None
        raise ClaudeFailed(
            f"claude did not return valid JSON (exit code {proc.returncode})."
        ) from None

    n_in, n_out, cost = _save_call(conn, run_id, model, data)

    failed = bool(data.get("is_error")) or proc.returncode != 0
    if failed:
        message = str(data.get("result") or proc.stderr or "unknown error")
        if data.get("api_error_status") == 429 or looks_rate_limited(message):
            raise ClaudeRateLimited(f"Claude is rate limited: {message[:200]}")
        raise ClaudeFailed(f"claude failed ({data.get('subtype') or 'error'}): {message[:300]}")

    out_path = run_dir / "output.json"
    if not out_path.exists():
        raise ClaudeFailed("claude finished but wrote no output.json.")
    try:
        output = json.loads(out_path.read_text(encoding="utf-8"))
    except ValueError:
        raise ClaudeFailed("output.json is not valid JSON.") from None

    return {
        "output": output,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "model": model,
        "input_tokens": n_in,
        "output_tokens": n_out,
        "cost_usd": cost,
        "num_turns": data.get("num_turns"),
    }
