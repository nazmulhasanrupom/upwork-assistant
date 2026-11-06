"""Upwork connection routes (plan D2).

- GET  /oauth/client-metadata.json  public: Upwork fetches it (SPEC 13.1)
- POST /oauth/start                 login + CSRF: sends the browser to Upwork
- GET  /oauth/callback              public: Upwork sends the browser back (no cookie arrives
                                    because of SameSite=Strict, so `state` in the DB is the check)
- GET  /upwork                      login: status and token expiry
- POST /upwork/disconnect           login + CSRF: revoke at Upwork, delete the token file
- POST /upwork/detect               login + CSRF: list_accounts, save the TALENT account (F0a)
- POST /upwork/account              login + CSRF: pick one of several detected TALENT accounts
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.auth_routes import render
from app.upwork import accounts
from app.upwork.client import UpworkAuthError, UpworkMCP, UpworkMCPError
from app.upwork.oauth import OAuthError, UpworkOAuth, utcnow

log = logging.getLogger("upwork.routes")

router = APIRouter()

MAX_SHOWN = 200  # cut text that comes from the query string before showing it


def oauth_for(request: Request) -> UpworkOAuth:
    s = request.app.state
    return UpworkOAuth(s.config, s.settings, s.db)


def detect_account(request: Request) -> tuple[str, str]:
    """Run list_accounts and save the result. Returns (notice, error); never raises.

    A test can set `app.state.mcp_factory` (a function returning an object with call_json/close).
    """
    s = request.app.state
    factory = getattr(s, "mcp_factory", None)
    client = None
    try:
        if factory:
            client = factory()
        else:
            client = UpworkMCP(s.settings, oauth_for(request), s.db)
        res = accounts.detect_and_save(client, s.settings)
    except UpworkAuthError:
        return "", "Upwork login expired. Connect Upwork again."
    except (UpworkMCPError, OAuthError, accounts.AccountError) as e:
        return "", f"Could not detect the freelancer account: {str(e)[:MAX_SHOWN]}"
    except Exception:
        log.exception("Account detection failed")
        return "", "Could not detect the freelancer account. See the log for details."
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                log.exception("Could not close the MCP client")
    if res.status == "none":
        return "", res.message
    return res.message, ""


def client_metadata(oauth: UpworkOAuth) -> dict:
    doc = {
        "client_id": oauth.client_id,
        "client_name": "Upwork Assistant (self-hosted)",
        "redirect_uris": [oauth.redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    scope = oauth.settings.get("upwork.oauth_scope").strip()
    if scope:
        doc["scope"] = scope
    return doc


@router.get("/oauth/client-metadata.json")
def metadata(request: Request):
    try:
        doc = client_metadata(oauth_for(request))
    except OAuthError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse(doc, headers={"Cache-Control": "public, max-age=300"})


# ---------- status page ----------


def _status_page(request: Request, status_code: int = 200, **ctx) -> Response:
    oauth = oauth_for(request)
    try:
        client_id, redirect_uri = oauth.client_id, oauth.redirect_uri
        base_ok = True
    except OAuthError:
        client_id = redirect_uri = ""
        base_ok = False
    st = oauth.status()
    minutes_left = None
    if st.state == "connected":
        left = datetime.fromisoformat(st.expires_at) - utcnow()
        minutes_left = max(0, int(left.total_seconds() // 60))
    return render(
        request,
        "upwork.html",
        status_code,
        st=st,
        minutes_left=minutes_left,
        client_id=client_id,
        redirect_uri=redirect_uri,
        base_ok=base_ok,
        account=accounts.current(request.app.state.settings),
        candidates=accounts.candidates(request.app.state.settings),
        **ctx,
    )


@router.get("/upwork")
def upwork_page(request: Request):
    q = request.query_params
    notice = ""
    if q.get("disconnected") == "1":
        notice = "Disconnected. The saved login was deleted."
    elif q.get("disconnected") == "revoke_failed":
        notice = (
            "The saved login was deleted here, but Upwork did not confirm the revoke. "
            "You can remove the app in your Upwork account settings too."
        )
    return _status_page(request, notice=notice, error="")


# ---------- connect ----------


@router.post("/oauth/start")
def oauth_start(request: Request):
    try:
        url = oauth_for(request).start()
    except OAuthError as e:
        return _status_page(request, 400, notice="", error=str(e))
    return RedirectResponse(url, status_code=303)


@router.get("/oauth/callback")
def oauth_callback(request: Request):
    """Public page. It must not touch the session: the browser sent no cookie, and a Set-Cookie
    here would replace the admin's real session cookie."""
    q = request.query_params
    state = q.get("state", "")[:MAX_SHOWN]
    code = q.get("code", "")
    oauth = oauth_for(request)
    error, status_code = "", 200
    connected = False
    try:
        if q.get("error"):
            # The user said no, or Upwork refused. Use up the state so the link cannot be reused.
            try:
                oauth.consume_state(state)
            except OAuthError:
                pass
            detail = q.get("error_description") or q.get("error", "")
            error = f"Upwork did not connect: {detail[:MAX_SHOWN]}"
            status_code = 400
        else:
            oauth.finish(state, code)
            connected = True
    except OAuthError as e:
        error, status_code = str(e), 400
    except Exception:
        log.exception("Upwork callback failed")
        error, status_code = "Something went wrong while saving the Upwork login.", 500
    # Detect the freelancer account here, on the server: this request has no session cookie, so a
    # later redirect could not do it. A failure here never undoes the login.
    account_notice = account_error = ""
    if connected:
        account_notice, account_error = detect_account(request)
    response = request.app.state.templates.TemplateResponse(
        request,
        "oauth_result.html",
        {"error": error, "account_notice": account_notice, "account_error": account_error},
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- freelancer account ----------


@router.post("/upwork/detect")
def detect(request: Request):
    if oauth_for(request).status().state != "connected":
        return _status_page(request, 400, notice="", error="Connect Upwork first.")
    notice, error = detect_account(request)
    return _status_page(request, 400 if error else 200, notice=notice, error=error)


@router.post("/upwork/account")
def pick_account(request: Request, org_uid: str = Form("")):
    try:
        picked = accounts.pick_candidate(request.app.state.settings, org_uid)
    except accounts.AccountError as e:
        return _status_page(request, 400, notice="", error=str(e))
    notice = f"Using freelancer account: {picked.get('name', '')}."
    return _status_page(request, notice=notice, error="")


# ---------- disconnect ----------


@router.post("/upwork/disconnect")
def disconnect(request: Request):
    revoked = oauth_for(request).disconnect()
    target = "/upwork?disconnected=" + ("1" if revoked else "revoke_failed")
    return RedirectResponse(target, status_code=303)
