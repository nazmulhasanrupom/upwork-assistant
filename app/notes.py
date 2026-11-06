"""Daily notes (SPEC2 5.1, layer 2). New notes go to <DATA_DIR>/memory/daily/YYYY-MM-DD.md.

Older installs wrote to <DATA_DIR>/notes/daily/. Those files are still read, never moved or
written to again.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from app.config import Config

log = logging.getLogger("notes")


def daily_dir(config: Config) -> Path:
    return config.memory_dir / "daily"


def legacy_daily_dir(config: Config) -> Path:
    return config.data_dir / "notes" / "daily"


def note_path(config: Config, day: date | str) -> Path:
    """Where the note of `day` is written (always the new folder)."""
    return daily_dir(config) / f"{day}.md"


def read_note(config: Config, day: date | str) -> str:
    """The note of `day`: the old file (if any) first, then the new one. Empty if none."""
    parts = []
    for folder in (legacy_daily_dir(config), daily_dir(config)):
        path = folder / f"{day}.md"
        try:
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8"))
        except OSError:
            log.exception("Could not read %s", path)
    return "".join(parts)


def list_note_days(config: Config) -> list[str]:
    """Days that have a note in either folder, oldest first."""
    days: set[str] = set()
    for folder in (legacy_daily_dir(config), daily_dir(config)):
        if folder.is_dir():
            days.update(p.stem for p in folder.glob("*.md"))
    return sorted(days)


def append_note(config: Config, day: date | str, block: str) -> bool:
    """Append `block` to the note of `day` in the new folder. Returns False on a disk error."""
    path = note_path(config, day)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
    except OSError:
        log.exception("Could not write the daily note")
        return False
    return True
