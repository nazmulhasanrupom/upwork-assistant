"""Invites and messages (plan H3): read them, draft a reply with Claude. Nothing is ever sent.

- Invites come from the `invites` table (filled by the daily run, plan X3).
- Messages (chat rooms) are read on a click with the READ bridge tool `get_messages` and stored
  in the `messages` table. The arguments of `get_messages` are GUESSED (never captured live),
  the answer is read leniently: a list called `rooms` anywhere in it, with the room fields of the
  dashboard (`get_freelancer_dashboard`) which were captured.
- Reply drafts: one Claude call; prompting.build_prompt gives the hard rules, USER.md and the style.
  The message text goes in the input inside `<untrusted_message>` tags. The draft is cleaned of em
  dashes and stored on the invite or room row. Replying stays manual on Upwork.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from app.config import Config
from app.llm import prompting
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.settings import Settings
from app.upwork.jobs import CLOSE_TAG, OPEN_TAG

log = logging.getLogger("inbox")

PROMPT_FILE = "prompts/draft_reply.md"
TASK = "draft_reply"
TAG = "untrusted_message"
UPWORK_HOST = "upwork.com"
ROOM_URL = "https://www.upwork.com/ab/messages/rooms/"
JOB_URL = "https://www.upwork.com/jobs/~02"

_TAG_RE = re.compile(r"</?\s*untrusted[a-z_]*\s*>", re.IGNORECASE)


class InboxError(RuntimeError):
    """A message that is safe to show to the user."""


# ---------------------------------------------------------------- text and links


def plain(text: Any, limit: int | None = None) -> str:
    """Client text for reading: the wrapper tags are removed, the length is cut."""
    out = _TAG_RE.sub("", str(text or "")).replace(OPEN_TAG, "").replace(CLOSE_TAG, "").strip()
    return out[:limit] if limit else out


def safe_link(url: Any) -> str | None:
    """The url when it is http(s) on upwork.com, else None (javascript:, data:, other hosts)."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if host != UPWORK_HOST and not host.endswith("." + UPWORK_HOST):
        return None
    if any(c in url for c in " <>\"'\\\n\r\t"):
        return None
    return url


def room_link(room_id: str, url: Any = None) -> str | None:
    return safe_link(url) or (f"{ROOM_URL}{quote(room_id, safe='')}" if room_id else None)


def invite_link(job_id: str | None) -> str | None:
    return f"{JOB_URL}{job_id}" if job_id and job_id.isdigit() else None


# ---------------------------------------------------------------- read rooms


