"""Upwork OAuth (plan D1, D2). All Upwork calls are mocked with respx."""

import base64
import hashlib
import os
import stat
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.db import init_db
from app.settings import Settings
from app.upwork import oauth
from app.upwork.oauth import OAuthError, TokenFileError, Tokens, TokenStore, UpworkOAuth
from tests.test_auth import csrf_from, login, make_client, setup_admin

TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"
REVOKE_URL = "https://www.upwork.com/api/v3/oauth2/token/revoke"
AUTH_URL = "https://www.upwork.com/ab/account-security/oauth2/authorize"
BASE = "https://test.example"


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def client(config, conn):
    return UpworkOAuth(config, Settings(conn), conn)


def start_params(client) -> dict[str, str]:
    url = client.start()
    assert url.startswith(AUTH_URL + "?")
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def token_body(**over):
    body = {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600, "scope": "s"}
    body.update(over)
    return body


# ---------- authorize URL and PKCE ----------


def test_authorize_url(client, conn):
    p = start_params(client)
    assert p["response_type"] == "code"
    assert p["client_id"] == f"{BASE}/oauth/client-metadata.json"
    assert p["redirect_uri"] == f"{BASE}/oauth/callback"
    assert p["code_challenge_method"] == "S256"
    assert p["resource"] == "https://mcp.upwork.com/mcp"
    assert "scope" not in p  # default scope is empty: not sent
    row = conn.execute("SELECT verifier FROM oauth_state WHERE state = ?", (p["state"],)).fetchone()
    digest = hashlib.sha256(row["verifier"].encode()).digest()
    assert p["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(row["verifier"]) <= 128


def test_scope_and_resource_follow_settings(config, conn):
    s = Settings(conn)
    s.set("upwork.oauth_scope", "read write")
    s.set("upwork.oauth_send_resource", False)
    p = start_params(UpworkOAuth(config, s, conn))
    assert p["scope"] == "read write"
    assert "resource" not in p


def test_state_is_random_and_not_a_cookie_matter(client):
    assert start_params(client)["state"] != start_params(client)["state"]


def test_start_needs_public_base_url(config, conn):
    from dataclasses import replace

    c = UpworkOAuth(replace(config, public_base_url=""), Settings(conn), conn)
    with pytest.raises(OAuthError, match="PUBLIC_BASE_URL"):
        c.start()


# ---------- state check ----------


def test_state_is_single_use(client):
    state = start_params(client)["state"]
    client.consume_state(state)
    with pytest.raises(OAuthError):
        client.consume_state(state)


def test_unknown_state_refused(client):
    with pytest.raises(OAuthError):
        client.consume_state("nope")


def test_state_expires_after_10_minutes(client, monkeypatch):
    state = start_params(client)["state"]
    real = oauth.utcnow
    monkeypatch.setattr(oauth, "utcnow", lambda: real() + timedelta(minutes=11))
    with pytest.raises(OAuthError, match="expired"):
        client.consume_state(state)


def test_old_states_are_purged_on_start(client, conn, monkeypatch):
    start_params(client)
    real = oauth.utcnow
    monkeypatch.setattr(oauth, "utcnow", lambda: real() + timedelta(minutes=30))
    start_params(client)
    assert conn.execute("SELECT COUNT(*) FROM oauth_state").fetchone()[0] == 1


# ---------- code exchange ----------


@respx.mock
def test_code_exchange_saves_encrypted_tokens(client, config):
    p = start_params(client)
    verifier = client.conn.execute("SELECT verifier FROM oauth_state").fetchone()[0]
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
    tokens = client.finish(p["state"], "the-code")
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["the-code"]
    assert sent["code_verifier"] == [verifier]
    assert sent["client_id"] == [f"{BASE}/oauth/client-metadata.json"]
    assert sent["redirect_uri"] == [f"{BASE}/oauth/callback"]
    assert sent["resource"] == ["https://mcp.upwork.com/mcp"]
    assert "client_secret" not in sent
    assert tokens.access_token == "AT-1"
    raw = config.tokens_path.read_text()
    assert "AT-1" not in raw and "RT-1" not in raw
    loaded = TokenStore(config.tokens_path, config.secret_key).load()
    assert loaded == tokens and loaded.refresh_token == "RT-1" and loaded.scope == "s"


@respx.mock
def test_exchange_with_bad_state_never_calls_upwork(client):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
    with pytest.raises(OAuthError):
        client.finish("bad-state", "code")
    assert not route.called


@respx.mock
def test_exchange_error_is_clear_and_leaks_nothing(client, config):
    p = start_params(client)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "code used", "x": "SECRET"}
        )
    )
    with pytest.raises(OAuthError) as e:
        client.finish(p["state"], "code")
    assert "invalid_grant" in str(e.value) and "SECRET" not in str(e.value)
    assert not config.tokens_path.exists()


