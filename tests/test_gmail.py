"""Gmail connect, send, refresh, disconnect and the Email page (plan M1). respx only: no Google."""

import base64
import email
import json
import re
import stat
from datetime import timedelta
from email import policy
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app import crypto, gmail
from app.gmail import Gmail, GmailAuthExpired, GmailError
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
BASE = "https://test.example"
CLIENT_ID = "cid-123.apps.googleusercontent.com"
SECRET = "GOCSPX-super-secret-value"
SCOPES = "openid https://www.googleapis.com/auth/userinfo.email " + gmail.SEND_SCOPE


def id_token(email_addr="me@gmail.example"):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64({'email': email_addr, 'sub': '1'})}.sig"


def token_body(**over):
    body = {
        "access_token": "AT-1",
        "refresh_token": "RT-1",
        "expires_in": 3600,
        "scope": SCOPES,
        "id_token": id_token(),
    }
    body.update(over)
    return body


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


@pytest.fixture
def g(web, config):
    app, _ = web
    obj = Gmail(config, app.state.settings, app.state.db)
    obj.save_client(CLIENT_ID, SECRET)
    return obj


def connect(g):
    """Run start + finish against a mocked token endpoint. Returns the request sent."""
    url = g.start()
    state = parse_qs(urlparse(url).query)["state"][0]
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
        g.finish(state, "the-code")
    return route.calls.last.request


def post_form(client, path, html_source="/settings/email", **data):
    token = csrf_from(client.get(html_source).text)
    return client.post(path, data={"csrf_token": token, **data})


# ---------- page ----------


def test_page_not_connected_shows_redirect_uri_and_steps(web):
    _, client = web
    r = client.get("/settings/email")
    assert r.status_code == 200
    assert f'<code id="redirect-uri">{BASE}/gmail/callback</code>' in r.text
    assert 'id="copy-redirect"' in r.text
    for word in ("Gmail API", "In production", "Web application", "Advanced"):
        assert word in r.text
    assert 'href="/settings/email"' in r.text and "Email</a>" in r.text  # nav item
    assert "Connect Gmail" in r.text and "Send test email" not in r.text


def test_secret_saved_encrypted_and_never_shown(web, config):
    app, client = web
    r = post_form(
        client, "/settings/email", client_id=CLIENT_ID, client_secret=SECRET, recipient=""
    )
    assert r.status_code == 200 and "Saved." in r.text
    assert SECRET not in r.text and CLIENT_ID in r.text and "saved" in r.text
    stored = app.state.settings.get("email.gmail_client_secret_enc")
    assert stored and SECRET not in stored
    assert crypto.decrypt(config.secret_key, stored) == SECRET
    assert SECRET not in client.get("/settings/email").text
    # empty secret field keeps the saved one
    post_form(client, "/settings/email", client_id=CLIENT_ID, client_secret="", recipient="")
    assert (
        crypto.decrypt(config.secret_key, app.state.settings.get("email.gmail_client_secret_enc"))
        == SECRET
    )


def test_save_validates(web):
    app, client = web
    r = post_form(client, "/settings/email", client_id="", client_secret="x", recipient="")
    assert r.status_code == 400
    r = post_form(client, "/settings/email", client_id="a", client_secret="", recipient="")
    assert r.status_code == 400 and "secret" in r.text
    r = post_form(
        client, "/settings/email", client_id="a", client_secret="b", recipient="not-an-address"
    )
    assert r.status_code == 400 and "valid email" in r.text
    r = post_form(
        client, "/settings/email", client_id="a", client_secret="b", recipient="x@y.z\nBcc: e@f.g"
    )
    assert r.status_code == 400
    assert app.state.settings.get("email.gmail_client_id") == ""


def test_all_posts_need_csrf(web, g):
    _, client = web
    for path in (
        "/settings/email",
        "/settings/email/connect",
        "/settings/email/test",
        "/settings/email/disconnect",
    ):
        assert client.post(path, data={}).status_code == 403
        assert client.post(path, data={"csrf_token": "wrong"}).status_code == 403


