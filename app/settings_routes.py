"""Settings page (plan J3, SPEC 4.2).

- GET  /settings                    login: every value in defaults.toml, by section
- POST /settings/save/{section}     login + CSRF: typed, validated, saved to the settings table
- POST /settings/toggle/{name}      login + CSRF: dry-run and kill switch
- GET  /settings/export.json        login: settings and a data summary (no secrets, no tokens)
- POST /settings/delete-all         login + CSRF: owner, typed phrase AND password or TOTP

Secrets are never shown, never exported and never saved here. Delete all data only touches
files inside DATA_DIR (every path is resolved and checked first).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import auth, crypto
from app.auth_routes import render

log = logging.getLogger("settings_page")
router = APIRouter()

CONFIRM_PHRASE = "DELETE ALL MY DATA"
TOGGLES = {"dry_run": "general.dry_run", "kill_switch": "general.kill_switch"}
SECRET_ENDINGS = ("password", "secret", "token", "_enc")
# Written by the app, shown read-only. Edited on their own pages (Email, Upwork) or never by hand.
READONLY_PREFIXES = ("scheduler.", "setup.done_steps", "setup.finished")
READONLY_KEYS = {
    "keywords.baseline_started",
    "safety.jss_guard_note",
    "email.gmail_address",
    "email.gmail_client_id",
}
# (min, max) for numbers with a real range. Others: 0 or more (any sign if the default is below 0).
RANGES: dict[str, tuple[float, float]] = {
    "security.session_hours": (1, 24 * 90),
    "security.password_min_length": (8, 128),
    "pay.fit_min": (0, 100),
}
NUMBER_MAX = 1_000_000_000
TEXT_MAX = 2000
LIST_MAX = 200


def is_secret(key: str) -> bool:
    last = key.rsplit(".", 1)[-1]
    return last.endswith(SECRET_ENDINGS)


def is_readonly(key: str) -> bool:
    return (
        key in READONLY_KEYS
        or key.startswith(READONLY_PREFIXES)
        or key.rsplit(".", 1)[-1] == "last_run"
    )


def section_of(key: str) -> str:
    return key.split(".", 1)[0]


def editable_keys(defaults: dict[str, Any]) -> list[str]:
    return [k for k in defaults if not is_secret(k) and not is_readonly(k)]


# ---------------------------------------------------------------- parse and validate


class Invalid(ValueError):
    pass


def _list_items(raw: str, int_items: bool) -> list[Any]:
    parts = [p.strip() for p in re.split(r"[\n,]" if int_items else r"\n", raw)]
    items = [p for p in parts if p]
    if len(items) > LIST_MAX:
        raise Invalid(f"at most {LIST_MAX} items")
    if int_items:
        try:
            return [int(i) for i in items]
        except ValueError as e:
            raise Invalid("whole numbers only, one per line or separated by commas") from e
    if any(len(i) > TEXT_MAX for i in items):
        raise Invalid(f"each item up to {TEXT_MAX} characters")
    return items


def parse_value(key: str, default: Any, raw: str) -> Any:
    """Turn the submitted text into the right type or raise Invalid with a short reason."""
    raw = raw if isinstance(raw, str) else ""
    if isinstance(default, bool):
        return raw == "1"
    if isinstance(default, int):
        text = raw.strip()
        if not re.fullmatch(r"-?\d+", text):
            raise Invalid("must be a whole number")
        value: float = int(text)
    elif isinstance(default, float):
        try:
            value = float(raw.strip())
        except ValueError as e:
            raise Invalid("must be a number") from e
        if not math.isfinite(value):
            raise Invalid("must be a number")
    elif isinstance(default, list):
        int_items = bool(default) and all(
            isinstance(i, int) and not isinstance(i, bool) for i in default
        )
        return _list_items(raw, int_items)
    else:
        text = " ".join(raw.split())
        if len(text) > TEXT_MAX:
            raise Invalid(f"at most {TEXT_MAX} characters")
        if key.startswith("schedules.") and not croniter.is_valid(text):
            raise Invalid("not a valid cron schedule")
        if key == "general.timezone":
            try:
                ZoneInfo(text)
            except Exception as e:
                raise Invalid("unknown time zone (example: Europe/Berlin)") from e
        return text
    low, high = RANGES.get(key, (-NUMBER_MAX if default < 0 else 0, NUMBER_MAX))
    if not low <= value <= high:
        raise Invalid(f"must be between {low:g} and {high:g}")
    return value


def input_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return "\n".join(str(i) for i in value)
    return str(value)


# ---------------------------------------------------------------- page


def _sections(request: Request, form: dict[str, str] | None = None, errors=None) -> list[dict]:
    settings = request.app.state.settings
    current = settings.all()
    defaults = settings.defaults
    errors = errors or {}
    out: dict[str, dict] = {}
    for key, default in defaults.items():
        if is_secret(key):
            continue
        kind = (
            "bool"
            if isinstance(default, bool)
            else "list"
            if isinstance(default, list)
            else "number"
            if isinstance(default, (int, float))
            else "text"
        )
        shown = form[key] if form and key in form else input_text(current[key])
        sec = out.setdefault(
            section_of(key), {"name": section_of(key), "items": [], "has_errors": False}
        )
        sec["items"].append(
            {
                "key": key,
                "kind": kind,
                "value": shown,
                "checked": shown == "1",
                "readonly": is_readonly(key),
                "error": errors.get(key, ""),
                "changed": current[key] != default,
                "default": input_text(default),
            }
        )
        if key in errors:
            sec["has_errors"] = True
    return list(out.values())


def _page(request: Request, status: int = 200, **ctx: Any) -> Response:
    st = request.app.state.settings
    ctx.setdefault("notice", "")
    ctx.setdefault("error", "")
    return render(
        request,
        "settings.html",
        status,
        sections=ctx.pop("sections", None) or _sections(request),
        dry_run=bool(st.get("general.dry_run")),
        kill_switch=bool(st.get("general.kill_switch")),
        phrase=CONFIRM_PHRASE,
        **ctx,
    )


@router.get("/settings")
def settings_page(request: Request) -> Response:
    return _page(request)


@router.post("/settings/toggle/{name}")
def settings_toggle(request: Request, name: str, value: str = Form("")) -> Response:
    key = TOGGLES.get(name)
    if key is None:
        raise HTTPException(status_code=404)
    on = value == "on"
    request.app.state.settings.set(key, on)
    label = "Dry-run" if name == "dry_run" else "Kill switch"
    return _page(request, notice=f"{label} is {'ON' if on else 'OFF'}.")


@router.post("/settings/save/{section}")
async def settings_save(request: Request, section: str) -> Response:
    st = request.app.state.settings
    keys = [k for k in editable_keys(st.defaults) if section_of(k) == section]
    if not keys:
        raise HTTPException(status_code=404)
    posted = await request.form()
    form = {k: str(posted.getlist(k)[-1]) for k in keys if k in posted}
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key in keys:
        if key not in form:
            continue
        try:
            values[key] = parse_value(key, st.defaults[key], form[key])
        except Invalid as e:
            errors[key] = str(e)
    if errors:
        msg = f"Nothing was saved. Fix {len(errors)} value(s) in the {section} section."
        return _page(request, 400, error=msg, sections=_sections(request, form, errors))
    for key, value in values.items():
        st.set(key, value)
    return _page(request, notice=f"Saved the {section} settings.")


# ---------------------------------------------------------------- export


def _tables(conn: sqlite3.Connection) -> list[str]:
    """User tables only: no sqlite_ tables, no FTS shadow tables."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    virtual = [r["name"] for r in rows if (r["sql"] or "").upper().startswith("CREATE VIRTUAL")]
    return [
        r["name"]
        for r in rows
        if not any(r["name"].startswith(v + "_") for v in virtual) and r["name"] != "schema_version"
    ]


