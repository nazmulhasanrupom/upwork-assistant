"""The Chat page `/chat` (plan C1). Login and CSRF come from the app-wide guard.

- GET  /chat        the messages, a box to ask, and a status while an answer is made
- POST /chat/send   save the question, queue the answer on the worker (one at a time)
- POST /chat/clear  delete the chat messages
The helper is read-only: it never writes to Upwork and never approves anything (app/chat.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app import chat
from app.auth_routes import render
from app.worker import ActionContext, known_action

router = APIRouter()

BUSY = "An answer is still being made. Wait for it, then ask again."
EMPTY = "Type a question first."


def _redirect(request: Request, action_id: str = "", msg: str = "", err: bool = False):
    from urllib.parse import quote

    parts = []
    if action_id:
        parts.append(f"action={action_id}")
    if msg:
        parts.append(f"msg={quote(msg)}")
        if err:
            parts.append("err=1")
    return RedirectResponse("/chat" + ("?" + "&".join(parts) if parts else ""), status_code=303)


def _active(request: Request) -> bool:
    row = request.app.state.db.execute(
        "SELECT 1 FROM actions WHERE kind = 'chat' AND status IN ('queued', 'running') LIMIT 1"
    ).fetchone()
    return row is not None


@router.get("/chat")
def chat_page(request: Request) -> Response:
    s = request.app.state
    rows = s.db.execute("SELECT * FROM chat_messages ORDER BY id").fetchall()
    messages = [
        {
            "role": r["role"],
            "created": r["created"],
            "html": chat.render_user(r["text"])
            if r["role"] == "user"
            else chat.render_reply(r["text"]),
        }
        for r in rows
    ]
    action_id = known_action(s.worker, request.query_params.get("action"))
    return render(
        request,
        "chat.html",
        messages=messages,
        action_id=action_id,
        max_chars=int(s.settings.get("chat.max_message_chars")),
    )


@router.post("/chat/send")
def chat_send(request: Request, message: Annotated[str, Form()] = "") -> Response:
    s = request.app.state
    text = message.strip()
    if not text:
        return _redirect(request, msg=EMPTY, err=True)
    if _active(request):
        return _redirect(request, msg=BUSY, err=True)
    limit = int(s.settings.get("chat.max_message_chars"))
    text = text[:limit]
    now = datetime.now(UTC).isoformat(timespec="seconds")
    s.db.execute(
        "INSERT INTO chat_messages(role, text, created) VALUES ('user', ?, ?)", (text, now)
    )
    turns = int(s.settings.get("chat.history_turns"))
    config = s.config

    def work(ctx: ActionContext) -> tuple[str, str]:
        rows = ctx.conn.execute(
            "SELECT role, text FROM chat_messages ORDER BY id DESC LIMIT ?", (turns + 1,)
        ).fetchall()
        history = list(reversed(rows))[:-1]  # the question just saved is sent on its own
        reply, cost = chat.answer(
            text, history, config=config, conn=ctx.conn, settings=ctx.settings
        )
        ctx.conn.execute(
            "INSERT INTO chat_messages(role, text, created, action_id, cost_usd) "
            "VALUES ('assistant', ?, ?, ?, ?)",
            (reply, datetime.now(UTC).isoformat(timespec="seconds"), ctx.action_id, cost),
        )
        return "/chat", "Answer ready."

    action_id = s.worker.submit("chat", "chat", "Answering your question", work, "chat")
    return _redirect(request, action_id=action_id)


@router.post("/chat/clear")
def chat_clear(request: Request) -> Response:
    if _active(request):
        return _redirect(request, msg=BUSY, err=True)
    request.app.state.db.execute("DELETE FROM chat_messages")
    return _redirect(request, msg="Chat cleared.")
