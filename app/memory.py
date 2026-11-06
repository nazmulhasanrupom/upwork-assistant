"""Memory files, their size cap, and the search index (SPEC2 5.1, plan L1).

USER.md and MEMORY.md share one character cap (`memory.total_cap_chars`). Every write goes through
`write_memory_file`, which refuses a write that ends over the cap. A write that makes an already
over-cap pair smaller is allowed, so the user (or the weekly consolidation) can always trim.
Daily notes and runs are searchable through the `search_idx` FTS5 table (migration 020).
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from app.config import Config

CAPPED = ("USER.md", "MEMORY.md")


class OverCap(ValueError):
    """The write would leave USER.md + MEMORY.md over the cap. The message is safe to show."""


def memory_file(config: Config, name: str) -> Path:
    return config.memory_dir / name


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def normalize(text: str) -> str:
    """One newline style, no trailing blanks at the end, one final newline if not empty."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text + "\n" if text else ""


def atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same folder, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def usage(config: Config, settings: Any, user_text: str | None = None) -> dict[str, Any]:
    """Characters used and left against the shared cap."""
    cap = int(settings.get("memory.total_cap_chars"))
    user = len(
        (user_text if user_text is not None else read_file(memory_file(config, "USER.md"))).strip()
    )
    memory = len(read_file(memory_file(config, "MEMORY.md")).strip())
    total = user + memory
    return {
        "user": user,
        "memory": memory,
        "total": total,
        "cap": cap,
        "left": cap - total,
        "over": total > cap,
        "percent": min(100, round(100 * total / cap)) if cap else 0,
    }


def check_cap(config: Config, settings: Any, name: str, text: str) -> None:
    """Raise OverCap if writing `text` as `name` leaves the pair over the cap and not smaller."""
    if name not in CAPPED:
        return
    now = usage(config, settings)
    other = now["memory"] if name == "USER.md" else now["user"]
    new_total = other + len(text.strip())
    if new_total > now["cap"] and new_total >= now["total"]:
        raise OverCap(
            f"{name} would make USER.md + MEMORY.md {new_total} characters, "
            f"over the {now['cap']} character cap. Shorten the text by {new_total - now['cap']}."
        )


def write_memory_file(config: Config, settings: Any, name: str, text: str) -> None:
    """The one write path for memory files. Raises OverCap (nothing written) or OSError."""
    check_cap(config, settings, name, text)
    atomic_write(memory_file(config, name), text)


# ---------------------------------------------------------------- search index


def index_note(conn: sqlite3.Connection, day: str, text: str) -> None:
    """Replace the index entry of the daily note `day`."""
    conn.execute("DELETE FROM search_idx WHERE kind = 'note' AND ref = ?", (str(day),))
    if text.strip():
        conn.execute(
            "INSERT INTO search_idx(kind, ref, body) VALUES ('note', ?, ?)", (str(day), text)
        )


def reindex_notes(conn: sqlite3.Connection, config: Config) -> int:
    """Index every note file again (for notes written before the index existed)."""
    from app import notes

    days = notes.list_note_days(config)
    for day in days:
        index_note(conn, day, notes.read_note(config, day))
    return len(days)


_WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: each word quoted, joined with AND."""
    return " ".join(f'"{w}"' for w in _WORD.findall(query)[:20])


def search(
    conn: sqlite3.Connection, query: str, limit: int = 20, kind: str | None = None
) -> list[dict[str, str]]:
    """Best matches first. Never raises on odd input; returns [] for an empty query."""
    q = fts_query(query)
    if not q:
        return []
    sql = (
        "SELECT kind, ref, snippet(search_idx, 2, '[', ']', '...', 12) AS snippet "
        "FROM search_idx WHERE search_idx MATCH ?"
    )
    args: list[Any] = [q]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"kind": r["kind"], "ref": r["ref"], "snippet": r["snippet"]} for r in rows]
