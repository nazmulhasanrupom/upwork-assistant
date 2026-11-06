"""Upwork page routes (plan P2, P3).

- GET  /upwork          login: bridge status, freelancer account in use, how to log in
- POST /upwork/test     login + CSRF: one `list_accounts` batch through the Claude bridge
- POST /upwork/account  login + CSRF: pick one of several detected TALENT accounts
- POST /upwork/import   login + CSRF: upload .credentials.json from a laptop (plan P4, "Advanced")
- POST /upwork/login/start   login + CSRF: start `claude mcp login upwork` in a pty (plan P5)
- POST /upwork/login/finish  login + CSRF: paste the localhost address, finish, then Test connection
- POST /upwork/login/cancel  login + CSRF: stop the login in progress

There are no /oauth/* routes any more: Upwork refuses our own OAuth client (allow list), so the
login lives in Claude Code (see `app/upwork/bridge.py`).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from app.auth_routes import render
from app.llm.runner import ClaudeError, claude_config_dir, claude_env
from app.upwork import accounts, login_import, mcp_login
from app.upwork.bridge import ClaudeUpworkBridge, last_fetch
from app.upwork.client import UpworkAuthError, UpworkMCPError

log = logging.getLogger("upwork.routes")

router = APIRouter()

MAX_SHOWN = 200  # cut text from tools/Claude before showing it

LOGIN_COMMANDS = (
    "export CLAUDE_CONFIG_DIR=$HOME/upwork-claude-test\n"
    "claude mcp add --transport http --scope user upwork https://mcp.upwork.com/mcp\n"
    "claude"
)


def mcp_url(settings) -> str:
    """The Upwork MCP URL: env UPWORK_MCP_URL (same as the entry script), else the setting."""
    return os.environ.get("UPWORK_MCP_URL", "").strip() or str(settings.get("upwork.mcp_url"))


def bridge_for(request: Request):
    """A test can set `app.state.mcp_factory` (a function returning an object with call_many)."""
    s = request.app.state
    factory = getattr(s, "mcp_factory", None)
    if factory:
        return factory()
    return ClaudeUpworkBridge(s.config, s.settings, s.db)


def record_test(settings, status: str, message: str) -> None:
    """Remember the last Test connection result for the status line on /upwork."""
    settings.set("upwork.last_test_status", status)
    settings.set("upwork.last_test_at", datetime.now(UTC).isoformat(timespec="seconds"))
    settings.set("upwork.last_test_message", message[:MAX_SHOWN])


def test_connection(request: Request) -> tuple[str, str]:
    """Run one list_accounts batch and save the result. Returns (notice, error); never raises."""
    notice, error, status = _run_test(request)
    record_test(request.app.state.settings, status, notice or error)
    return notice, error


def _run_test(request: Request) -> tuple[str, str, str]:
    s = request.app.state
    client = None
    try:
        client = bridge_for(request)
        res = accounts.detect_and_save(client, s.settings)
    except UpworkAuthError as e:
        return "", str(e)[:MAX_SHOWN], "needs_login"
    except (UpworkMCPError, ClaudeError, accounts.AccountError) as e:
        return "", f"Test failed: {str(e)[:MAX_SHOWN]}", "failed"
    except Exception:
        log.exception("Upwork test connection failed")
        return "", "Test failed. See the log for details.", "failed"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                log.exception("Could not close the Upwork bridge")
    if res.status == "none":
        return "", res.message, "failed"
    return res.message, "", "ok"


def _page(request: Request, status_code: int = 200, **ctx) -> Response:
    s = request.app.state
    ctx.setdefault("login_failed", False)
    return render(
        request,
        "upwork.html",
        status_code,
        account=accounts.current(s.settings),
        candidates=accounts.candidates(s.settings),
        fetch=last_fetch(s.db),
        test={
            "status": s.settings.get("upwork.last_test_status"),
            "at": s.settings.get("upwork.last_test_at"),
            "message": s.settings.get("upwork.last_test_message"),
        },
        login=mcp_login.manager.active(),
        login_minutes=max(1, mcp_login.manager.remaining_seconds() // 60),
        model=s.settings.get("models.fetch"),
        login_commands=LOGIN_COMMANDS,
        max_upload_kb=login_import.MAX_UPLOAD_BYTES // 1024,
        **ctx,
    )


@router.get("/upwork")
def upwork_page(request: Request):
    return _page(request, notice="", error="")


@router.post("/upwork/test")
def test_post(request: Request):
    notice, error = test_connection(request)
    return _page(request, 400 if error else 200, notice=notice, error=error, tested=not error)


@router.post("/upwork/account")
def pick_account(request: Request, org_uid: str = Form("")):
    try:
        picked = accounts.pick_candidate(request.app.state.settings, org_uid)
    except accounts.AccountError as e:
        return _page(request, 400, notice="", error=str(e))
    return _page(request, notice=f"Using freelancer account: {picked.get('name', '')}.", error="")


@router.post("/upwork/import")
async def import_login_post(request: Request, file: Annotated[UploadFile | None, File()] = None):
    """Save the Upwork part of a Claude Code login. Never logs or shows token values."""
    s = request.app.state
    if file is None or not file.filename:
        return _page(request, 400, notice="", error="Choose the .credentials.json file first.")
    raw = await file.read(login_import.MAX_UPLOAD_BYTES + 1)
    try:
        login_import.import_login(raw, claude_config_dir(s.config, s.settings), mcp_url(s.settings))
    except login_import.LoginImportError as e:
        return _page(request, 413 if "too big" in str(e) else 400, notice="", error=str(e))
    except OSError:
        log.exception("Could not save the Claude login")
        return _page(request, 500, notice="", error="Could not save the login. See the log.")
    log.info("Claude login for the Upwork server imported")
    return _page(request, notice="Login imported. Click Test connection.", error="")


# ---------- one-click login (plan P5) ----------


def _login_env(request: Request) -> tuple[dict[str, str], str]:
    s = request.app.state
    config_dir = claude_config_dir(s.config, s.settings)
    config_dir.mkdir(parents=True, exist_ok=True)
    env = claude_env(s.config)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env, str(config_dir)


@router.post("/upwork/login/start")
def login_start(request: Request):
    env, cwd = _login_env(request)
    try:
        mcp_login.manager.start(env, cwd)
    except mcp_login.LoginError as e:
        return _page(request, 400, notice="", error=str(e), login_failed=True)
    except Exception:
        log.exception("Could not start the Upwork login")
        return _page(
            request,
            500,
            notice="",
            error="Could not start the login. See the log.",
            login_failed=True,
        )
    return _page(request, notice="", error="")


@router.post("/upwork/login/finish")
def login_finish(request: Request, pasted: str = Form("")):
    ok, message = mcp_login.manager.finish(pasted)
    if not ok:
        # A bad address leaves the session open (the paste box shows again); otherwise it is over.
        return _page(
            request,
            400,
            notice="",
            error=message,
            login_failed=mcp_login.manager.active() is None,
        )
    notice, error = test_connection(request)
    if error:
        return _page(
            request,
            400,
            notice="",
            error=f"The login was saved, but Test connection failed: {error}",
        )
    return _page(request, notice=f"Upwork login saved. {notice}", error="")


@router.post("/upwork/login/cancel")
def login_cancel(request: Request):
    mcp_login.manager.cancel()
    return _page(request, notice="Login cancelled.", error="")
