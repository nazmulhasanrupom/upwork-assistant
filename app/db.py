"""SQLite connection and migration runner."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d+)_.+\.sql$")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection. Use ':memory:' for tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def _migration_files(directory: Path) -> list[tuple[int, Path]]:
    found = []
    for p in directory.glob("*.sql"):
        m = _NAME_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found)


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply migration files newer than schema_version. Returns applied versions."""
    directory = directory or MIGRATIONS_DIR
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    done = current_version(conn)
    applied = []
    for version, path in _migration_files(directory):
        if version <= done:
            continue
        sql = path.read_text(encoding="utf-8")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        conn.executescript(
            "BEGIN;\n"
            + sql
            + f"\nINSERT INTO schema_version(version, applied_at) VALUES ({version}, '{now}');\n"
            "COMMIT;"
        )
        applied.append(version)
    return applied


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
