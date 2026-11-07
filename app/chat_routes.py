"""The Chat page `/chat` (plan C1). Login and CSRF come from the app-wide guard.

- GET  /chat        the messages and the box to ask (no reload: HTMX adds messages)
- GET  /chat/poll/{id}  one answer in the making: thinking and live log, then the reply
- POST /chat/send   save the question, queue the answer on the worker (one at a time)
- POST /chat/clear  delete the chat messages
The helper is read-only: it never writes to Upwork and never approves anything (app/chat.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import chat
from app.auth_routes import render
from app.llm import live
from app.worker import ActionContext

router = APIRouter()

BUSY = "An answer is still being made. Wait for it, then ask again."
EMPTY = "Type a question first."
STOPPED = "Stopped. No answer was made."
FAILED = "Could not get an answer. Try again."
SUGGESTIONS = [
    "What happened today?",
    "Best jobs to apply for",
    "Why did the last run fail?",
    "What's waiting in To do?",
]


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


def _active_id(request: Request) -> str:
    row = request.app.state.db.execute(
        "SELECT id FROM actions WHERE kind = 'chat' AND status IN ('queued', 'running') "
        "ORDER BY created DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else ""


def _active(request: Request) -> bool:
    return bool(_active_id(request))


def _live(request: Request, action_id: str) -> tuple[str, str]:
    """The running claude job of this answer and the end of its live log ('' when none)."""
    s = request.app.state
    row = s.db.execute(
        "SELECT id FROM claude_live WHERE action_id = ? AND status = 'running' "
        "ORDER BY started DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    if not row:
        return "", ""
    lines = int(s.settings.get("live.tail_lines"))
    return row["id"], live.tail(s.config.data_dir, row["id"], lines)


def _message(r) -> dict:
    return {
        "role": r["role"],
        "created": r["created"],
        "html": chat.render_user(r["text"])
        if r["role"] == "user"
        else chat.render_reply(r["text"]),
    }


def _pending(request: Request, action_id: str, template: str = "_chat_pending.html", **ctx):
    live_id, text = _live(request, action_id)
    return render(
        request, template, action_id=action_id, live_id=live_id, live_text=text, oob=True, **ctx
    )


def _note(request: Request, text: str, err: bool = True) -> Response:
    return render(request, "_chat_note.html", text=text, err=err)


@router.get("/chat")
def chat_page(request: Request) -> Response:
    s = request.app.state
    rows = s.db.execute("SELECT * FROM chat_messages ORDER BY id").fetchall()
    pending_id = _active_id(request)
    live_id, live_text = _live(request, pending_id) if pending_id else ("", "")
    return render(
        request,
        "chat.html",
        messages=[_message(r) for r in rows],
        pending_id=pending_id,
        pending_live=live_id,
        live_text=live_text,
        suggestions=SUGGESTIONS,
        max_chars=int(s.settings.get("chat.max_message_chars")),
    )


@router.get("/chat/poll/{action_id}")
def chat_poll(request: Request, action_id: str) -> Response:
    s = request.app.state
    action = s.worker.get(action_id)
    if action is None or action["kind"] != "chat":
        raise HTTPException(status_code=404, detail="Unknown action")
    if action["status"] in ("queued", "running"):
        return _pending(request, action_id)
    row = s.db.execute(
        "SELECT * FROM chat_messages WHERE action_id = ? AND role = 'assistant' ORDER BY id DESC",
        (action_id,),
    ).fetchone()
    if row:
        return render(request, "_chat_poll_done.html", m=_message(row))
    text = STOPPED if action["status"] == "stopped" else (action["error"] or FAILED)
    return render(request, "_chat_poll_done.html", m=None, text=text, err=True)


@router.post("/chat/send")
def chat_send(request: Request, message: Annotated[str, Form()] = "") -> Response:
    s = request.app.state
    text = message.strip()
    htmx = bool(request.headers.get("hx-request"))
    if not text:
        return _note(request, EMPTY) if htmx else _redirect(request, msg=EMPTY, err=True)
    if _active(request):
        return _note(request, BUSY) if htmx else _redirect(request, msg=BUSY, err=True)
    limit = int(s.settings.get("chat.max_message_chars"))
    text = text[:limit]
    now = datetime.now(UTC).isoformat(timespec="seconds")
    s.db.execute(
        "INSERT INTO chat_messages(role, text, created) VALUES ('user', ?, ?)", (text, now)
    )
    asked = {"role": "user", "created": now, "html": chat.render_user(text)}
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
    if htmx:
        return _pending(request, action_id, "_chat_turn.html", m=asked)
    return _redirect(request, action_id=action_id)


@router.post("/chat/clear")
def chat_clear(request: Request) -> Response:
    if _active(request):
        return _redirect(request, msg=BUSY, err=True)
    request.app.state.db.execute("DELETE FROM chat_messages")
    return _redirect(request, msg="Chat cleared.")