@respx.mock
def test_exchange_non_json_and_network_errors(client):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(OAuthError, match="HTTP 502"):
        client.finish(start_params(client)["state"], "code")
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(OAuthError, match="Could not reach"):
        client.finish(start_params(client)["state"], "code")


@respx.mock
def test_missing_expires_in_uses_default(client):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "A", "refresh_token": "R"})
    )
    t = client.finish(start_params(client)["state"], "c")
    left = (t.expires - oauth.utcnow()).total_seconds()
    assert 3500 < left <= 3600


# ---------- token file ----------


def test_token_file_mode_atomic_and_no_leftovers(config):
    store = TokenStore(config.tokens_path, config.secret_key)
    t = Tokens("A", "R", "2030-01-01T00:00:00+00:00", "s")
    store.save(t)
    assert stat.S_IMODE(os.stat(config.tokens_path).st_mode) == 0o600
    store.save(Tokens("A2", "R", "2030-01-01T00:00:00+00:00", "s"))
    assert store.load().access_token == "A2"
    assert [p.name for p in config.data_dir.glob(".tokens-*")] == []


def test_failed_write_keeps_old_file(config, monkeypatch):
    store = TokenStore(config.tokens_path, config.secret_key)
    store.save(Tokens("OLD", "R", "2030-01-01T00:00:00+00:00", ""))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(Tokens("NEW", "R", "2030-01-01T00:00:00+00:00", ""))
    monkeypatch.undo()
    assert store.load().access_token == "OLD"
    assert [p.name for p in config.data_dir.glob(".tokens-*")] == []


def test_wrong_key_or_damage_is_reported_not_crashed(config):
    store = TokenStore(config.tokens_path, config.secret_key)
    store.save(Tokens("A", "R", "2030-01-01T00:00:00+00:00", ""))
    with pytest.raises(TokenFileError):
        TokenStore(config.tokens_path, "z" * 40).load()
    config.tokens_path.write_text("garbage")
    with pytest.raises(TokenFileError):
        TokenStore(config.tokens_path, config.secret_key).load()


def test_load_missing_is_none(config):
    assert TokenStore(config.tokens_path, config.secret_key).load() is None


# ---------- refresh ----------


def put_tokens(client, access="OLD-AT", refresh="OLD-RT", expires_in=3600):
    exp = (oauth.utcnow() + timedelta(seconds=expires_in)).isoformat(timespec="seconds")
    client.store.save(Tokens(access, refresh, exp, "s"))


@respx.mock
def test_refresh_saves_new_tokens_and_keeps_refresh_token_if_not_rotated(client):
    put_tokens(client)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "NEW-AT", "expires_in": 100})
    )
    t = client.refresh()
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["OLD-RT"]
    assert sent["client_id"] == [f"{BASE}/oauth/client-metadata.json"]
    assert t.access_token == "NEW-AT" and t.refresh_token == "OLD-RT" and t.scope == "s"
    assert client.store.load().access_token == "NEW-AT"


@respx.mock
def test_refresh_uses_rotated_refresh_token(client):
    put_tokens(client)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_body(access_token="N", refresh_token="N-RT"))
    )
    assert client.refresh().refresh_token == "N-RT"


@respx.mock
def test_refresh_refused_keeps_old_file_and_raises(client):
    put_tokens(client)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(OAuthError, match="connect Upwork again"):
        client.refresh()
    assert client.store.load().access_token == "OLD-AT"


def test_refresh_when_not_connected(client):
    with pytest.raises(OAuthError, match="not connected"):
        client.refresh()


@respx.mock
def test_access_token_valid_means_no_call(client):
    put_tokens(client, expires_in=3600)
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
    assert client.access_token() == "OLD-AT"
    assert not route.called


@respx.mock
def test_access_token_refreshes_when_close_to_expiry(client):
    put_tokens(client, expires_in=30)  # inside the 60 s skew
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_body(access_token="FRESH"))
    )
    assert client.access_token() == "FRESH"


@respx.mock
def test_access_token_after_401_refreshes_only_if_same_token(client):
    put_tokens(client)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_body(access_token="FRESH"))
    )
    assert client.access_token(rejected="OLD-AT") == "FRESH"
    assert route.call_count == 1
    # Someone else already refreshed: the rejected token is not the stored one, so no new call.
    assert client.access_token(rejected="OLD-AT") == "FRESH"
    assert route.call_count == 1


