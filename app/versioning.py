"""Version history of the learning files (SPEC2 5.2, plan L2).

`<DATA_DIR>/memory` and `<DATA_DIR>/skills` live in one git repo rooted at DATA_DIR. A
`.gitignore` ignores everything else there (app.db, tokens, runs, claude config), and every commit
adds explicit paths inside memory/ or skills/ only, so a secret can never be tracked.

Rules:
- git runs through `subprocess` with an argument list (no shell), with a timeout from settings,
  and with a clean environment (no user git config, no hooks, no signing).
- `commit` never raises: a git problem is logged and shown as a dashboard alert.
- `revert` aborts on a conflict and leaves the repo clean.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Config
from app.settings import load_defaults

log = logging.getLogger("versioning")

ALERT_KEY = "versioning_failed"
TRACKED = ("memory", "skills")
GITIGNORE = (
    "# Only memory/ and skills/ are versioned. Never track the database, tokens or runs.\n"
    "/*\n"
    "!/.gitignore\n"
    "!/memory/\n"
    "!/skills/\n"
    "*.tmp\n"
)
_SHA = re.compile(r"[0-9a-f]{7,40}")
_LOCK = threading.RLock()


class VersioningError(Exception):
    """A git problem. The message is short and safe to show on the page."""


def git_available() -> bool:
    import shutil

    return shutil.which("git") is not None


def _get(settings: Any, key: str) -> Any:
    return settings.get(key) if settings is not None else load_defaults()[key]


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
    )
    return env


def _git(config: Config, settings: Any, *args: str, check: bool = True) -> str:
    data = config.data_dir
    cmd = [
        "git",
        f"--git-dir={data / '.git'}",
        f"--work-tree={data}",
        "-c",
        f"user.name={_get(settings, 'versioning.author_name')}",
        "-c",
        f"user.email={_get(settings, 'versioning.author_email')}",
        "-c",
        f"safe.directory={data}",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.quotepath=false",
        "-c",
        "gc.auto=0",  # no detached background gc: it would leave zombies when we are PID 1
        "-c",
        "maintenance.auto=false",
        *args,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - argument list, no shell
            cmd,
            cwd=data,
            env=_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(_get(settings, "versioning.git_timeout_seconds")),
            check=False,
        )
    except FileNotFoundError as e:
        raise VersioningError("git is not installed.") from e
    except subprocess.TimeoutExpired as e:
        raise VersioningError("git took too long.") from e
    if check and proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[:300]
        raise VersioningError(f"git {args[0]} failed: {detail}")
    return proc.stdout


def _rel_paths(config: Config, paths: list[Path | str] | None) -> list[str]:
    """Paths relative to DATA_DIR, each inside memory/ or skills/. Raises VersioningError."""
    if not paths:
        return list(TRACKED)
    data = config.data_dir.resolve()
    out: list[str] = []
    for p in paths:
        path = Path(p)
        full = (path if path.is_absolute() else data / path).resolve()
        try:
            rel = full.relative_to(data)
        except ValueError as e:
            raise VersioningError("Path is outside the data folder.") from e
        if not rel.parts or rel.parts[0] not in TRACKED:
            raise VersioningError("Only memory/ and skills/ are versioned.")
        out.append(rel.as_posix())
    return out


def _existing(config: Config, rels: list[str]) -> list[str]:
    return [r for r in rels if (config.data_dir / r).exists()]


def ensure_repo(config: Config, settings: Any = None, baseline: bool = True) -> bool:
    """Create the repo, the .gitignore and a first commit when missing. True if created.

    `baseline` puts the files that exist now in the first commit (done at app start). `commit`
    passes False so the change being saved gets its own entry."""
    with _LOCK:
        data = config.data_dir
        if (data / ".git").exists():
            _ensure_gitignore(config)
            return False
        data.mkdir(parents=True, exist_ok=True)
        for name in TRACKED:
            (data / name).mkdir(exist_ok=True)
        _git(config, settings, "init", "--quiet", "--initial-branch=main")
        _ensure_gitignore(config)
        _git(
            config,
            settings,
            "add",
            "-A",
            "--",
            ".gitignore",
            *(_existing(config, list(TRACKED)) if baseline else []),
        )
        _git(
            config,
            settings,
            "commit",
            "--quiet",
            "--no-verify",
            "--allow-empty",
            "-m",
            "Start version history",
        )
        return True


def _ensure_gitignore(config: Config) -> None:
    path = config.data_dir / ".gitignore"
    if not path.exists() or path.read_text(encoding="utf-8") != GITIGNORE:
        path.write_text(GITIGNORE, encoding="utf-8")


def _set_alert(settings: Any, message: str | None) -> None:
    conn = getattr(settings, "conn", None)
    if conn is None:
        return
    try:
        if message is None:
            conn.execute("DELETE FROM alerts WHERE key = ?", (ALERT_KEY,))
        else:
            conn.execute(
                "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
                (
                    ALERT_KEY,
                    f"Could not save version history: {message}"[:300],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
    except Exception:
        log.exception("Could not update the versioning alert")


def _clean_message(message: str, settings: Any) -> str:
    text = " ".join(str(message or "").split()) or "Update"
    return text[: int(_get(settings, "versioning.message_max_chars"))]


def commit(
    config: Config, settings: Any, message: str, paths: list[Path | str] | None = None
) -> str | None:
    """Commit the given paths (default: memory/ and skills/) with `message`.

    Returns the short sha, or None when nothing changed or git failed. NEVER raises: a git
    problem is logged and becomes a dashboard alert, so the caller's own write is not lost.
    """
    try:
        with _LOCK:
            ensure_repo(config, settings, baseline=False)
            rels = _rel_paths(config, paths)
            present = _existing(config, rels)
            if present:
                _git(config, settings, "add", "-A", "--", *present)
            for gone in [r for r in rels if r not in present]:  # a deleted file or folder
                _git(config, settings, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", gone)
            staged = _git(config, settings, "diff", "--cached", "--name-only")
            if not staged.strip():
                _set_alert(settings, None)
                return None
            _git(
                config,
                settings,
                "commit",
                "--quiet",
                "--no-verify",
                "-m",
                _clean_message(message, settings),
            )
            sha = _git(config, settings, "rev-parse", "--short", "HEAD").strip()
        _set_alert(settings, None)
        return sha
    except Exception as e:
        log.exception("Version history commit failed")
        _set_alert(settings, str(e)[:200])
        return None


def _check_sha(sha: str) -> str:
    sha = str(sha or "").strip().lower()
    if not _SHA.fullmatch(sha):
        raise VersioningError("That is not a valid version id.")
    return sha


def _resolve(config: Config, settings: Any, sha: str) -> str:
    sha = _check_sha(sha)
    out = _git(
        config, settings, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False
    )
    full = out.strip()
    if not _SHA.fullmatch(full):
        raise VersioningError("That version does not exist.")
    return full


def list_commits(config: Config, settings: Any = None, limit: int | None = None) -> list[dict]:
    """Newest first: sha, short, date (ISO), subject, files. [] when there is no repo."""
    n = max(1, min(int(limit or _get(settings, "versioning.page_commits")), 500))
    with _LOCK:
        if not (config.data_dir / ".git").exists():
            return []
        out = _git(
            config,
            settings,
            "log",
            f"-n{n}",
            "--name-only",
            "--format=%x1e%H%x1f%aI%x1f%s",
            check=False,
        )
    commits = []
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        head, _, files = block.partition("\n")
        parts = head.split("\x1f")
        if len(parts) != 3:
            continue
        commits.append(
            {
                "sha": parts[0],
                "short": parts[0][:7],
                "date": parts[1],
                "subject": parts[2],
                "files": [f for f in files.splitlines() if f.strip()],
            }
        )
    return commits


def show_diff(config: Config, settings: Any, sha: str) -> str:
    """The patch of one commit as text. Raises VersioningError for a bad or unknown sha."""
    with _LOCK:
        full = _resolve(config, settings, sha)
        return _git(config, settings, "show", "--no-color", "--format=", "--patch", full)


def _revert_in_progress(config: Config) -> bool:
    return (config.data_dir / ".git" / "REVERT_HEAD").exists()


def revert(config: Config, settings: Any, sha: str) -> str:
    """Undo one commit with `git revert --no-edit`. Returns the new short sha.

    On a conflict (or any failure) the revert is aborted and VersioningError is raised: the
    repo is left as it was.
    """
    with _LOCK:
        full = _resolve(config, settings, sha)
        # Save hand edits of tracked files first, so the revert starts from a clean tree.
        if _git(config, settings, "status", "--porcelain", "--untracked-files=no").strip():
            _git(config, settings, "add", "-u")
            _git(
                config,
                settings,
                "commit",
                "--quiet",
                "--no-verify",
                "-m",
                "Save changes made outside the app",
                check=False,
            )
        try:
            _git(config, settings, "revert", "--no-edit", full)
        except VersioningError as e:
            if _revert_in_progress(config) or "conflict" in str(e).lower():
                _git(config, settings, "revert", "--abort", check=False)
            if _revert_in_progress(config):  # abort did not work: last resort
                _git(config, settings, "reset", "--merge", check=False)
            _git(config, settings, "revert", "--quit", check=False)
            raise VersioningError(
                "This change cannot be undone on its own: later changes touch the same lines. "
                "Nothing was changed."
            ) from e
        return _git(config, settings, "rev-parse", "--short", "HEAD").strip()