def data_summary(conn: sqlite3.Connection, data_dir: Path) -> dict[str, Any]:
    """Counts only. No row content, no file names, no tokens."""
    skip = {"admin", "auth_state", "setup_code", "oauth_state"}
    tables = {
        t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]  # noqa: S608
        for t in _tables(conn)
        if t not in skip
    }
    files: dict[str, int] = {}
    root = data_dir.resolve()
    for sub in ("memory", "skills", "runs"):
        folder = root / sub
        files[sub] = (
            sum(1 for p in folder.rglob("*") if p.is_file() and ".git" not in p.parts)
            if folder.is_dir()
            else 0
        )
    return {"tables": tables, "files": files}


@router.get("/settings/export.json")
def settings_export(request: Request) -> Response:
    s = request.app.state
    values = {k: v for k, v in s.settings.all().items() if not is_secret(k)}
    body = {
        "settings": values,
        "data_summary": data_summary(s.db, s.config.data_dir),
    }
    return Response(
        json.dumps(body, indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="upwork-assistant-export.json"'},
    )


# ---------------------------------------------------------------- delete all data


class UnsafePath(RuntimeError):
    pass


def inside(data_dir: Path, path: Path) -> Path:
    """The path itself (a link is not followed) must sit strictly inside data_dir."""
    root = data_dir.resolve()
    if len(root.parts) < 3:
        raise UnsafePath(f"Refusing to work on {root}: too close to the file system root.")
    # resolve the parent only: a symlink in DATA_DIR is removed itself, never followed
    candidate = path.parent.resolve() / path.name
    if candidate == root or root not in candidate.parents:
        raise UnsafePath(f"{path} is outside the data folder.")
    return candidate


def wipe_files(
    data_dir: Path, keep: tuple[str, ...] = ("app.db", "app.db-wal", "app.db-shm")
) -> int:
    """Delete everything inside data_dir except the database files. Returns entries removed."""
    root = data_dir.resolve()
    if not root.is_dir():
        return 0
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.name in keep:
            continue
        target = inside(data_dir, entry)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        removed += 1
    return removed


def wipe_db(conn: sqlite3.Connection) -> int:
    """Delete every row of every user table. The schema and its version stay."""
    tables = _tables(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tables:
                conn.execute(f'DELETE FROM "{t}"')  # noqa: S608
            # fixed rows the schema expects (003_admin.sql)
            conn.execute("INSERT OR IGNORE INTO auth_state(id, failed_count) VALUES (1, 0)")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        log.warning("Could not compact the database after the wipe")
    return len(tables)


def reauth_ok(request: Request, password: str, code: str) -> bool:
    """The owner's password, or a current TOTP code. Wrong tries count toward the login lock."""
    s = request.app.state
    admin = auth.get_admin(s.db)
    if admin is None or auth.lock_seconds_remaining(s.db):
        return False
    ok = bool(password) and auth.verify_password(admin["password_hash"], password)
    if not ok and code.strip():
        try:
            ok = auth.verify_totp(
                crypto.decrypt(s.config.secret_key, admin["totp_secret_enc"]), code
            )
        except Exception:
            log.error("could not read the stored TOTP secret (SECRET_KEY changed?)")
    if ok:
        auth.record_success(s.db)
    else:
        auth.record_failure(s.db, s.settings)
    return ok


@router.post("/settings/delete-all")
def settings_delete_all(
    request: Request,
    phrase: str = Form(""),
    password: str = Form(""),
    code: str = Form(""),
) -> Response:
    s = request.app.state
    if auth.current_user(request) is None:  # the owner is the only account
        raise HTTPException(status_code=403)
    if phrase.strip() != CONFIRM_PHRASE:
        return _page(request, 400, error=f"Type the phrase exactly: {CONFIRM_PHRASE}")
    if auth.lock_seconds_remaining(s.db):
        return _page(request, 429, error="Too many wrong tries. Try again later.")
    if not reauth_ok(request, password, code):
        return _page(request, 403, error="Wrong password or code. Nothing was deleted.")
    try:
        wipe_files(s.config.data_dir)
        wipe_db(s.db)
    except UnsafePath as e:
        log.error("Delete all data refused: %s", e)
        return _page(request, 400, error=str(e))
    except (OSError, sqlite3.Error):
        log.exception("Delete all data failed")
        return _page(request, 500, error="Deleting failed part way. See the log.")
    log.warning("All data was deleted by the owner")
    # Back to a fresh install: a new one-time setup code, then log out.
    print(f"SETUP CODE: {auth.issue_setup_code(s.db, s.config.secret_key)}", flush=True)
    auth.load_session(request).logout()
    return RedirectResponse("/setup", status_code=303)
