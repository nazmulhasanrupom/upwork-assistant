"""Inbox page `/inbox` (plan H3). Login and CSRF come from the app-wide guard.

- GET  /inbox                       open invites and the stored chat rooms, each with an Upwork link
- POST /inbox/refresh               read the rooms (READ bridge, get_messages) and store them
- POST /inbox/invite/<id>/draft     one Claude call: a reply draft for the invite
- POST /inbox/room/<id>/draft       the same for a chat room

Nothing is sent: the draft is shown with a copy button and the reply is posted by hand on Upwork.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app import inbox
from app.auth_routes import render
from app.llm.runner import ClaudeError
from app.upwork.client import UpworkAuthError, UpworkMCPError
from app.upwork.routes import bridge_for

log = logging.getLogger("inbox")

router = APIRouter()


def _page(request: Request, status: int = 200, **ctx: Any) -> Response:
    s = request.app.state
    ctx.setdefault("notice", None)
    ctx.setdefault("cost", None)
    return render(
        request,
        "inbox.html",
        status,
        invites=inbox.list_invites(s.db),
        rooms=inbox.list_rooms(s.db),
        fetched_at=inbox.last_fetch(s.db),
        **ctx,
    )


@router.get("/inbox")
def inbox_page(request: Request) -> Response:
    return _page(request)


@router.post("/inbox/refresh")
def refresh(request: Request) -> Response:
    s = request.app.state
    client = None
    try:
        client = bridge_for(request)
        n = inbox.refresh_rooms(client, s.db, s.settings)
    except inbox.InboxError as e:
        return _page(request, 400, notice=("error", str(e)))
    except UpworkAuthError as e:
        return _page(request, 400, notice=("error", str(e)))
    except (UpworkMCPError, ClaudeError) as e:
        return _page(request, 502, notice=("error", f"Could not read the messages: {str(e)[:200]}"))
    except Exception:
        log.exception("Inbox refresh failed")
        return _page(request, 500, notice=("error", "Could not read the messages. See the log."))
    finally:
        if client is not None:
            try:
                getattr(client, "close", lambda: None)()
            except Exception:
                log.exception("Could not close the Upwork bridge")
    text = f"Read {n} chat room(s)." if n else "No chat rooms came back."
    return _page(request, notice=("ok", text))


def _draft(request: Request, kind: str, ref: str) -> Response:
    s = request.app.state
    try:
        _, cost = inbox.draft_reply(s.db, s.config, s.settings, kind, ref)
    except inbox.InboxError as e:
        return _page(request, 400, notice=("error", str(e)))
    except Exception:
        log.exception("Draft reply failed")
        return _page(request, 500, notice=("error", "Could not write the reply. See the log."))
    return _page(request, notice=("ok", "Draft ready. Nothing was sent."), cost=cost)


@router.post("/inbox/invite/{invite_id}/draft")
def draft_invite(request: Request, invite_id: str) -> Response:
    return _draft(request, "invite", invite_id)


@router.post("/inbox/room/{room_id}/draft")
def draft_room(request: Request, room_id: str) -> Response:
    return _draft(request, "room", room_id)
