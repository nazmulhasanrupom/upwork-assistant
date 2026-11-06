"""Import the Claude Code Upwork login (plan P4).

The user logs in to Upwork with Claude Code on a laptop (own CLAUDE_CONFIG_DIR) and uploads that
folder's `.credentials.json` on the /upwork page. We keep ONLY the `mcpOAuth` entries whose
`serverUrl` is the Upwork MCP URL and merge them into `<config dir>/.credentials.json`.
Other keys already in that file (for example `claudeAiOauth`) stay. Nothing in the upload
other than those entries is kept; the model login comes from CLAUDE_CODE_OAUTH_TOKEN.

Token values are never logged, returned in messages, or rendered.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_UPLOAD_BYTES = 64 * 1024
CREDENTIALS_NAME = ".credentials.json"


class LoginImportError(ValueError):
    """The upload is not usable. The message is safe to show (it never holds token values)."""


def _same_url(a: object, b: str) -> bool:
    return isinstance(a, str) and a.strip().rstrip("/") == b.strip().rstrip("/")


def extract_upwork_entries(raw: bytes, mcp_url: str) -> dict[str, Any]:
    """Validate the upload and return only the mcpOAuth entries for the Upwork URL."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise LoginImportError(f"The file is too big (max {MAX_UPLOAD_BYTES // 1024} KB).")
    if not raw.strip():
        raise LoginImportError("The file is empty.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LoginImportError("The file is not valid JSON.") from None
    if not isinstance(data, dict):
        raise LoginImportError("The file must be a JSON object.")
    oauth = data.get("mcpOAuth")
    if not isinstance(oauth, dict) or not oauth:
        raise LoginImportError("The file has no mcpOAuth login. Did you log in with /mcp?")
    picked = {
        key: entry
        for key, entry in oauth.items()
        if isinstance(entry, dict) and _same_url(entry.get("serverUrl"), mcp_url)
    }
    if not picked:
        raise LoginImportError(f"No login for the Upwork server ({mcp_url}) was found in the file.")
    return picked


def _read_existing(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_private(path: Path, data: dict[str, Any]) -> None:
    """Atomic write with mode 600 (temp file in the same folder, fsync, rename)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cred-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def import_login(raw: bytes, config_dir: Path, mcp_url: str) -> int:
    """Validate `raw` and merge the Upwork login into the config dir. Returns the entry count."""
    picked = extract_upwork_entries(raw, mcp_url)
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, 0o700)
    except OSError:
        pass
    target = config_dir / CREDENTIALS_NAME
    merged = _read_existing(target)
    old = merged.get("mcpOAuth")
    old = old if isinstance(old, dict) else {}
    # Drop earlier Upwork logins (they are replaced), keep other servers' entries.
    kept = {
        k: v
        for k, v in old.items()
        if not (isinstance(v, dict) and _same_url(v.get("serverUrl"), mcp_url))
    }
    kept.update(picked)
    merged["mcpOAuth"] = kept
    write_private(target, merged)
    return len(picked)
