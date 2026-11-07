"""Plain names for the settings page. One file, app/settings_labels.toml."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

LABELS_PATH = Path(__file__).resolve().parent / "settings_labels.toml"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, str]]:
    with LABELS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    return {
        "sections": {str(k): str(v) for k, v in data.get("sections", {}).items()},
        "keys": {str(k): str(v) for k, v in data.get("keys", {}).items()},
    }


def humanize(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def section_title(section: str) -> str:
    return _load()["sections"].get(section) or humanize(section)


def key_label(key: str) -> str:
    return _load()["keys"].get(key) or humanize(key.rsplit(".", 1)[-1])
