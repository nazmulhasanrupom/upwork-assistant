"""Help text for the "i" icons. One file, app/help.toml: key -> text."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

HELP_PATH = Path(__file__).resolve().parent / "help.toml"


@lru_cache(maxsize=1)
def load_help() -> dict[str, str]:
    with HELP_PATH.open("rb") as fh:
        return {str(k): str(v) for k, v in tomllib.load(fh).items()}


def help_text(key: str) -> str:
    """The text for a key. A missing key shows the key itself (a test catches it)."""
    return load_help().get(key, key)
