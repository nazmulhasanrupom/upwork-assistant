"""Pages: /setup/admin (first user only), /login, /logout."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import auth, crypto

router = APIRouter()


def render(request: Request, name: str, status: int = 200, **ctx) -> Response:
    session = auth.load_session(request)
    ctx.update(csrf_token=session.csrf, user=auth.current_user(request))
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- first user ----------


def _totp_page(request: Request, pending: dict, error: str = "", status: int = 200) -> Response:
    secret = crypto.decrypt(request.app.state.config.secret_key, pending["s"])
    return render(
        request,
        "setup_totp.html",
        status,
        otpauth_url=auth.provisioning_uri(secret, pending["u"]),
        totp_secret=secret,
        error=error,
    )


@router.get("/setup/admin")
def setup_form(request: Request):
    if auth.admin_exists(request.app.state.db):
        raise HTTPException(status_code=404)
    return render(request, "setup_admin.html", error="")


@router.post("/setup/admin")
def setup_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    setup_code: str = Form(""),
):
    conn: sqlite3.Connection = request.app.state.db
    if auth.admin_exists(conn):
        raise HTTPException(status_code=404)
    # The panel may be on a public domain before the admin exists: check the code first.
    left = auth.lock_seconds_remaining(conn)
    if left:
        minutes = max(1, -(-left // 60))
        msg = f"Too many wrong setup codes. Try again in about {minutes} minute(s)."
        return render(request, "setup_admin.html", 429, error=msg)
    if not auth.check_setup_code(conn, request.app.state.config.secret_key, setup_code):
        auth.record_failure(conn, request.app.state.settings)
        msg = "The setup code is missing or wrong. Find it in the container log (SETUP CODE)."
        return render(request, "setup_admin.html", 403, error=msg)
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
        return render(request, "setup_admin.html", 400, error=error)
    key = request.app.state.config.secret_key
    pending = {
        "u": username,
        "h": auth.hash_password(password),
        "s": crypto.encrypt(key, auth.new_totp_secret()),
    }
    auth.load_session(request).set("pending", pending)
    return _totp_page(request, pending)


@router.post("/setup/admin/confirm")
def setup_confirm(request: Request, code: str = Form("")):
    conn: sqlite3.Connection = request.app.state.db
    if auth.admin_exists(conn):
        raise HTTPException(status_code=404)
    session = auth.load_session(request)
    pending = session.data.get("pending")
    if not pending:
        return _redirect("/setup/admin")
    key = request.app.state.config.secret_key
    if not auth.verify_totp(crypto.decrypt(key, pending["s"]), code):
        return _totp_page(request, pending, "That code is wrong. Try the next one.", 400)
    try:
        auth.create_admin(conn, pending["u"], pending["h"], pending["s"])
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=404) from None
    auth.finish_setup(conn)
    session.login(pending["u"])
    return _redirect("/")


# ---------- login / logout ----------


@router.get("/login")
def login_form(request: Request):
    if not auth.admin_exists(request.app.state.db):
        return _redirect("/setup/admin")
    if auth.current_user(request):
        return _redirect("/")
    return render(request, "login.html", error="")


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    code: str = Form(""),
):
    app = request.app
    result = auth.attempt_login(
        app.state.db, app.state.settings, app.state.config.secret_key, username, password, code
    )
    if result.status == "ok":
        auth.load_session(request).login(username.strip())
        return _redirect("/")
    if result.status == "locked":
        minutes = max(1, -(-result.locked_seconds // 60))
        msg = f"Too many wrong tries. Try again in about {minutes} minute(s)."
        return render(request, "login.html", 429, error=msg)
    return render(request, "login.html", 401, error="Wrong user name, password or code.")


@router.post("/logout")
def logout(request: Request):
    auth.load_session(request).logout()
    return _redirect("/login")
