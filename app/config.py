"""Environment configuration. Refuses to start on a bad SECRET_KEY (SPEC 10.4)."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

EXAMPLE_SECRET = "change-me-long-random-string"
MIN_SECRET_LENGTH = 32
SECRET_FILE = "secret_key"


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
    def gmail_path(self) -> Path:
        return self.data_dir / "gmail.enc"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def memory_dir(self) -> Path:
        """Runtime memory files: USER.md, MEMORY.md, PLAYBOOK.md, DREAMS.md, daily/ (SPEC2 5.1)."""
        return self.data_dir / "memory"


def _read_key_file(path: Path) -> str:
    # A racing process may have created the file but not written it yet: wait a little.
    for _ in range(50):
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
        time.sleep(0.05)
    raise ConfigError(f"The key file {path} is empty. Delete it and start again.")


def data_secret(data_dir: Path) -> str:
    """The key made by the app itself: <data_dir>/secret_key, created once (0600).
    O_EXCL makes it race-safe: the panel and the task processes share /data, and the
    process that loses the race reads the winner's key. The key is never logged."""
    path = data_dir / SECRET_FILE
    try:
        if path.exists():
            return _read_key_file(path)
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _read_key_file(path)
        key = secrets.token_urlsafe(48)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
        return key
    except OSError as e:
        msg = f"Cannot read or create the key file in {data_dir}: {e.strerror}"
        raise ConfigError(msg) from None


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    data_dir = Path(env.get("DATA_DIR") or "/data")
    secret = env.get("SECRET_KEY", "")
    if not secret:
        secret = data_secret(data_dir)  # no .env needed: the app makes its own key
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f"The key file in {data_dir} is too short. Delete it and restart.")
    elif secret == EXAMPLE_SECRET:
        raise ConfigError("SECRET_KEY is still the example value. Set a random value or clear it.")
    elif len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(f"SECRET_KEY is too short. Use at least {MIN_SECRET_LENGTH} characters.")
    return Config(
        secret_key=secret,
        public_base_url=env.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        data_dir=data_dir,
        tz=env.get("TZ") or "UTC",
        log_level=(env.get("LOG_LEVEL") or "info").lower(),
        claude_code_oauth_token=env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
    )
