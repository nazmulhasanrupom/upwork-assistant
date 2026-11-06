import re
from datetime import timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import auth, crypto
from app.main import create_app

USER = "owner"
PASSWORD = "correct horse battery"


def make_client(config):
    app = create_app(config)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    # The real code is only printed at start; tests make a fresh one the same way.
    client.setup_code = auth.issue_setup_code(app.state.db, config.secret_key)
    return app, client


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def totp_secret_from(html: str) -> str:
    return re.search(r"<code>([A-Z2-7]+)</code>", html).group(1)


def setup_admin(client) -> str:
    """Run the two-step first-user flow. Returns the TOTP secret."""
    token = csrf_from(client.get("/setup").text)
    r = client.post(
        "/setup/account",
        data={
            "username": USER,
            "password": PASSWORD,
            "password2": PASSWORD,
            "setup_code": client.setup_code,
            "csrf_token": token,
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/setup/totp"
    r = client.get("/setup/totp")
    assert r.status_code == 200
    secret = totp_secret_from(r.text)
    r = client.post(
        "/setup/confirm",
        data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf_from(r.text)},
    )
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    return secret


@pytest.fixture
def env(config):
    app, client = make_client(config)
    secret = setup_admin(client)
    client.cookies.clear()  # drop the session from setup
    return app, client, secret


def login(client, secret, password=PASSWORD, code=None, username=USER):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "code": code if code is not None else pyotp.TOTP(secret).now(),
            "csrf_token": token,
        },
    )


def test_setup_and_login(env):
    _, client, secret = env
    r = login(client, secret)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.get("/")
    assert r.status_code == 200 and USER in r.text


def test_setup_logs_in_and_closes(config):
    _, client = make_client(config)
    setup_admin(client)
    assert client.get("/").status_code == 200  # logged in right after setup
    assert client.get("/setup/totp").status_code == 404  # the admin steps are closed
    assert client.get("/setup").status_code == 200  # the wizard checklist is for the admin
    token = csrf_from(client.get("/").text)
    r = client.post(
        "/setup/account",
        data={"username": "x", "password": PASSWORD, "password2": PASSWORD, "csrf_token": token},
    )
    assert r.status_code == 404