# ---------- revoke / disconnect ----------


@respx.mock
def test_disconnect_revokes_and_deletes(client, config):
    put_tokens(client)
    route = respx.post(REVOKE_URL).mock(return_value=httpx.Response(200))
    assert client.disconnect() is True
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["token"] == ["OLD-RT"] and sent["token_type_hint"] == ["refresh_token"]
    assert not config.tokens_path.exists()


@respx.mock
def test_disconnect_deletes_file_even_if_revoke_fails(client, config):
    put_tokens(client)
    respx.post(REVOKE_URL).mock(return_value=httpx.Response(500))
    assert client.disconnect() is False
    assert not config.tokens_path.exists()
    put_tokens(client)
    respx.post(REVOKE_URL).mock(side_effect=httpx.ConnectError("x"))
    assert client.disconnect() is False
    assert not config.tokens_path.exists()


@respx.mock
def test_revoke_access_token_when_no_refresh_token(client):
    put_tokens(client, refresh="")
    route = respx.post(REVOKE_URL).mock(return_value=httpx.Response(200))
    client.disconnect()
    assert parse_qs(route.calls.last.request.content.decode())["token"] == ["OLD-AT"]


@respx.mock
def test_disconnect_when_not_connected_makes_no_call(client):
    route = respx.post(REVOKE_URL).mock(return_value=httpx.Response(200))
    assert client.disconnect() is True
    assert not route.called


# ---------- status ----------


def test_status(client, config):
    assert client.status().state == "not_connected"
    put_tokens(client)
    st = client.status()
    assert st.state == "connected" and not st.expired and st.has_refresh_token and st.expires_at
    put_tokens(client, expires_in=-10)
    assert client.status().expired
    config.tokens_path.write_text("junk")
    assert client.status().state == "unreadable"


def test_no_tokens_in_logs(client, caplog):
    import logging

    put_tokens(client)
    with caplog.at_level(logging.DEBUG), respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
        client.refresh()
    assert "AT-1" not in caplog.text and "RT-1" not in caplog.text and "OLD-RT" not in caplog.text


# ---------- routes (plan D2) ----------

@pytest.fixture
def app_env(config):
    app, c = make_client(config)
    secret = setup_admin(c)
    c.cookies.clear()
    return app, c, secret


@pytest.fixture
def logged_in(app_env):
    app, c, secret = app_env
    login(c, secret)
    return app, c


def test_metadata_is_public_and_matches_spec(app_env):
    _, c, _ = app_env  # not logged in
    r = c.get("/oauth/client-metadata.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {
        "client_id": f"{BASE}/oauth/client-metadata.json",
        "client_name": "Upwork Assistant (self-hosted)",
        "redirect_uris": [f"{BASE}/oauth/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def test_metadata_works_before_any_admin_exists(config):
    _, c = make_client(config)
    assert c.get("/oauth/client-metadata.json").status_code == 200


def test_metadata_without_base_url_is_503(config):
    from dataclasses import replace

    _, c = make_client(replace(config, public_base_url=""))
    assert c.get("/oauth/client-metadata.json").status_code == 503


def test_metadata_includes_scope_only_when_set(logged_in):
    app, c = logged_in
    app.state.settings.set("upwork.oauth_scope", "a b")
    assert c.get("/oauth/client-metadata.json").json()["scope"] == "a b"


def test_upwork_page_and_start_need_login(app_env):
    _, c, _ = app_env
    r = c.get("/upwork")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.get("/oauth/start")  # a GET never starts anything
    assert r.status_code in (303, 404, 405)
    r = c.post("/oauth/start")  # no CSRF token
    assert r.status_code == 403


def test_start_needs_csrf_even_when_logged_in(logged_in):
    _, c = logged_in
    assert c.post("/oauth/start").status_code == 403
    assert c.post("/oauth/start", data={"csrf_token": "wrong"}).status_code == 403


def test_start_redirects_to_upwork(logged_in):
    app, c = logged_in
    token = csrf_from(c.get("/upwork").text)
    r = c.post("/oauth/start", data={"csrf_token": token})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(AUTH_URL + "?")
    state = parse_qs(urlparse(loc).query)["state"][0]
    assert app.state.db.execute("SELECT 1 FROM oauth_state WHERE state=?", (state,)).fetchone()


@respx.mock
def test_full_login_flow_and_callback_without_cookie(logged_in, config):
    app, c = logged_in
    token = csrf_from(c.get("/upwork").text)
    r = c.post("/oauth/start", data={"csrf_token": token})
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))

    # The browser comes back from Upwork without the Strict session cookie.
    c.cookies.clear()
    r = c.get("/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200
    assert 'href="/upwork"' in r.text and "connected" in r.text.lower()
    assert "set-cookie" not in r.headers  # must not replace the admin's real cookie
    assert r.headers["cache-control"] == "no-store"
    assert config.tokens_path.exists()
    assert "AT-1" not in r.text and "abc" not in r.text

    # The same link cannot be used twice.
    r = c.get("/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400 and "already used" in r.text


@respx.mock
def test_callback_bad_or_missing_state_makes_no_upwork_call(app_env, config):
    _, c, _ = app_env
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body()))
    assert c.get("/oauth/callback", params={"code": "x", "state": "nope"}).status_code == 400
    assert c.get("/oauth/callback", params={"code": "x"}).status_code == 400
    assert c.get("/oauth/callback").status_code == 400
    assert not route.called and not config.tokens_path.exists()


