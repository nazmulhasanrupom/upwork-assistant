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
from fastapi.responses import RedirectResponse, Response

from app import inbox
from app.auth_routes import render
from app.llm.runner import ClaudeError
from app.upwork.bridge import ClaudeUpworkBridge
from app.upwork.client import UpworkAuthError, UpworkMCPError
from app.worker import WorkerError, known_action

log = logging.getLogger("inbox")

router = APIRouter()


def _page(request: Request, status: int = 200, **ctx: Any) -> Response:
    s = request.app.state
    ctx.setdefault("notice", None)
    ctx.setdefault("cost", None)
    ctx.setdefault("action_id", "")
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
def inbox_page(request: Request, action: str = "") -> Response:
    return _page(request, action_id=known_action(request.app.state.worker, action))


# Refresh and the draft calls are slow, so they run on the worker (plan W3). The route answers at
# once with 303 to /inbox?action=<id>; the page shows the status and the final message.


def _queue(request: Request, kind: str, target: str, label: str, fn: Any, key: str) -> Response:
    action_id = request.app.state.worker.submit(kind, target, label, fn, key)
    return RedirectResponse(f"/inbox?action={action_id}", status_code=303)


@router.post("/inbox/refresh")
def refresh(request: Request) -> Response:
    config = request.app.state.config
    factory = getattr(request.app.state, "mcp_factory", None)

    def work(ctx: Any) -> tuple[str, str]:
        client = None
        try:
            client = factory() if factory else ClaudeUpworkBridge(config, ctx.settings, ctx.conn)
            n = inbox.refresh_rooms(client, ctx.conn, ctx.settings)
        except (inbox.InboxError, UpworkAuthError) as e:
            raise WorkerError(str(e)) from e
        except (UpworkMCPError, ClaudeError) as e:
            raise WorkerError(f"Could not read the messages: {str(e)[:200]}") from e
        finally:
            if client is not None:
                try:
                    getattr(client, "close", lambda: None)()
                except Exception:
                    log.exception("Could not close the Upwork bridge")
        text = f"Read {n} chat room(s)." if n else "No chat rooms came back."
        return f"/inbox?action={ctx.action_id}", text

    return _queue(request, "inbox_refresh", "inbox", "Reading messages", work, "inbox_refresh")


def _draft(request: Request, kind: str, ref: str) -> Response:
    config = request.app.state.config

    def work(ctx: Any) -> tuple[str, str]:
        try:
            _, cost = inbox.draft_reply(ctx.conn, config, ctx.settings, kind, ref)
        except inbox.InboxError as e:
            raise WorkerError(str(e)) from e
        text = "Draft ready. Nothing was sent."
        if cost is not None:
            text += f" Claude cost: ${cost:.4f}"
        return f"/inbox?action={ctx.action_id}", text

    return _queue(request, "inbox_draft", ref, "Writing a reply", work, f"inbox_draft:{kind}:{ref}")


@router.post("/inbox/invite/{invite_id}/draft")
def draft_invite(request: Request, invite_id: str) -> Response:
    return _draft(request, "invite", invite_id)


@router.post("/inbox/room/{room_id}/draft")
def draft_room(request: Request, room_id: str) -> Response:
    return _draft(request, "room", room_id)
