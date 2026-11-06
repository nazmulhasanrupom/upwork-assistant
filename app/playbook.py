"""Playbook and experiments (plan O2, SPEC2 4.C phase 2 and 6).

- `<data_dir>/memory/PLAYBOOK.md` is seeded on first run from `config/playbook_seed.md`.
  The tactic table keeps one status per tactic; finishing a test appends a line to the results log.
- `experiments` holds the tests. Only ONE can be active: checked here and by a unique index.
- A test cannot start while the keyword baseline runs (or was never started), while the JSS guard
  is on, or while another test is active (`can_start_test` returns the reason).
- The baseline of a test is the 7-day averages of the metrics and of every approved keyword rank.

No Claude call and no Upwork call here: the test loop (plan O3) uses these functions.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app import alerts, keywords, metrics, rank, versioning
from app.config import Config
from app.settings import Settings

SEED_PATH = Path(__file__).parent.parent / "config" / "playbook_seed.md"
FILE_NAME = "PLAYBOOK.md"
OUTCOMES = ("worked", "no effect", "hurt")  # status words a finished test can give a tactic
TESTING = "testing"

_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|(.+)\|([^|]*)\|([^|]*)\|\s*$")


class PlaybookError(RuntimeError):
    """A message that is safe to show to the user."""


def path(config: Config) -> Path:
    return config.memory_dir / FILE_NAME


def _commit(config: Config, settings: Settings | None, message: str) -> None:
    versioning.commit(config, settings, message, [path(config)])


def ensure_seeded(
    config: Config, seed: Path | None = None, settings: Settings | None = None
) -> bool:
    """Write PLAYBOOK.md from the seed file when it does not exist. True when it was written."""
    target = path(config)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((seed or SEED_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    _commit(config, settings, "Seed the playbook")
    return True


def read(config: Config) -> str:
    ensure_seeded(config)
    return path(config).read_text(encoding="utf-8")


def tactics(config: Config) -> list[dict[str, Any]]:
    """The rows of the tactic table: number, tactic, type, status."""
    out = []
    for line in read(config).splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            out.append(
                {
                    "no": int(m.group(1)),
                    "tactic": m.group(2).strip().replace("\\|", "|"),
                    "type": m.group(3).strip(),
                    "status": m.group(4).strip(),
                }
            )
    return out


def _set_status(config: Config, tactic_no: int | None, status: str) -> None:
    if tactic_no is None:
        return
    lines = read(config).splitlines()
    for i, line in enumerate(lines):
        m = _ROW_RE.match(line.strip())
        if m and int(m.group(1)) == tactic_no:
            cells = line.strip().strip("|").split("|")
            # escaped pipes in the tactic text are split too, so only the last cell is replaced
            cells[-1] = f" {status} "
            lines[i] = "|" + "|".join(cells) + "|"
            break
    path(config).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_log(config: Config, line: str) -> None:
    text = read(config).rstrip("\n")
    path(config).write_text(f"{text}\n{line}\n", encoding="utf-8")


def _today(settings: Settings, now: datetime) -> date:
    return now.astimezone(keywords._zone(settings)).date()


# ---------------------------------------------------------------- research updates (plan L5)

OUTDATED = "outdated"
CHECKED_HEADING = "## Last checked"
_CHECKED_RE = re.compile(r"^-\s*#(\d+):\s*(\d{4}-\d{2}-\d{2})\s*$")


def last_checked(config: Config) -> dict[int, str]:
    """Tactic number -> the date the weekly research last checked it."""
    out: dict[int, str] = {}
    for line in read(config).splitlines():
        m = _CHECKED_RE.match(line.strip())
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def _write_lines(config: Config, lines: list[str]) -> None:
    path(config).write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def _set_checked_lines(lines: list[str], numbers: list[int], day: str) -> list[str]:
    """Put `- #N: day` lines in the "Last checked" section (made before the results log)."""
    known = {n: day for n in numbers}
    lines = [
        ln
        for ln in lines
        if not ((m := _CHECKED_RE.match(ln.strip())) and int(m.group(1)) in known)
    ]
    if CHECKED_HEADING not in [ln.strip() for ln in lines]:
        at = next((i for i, ln in enumerate(lines) if ln.strip() == "## Results log"), len(lines))
        lines[at:at] = [CHECKED_HEADING, "", ""]
    head = [ln.strip() for ln in lines].index(CHECKED_HEADING)
    end = head + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    entries = {
        int(m.group(1)): m.group(2)
        for ln in lines[head + 1 : end]
        if (m := _CHECKED_RE.match(ln.strip()))
    }
    entries.update(known)
    block = [f"- #{n}: {d}" for n, d in sorted(entries.items())]
    return [*lines[: head + 1], "", *block, "", *lines[end:]]


def set_checked(
    config: Config, settings: Settings | None, numbers: list[int], day: str, message: str
) -> None:
    """Record the last-checked date for these tactics. One commit."""
    existing = {t["no"] for t in tactics(config)}
    nums = sorted({n for n in numbers if n in existing})
    if not nums:
        raise PlaybookError("None of those tactics exists in the playbook.")
    _write_lines(config, _set_checked_lines(read(config).splitlines(), nums, day))
    _commit(config, settings, message)


def add_tactic(config: Config, settings: Settings | None, text: str, type_: str, day: str) -> int:
    """Add a new tactic row (status untested) after the last row of the table. Returns its number.
    The row is checked as of `day`. One commit."""
    text = " ".join(str(text).split()).replace("|", "\\|")
    type_ = " ".join(str(type_).split()).replace("|", "/")
    if not text or not type_:
        raise PlaybookError("A new tactic needs a text and a type.")
    rows = tactics(config)
    number = max([r["no"] for r in rows], default=0) + 1
    lines = read(config).splitlines()
    last = max((i for i, ln in enumerate(lines) if _ROW_RE.match(ln.strip())), default=None)
    if last is None:
        raise PlaybookError("The playbook table was not found.")
    lines.insert(last + 1, f"| {number} | {text} | {type_} | untested |")
    lines = _set_checked_lines(lines, [number], day)
    _write_lines(config, lines)
    _commit(config, settings, f"Playbook: new tactic #{number}: {text[:80]}")
    return number


def mark_outdated(
    config: Config, settings: Settings | None, number: int, reason: str, day: str
) -> None:
    """Set the status of a tactic to `outdated` and log the reason. One commit."""
    row = next((r for r in tactics(config) if r["no"] == number), None)
    if row is None:
        raise PlaybookError(f"Tactic #{number} does not exist.")
    if row["status"] == TESTING:
        raise PlaybookError(f"Tactic #{number} is being tested now. Finish the test first.")
    _set_status(config, number, OUTDATED)
    lines = _set_checked_lines(read(config).splitlines(), [number], day)
    _write_lines(config, lines)
    _append_log(config, f"- {day}: #{number} {row['tactic']}: outdated. {' '.join(reason.split())}")
    _commit(config, settings, f"Playbook: tactic #{number} marked outdated")


# ---------------------------------------------------------------- experiments


def active_experiment(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM experiments WHERE status = 'active'").fetchone()


def list_experiments(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM experiments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def can_start_test(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> str | None:
    """None when a test may start, else the reason (a sentence for the user)."""
    now = now or datetime.now(UTC)
    if alerts.jss_guard_on(settings):
        return (
            "The JSS guard is on: your Job Success Score dropped. Clear it on the dashboard first."
        )
    left = keywords.baseline_days_left(settings, _today(settings, now))
    if left is None:
        return "The baseline has not started: approve your keywords first."
    if left > 0:
        return f"The baseline is still running: {left} more day(s) without profile changes."
    active = active_experiment(conn)
    if active is not None:
        return f"A test is already running (until {active['ends']}). Only one test at a time."
    return None


def baseline_metrics(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    """7-day averages (metrics and keyword ranks) ending today. None = no data."""
    today = _today(settings, now or datetime.now(UTC))
    columns = [str(c) for c in settings.get("playbook.baseline_metrics")]
    rank_days = max(1, int(settings.get("rank.average_days")))
    return {
        "date": today.isoformat(),
        "metrics": {
            c: metrics.seven_day_average(conn, settings, c, end=today)
            for c in columns
            if c in metrics.NUMERIC_COLUMNS
        },
        "rank": {
            k: rank.avg_7d(conn, k, today, days=rank_days)
            for k in rank.approved_keywords(conn, settings)
        },
    }


def start_experiment(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    tactic: str,
    tactic_no: int | None = None,
    now: datetime | None = None,
) -> int:
    """Start a test: saves the baseline, marks the tactic `testing`. Returns the id."""
    now = now or datetime.now(UTC)
    tactic = tactic.strip()
    if not tactic:
        raise PlaybookError("A test needs a tactic.")
    reason = can_start_test(conn, settings, now)
    if reason:
        raise PlaybookError(reason)
    started = _today(settings, now)
    ends = started + timedelta(days=max(1, int(settings.get("playbook.test_days"))))
    try:
        cur = conn.execute(
            "INSERT INTO experiments(tactic, tactic_no, status, started, ends, baseline_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                tactic,
                tactic_no,
                "active",
                started.isoformat(),
                ends.isoformat(),
                json.dumps(baseline_metrics(conn, settings, now)),
            ),
        )
    except sqlite3.IntegrityError as exc:  # the unique index: another test started meanwhile
        raise PlaybookError("A test is already running. Only one test at a time.") from exc
    _set_status(config, tactic_no, TESTING)
    _commit(config, settings, f"Playbook: test started: {tactic}")
    return int(cur.lastrowid)


def finish_experiment(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    outcome: str,
    result: str = "",
    now: datetime | None = None,
) -> int:
    """Finish the active test with an outcome (worked / no effect / hurt) and a short result.
    Updates the tactic status and appends one line to the playbook. Returns the id."""
    now = now or datetime.now(UTC)
    if outcome not in OUTCOMES:
        raise PlaybookError(f"The outcome must be one of: {', '.join(OUTCOMES)}.")
    active = active_experiment(conn)
    if active is None:
        raise PlaybookError("No test is running.")
    text = " ".join(str(result or "").split())[: int(settings.get("playbook.result_max_chars"))]
    conn.execute(
        "UPDATE experiments SET status = 'finished', outcome = ?, result = ?, finished_at = ? "
        "WHERE id = ?",
        (outcome, text, now.astimezone(UTC).isoformat(timespec="seconds"), active["id"]),
    )
    _set_status(config, active["tactic_no"], outcome)
    day = _today(settings, now).isoformat()
    no = f"#{active['tactic_no']} " if active["tactic_no"] is not None else ""
    _append_log(config, f"- {day}: {no}{active['tactic']}: {outcome}. {text}".rstrip())
    _commit(config, settings, f"Playbook: test finished ({outcome}): {active['tactic']}")
    return int(active["id"])


def page_data(
    conn: sqlite3.Connection, settings: Settings, config: Config, now: datetime | None = None
) -> dict[str, Any]:
    """Everything the read-only panel section shows."""
    now = now or datetime.now(UTC)
    rows = []
    for r in list_experiments(conn, int(settings.get("playbook.experiments_shown"))):
        try:
            base = json.loads(r["baseline_json"] or "{}")
        except ValueError:
            base = {}
        rows.append({**dict(r), "baseline": base})
    return {
        "tactics": tactics(config),
        "experiments": rows,
        "active": active_experiment(conn),
        "blocked": can_start_test(conn, settings, now),
        "test_days": int(settings.get("playbook.test_days")),
    }
