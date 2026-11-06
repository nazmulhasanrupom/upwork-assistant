"""Environment configuration. Refuses to start on a bad SECRET_KEY (SPEC 10.4)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

EXAMPLE_SECRET = "change-me-long-random-string"
MIN_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised when the environment is not safe or complete enough to start."""


@dataclass(frozen=True)
class Config:
    secret_key: str
    public_base_url: str
    data_dir: Path
    tz: str
    log_level: str
    claude_code_oauth_token: str
    anthropic_api_key: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.enc"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    secret = env.get("SECRET_KEY", "")
    if not secret:
        raise ConfigError("SECRET_KEY is missing. Set a random value of 32+ characters.")
    if secret == EXAMPLE_SECRET:
        raise ConfigError("SECRET_KEY is still the example value. Set a random value.")
    if len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(f"SECRET_KEY is too short. Use at least {MIN_SECRET_LENGTH} characters.")
    return Config(
        secret_key=secret,
        public_base_url=env.get("PUBLIC_BASE_URL", "").rstrip("/"),
        data_dir=Path(env.get("DATA_DIR") or "/data"),
        tz=env.get("TZ") or "UTC",
        log_level=(env.get("LOG_LEVEL") or "info").lower(),
        claude_code_oauth_token=env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
    )
