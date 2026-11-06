"""Pages: /login, /logout. The first-user setup lives in setup_routes.py."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app import auth

router = APIRouter()


def render(request: Request, name: str, status: int = 200, **ctx) -> Response:
    session = auth.load_session(request)
    ctx.update(csrf_token=session.csrf, user=auth.current_user(request))
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- login / logout ----------


@router.get("/login")
def login_form(request: Request):
    if not auth.admin_exists(request.app.state.db):
        return _redirect("/setup")
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
