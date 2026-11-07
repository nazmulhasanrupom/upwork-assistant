"""Claude login values: from the environment if set, else the encrypted copy saved in settings.

The token and key are never shown back. Only the last 4 characters are (`hint`).
"""

from __future__ import annotations

from typing import Any

from app import crypto
from app.config import Config

TOKEN_KEY = "claude.oauth_token_enc"
API_KEY = "claude.api_key_enc"
VARS = {TOKEN_KEY: "CLAUDE_CODE_OAUTH_TOKEN", API_KEY: "ANTHROPIC_API_KEY"}
VALUE_MAX = 2000


def _env_value(config: Config, key: str) -> str:
    return (config.claude_code_oauth_token if key == TOKEN_KEY else config.anthropic_api_key) or ""


def stored(settings: Any, config: Config, key: str) -> str:
    """The decrypted saved value, '' when none or unreadable."""
    if settings is None:
        return ""
    try:
        raw = str(settings.get(key) or "")
        return crypto.decrypt(config.secret_key, raw) if raw else ""
    except Exception:
        return ""


def value(settings: Any, config: Config, key: str) -> str:
    """Env value if set, else the saved one."""
    return _env_value(config, key).strip() or stored(settings, config, key)


def save(settings: Any, config: Config, key: str, plain: str) -> None:
    plain = plain.strip()
    settings.set(key, crypto.encrypt(config.secret_key, plain) if plain else "")


def hint(settings: Any, config: Config, key: str) -> str:
    """'' when nothing is saved, else 'Saved, ends with ...abcd'."""
    plain = stored(settings, config, key)
    return f"Saved, ends with ...{plain[-4:]}" if plain else ""


def apply(env: dict[str, str], settings: Any, config: Config) -> dict[str, str]:
    for key, name in VARS.items():
        v = value(settings, config, key)
        if v:
            env[name] = v
    return env