def test_pages_need_login(config):
    _, client = make_web_client(config)
    setup_admin(client)
    client.cookies.clear()
    r = client.get("/settings/email")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.post("/settings/email/connect", data={})
    assert r.status_code == 403


# ---------- connect ----------


def test_connect_redirect_params(web, g):
    app, client = web
    r = post_form(client, "/settings/email/connect")
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == AUTH_URL
    p = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert p["response_type"] == "code" and p["client_id"] == CLIENT_ID
    assert p["redirect_uri"] == f"{BASE}/gmail/callback"
    assert gmail.SEND_SCOPE in p["scope"].split() and "openid" in p["scope"].split()
    assert p["access_type"] == "offline" and p["prompt"] == "consent"
    assert p["include_granted_scopes"] == "false"
    assert p["code_challenge_method"] == "S256"
    row = app.state.db.execute(
        "SELECT verifier FROM oauth_state WHERE state = ?", (p["state"],)
    ).fetchone()
    assert p["code_challenge"] == gmail.challenge_for(row["verifier"])
    assert SECRET not in r.headers["location"]


def test_connect_without_client_is_an_error(web):
    _, client = web
    r = post_form(client, "/settings/email/connect")
    assert r.status_code == 400 and "client ID" in r.text


# ---------- callback ----------


def test_callback_is_public_and_rejects_bad_state(web):
    _, client = web
    client.cookies.clear()  # Google's redirect carries no session cookie
    for q in ("", "?state=nope&code=c", "?state=gm-nope&code=c"):
        r = client.get("/gmail/callback" + q)
        assert r.status_code == 400 and "Start again" in r.text
        assert "set-cookie" not in r.headers


def test_callback_other_flow_state_is_refused(web, g):
    app, client = web
    app.state.db.execute(
        "INSERT INTO oauth_state(state, verifier, created_at) VALUES ('plain', 'v', ?)",
        (gmail._iso(gmail.utcnow()),),
    )
    r = client.get("/gmail/callback?state=plain&code=c")
    assert r.status_code == 400
    assert app.state.db.execute("SELECT 1 FROM oauth_state WHERE state='plain'").fetchone()


def test_callback_expired_state(web, g, monkeypatch):
    app, client = web
    state = parse_qs(urlparse(g.start()).query)["state"][0]
    later = gmail.utcnow() + timedelta(minutes=11)
    monkeypatch.setattr(gmail, "utcnow", lambda: later)
    r = client.get(f"/gmail/callback?state={state}&code=c")
    assert r.status_code == 400
    assert not app.state.config.gmail_path.exists()


def test_callback_exchange_and_state_is_single_use(web, g, config):
    app, client = web
    state = parse_qs(urlparse(g.start()).query)["state"][0]
    client.cookies.clear()
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
        r = client.get(f"/gmail/callback?state={state}&code=the-code")
        assert r.status_code == 200 and "me@gmail.example" in r.text
        sent = parse_qs(route.calls.last.request.content.decode())
        again = client.get(f"/gmail/callback?state={state}&code=the-code")
        assert again.status_code == 400 and route.call_count == 1  # reused: no second exchange
    f = {k: v[0] for k, v in sent.items()}
    assert f["grant_type"] == "authorization_code" and f["code"] == "the-code"
    assert f["client_id"] == CLIENT_ID and f["client_secret"] == SECRET
    assert f["redirect_uri"] == f"{BASE}/gmail/callback" and len(f["code_verifier"]) >= 43
    # token file: 0600, encrypted, atomic (no temp files left)
    path = config.gmail_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert "RT-1" not in raw and "AT-1" not in raw
    assert json.loads(crypto.decrypt(config.secret_key, raw))["refresh_token"] == "RT-1"
    assert [p.name for p in path.parent.glob(".gmail-*")] == []
    assert app.state.settings.get("email.gmail_address") == "me@gmail.example"
    page = client.get("/settings/email")  # session was cleared: log in state gone
    assert page.status_code == 303


