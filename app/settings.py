"""Typed settings: the `settings` table (JSON values) falls back to defaults.toml."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS_PATH = Path(__file__).parent.parent / "config" / "defaults.toml"


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, full + "."))
        else:
            out[full] = value
    return out


def load_defaults(path: Path | None = None) -> dict[str, Any]:
    with open(path or DEFAULTS_PATH, "rb") as f:
        return _flatten(tomllib.load(f))


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return True


class Settings:
    def __init__(self, conn: sqlite3.Connection, defaults_path: Path | None = None):
        self._conn = conn
        self._defaults = load_defaults(defaults_path)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self._defaults)

    def get(self, key: str) -> Any:
        if key not in self._defaults:
            raise KeyError(f"Unknown setting: {key}")
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return self._defaults[key]
        return json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        if key not in self._defaults:
            raise KeyError(f"Unknown setting: {key}")
        if not _type_ok(self._defaults[key], value):
            raise TypeError(
                f"Setting {key} expects {type(self._defaults[key]).__name__}, "
                f"got {type(value).__name__}"
            )
        if isinstance(self._defaults[key], float):
            value = float(value)
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def reset(self, key: str) -> None:
        self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def all(self) -> dict[str, Any]:
        values = dict(self._defaults)
        for row in self._conn.execute("SELECT key, value FROM settings"):
            if row["key"] in values:
                values[row["key"]] = json.loads(row["value"])
        return values
