"""Email page and Gmail connect routes (plan M1).

- GET  /settings/email             login: setup steps, client form, connect state
- POST /settings/email             login + CSRF: save client id, secret (never shown), recipient
- POST /settings/email/connect     login + CSRF: redirect to Google
- GET  /gmail/callback             PUBLIC: Google sends the browser back here. No session cookie
                                   arrives (SameSite=Strict), so the page never touches the session.
- POST /settings/email/test        login + CSRF: send a test email
- POST /settings/email/disconnect  login + CSRF: revoke at Google, delete the token
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app import mail
from app.auth_routes import render
from app.gmail import Gmail, GmailError
from app.worker import WorkerError, known_action

log = logging.getLogger("gmail.routes")

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")
MAX_FIELD = 300  # client ids and secrets are shorter than this

SETUP_STEPS = (
    "Open console.cloud.google.com and create a project (or pick one).",
    "APIs and services, Library: enable the Gmail API.",
    "OAuth consent screen: user type External. Add the scope gmail.send. "
    "Then press Publish app so the status is In production. "
    "Left in Testing, Google ends the login after 7 days.",
    "Credentials, Create credentials, OAuth client ID, application type Web application. "
    "Under Authorized redirect URIs add the address shown above.",
    "Copy the Client ID and the Client secret into the form below and press Save.",
    'Press Connect Gmail. Google shows "Google hasn\'t verified this app": this is expected '
    "for your own app. Press Advanced, then Go to the app (unsafe), and allow it.",
)


def gmail_for(request: Request) -> Gmail:
    s = request.app.state
    return Gmail(s.config, s.settings, s.db)


def _page(request: Request, status_code: int = 200, **ctx) -> Response:
    g = gmail_for(request)
    s = request.app.state
    base = s.config.public_base_url
    return render(
        request,
        "email_settings.html",
        status_code,
        redirect_uri=f"{base}/gmail/callback" if base else "",
        client_id=g.client_id,
        secret_saved=bool(s.settings.get("email.gmail_client_secret_enc")),
        connected=g.connected(),
        address=g.address(),
        recipient_saved=str(s.settings.get("email.gmail_recipient")).strip(),
        recipient=g.recipient(),
        expired=bool(s.db.execute("SELECT 1 FROM alerts WHERE key = 'gmail_expired'").fetchone()),
        steps=SETUP_STEPS,
        notice=ctx.pop("notice", ""),
        error=ctx.pop("error", ""),
        action_id=ctx.pop("action_id", ""),
        **ctx,
    )


@router.get("/settings/email")
def email_page(request: Request, action: str = ""):
    return _page(request, action_id=known_action(request.app.state.worker, action))


@router.post("/settings/email")
def save_email(
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
    recipient: str = Form(""),
):
    g = gmail_for(request)
    s = request.app.state
    client_id, client_secret, recipient = (
        client_id.strip(),
        client_secret.strip(),
        recipient.strip(),
    )
    error = ""
    if len(client_id) > MAX_FIELD or len(client_secret) > MAX_FIELD:
        error = "The client ID or secret is too long."
    elif not client_id:
        error = "Enter the Google client ID."
    elif not client_secret and not s.settings.get("email.gmail_client_secret_enc"):
        error = "Enter the Google client secret."
    elif recipient and not EMAIL_RE.match(recipient):
        error = "The recipient is not a valid email address."
    if error:
        return _page(request, 400, error=error)
    notice = "Saved."
    if g.connected() and client_id != g.client_id:
        g.store.delete()  # the login belongs to the old client and would not refresh
        notice = "Saved. Gmail was disconnected because the client ID changed: connect again."
    g.save_client(client_id, client_secret)
    s.settings.set("email.gmail_recipient", recipient)
    return _page(request, notice=notice)


@router.post("/settings/email/connect")
def connect(request: Request):
    try:
        url = gmail_for(request).start()
    except GmailError as e:
        return _page(request, 400, error=str(e))
    return RedirectResponse(url, status_code=303)


@router.get("/gmail/callback")
def callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
):
    """Public. Shows only fixed texts or GmailError messages (safe by design)."""
    ctx = {"ok": False, "message": "", "address": "", "user": None, "csrf_token": ""}
    g = gmail_for(request)
    try:
        if error:
            # Use up the state so the link cannot be tried again, then report the refusal.
            g.consume_state(state)
            raise GmailError(
                "Google did not allow the connection. Press Connect Gmail and try again."
            )
        ctx["address"] = g.finish(state, code)
        ctx["ok"] = True
    except GmailError as e:
        ctx["message"] = str(e)
    except Exception:
        log.exception("Gmail callback failed")
        ctx["message"] = "Could not finish the connection. See the log for details."
    response = request.app.state.templates.TemplateResponse(
        request, "gmail_result.html", ctx, status_code=200 if ctx["ok"] else 400
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/settings/email/test")
def send_test(request: Request):
    g = gmail_for(request)
    s = request.app.state
    if not g.connected():
        return _page(request, 400, error="Gmail is not connected.")
    to = g.recipient()
    text, html = mail.build_email(
        "Test email",
        ["This is a test from Upwork Assistant.", "If you can read this, email works."],
        base_url=s.config.public_base_url,
    )
    config = s.config

    def work(ctx: Any) -> tuple[str, str]:
        try:
            Gmail(config, ctx.settings, ctx.conn).send(
                to, "Test email from Upwork Assistant", text, html
            )
        except GmailError as e:
            raise WorkerError(f"Test email failed: {e}") from e
        return f"/settings/email?action={ctx.action_id}", f"Test email sent to {to}."

    action_id = s.worker.submit("email_test", "gmail", "Sending a test email", work, "email_test")
    return RedirectResponse(f"/settings/email?action={action_id}", status_code=303)


@router.post("/settings/email/disconnect")
def disconnect(request: Request):
    revoked = gmail_for(request).disconnect()
    note = (
        "Gmail disconnected."
        if revoked
        else (
            "Gmail disconnected here. Google could not be reached to revoke it: "
            "remove the app at myaccount.google.com/permissions."
        )
    )
    return _page(request, notice=note)