def test_callback_google_error_uses_up_state(web, g):
    app, client = web
    state = parse_qs(urlparse(g.start()).query)["state"][0]
    r = client.get(f"/gmail/callback?error=access_denied&state={state}")
    assert r.status_code == 400 and "did not allow" in r.text
    assert app.state.db.execute("SELECT COUNT(*) FROM oauth_state").fetchone()[0] == 0


def test_missing_send_scope_or_refresh_token(g, config):
    for body, word in (
        (token_body(scope="openid email"), "permission to send"),
        (token_body(refresh_token=None), "no refresh token"),
    ):
        url = g.start()
        state = parse_qs(urlparse(url).query)["state"][0]
        with respx.mock:
            respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
            with pytest.raises(GmailError, match=word):
                g.finish(state, "c")
        assert not config.gmail_path.exists()


def test_token_error_text_is_safe(g):
    state = parse_qs(urlparse(g.start()).query)["state"][0]
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_client", "error_description": "bad client"}
            )
        )
        with pytest.raises(GmailError) as e:
            g.finish(state, "the-code")
    assert "invalid_client" in str(e.value) and SECRET not in str(e.value)


def test_id_token_email_parse():
    assert gmail.email_from_id_token(id_token("a@b.example")) == "a@b.example"
    for bad in ("", "x", "a.b.c", "a." + base64.urlsafe_b64encode(b'{"email":5}').decode() + ".c"):
        assert gmail.email_from_id_token(bad) == ""


# ---------- send ----------


def test_send_builds_valid_mime(g):
    connect(g)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
        g.send("to@x.example", "Hello é", "plain body", "<p>html body</p>")
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer AT-1"
    raw = json.loads(req.content)["raw"]
    assert "=" not in raw.rstrip("=") and "+" not in raw and "/" not in raw  # base64url
    msg = email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
    )
    assert msg["To"] == "to@x.example" and msg["From"] == "me@gmail.example"
    assert msg["Subject"] == "Hello é"
    assert msg.get_content_type() == "multipart/alternative"
    assert msg.get_body(("plain",)).get_content().strip() == "plain body"
    assert "<p>html body</p>" in msg.get_body(("html",)).get_content()


def test_send_refuses_header_injection(g):
    connect(g)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(GmailError):
            g.send("to@x.example\nBcc: e@f.g", "s", "t", None)
        assert route.call_count == 0


def test_refresh_when_access_token_ended_then_cached(g, monkeypatch):
    connect(g)
    later = gmail.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(gmail, "utcnow", lambda: later)
    with respx.mock:
        tok = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "AT-2", "expires_in": 3600})
        )
        send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        g.send("to@x.example", "s", "t")
        g.send("to@x.example", "s", "t")
    f = {k: v[0] for k, v in parse_qs(tok.calls.last.request.content.decode()).items()}
    assert f["grant_type"] == "refresh_token" and f["refresh_token"] == "RT-1"
    assert f["client_id"] == CLIENT_ID and f["client_secret"] == SECRET
    assert tok.call_count == 1 and send.call_count == 2  # second send used the cached token
    assert send.calls.last.request.headers["authorization"] == "Bearer AT-2"
    assert g.store.load().refresh_token == "RT-1"  # kept when Google sends no new one


def test_401_on_send_refreshes_once_and_retries(g):
    connect(g)
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "AT-9", "expires_in": 3600})
        )
        send = respx.post(SEND_URL).mock(
            side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={})]
        )
        g.send("to@x.example", "s", "t")
    assert send.call_count == 2
    assert send.calls.last.request.headers["authorization"] == "Bearer AT-9"


