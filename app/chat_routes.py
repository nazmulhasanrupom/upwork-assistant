"""The Chat page `/chat` (plan C1, C4). Login and CSRF come from the app-wide guard.

- GET  /chat                 the newest chat (or an empty new one)
- GET  /chat/new             an empty new chat (nothing is saved until the first question)
- GET  /chat/{id}            one chat: the messages and the box to ask (HTMX adds messages)
- GET  /chat/stream/{id}     Server-Sent Events: thinking steps and answer text of a running answer
- GET  /chat/poll/{id}       the same answer as a page part (the fallback), then the reply
- POST /chat/send            save the question, queue the answer on the worker (one at a time)
- POST /chat/{id}/delete     delete one chat
The helper is read-only: it never writes to Upwork and never approves anything (app/chat.py).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app import chat
from app.auth_routes import render
from app.pages import zone
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


def _redirect(conv_id: int = 0, action_id: str = "", msg: str = "", err: bool = False):
    from urllib.parse import quote

    parts = []
    if action_id:
        parts.append(f"action={action_id}")
    if msg:
        parts.append(f"msg={quote(msg)}")
        if err:
            parts.append("err=1")
    base = f"/chat/{conv_id}" if conv_id else "/chat/new"
    return RedirectResponse(base + ("?" + "&".join(parts) if parts else ""), status_code=303)


def _active(request: Request) -> dict | None:
    """The chat answer being made now (queued or running), if any."""
    row = request.app.state.db.execute(
        "SELECT id, target, created, started FROM actions "
        "WHERE kind = 'chat' AND status IN ('queued', 'running') ORDER BY created DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _live_id(request: Request, action_id: str) -> str:
    """The running claude job of this answer ('' when none): the Stop button cancels it."""
    row = request.app.state.db.execute(
        "SELECT id FROM claude_live WHERE action_id = ? AND status = 'running' "
        "ORDER BY started DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    return row["id"] if row else ""


def _since(action: dict | None) -> int:
    """Seconds since the answer was asked, so the timer carries on after a reload."""
    if not action:
        return 0
    try:
        t0 = datetime.fromisoformat(action.get("started") or action["created"])
    except (ValueError, KeyError, TypeError):
        return 0
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - t0).total_seconds()))


def _message(r) -> dict:
    user = r["role"] == "user"
    seconds = None if user else r["think_seconds"]
    return {
        "role": r["role"],
        "created": r["created"],
        "html": chat.render_user(r["text"]) if user else chat.render_reply(r["text"]),
        "seconds": seconds,
        "steps": [x for x in (r["thinking"] or "").splitlines() if x.strip()],
    }


def _stream_ctx(request: Request) -> dict:
    s = request.app.state.settings
    return {"word_ms": int(s.get("chat.stream_word_ms"))}


def _side(request: Request, active_id: int) -> dict:
    s = request.app.state
    return {
        "groups": chat.group_conversations(s.db, zone(s.config.tz)),
        "active_id": active_id,
    }


def _pending(request: Request, action_id: str, template: str = "_chat_pending.html", **ctx):
    s = request.app.state
    action = s.worker.get(action_id)
    steps, _ = chat.live_view(s.db, s.config.data_dir, action_id)
    return render(
        request,
        template,
        action_id=action_id,
        live_id=_live_id(request, action_id),
        steps=steps,
        since=_since(action),
        oob=True,
        **_stream_ctx(request),
        **ctx,
    )


def _note(request: Request, text: str, err: bool = True) -> Response:
    return render(request, "_chat_note.html", text=text, err=err)


def _page(request: Request, conv_id: int) -> Response:
    s = request.app.state
    conv = chat.get_conversation(s.db, conv_id) if conv_id else None
    rows = (
        s.db.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
        ).fetchall()
        if conv
        else []
    )
    active = _active(request)
    pending_id = active["id"] if active and active["target"] == str(conv_id) and conv else ""
    steps: list[str] = []
    if pending_id:
        steps, _ = chat.live_view(s.db, s.config.data_dir, pending_id)
    return render(
        request,
        "chat.html",
        messages=[_message(r) for r in rows],
        conv=conv,
        conv_id=conv_id if conv else 0,
        pending_id=pending_id,
        pending_live=_live_id(request, pending_id) if pending_id else "",
        steps=steps,
        since=_since(active) if pending_id else 0,
        suggestions=SUGGESTIONS,
        max_chars=int(s.settings.get("chat.max_message_chars")),
        side=_side(request, conv_id if conv else 0),
        **_stream_ctx(request),
    )


@router.get("/chat")
def chat_home(request: Request) -> Response:
    return _page(request, chat.newest_conversation_id(request.app.state.db))


@router.get("/chat/new")
def chat_new(request: Request) -> Response:
    return _page(request, 0)


@router.get("/chat/stream/{action_id}")
async def chat_stream(request: Request, action_id: str) -> Response:
    """Server-Sent Events for one answer in the making (login required by the app-wide guard).

    Events: `think` (one thinking step), `delta` (more answer text), `reset` (start the answer text
    again), `done` (the action ended: ask /chat/poll for the final reply)."""
    s = request.app.state
    action = s.worker.get(action_id)
    if action is None or action["kind"] != "chat":
        raise HTTPException(status_code=404, detail="Unknown action")
    interval = max(0.05, float(s.settings.get("chat.stream_poll_seconds")))
    limit = float(s.settings.get("chat.stream_max_seconds"))

    def event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def gen():
        began = last = time.monotonic()
        steps_sent = 0
        sent = ""
        yield ": open\n\n"
        while True:
            now_action = await asyncio.to_thread(s.worker.get, action_id)
            finished = now_action is None or now_action["status"] not in ("queued", "running")
            steps, text = await asyncio.to_thread(
                chat.live_view, s.db, s.config.data_dir, action_id
            )
            out = []
            for step in steps[steps_sent:]:
                out.append(event("think", {"text": step}))
            steps_sent = len(steps)
            if text != sent:
                if sent and not text.startswith(sent):
                    out.append(event("reset", {}))
                    sent = ""
                out.append(event("delta", {"text": text[len(sent) :]}))
                sent = text
            if finished or time.monotonic() - began > limit:
                out.append(event("done", {}))
            elif not out and time.monotonic() - last > 15:
                out.append(": ping\n\n")
            if out:
                last = time.monotonic()
                yield "".join(out)
            if finished or time.monotonic() - began > limit:
                return
            await asyncio.sleep(interval)

    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


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


@router.get("/chat/{conv_id:int}")
def chat_conversation(request: Request, conv_id: int) -> Response:
    if chat.get_conversation(request.app.state.db, conv_id) is None:
        raise HTTPException(status_code=404, detail="Unknown chat")
    return _page(request, conv_id)


@router.post("/chat/send")
def chat_send(
    request: Request,
    message: Annotated[str, Form()] = "",
    conversation_id: Annotated[str, Form()] = "",
) -> Response:
    s = request.app.state
    text = message.strip()
    htmx = bool(request.headers.get("hx-request"))
    conv = chat.get_conversation(s.db, int(conversation_id)) if conversation_id.isdigit() else None
    cid = int(conv["id"]) if conv else 0
    if not text:
        return _note(request, EMPTY) if htmx else _redirect(cid, msg=EMPTY, err=True)
    if _active(request):
        return _note(request, BUSY) if htmx else _redirect(cid, msg=BUSY, err=True)
    limit = int(s.settings.get("chat.max_message_chars"))
    text = text[:limit]
    now = datetime.now(UTC).isoformat(timespec="seconds")
    created_new = conv is None
    if conv is None:
        cid = chat.create_conversation(s.db, text, int(s.settings.get("chat.title_chars")))
    else:
        s.db.execute("UPDATE chat_conversations SET updated = ? WHERE id = ?", (now, cid))
    s.db.execute(
        "INSERT INTO chat_messages(role, text, created, conversation_id) VALUES ('user', ?, ?, ?)",
        (text, now, cid),
    )
    asked = {"role": "user", "created": now, "html": chat.render_user(text)}
    turns = int(s.settings.get("chat.history_turns"))
    config = s.config

    def work(ctx: ActionContext) -> tuple[str, str]:
        t0 = time.monotonic()
        rows = ctx.conn.execute(
            "SELECT role, text FROM chat_messages WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (cid, turns + 1),
        ).fetchall()
        history = list(reversed(rows))[:-1]  # the question just saved is sent on its own
        trace: list[str] = []
        reply, cost = chat.answer(
            text, history, config=config, conn=ctx.conn, settings=ctx.settings, trace=trace
        )
        done = datetime.now(UTC).isoformat(timespec="seconds")
        ctx.conn.execute(
            "INSERT INTO chat_messages(role, text, created, action_id, cost_usd, conversation_id, "
            "thinking, think_seconds) VALUES ('assistant', ?, ?, ?, ?, ?, ?, ?)",
            (
                reply,
                done,
                ctx.action_id,
                cost,
                cid,
                "\n".join(trace),
                max(1, round(time.monotonic() - t0)) if trace else None,
            ),
        )
        ctx.conn.execute("UPDATE chat_conversations SET updated = ? WHERE id = ?", (done, cid))
        return f"/chat/{cid}", "Answer ready."

    action_id = s.worker.submit("chat", str(cid), "Answering your question", work, "chat")
    if htmx:
        resp = _pending(
            request,
            action_id,
            "_chat_turn.html",
            m=asked,
            cid=cid,
            side=_side(request, cid),
        )
        if created_new:
            resp.headers["HX-Push-Url"] = f"/chat/{cid}"
        return resp
    return _redirect(cid, action_id=action_id)


@router.post("/chat/{conv_id:int}/delete")
def chat_delete(request: Request, conv_id: int) -> Response:
    s = request.app.state
    if chat.get_conversation(s.db, conv_id) is None:
        raise HTTPException(status_code=404, detail="Unknown chat")
    active = _active(request)
    if active and active["target"] == str(conv_id):
        return _redirect(conv_id, msg=BUSY, err=True)
    s.db.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conv_id,))
    s.db.execute("DELETE FROM chat_conversations WHERE id = ?", (conv_id,))
    newest = chat.newest_conversation_id(s.db)
    return RedirectResponse(
        "/chat/new?msg=Chat%20deleted." if not newest else f"/chat/{newest}?msg=Chat%20deleted.",
        status_code=303,
    )