def _find_rooms(data: Any, depth: int = 0) -> list[Any] | None:
    if depth > 6:
        return None
    if isinstance(data, dict):
        rooms = data.get("rooms")
        if isinstance(rooms, list):
            return rooms
        for value in data.values():
            found = _find_rooms(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_rooms(value, depth + 1)
            if found is not None:
                return found
    return None


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_rooms(data: Any, text_max: int = 2000) -> list[dict[str, Any]]:
    """Rooms from a get_messages (or dashboard) answer. Items without an id are skipped."""
    out: list[dict[str, Any]] = []
    for room in _find_rooms(data) or []:
        if not isinstance(room, dict) or not room.get("id"):
            continue
        story = room.get("latestStory") if isinstance(room.get("latestStory"), dict) else {}
        out.append(
            {
                "room_id": str(room["id"]),
                "topic": plain(room.get("topic"), 300),
                "awaiting": room.get("awaiting_reply_from")
                if room.get("awaiting_reply_from") in ("you", "them")
                else None,
                "last_from": plain(story.get("from"), 200) or story.get("from_side") or "",
                "last_text": plain(story.get("message"), text_max),
                "last_at": str(story.get("createdDateTime") or room.get("createdAtDateTime") or ""),
                "unread": _int(room.get("numUnread")),
                "url": safe_link(room.get("url")),
            }
        )
    return out


def rooms_call(settings: Settings) -> tuple[str, dict[str, Any]]:
    """The one READ call. The arguments are a guess (see README of the fixtures)."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise InboxError("No freelancer account is chosen yet (see the Upwork page).")
    return (
        "get_messages",
        {
            "action": "list_rooms",
            "org_uid": org_uid,
            "params": {"limit": int(settings.get("inbox.rooms_limit"))},
        },
    )


def refresh_rooms(
    bridge: Any, conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> int:
    """Read the rooms (one bridge call) and store them. Returns how many rooms were stored.
    Raises InboxError when the read fails; Upwork auth errors pass through."""
    from app.upwork.client import UpworkAuthError, UpworkMCPError

    call = rooms_call(settings)
    try:
        [result] = bridge.call_many([call])
        data = result.unwrap()
    except UpworkAuthError:
        raise
    except UpworkMCPError as e:
        raise InboxError(f"Could not read the messages: {str(e)[:200]}") from e
    rooms = parse_rooms(data, int(settings.get("inbox.text_max_chars")))
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    for r in rooms:
        conn.execute(
            "INSERT INTO messages(room_id, unread, topic, awaiting, last_from, last_text, "
            "last_at, url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(room_id) DO UPDATE SET unread = excluded.unread, topic = excluded.topic, "
            "awaiting = excluded.awaiting, last_from = excluded.last_from, "
            "last_text = excluded.last_text, last_at = excluded.last_at, url = excluded.url, "
            "fetched_at = excluded.fetched_at",
            (
                r["room_id"],
                r["unread"],
                r["topic"],
                r["awaiting"],
                r["last_from"],
                r["last_text"],
                r["last_at"],
                r["url"],
                stamp,
            ),
        )
    return len(rooms)


# ---------------------------------------------------------------- lists for the page


def list_invites(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM invites WHERE answered_at IS NULL ORDER BY COALESCE(first_seen, received) "
        "DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "invite_id": r["invite_id"],
            "title": plain(r["job_title"])
            or (f"Job {r['job_id']}" if r["job_id"] else "Invitation"),
            "job_id": r["job_id"],
            "link": invite_link(r["job_id"]),
            "first_seen": r["first_seen"] or r["received"],
            "draft": r["draft_reply"],
            "draft_at": r["draft_at"],
        }
        for r in rows
    ]


def list_rooms(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE topic IS NOT NULL "
        "ORDER BY (awaiting = 'you') DESC, last_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "room_id": r["room_id"],
            "topic": r["topic"] or "Chat",
            "awaiting": r["awaiting"],
            "last_from": r["last_from"],
            "last_text": r["last_text"],
            "last_at": r["last_at"],
            "unread": r["unread"],
            "link": room_link(r["room_id"], r["url"]),
            "draft": r["draft_reply"],
            "draft_at": r["draft_at"],
            "fetched_at": r["fetched_at"],
        }
        for r in rows
    ]


def last_fetch(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(fetched_at) AS m FROM messages").fetchone()
    return row["m"] if row else None


# ---------------------------------------------------------------- draft a reply


def wrap(text: str) -> str:
    """Wrap in our own untrusted tag; the text cannot close or reopen it."""
    safe = re.sub(rf"<(/?)\s*{TAG}", r"&lt;\1" + TAG, text, flags=re.IGNORECASE)
    return f"<{TAG}>\n{safe}\n</{TAG}>"


def _context(conn: sqlite3.Connection, kind: str, ref: str, text_max: int) -> str:
    """Plain text about the item. Raises InboxError when it does not exist."""
    if kind == "invite":
        row = conn.execute("SELECT * FROM invites WHERE invite_id = ?", (ref,)).fetchone()
        if row is None:
            raise InboxError("That invitation does not exist.")
        lines = [
            "Type: the client invited me to a job.",
            f"Job title: {plain(row['job_title']) or '(unknown)'}",
        ]
    elif kind == "room":
        row = conn.execute("SELECT * FROM messages WHERE room_id = ?", (ref,)).fetchone()
        if row is None:
            raise InboxError("That chat room does not exist.")
        lines = [
            "Type: a chat message in an Upwork room.",
            f"Room topic: {plain(row['topic']) or '(unknown)'}",
            f"Waiting for a reply from: {row['awaiting'] or 'unknown'}",
            f"Last message from: {plain(row['last_from']) or 'unknown'}",
            f"Last message: {plain(row['last_text'], text_max) or '(no text)'}",
        ]
    else:
        raise InboxError("Unknown item.")
    return "\n".join(lines)[: text_max + 600]


def draft_reply(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    kind: str,
    ref: str,
) -> tuple[str, float]:
    """One Claude call. Stores the draft and returns (draft text, cost in USD)."""
    text_max = int(settings.get("inbox.text_max_chars"))
    context = _context(conn, kind, ref, text_max)
    reply_max = int(settings.get("inbox.reply_max_chars"))
    prompt = prompting.build_prompt(PROMPT_FILE, config=config)
    try:
        res = run_claude(
            TASK,
            PROMPT_FILE,
            {"kind": kind, "max_chars": reply_max, "message": wrap(context)},
            str(settings.get("models.write")),
            int(settings.get("inbox.reply_max_turns")),
            float(settings.get("inbox.reply_max_budget_usd")),
            config=config,
            conn=conn,
            settings=settings,
            effort=settings.get("models.write_effort") or None,
            timeout=int(settings.get("inbox.reply_timeout_seconds")),
            prompt_text=prompt,
        )
    except BudgetCapReached as e:
        raise InboxError(str(e)) from e
    except ClaudeRateLimited as e:
        raise InboxError(f"Claude is rate limited. Try again later. ({str(e)[:120]})") from e
    except ClaudeFailed as e:
        raise InboxError(f"Claude could not write the reply: {str(e)[:200]}") from e
    out = res["output"]
    reply = out.get("reply") if isinstance(out, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        raise InboxError("Claude's answer had no reply text. Try again.")
    reply = prompting.no_em_dash(reply).strip()[:reply_max]
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    if kind == "invite":
        conn.execute(
            "UPDATE invites SET draft_reply = ?, draft_at = ? WHERE invite_id = ?",
            (reply, stamp, ref),
        )
    else:
        conn.execute(
            "UPDATE messages SET draft_reply = ?, draft_at = ? WHERE room_id = ?",
            (reply, stamp, ref),
        )
    return reply, float(res["cost_usd"])
