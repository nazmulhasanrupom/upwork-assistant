"""First-run setup wizard `/setup` (SPEC 4.2, plan J1).

Step 1 (admin account + authenticator) is the only part open without a login, and only while no
admin exists. Once an admin exists every step that creates one answers 404 (no takeover). The other
steps need login + CSRF (the app-wide guard). The wizard does not redo work that has its own page:
it links to /upwork, /context and /settings/email, and keeps the progress in the `setup.*` settings.

- GET  /setup            no admin: step 1 form. Admin but not logged in: /login. Else the checklist.
- POST /setup/account    step 1a: setup code, user name, password (hashed with argon2)
- GET  /setup/totp       step 1b: authenticator QR (a new secret on every load until confirmed)
- POST /setup/confirm    step 1c: first code creates the admin and logs in
- POST /setup/claude     step 2: run the Claude check
- POST /setup/keywords   step 5 | POST /setup/pay  step 6 | POST /setup/schedule  step 9
- POST /setup/style      step 7: keep the default writing style
- POST /setup/skip/{key} skip an optional step (email)
- POST /setup/dryrun     step 10: dry-run ON (never turns it off)
- POST /setup/finish     all required steps done
- GET  /setup/admin[...] old URLs: redirect
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import auth, crypto
from app.auth_routes import render
from app.gmail import Gmail
from app.memory import memory_file, read_file
from app.tools import claude_check
from app.upwork import accounts

log = logging.getLogger("setup")

router = APIRouter()

# key, title, what to do. "link" steps have their own page; "inline" steps have a form here.
STEPS: tuple[dict[str, str], ...] = (
    {"key": "admin", "title": "Admin account", "kind": "admin"},
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
    done.add("admin")  # a logged-in user means the admin exists
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


def mark_done(request: Request, key: str) -> None:
    settings = request.app.state.settings
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
        dry_run=bool(st.get("general.dry_run")),
        **ctx,
    )


def _admin_page(request: Request, status: int = 200, error: str = "") -> Response:
    return render(request, "setup_admin.html", status, error=error)


# ---------- entry ----------


@router.get("/setup")
def setup_home(request: Request) -> Response:
    if not auth.admin_exists(request.app.state.db):
        return _admin_page(request)
    if auth.current_user(request) is None:
        return _redirect("/login")
    return _hub(request)


@router.get("/setup/admin")
def old_setup_admin() -> Response:
    return _redirect("/setup")


@router.get("/setup/admin/totp")
def old_setup_totp() -> Response:
    return _redirect("/setup/totp")


# ---------- step 1: admin ----------


def _totp_page(request: Request, pending: dict, error: str = "", status: int = 200) -> Response:
    secret = crypto.decrypt(request.app.state.config.secret_key, pending["s"])
    return render(
        request,
        "setup_totp.html",
        status,
        otpauth_url=(url := auth.provisioning_uri(secret, pending["u"])),
        qr_svg=auth.qr_svg(url),
        totp_secret=secret,
        error=error,
    )


@router.post("/setup/account")
def setup_account(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    setup_code: str = Form(""),
):
    conn: sqlite3.Connection = request.app.state.db
    _closed(request)
    # The panel may be on a public domain before the admin exists: check the code first.
    left = auth.lock_seconds_remaining(conn)
    if left:
        minutes = max(1, -(-left // 60))
        msg = f"Too many wrong setup codes. Try again in about {minutes} minute(s)."
        return _admin_page(request, 429, msg)
    if not auth.check_setup_code(conn, request.app.state.config.secret_key, setup_code):
        auth.record_failure(conn, request.app.state.settings)
        msg = "The setup code is missing or wrong. Find it in the container log (SETUP CODE)."
        return _admin_page(request, 403, msg)
    username = username.strip()
    min_len = request.app.state.settings.get("security.password_min_length")
    error = ""
    if not username or len(username) > 64:
        error = "Enter a user name (up to 64 characters)."
    elif len(password) < min_len:
        error = f"The password needs at least {min_len} characters."
    elif password != password2:
        error = "The two passwords are not the same."
    if error:
        return _admin_page(request, 400, error)
    key = request.app.state.config.secret_key
    pending = {
        "u": username,
        "h": auth.hash_password(password),
        "s": crypto.encrypt(key, auth.new_totp_secret()),
    }
    auth.load_session(request).set("pending", pending)
    return _redirect("/setup/totp")


@router.get("/setup/totp")
def setup_totp(request: Request):
    """Every load makes a NEW secret until the code is confirmed, so a reloaded page never
    shows a key that was seen (and maybe leaked) before."""
    _closed(request)
    session = auth.load_session(request)
    pending = session.data.get("pending")
    if not pending:
        return _redirect("/setup")
    key = request.app.state.config.secret_key
    pending = {**pending, "s": crypto.encrypt(key, auth.new_totp_secret())}
    session.set("pending", pending)
    return _totp_page(request, pending)


@router.post("/setup/confirm")
def setup_confirm(request: Request, code: str = Form("")):
    conn: sqlite3.Connection = request.app.state.db
    _closed(request)
    session = auth.load_session(request)
    pending = session.data.get("pending")
    if not pending:
        return _redirect("/setup")
    key = request.app.state.config.secret_key
    if not auth.verify_totp(crypto.decrypt(key, pending["s"]), code):
        return _totp_page(request, pending, "That code is wrong. Try the next one.", 400)
    try:
        auth.create_admin(conn, pending["u"], pending["h"], pending["s"])
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=404) from None
    auth.finish_setup(conn)
    session.login(pending["u"])
    return _redirect("/setup")


# ---------- step 2: Claude ----------


@router.post("/setup/claude")
def setup_claude(request: Request) -> Response:
    config = request.app.state.config
    env = dict(os.environ)
    if config.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.claude_code_oauth_token
    if config.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    ok, message = claude_check.check(env)  # the message never holds the token
    if ok:
        mark_done(request, "claude")
        return _hub(request, notice="Claude works.")
    return _hub(request, 400, error=message)


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
