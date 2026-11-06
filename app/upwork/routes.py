"""Upwork page routes (plan P2, P3).

- GET  /upwork          login: bridge status, freelancer account in use, how to log in
- POST /upwork/test     login + CSRF: one `list_accounts` batch through the Claude bridge
- POST /upwork/account  login + CSRF: pick one of several detected TALENT accounts

There are no /oauth/* routes any more: Upwork refuses our own OAuth client (allow list), so the
login lives in Claude Code (see `app/upwork/bridge.py`).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from app.auth_routes import render
from app.llm.runner import ClaudeError
from app.upwork import accounts
from app.upwork.bridge import ClaudeUpworkBridge, last_fetch
from app.upwork.client import UpworkAuthError, UpworkMCPError

log = logging.getLogger("upwork.routes")

router = APIRouter()

MAX_SHOWN = 200  # cut text from tools/Claude before showing it

LOGIN_STEPS = [
    "On your own computer, install Claude Code. Use a new empty config folder so nothing else is "
    "mixed in: export CLAUDE_CONFIG_DIR=$HOME/upwork-claude",
    "With that variable set, add the Upwork server: claude mcp add --transport http --scope user "
    "upwork https://mcp.upwork.com/mcp",
    "Start claude, type /mcp, choose upwork, and log in to Upwork in the browser. Then quit.",
    "Copy the file .credentials.json from that folder into /data/claude/ in this app's "
    "container (docker cp, or the Coolify terminal/file tools). Keep it private: it holds your "
    "Upwork login.",
    "The Upwork server must also be listed in the container's Claude config. Until the "
    "container does that by itself, run the same claude mcp add command inside the container "
    "with CLAUDE_CONFIG_DIR=/data/claude.",
    "Come back to this page and press Test connection.",
]


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
        steps=LOGIN_STEPS,
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