def test_setup_rejects_weak_or_mismatched_password(config):
    _, client = make_client(config)
    token = csrf_from(client.get("/setup").text)
    base = {"username": USER, "csrf_token": token, "setup_code": client.setup_code}
    assert (
        client.post(
            "/setup/account", data={**base, "password": "short", "password2": "short"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/setup/account", data={**base, "password": PASSWORD, "password2": "other"}
        ).status_code
        == 400
    )


def test_setup_wrong_totp_creates_nothing(config):
    app, client = make_client(config)
    token = csrf_from(client.get("/setup").text)
    r = client.post(
        "/setup/account",
        data={
            "username": USER,
            "password": PASSWORD,
            "password2": PASSWORD,
            "setup_code": client.setup_code,
            "csrf_token": token,
        },
    )
    r = client.get(r.headers["location"])
    bad = {"code": "000000", "csrf_token": csrf_from(r.text)}
    r = client.post("/setup/confirm", data=bad)
    assert r.status_code == 400
    assert not auth.admin_exists(app.state.db)


def _to_totp_page(client):
    token = csrf_from(client.get("/setup").text)
    r = client.post(
        "/setup/account",
        data={
            "username": USER,
            "password": PASSWORD,
            "password2": PASSWORD,
            "setup_code": client.setup_code,
            "csrf_token": token,
        },
    )
    assert r.status_code == 303
    return client.get(r.headers["location"])


def test_totp_page_shows_inline_svg_qr_of_the_otpauth_url(config):
    _, client = make_client(config)
    r = _to_totp_page(client)
    assert r.status_code == 200
    assert "<svg" in r.text and 'class="qr"' in r.text
    assert "Can&#39;t scan?" in r.text or "Can't scan?" in r.text
    assert "otpauth://totp/" in r.text  # link text stays under the QR code
    assert totp_secret_from(r.text) in r.text
    # no image file, no external service: the QR is inline (the only script is local htmx)
    assert "<img" not in r.text and "data:image" not in r.text
    assert 'src="http' not in r.text and "//" not in r.text.split("<svg")[1].split("</svg>")[0]
    assert r.text.index("<svg") < r.text.index("Key:")


def test_qr_svg_encodes_the_url_with_white_background():
    import segno

    url = auth.provisioning_uri(auth.new_totp_secret(), USER)
    svg = auth.qr_svg(url)
    assert svg.startswith("<svg") and 'fill="#fff"' in svg and "viewBox" in svg
    assert svg != auth.qr_svg(auth.provisioning_uri(auth.new_totp_secret(), USER))
    assert segno.make(url, error="m").svg_inline  # same lib, same input is accepted


def test_reload_of_totp_page_makes_a_new_secret(config):
    app, client = make_client(config)
    first = _to_totp_page(client)
    s1 = totp_secret_from(first.text)
    second = client.get("/setup/totp")
    s2 = totp_secret_from(second.text)
    assert s1 != s2
    assert first.text.split("<svg")[1] != second.text.split("<svg")[1]
    # the page after the reload is the one that counts: old secret no longer works
    assert (
        client.post(
            "/setup/confirm",
            data={"code": pyotp.TOTP(s1).now(), "csrf_token": csrf_from(second.text)},
        ).status_code
        == 400
    )
    assert not auth.admin_exists(app.state.db)
    # a wrong code keeps the same secret (no reload), and the newest secret finishes setup
    r = client.post(
        "/setup/confirm",
        data={"code": pyotp.TOTP(s2).now(), "csrf_token": csrf_from(second.text)},
    )
    assert r.status_code == 303 and auth.admin_exists(app.state.db)


def test_wrong_code_page_keeps_the_same_secret(config):
    _, client = make_client(config)
    page = _to_totp_page(client)
    s1 = totp_secret_from(page.text)
    r = client.post("/setup/confirm", data={"code": "000000", "csrf_token": csrf_from(page.text)})
    assert r.status_code == 400 and totp_secret_from(r.text) == s1 and "<svg" in r.text


def test_totp_page_without_first_step_redirects_and_404_after_setup(config):
    _, client = make_client(config)
    r = client.get("/setup/totp")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    setup_admin(client)
    assert client.get("/setup/totp").status_code == 404


def test_wrong_password_wrong_totp_wrong_user(env):
    _, client, secret = env
    assert login(client, secret, password="nope nope nope").status_code == 401
    assert login(client, secret, code="000000").status_code == 401
    assert login(client, secret, username="someone").status_code == 401
    assert client.get("/").status_code == 303


def test_protected_page_redirects_and_public_pages_open(env):
    _, client, _ = env
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200


def test_redirect_to_setup_when_no_admin(config):
    _, client = make_client(config)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_htmx_request_when_logged_out_gets_401(env):
    _, client, _ = env
    r = client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_login_without_csrf_is_403(env):
    _, client, secret = env
    r = client.post(
        "/login",
        data={"username": USER, "password": PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 403
    client.get("/login")  # now there is a session, but the token is still missing or wrong
    r = client.post(
        "/login",
        data={
            "username": USER,
            "password": PASSWORD,
            "code": pyotp.TOTP(secret).now(),
            "csrf_token": "wrong",
        },
    )
    assert r.status_code == 403


def test_logout_needs_csrf_then_works(env):
    _, client, secret = env
    login(client, secret)
    assert client.post("/logout").status_code == 403
    assert client.get("/").status_code == 200  # still logged in
    token = csrf_from(client.get("/").text)
    r = client.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get("/").status_code == 303


def test_csrf_header_accepted_for_htmx(env):
    _, client, secret = env
    login(client, secret)
    token = csrf_from(client.get("/").text)
    r = client.post("/logout", headers={"X-CSRF-Token": token})
    assert r.status_code == 303


def test_session_cookie_flags(env):
    _, client, secret = env
    r = login(client, secret)
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith(auth.COOKIE_NAME)][0]
    low = cookie.lower()
    assert "secure" in low and "httponly" in low and "samesite=strict" in low


def test_tampered_cookie_is_not_logged_in(env):
    _, client, secret = env
    login(client, secret)
    value = client.cookies[auth.COOKIE_NAME]
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, value[:-2] + "xx", domain="testserver")
    assert client.get("/").status_code == 303


def test_lockout_after_five_wrong_tries(env):
    app, client, secret = env
    for _ in range(5):
        assert login(client, secret, password="wrong wrong wrong").status_code in (401, 429)
    # Even the right credentials are refused while locked.
    r = login(client, secret)
    assert r.status_code == 429
    assert client.get("/").status_code == 303


def test_lockout_ends_after_15_minutes(env, monkeypatch):
    app, client, secret = env
    for _ in range(5):
        login(client, secret, password="wrong wrong wrong")
    assert login(client, secret).status_code == 429
    real = auth.utcnow
    monkeypatch.setattr(auth, "utcnow", lambda: real() + timedelta(minutes=16))
    assert login(client, secret).status_code == 303


def test_lockout_survives_restart(env, config):
    app, client, secret = env
    for _ in range(5):
        login(client, secret, password="wrong wrong wrong")
    app.state.db.close()
    _, client2 = make_client(config)  # new app, same DB file
    assert login(client2, secret).status_code == 429


def test_success_resets_failed_count(env):
    app, client, secret = env
    for _ in range(4):
        login(client, secret, password="wrong wrong wrong")
    assert login(client, secret).status_code == 303
    client.cookies.clear()
    for _ in range(4):
        login(client, secret, password="wrong wrong wrong")
    assert login(client, secret).status_code == 303  # would be locked without the reset


def test_totp_secret_stored_encrypted(env, config):
    app, _, secret = env
    row = auth.get_admin(app.state.db)
    assert secret not in row["totp_secret_enc"]
    assert crypto.decrypt(config.secret_key, row["totp_secret_enc"]) == secret
    assert row["password_hash"].startswith("$argon2")
    assert PASSWORD not in row["password_hash"]


def test_secrets_not_in_logs(config, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        _, client = make_client(config)
        secret = setup_admin(client)
        client.cookies.clear()
        login(client, secret, password="wrong wrong wrong")
    assert secret not in caplog.text and PASSWORD not in caplog.text


def test_session_for_deleted_admin_is_invalid(env):
    app, client, secret = env
    login(client, secret)
    app.state.db.execute("DELETE FROM admin")
    assert client.get("/").status_code == 303


# ---------- setup code (plan D0) ----------


def _first_step(client, code):
    token = csrf_from(client.get("/setup").text)
    data = {"username": USER, "password": PASSWORD, "password2": PASSWORD, "csrf_token": token}
    if code is not None:
        data["setup_code"] = code
    return client.post("/setup/account", data=data)


def test_setup_fails_without_code(config):
    app, client = make_client(config)
    assert _first_step(client, None).status_code in (403, 422)
    assert _first_step(client, "").status_code == 403
    assert not auth.admin_exists(app.state.db)


def test_setup_fails_with_wrong_code(config):
    app, client = make_client(config)
    r = _first_step(client, "not-the-code")
    assert r.status_code == 403
    assert "otpauth" not in r.text  # never got to the authenticator step
    assert not auth.admin_exists(app.state.db)


def test_setup_works_with_right_code(config):
    app, client = make_client(config)
    setup_admin(client)
    assert auth.admin_exists(app.state.db)
    # The code is gone once the admin exists.
    assert app.state.db.execute("SELECT COUNT(*) FROM setup_code").fetchone()[0] == 0


def test_setup_code_printed_on_start_and_only_hash_stored(config, capsys):
    app = create_app(config)
    out = capsys.readouterr().out
    code = re.search(r"SETUP CODE: (\S+)", out).group(1)
    stored = app.state.db.execute("SELECT code_hash FROM setup_code").fetchone()[0]
    assert code not in stored
    assert auth.check_setup_code(app.state.db, config.secret_key, code)
    assert not auth.check_setup_code(app.state.db, config.secret_key, code + "x")
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    client.setup_code = code
    setup_admin(client)


def test_no_code_printed_when_admin_exists(config, capsys):
    _, client = make_client(config)
    setup_admin(client)
    capsys.readouterr()
    create_app(config)  # restart
    assert "SETUP CODE" not in capsys.readouterr().out


def test_restart_makes_a_new_code(config):
    _, client = make_client(config)
    old = client.setup_code
    app2, client2 = make_client(config)  # restart before the admin exists
    assert not auth.check_setup_code(app2.state.db, config.secret_key, old)
    assert auth.check_setup_code(app2.state.db, config.secret_key, client2.setup_code)


def test_setup_code_guesses_are_locked(config):
    app, client = make_client(config)
    for _ in range(5):
        assert _first_step(client, "guess").status_code in (403, 429)
    # Even the right code is refused while locked.
    assert _first_step(client, client.setup_code).status_code == 429
    assert not auth.admin_exists(app.state.db)


def test_setup_lock_does_not_block_the_new_admin(config):
    app, client = make_client(config)
    for _ in range(3):
        _first_step(client, "guess")
    setup_admin(client)
    assert app.state.db.execute("SELECT failed_count FROM auth_state").fetchone()[0] == 0
