"""Check that Claude works: python -m app.tools.claude_check

Runs `claude -p "say ok" --output-format json --max-turns 1` and prints OK or a clear
error. Needs no SECRET_KEY. Never prints the token.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from app.llm import live
from app.llm.runner import looks_like_auth_error, looks_rate_limited

TIMEOUT_SECONDS = 120
CMD = ["claude", "-p", "say ok", "--output-format", "json", "--max-turns", "1"]


def check(
    env: dict[str, str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    data_dir: Path | None = None,
) -> tuple[bool, str]:
    """Returns (ok, message). With `conn` and `data_dir` the call shows in the Claude jobs dock."""
    env = dict(os.environ if env is None else env)
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
        return False, (
            "NO TOKEN: paste the token from `claude setup-token` on the Claude step "
            "(or in Settings, Claude)."
        )
    try:
        with tempfile.TemporaryDirectory() as workdir:
            proc = live.run_process(
                CMD,
                cwd=workdir,
                env=env,
                timeout=TIMEOUT_SECONDS,
                conn=conn,
                data_dir=data_dir,
                kind="check",
                label="Check Claude",
            )
    except FileNotFoundError:
        return False, "CLAUDE NOT FOUND: the `claude` command is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT: claude did not answer within {TIMEOUT_SECONDS} seconds."

    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    data = None
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict):
            data = parsed
    except (ValueError, TypeError):
        pass

    if data is not None and not data.get("is_error") and proc.returncode == 0:
        return True, "OK"

    detail = str((data or {}).get("result") or proc.stderr or proc.stdout or "").strip()
    if data is not None and data.get("api_error_status") == 429 or looks_rate_limited(text):
        return False, "RATE LIMIT: Claude hit a 429 or session limit. Try again later."
    if looks_like_auth_error(text):
        return (
            False,
            "BAD TOKEN: Claude refused the login. "
            "Make a new token with `claude setup-token` and paste it again.",
        )
    return False, f"ERROR: claude failed (exit code {proc.returncode}). {detail[:300]}"


def main() -> int:
    ok, message = check()
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
