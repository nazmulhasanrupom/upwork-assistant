"""First-run setup wizard `/setup` (SPEC 4.2, plan J1).

Step 1 (create the account: email + password) is the only part open without a login, and only
while no admin exists. Once an admin exists it answers 404 (no takeover). 2-step login is optional
and lives in Settings. The other
steps need login + CSRF (the app-wide guard). The wizard does not redo work that has its own page:
it links to /upwork, /context and /settings/email, and keeps the progress in the `setup.*` settings.

- GET  /setup            no admin: step 1 form. Admin but not logged in: /login. Else the checklist.
- POST /setup/account    step 1: email + password (argon2) -> admin, logged in, redirect to /
- POST /setup/claude     step 1: save the pasted token/key (encrypted), run the Claude check
- POST /setup/keywords   step 5 | POST /setup/pay  step 6 | POST /setup/schedule  step 9
- POST /setup/style      step 7: keep the default writing style
- POST /setup/skip/{key} skip an optional step (email)
- POST /setup/dryrun     step 10: dry-run ON (never turns it off)
- POST /setup/finish     all required steps done
- GET  /setup/admin[...], /setup/totp  old URLs: redirect to /setup
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import auth, claude_auth
from app.auth_routes import render
from app.gmail import Gmail
from app.memory import memory_file, read_file
from app.tools import claude_check
from app.upwork import accounts
from app.worker import WorkerError, known_action

log = logging.getLogger("setup")

router = APIRouter()

# key, title, what to do. "link" steps have their own page; "inline" steps have a form here.
STEPS: tuple[dict[str, str], ...] = (
    {"key": "claude", "title": "Claude check", "kind": "inline"},
    {"key": "upwork", "title": "Connect Upwork", "kind": "link", "link": "/upwork"},
    {"key": "profile", "title": "Build profile context", "kind": "link", "link": "/context"},
    {"key": "keywords", "title": "Search keywords", "kind": "inline"},
    {"key": "pay", "title": "Pay rules", "kind": "inline"},
    {"key": "style", "title": "Writing style", "kind": "inline"},
    {"key": "email", "title": "Email (optional)", "kind": "link", "link": "/settings/email"},
    {"key": "schedule", "title": "Schedules and time zone", "kind": "inline"},
    {"key": "dryrun", "title": "Start in dry-run mode", "kind": "inline"},
)
OPTIONAL = {"email"}
PAY_KEYS = (
    "pay.good_spending",
    "pay.low_rate_max",
    "pay.mid_rate_min",
    "pay.mid_rate_max",
    "pay.high_rate_min",
    "pay.hourly_budget_min",
    "pay.fixed_budget_min",
    "pay.low_rate_high_hires",
    "pay.fit_min",
)
SCHEDULE_KEYS = ("schedules.daily", "schedules.digest", "schedules.weekly", "schedules.monthly")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _closed(request: Request) -> None:
    """Steps that create the admin: gone once an admin exists."""
    if auth.admin_exists(request.app.state.db):
        raise HTTPException(status_code=404)


# ---------- progress ----------


def done_keys(request: Request) -> set[str]:
    s = request.app.state
    done = {str(k) for k in s.settings.get("setup.done_steps")}
    if accounts.current(s.settings) is not None:
        done.add("upwork")
    if read_file(memory_file(s.config, "USER.md")).strip():
        done.add("profile")
    try:
        if Gmail(s.config, s.settings, s.db).connected:
            done.add("email")
    except Exception:
        log.exception("Could not read the Gmail state")
    return done


def progress(request: Request) -> dict[str, int]:
    """Required steps done / total, for the top bar. Optional steps are not counted."""
    done = done_keys(request)
    keys = [st["key"] for st in STEPS if st["key"] not in OPTIONAL]
    n = sum(1 for k in keys if k in done)
    return {"done": n, "total": len(keys), "percent": round(100 * n / len(keys)) if keys else 100}


def mark_done(request: Request, key: str) -> None:
    mark_done_in(request.app.state.settings, key)


def mark_done_in(settings: Any, key: str) -> None:
    done = [str(k) for k in settings.get("setup.done_steps")]
    if key not in done:
        settings.set("setup.done_steps", [*done, key])


def _current_step(request: Request, steps: list[dict[str, Any]]) -> str:
    """The one step shown on screen: ?step=, else the step a POST belongs to, else the first
    step still to do, else the Finish screen."""
    keys = [st["key"] for st in steps] + ["finish"]
    asked = request.query_params.get("step", "")
    if asked in keys:
        return asked
    last = request.url.path.rstrip("/").rsplit("/", 1)[-1]
    if request.url.path.startswith("/setup/") and last in keys:
        return last
    return next((st["key"] for st in steps if not st["done"]), "finish")


def _hub(request: Request, status: int = 200, **ctx: Any) -> Response:
    s = request.app.state
    steps = _hub_steps(request)
    required_left = [st["title"] for st in steps if not st["done"] and not st["optional"]]
    st = s.settings
    ctx.setdefault("notice", "")
    ctx.setdefault("error", "")
    ctx.setdefault("action_id", "")
    return render(
        request,
        "setup.html",
        status,
        steps=steps,
        current=_current_step(request, steps),
        required_left=required_left,
        finished=bool(st.get("setup.finished")),
        keywords_text="\n".join(str(k) for k in st.get("search.keywords")),
        pay_values={k: st.get(k) for k in PAY_KEYS},
        schedule_values={k: st.get(k) for k in SCHEDULE_KEYS},
        timezone=st.get("general.timezone"),
        claude_token_hint=claude_auth.hint(st, s.config, claude_auth.TOKEN_KEY),
        claude_key_hint=claude_auth.hint(st, s.config, claude_auth.API_KEY),
        claude_from_env=bool(s.config.claude_code_oauth_token or s.config.anthropic_api_key),
        dry_run=bool(st.get("general.dry_run")),
        **ctx,
    )


def _admin_page(request: Request, status: int = 200, error: str = "") -> Response:
    return render(request, "setup_admin.html", status, error=error)


# ---------- entry ----------


@router.get("/setup")
def setup_home(request: Request, action: str = "") -> Response:
    if not auth.admin_exists(request.app.state.db):
        return _admin_page(request)
    if auth.current_user(request) is None:
        return _redirect("/login")
    return _hub(request, action_id=known_action(request.app.state.worker, action))


@router.get("/setup/admin")
def old_setup_admin() -> Response:
    return _redirect("/setup")


@router.get("/setup/admin/totp")
@router.get("/setup/totp")
def old_setup_totp() -> Response:
    return _redirect("/setup")


# ---------- step 1: create the account ----------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX = 254


@router.post("/setup/account")
def setup_account(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    """The first visitor makes the account (email + password) and is logged in. Closed (404)
    once an admin exists. 2-step login starts off; it is turned on in Settings."""
    conn: sqlite3.Connection = request.app.state.db
    _closed(request)
    email = email.strip().lower()
    min_len = request.app.state.settings.get("security.password_min_length")
    error = ""
    if len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
        error = "Enter a valid email address."
    elif len(password) < min_len:
        error = f"The password needs at least {min_len} characters."
    elif password != password2:
        error = "The two passwords are not the same."
    if error:
        return _admin_page(request, 400, error)
    try:
        auth.create_admin(conn, email, auth.hash_password(password))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=404) from None
    auth.record_success(conn)
    auth.load_session(request).login(email)
    return _redirect("/")


# ---------- step 2: Claude ----------


def save_claude_fields(request: Request, token: str, api_key: str) -> None:
    """Save what was pasted (empty = keep what is saved). Values are stored encrypted."""
    s = request.app.state
    for key, plain in ((claude_auth.TOKEN_KEY, token), (claude_auth.API_KEY, api_key)):
        plain = plain.strip()
        if plain:
            claude_auth.save(s.settings, s.config, key, plain[: claude_auth.VALUE_MAX])


@router.post("/setup/claude")
def setup_claude(
    request: Request, oauth_token: str = Form(""), api_key: str = Form("")
) -> Response:
    config = request.app.state.config
    save_claude_fields(request, oauth_token, api_key)
    env = dict(os.environ)
    claude_auth.apply(env, request.app.state.settings, config)

    def work(ctx: Any) -> tuple[str, str]:
        # the message never holds the token
        ok, message = claude_check.check(env, conn=ctx.conn, data_dir=config.data_dir)
        if not ok:
            raise WorkerError(message)
        mark_done_in(ctx.settings, "claude")
        return f"/setup?step=claude&action={ctx.action_id}", "Claude works."

    # The check starts Claude, which is slow: it runs on the worker (plan W3).
    action_id = request.app.state.worker.submit(
        "setup_claude", "claude", "Checking Claude", work, "setup_claude"
    )
    return _redirect(f"/setup?step=claude&action={action_id}")


# ---------- steps 5, 6, 9: forms that save settings ----------


@router.post("/setup/keywords")
def setup_keywords(request: Request, keywords: str = Form("")) -> Response:
    st = request.app.state.settings
    max_n, max_chars = int(st.get("setup.keywords_max")), int(st.get("setup.keyword_max_chars"))
    items: list[str] = []
    for line in keywords.replace(",", "\n").splitlines():
        word = " ".join(line.split())
        if word and word.lower() not in {i.lower() for i in items}:
            items.append(word)
    if not items:
        return _hub(request, 400, error="Enter at least one keyword.")
    if len(items) > max_n or any(len(i) > max_chars for i in items):
        msg = f"Use at most {max_n} keywords, each up to {max_chars} characters."
        return _hub(request, 400, error=msg)
    st.set("search.keywords", items)
    mark_done(request, "keywords")
    return _hub(request, notice="Keywords saved.")


@router.post("/setup/pay")
async def setup_pay(request: Request) -> Response:
    st = request.app.state.settings
    form = await request.form()
    values: dict[str, int] = {}
    for key in PAY_KEYS:
        raw = str(form.get(key, "")).strip()
        if not raw.isdigit():
            return _hub(request, 400, error=f"{key} must be a whole number, 0 or more.")
        values[key] = int(raw)
    if values["pay.mid_rate_min"] > values["pay.mid_rate_max"]:
        return _hub(request, 400, error="The middle rate minimum is above its maximum.")
    for key, value in values.items():
        st.set(key, value)
    mark_done(request, "pay")
    return _hub(request, notice="Pay rules saved.")


@router.post("/setup/schedule")
async def setup_schedule(request: Request) -> Response:
    st = request.app.state.settings
    form = await request.form()
    limit = int(st.get("setup.cron_max_chars"))
    values: dict[str, str] = {}
    for key in SCHEDULE_KEYS:
        expr = " ".join(str(form.get(key, "")).split())
        if not expr or len(expr) > limit or not croniter.is_valid(expr):
            return _hub(request, 400, error=f"{key}: that is not a valid cron schedule.")
        values[key] = expr
    tz = str(form.get("general.timezone", "")).strip()
    try:
        ZoneInfo(tz)
    except Exception:
        return _hub(request, 400, error="That time zone is not known (example: Europe/Berlin).")
    for key, value in values.items():
        st.set(key, value)
    st.set("general.timezone", tz)
    mark_done(request, "schedule")
    return _hub(request, notice="Schedules saved.")


# ---------- step 7, skip, step 10, finish ----------


@router.post("/setup/style")
def setup_style(request: Request) -> Response:
    mark_done(request, "style")
    return _hub(request, notice="The default writing style stays. Change it any time on /context.")


@router.post("/setup/skip/{key}")
def setup_skip(request: Request, key: str) -> Response:
    if key not in OPTIONAL:
        raise HTTPException(status_code=404)
    mark_done(request, key)
    return _hub(request, notice="Skipped.")


@router.post("/setup/dryrun")
def setup_dryrun(request: Request) -> Response:
    request.app.state.settings.set("general.dry_run", True)
    mark_done(request, "dryrun")
    return _hub(request, notice="Dry-run is ON. Nothing is sent to Upwork.")


@router.post("/setup/finish")
def setup_finish(request: Request) -> Response:
    st = request.app.state.settings
    left = [s["title"] for s in _hub_steps(request) if not s["done"] and not s["optional"]]
    if left:
        return _hub(request, 400, error="Not finished yet: " + ", ".join(left) + ".")
    st.set("general.dry_run", True)  # the wizard never ends with dry-run off
    st.set("setup.finished", True)
    return _redirect("/")


def _hub_steps(request: Request) -> list[dict[str, Any]]:
    done = done_keys(request)
    return [{**st, "done": st["key"] in done, "optional": st["key"] in OPTIONAL} for st in STEPS]
