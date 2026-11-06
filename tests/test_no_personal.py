"""Plan J4: nothing personal in the repo (CLAUDE.md rule). Scans tracked and untracked files.

Looks for real email addresses, home folders of real users, phone numbers, Upwork profile keys,
API token shapes, and the git author's own name and address. Generic placeholders pass.
Never reads .env, test_keys.txt, .venv, .ship or .git.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", ".ship", "__pycache__", ".pytest_cache", ".ruff_cache", "data"}
SKIP_FILES = {".env", "test_keys.txt"}
# Working logs written by the build agents (they hold the dev machine path). Not shipped docs.
LOG_FILES = {"plan.md", "progress.md"}
THIS_FILE = "tests/test_no_personal.py"  # holds fake samples on purpose
SKIP_SUFFIXES = {".db", ".pyc", ".png", ".jpg", ".ico", ".woff", ".woff2", ".gz", ".zip"}

# Domains that are placeholders by definition (RFC 2606 and common test names).
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "localhost", "invalid", "test"}
PLACEHOLDER_TLDS = {"example", "test", "invalid", "localhost"}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
HOME_PATH = re.compile(r"(?:/home/(?!app\b)|/Users/|C:\\Users\\)([A-Za-z][\w.-]*)")
PHONE = re.compile(r"(?<![\w.])\+\d{1,3}[ -]?\(?\d{2,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}(?![\w])")
UPWORK_KEY = re.compile(r"~01[0-9a-f]{12,}")
TOKEN = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}")


def repo_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.decode()
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        names = []
    if not names:  # no git (the image): walk the folder
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            names += [str((Path(base) / f).relative_to(ROOT)) for f in files]
    found = []
    for n in sorted(set(names)):
        p = Path(n)
        if (
            SKIP_DIRS & set(p.parts)
            or p.name in SKIP_FILES
            or p.name in LOG_FILES
            or p.as_posix() == THIS_FILE
            or p.suffix in SKIP_SUFFIXES
        ):
            continue
        if (ROOT / p).is_file():
            found.append(p)
    return found


def read(p: Path) -> str | None:
    try:
        return (ROOT / p).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def is_placeholder_domain(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    return (
        d in PLACEHOLDER_DOMAINS
        or d.rsplit(".", 1)[-1] in PLACEHOLDER_TLDS
        or any(d.endswith("." + x) for x in PLACEHOLDER_DOMAINS)
    )


def git_identity() -> set[str]:
    """The names and addresses of the people who made the commits (empty without git)."""
    try:
        out = subprocess.run(
            ["git", "log", "--format=%an%n%ae%n%cn%n%ce"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return set()
    return {x.strip() for x in out.splitlines() if len(x.strip()) >= 4}


def scan_text(text: str) -> list[str]:
    hits = []
    for m in EMAIL.finditer(text):
        if not is_placeholder_domain(m.group(1)):
            hits.append(f"email {m.group(0)}")
    for m in HOME_PATH.finditer(text):
        hits.append(f"home folder {m.group(0)}")
    for m in PHONE.finditer(text):
        hits.append(f"phone number {m.group(0)}")
    for m in UPWORK_KEY.finditer(text):
        if len(set(m.group(0)[3:])) >= 6:  # ~01aaaa...1 is a placeholder; real keys are random
            hits.append(f"Upwork profile key {m.group(0)}")
    if TOKEN.search(text):
        hits.append("token-like string")
    return hits


def test_no_personal_data_in_repo():
    problems = []
    for p in repo_files():
        text = read(p)
        if text is None:
            continue
        problems += [f"{p}: {h}" for h in scan_text(text)]
    assert not problems, "Personal data found:\n" + "\n".join(problems)


def test_git_author_is_not_written_in_any_file():
    identity = git_identity()
    problems = []
    for p in repo_files():
        text = read(p)
        if text is None:
            continue
        low = text.lower()
        problems += [f"{p}: {who}" for who in identity if who.lower() in low]
    assert not problems, "The author's name or address is written in the repo:\n" + "\n".join(
        problems
    )


# ---------- the scanner itself ----------


def test_scanner_flags_real_looking_data():
    assert scan_text("write to jane.doe@gmail.com")
    assert scan_text("path /home/jane/project/app.py")
    assert scan_text("path /Users/jane/x")
    assert scan_text("call +49 170 1234567")
    assert scan_text("key ~01f3a9c2b7d4e6a81b2c")
    assert scan_text("sk-ant-api03-abcdefghijklmnopqrstuvwx")


def test_scanner_allows_placeholders():
    ok = (
        "me@example.com a@b.example to@x.test you@localhost user@mail.example.org "
        "/home/app/.local/bin ~01aaaaaaaaaaaaaaa1 +x "
        "see https://www.upwork.com/jobs/~022118563532176214229"
    )
    assert scan_text(ok) == []


def test_scan_skips_secrets_and_logs():
    names = {p.name for p in repo_files()}
    assert not names & (SKIP_FILES | LOG_FILES)
