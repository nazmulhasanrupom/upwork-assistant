"""Upwork page routes (plan P2, P3).

- GET  /upwork          login: bridge status, freelancer account in use, how to log in
- POST /upwork/test     login + CSRF: one `list_accounts` batch through the Claude bridge
- POST /upwork/account  login + CSRF: pick one of several detected TALENT accounts
- POST /upwork/import   login + CSRF: upload .credentials.json from a laptop (plan P4)

There are no /oauth/* routes any more: Upwork refuses our own OAuth client (allow list), so the
login lives in Claude Code (see `app/upwork/bridge.py`).
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from app.auth_routes import render
from app.llm.runner import ClaudeError, claude_config_dir
from app.upwork import accounts, login_import
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


def test_connection(request: Request) -> tuple[str, str]:
    """Run one list_accounts batch and save the result. Returns (notice, error); never raises."""
    s = request.app.state
    client = None
    try:
        client = bridge_for(request)
        res = accounts.detect_and_save(client, s.settings)
    except UpworkAuthError as e:
        return "", str(e)[:MAX_SHOWN]
    except (UpworkMCPError, ClaudeError, accounts.AccountError) as e:
        return "", f"Test failed: {str(e)[:MAX_SHOWN]}"
    except Exception:
        log.exception("Upwork test connection failed")
        return "", "Test failed. See the log for details."
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                log.exception("Could not close the Upwork bridge")
    if res.status == "none":
        return "", res.message
    return res.message, ""


def _page(request: Request, status_code: int = 200, **ctx) -> Response:
    s = request.app.state
    return render(
        request,
        "upwork.html",
        status_code,
        account=accounts.current(s.settings),
        candidates=accounts.candidates(s.settings),
        fetch=last_fetch(s.db),
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