def test_send_error_is_safe(g):
    connect(g)
    with respx.mock:
        respx.post(SEND_URL).mock(
            return_value=httpx.Response(403, json={"error": {"message": "Insufficient scope"}})
        )
        with pytest.raises(GmailError, match="Insufficient scope") as e:
            g.send("to@x.example", "s", "t")
    assert "AT-1" not in str(e.value)


def test_invalid_grant_marks_disconnected_and_alerts(web, g, config, monkeypatch):
    app, client = web
    connect(g)
    later = gmail.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(gmail, "utcnow", lambda: later)
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(GmailAuthExpired):
            g.send("to@x.example", "s", "t")
        assert send.call_count == 0
    assert not config.gmail_path.exists() and not g.connected()
    assert app.state.db.execute("SELECT message FROM alerts WHERE key='gmail_expired'").fetchone()
    assert "Gmail login expired" in client.get("/").text
    assert "Connect Gmail" in client.get("/settings/email").text
    # connecting again clears the alert
    connect(g)
    assert not app.state.db.execute("SELECT 1 FROM alerts WHERE key='gmail_expired'").fetchone()


# ---------- test email button and disconnect ----------


def test_test_email_button(web, g):
    _, client = web
    connect(g)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        r = post_form(client, "/settings/email/test")
    assert r.status_code == 200 and "Test email sent to me@gmail.example" in r.text
    raw = json.loads(route.calls.last.request.content)["raw"]
    msg = email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
    )
    assert msg["To"] == "me@gmail.example"
    html = msg.get_body(("html",)).get_content()
    assert "Open in panel" in html and f"{BASE}" in html


def test_test_email_failure_and_not_connected(web, g):
    _, client = web
    r = post_form(client, "/settings/email/test")
    assert r.status_code == 400 and "not connected" in r.text
    connect(g)
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=httpx.Response(500, json={}))
        r = post_form(client, "/settings/email/test")
    assert r.status_code == 400 and "Test email failed" in r.text


def test_recipient_setting_wins(web, g):
    _, client = web
    connect(g)
    post_form(
        client,
        "/settings/email",
        client_id=CLIENT_ID,
        client_secret="",
        recipient="other@x.example",
    )
    assert g.recipient() == "other@x.example"
    page = client.get("/settings/email").text
    assert "other@x.example" in page and SECRET not in page


def test_connected_page_shows_address(web, g):
    _, client = web
    connect(g)
    page = client.get("/settings/email").text
    assert "me@gmail.example" in page and "Send test email" in page and "Disconnect" in page
    assert "AT-1" not in page and "RT-1" not in page and SECRET not in page


def test_disconnect_revokes_and_deletes(web, g, config):
    _, client = web
    connect(g)
    with respx.mock:
        route = respx.post(REVOKE_URL).mock(return_value=httpx.Response(200))
        r = post_form(client, "/settings/email/disconnect")
    assert r.status_code == 200 and "Gmail disconnected." in r.text
    assert parse_qs(route.calls.last.request.content.decode())["token"] == ["RT-1"]
    assert not config.gmail_path.exists()


def test_disconnect_deletes_even_when_revoke_fails(web, g, config):
    _, client = web
    connect(g)
    with respx.mock:
        respx.post(REVOKE_URL).mock(side_effect=httpx.ConnectError("down"))
        r = post_form(client, "/settings/email/disconnect")
    assert r.status_code == 200 and "could not be reached" in r.text
    assert not config.gmail_path.exists()


def test_changing_client_id_drops_the_login(web, g, config):
    _, client = web
    connect(g)
    r = post_form(
        client, "/settings/email", client_id="other-client", client_secret="", recipient=""
    )
    assert "disconnected" in r.text and not config.gmail_path.exists()


def test_unreadable_token_file_counts_as_not_connected(g, config):
    config.gmail_path.write_text("garbage")
    assert not g.connected()
    assert re.search("not connected", str(pytest.raises(GmailError, g.access_token).value), re.I)