def test_callback_denied_by_user(logged_in, config):
    app, c = logged_in
    state = parse_qs(urlparse(UpworkOAuth(config, app.state.settings, app.state.db).start()).query)[
        "state"
    ][0]
    r = c.get(
        "/oauth/callback",
        params={
            "error": "access_denied",
            "error_description": "<script>x</script>",
            "state": state,
        },
    )
    assert r.status_code == 400
    assert "<script>x</script>" not in r.text  # escaped
    assert not config.tokens_path.exists()
    assert app.state.db.execute("SELECT COUNT(*) FROM oauth_state").fetchone()[0] == 0


@respx.mock
def test_callback_when_upwork_refuses_the_code(logged_in, config):
    app, c = logged_in
    state = parse_qs(urlparse(UpworkOAuth(config, app.state.settings, app.state.db).start()).query)[
        "state"
    ][0]
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    r = c.get("/oauth/callback", params={"code": "x", "state": state})
    assert r.status_code == 400 and "invalid_grant" in r.text
    assert not config.tokens_path.exists()


@respx.mock
def test_upwork_page_shows_status_and_expiry(logged_in, config):
    app, c = logged_in
    assert "Not connected" in c.get("/upwork").text
    oauth_client = UpworkOAuth(config, app.state.settings, app.state.db)
    put_tokens(oauth_client)
    html = c.get("/upwork").text
    assert "Connected" in html and "Not connected" not in html
    assert "UTC" in html and "Disconnect" in html
    assert oauth_client.store.load().expires_at in html
    assert "OLD-AT" not in html and "OLD-RT" not in html
    assert f"{BASE}/oauth/client-metadata.json" in html


def test_upwork_page_with_unreadable_token_file(logged_in, config):
    _, c = logged_in
    config.tokens_path.write_text("junk")
    r = c.get("/upwork")
    assert r.status_code == 200 and "cannot be read" in r.text


@respx.mock
def test_disconnect_needs_login_and_csrf(app_env, config):
    app, c, secret = app_env
    put_tokens(UpworkOAuth(config, app.state.settings, app.state.db))
    assert c.post("/upwork/disconnect").status_code == 403  # no session at all
    r = c.post("/upwork/disconnect", data={"csrf_token": "x"})
    assert r.status_code == 403
    login(c, secret)
    assert c.post("/upwork/disconnect").status_code == 403  # logged in, no CSRF
    assert config.tokens_path.exists()
    # Not logged in but with a valid CSRF token: redirected to login, file stays.
    c.cookies.clear()
    token = csrf_from(c.get("/login").text)
    r = c.post("/upwork/disconnect", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert config.tokens_path.exists()


@respx.mock
def test_disconnect_revokes_and_deletes_file(logged_in, config):
    app, c = logged_in
    put_tokens(UpworkOAuth(config, app.state.settings, app.state.db))
    revoke = respx.post(REVOKE_URL).mock(return_value=httpx.Response(200))
    token = csrf_from(c.get("/upwork").text)
    r = c.post("/upwork/disconnect", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/upwork?disconnected=1"
    assert revoke.called and not config.tokens_path.exists()
    assert "Not connected" in c.get("/upwork").text


@respx.mock
def test_disconnect_when_revoke_fails_still_deletes(logged_in, config):
    app, c = logged_in
    put_tokens(UpworkOAuth(config, app.state.settings, app.state.db))
    respx.post(REVOKE_URL).mock(return_value=httpx.Response(500))
    token = csrf_from(c.get("/upwork").text)
    r = c.post("/upwork/disconnect", data={"csrf_token": token})
    assert r.headers["location"] == "/upwork?disconnected=revoke_failed"
    assert not config.tokens_path.exists()
    assert "did not confirm" in c.get(r.headers["location"]).text


