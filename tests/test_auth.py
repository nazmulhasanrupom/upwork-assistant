import re
from datetime import timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import auth, crypto
from app.main import create_app

USER = "owner@example.test"
PASSWORD = "correct horse battery"


def make_client(config, base_url="https://testserver"):
    app = create_app(config)
    return app, TestClient(app, base_url=base_url, follow_redirects=False)


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def totp_secret_from(html: str) -> str:
    return re.search(r"<code>([A-Z2-7]+)</code>", html).group(1)


def create_account(client, email=USER, password=PASSWORD, password2=None):
    token = csrf_from(client.get("/setup").text)
    return client.post(
        "/setup/account",
        data={
            "email": email,
            "password": password,
            "password2": password if password2 is None else password2,
            "csrf_token": token,
        },
    )


def enable_totp(client) -> str:
    """Turn on 2-step login through Settings (logged in). Returns the secret."""
    token = csrf_from(client.get("/settings").text)
    r = client.post("/settings/two-step/start", data={"csrf_token": token})
    assert r.status_code == 200 and "<svg" in r.text
    secret = totp_secret_from(r.text)
    r = client.post(
        "/settings/two-step/enable",
        data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf_from(r.text)},
    )
    assert r.status_code == 200 and "Two-step login is on" in r.text
    return secret


def setup_admin(client, totp: bool = False) -> str:
    """First visit: email + password, straight to Home. Returns the TOTP secret when
    `totp` is true (turned on in Settings), else ''."""
    r = create_account(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return enable_totp(client) if totp else ""


@pytest.fixture
def env(config):
    """Account with 2-step login ON. Logged out."""
    app, client = make_client(config)
    secret = setup_admin(client, totp=True)
    client.cookies.clear()  # drop the session from setup
    return app, client, secret


@pytest.fixture
def env_plain(config):
    """Account with 2-step login OFF (the default). Logged out."""
    app, client = make_client(config)
    setup_admin(client)
    client.cookies.clear()
    return app, client


def login(client, secret="", password=PASSWORD, code=None, username=USER):
    """Works both ways: a code is sent only when there is a secret (or `code` is given)."""
    token = csrf_from(client.get("/login").text)
    data = {"username": username, "password": password, "csrf_token": token}
    if code is not None:
        data["code"] = code
    elif secret:
        data["code"] = pyotp.TOTP(secret).now()
    return client.post("/login", data=data)


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
    assert client.get("/setup/totp").headers["location"] == "/setup"  # old URL, no page
    assert client.get("/setup").status_code == 200  # the wizard checklist is for the admin
    token = csrf_from(client.get("/").text)
    r = client.post(
        "/setup/account",
        data={
            "email": "x@example.com",
            "password": PASSWORD,
            "password2": PASSWORD,
            "csrf_token": token,
        },
    )
    assert r.status_code == 404


def test_fresh_install_shows_create_account_form(config):
    _, client = make_client(config)
    r = client.get("/setup")
    assert r.status_code == 200 and "Create your account" in r.text
    assert 'name="email"' in r.text and 'name="password2"' in r.text
    assert "setup_code" not in r.text and "Setup code" not in r.text


def test_first_visit_goes_straight_to_home_without_any_code(config):
    app, client = make_client(config)
    r = create_account(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200
    row = auth.get_admin(app.state.db)
    assert row["username"] == USER and row["totp_secret_enc"] is None
    assert not auth.totp_enabled(app.state.db)


def test_account_email_is_the_username_and_case_is_ignored(config):
    app, client = make_client(config)
    assert create_account(client, email="  Me@Example.TEST ").status_code == 303
    assert auth.get_admin(app.state.db)["username"] == "me@example.test"
    client.cookies.clear()
    assert login(client, username="ME@example.test").status_code == 303


def test_setup_rejects_bad_email_weak_or_mismatched_password(config):
    app, client = make_client(config)
    assert create_account(client, email="not-an-email").status_code == 400
    assert create_account(client, password="short").status_code == 400
    assert create_account(client, password2="other").status_code == 400
    assert not auth.admin_exists(app.state.db)


def test_account_post_needs_csrf(config):
    app, client = make_client(config)
    r = client.post(
        "/setup/account",
        data={"email": "a@example.com", "password": PASSWORD, "password2": PASSWORD},
    )
    assert r.status_code == 403 and not auth.admin_exists(app.state.db)


def test_no_takeover_second_account_is_404(env_plain):
    app, client = env_plain
    client.get("/login")
    token = csrf_from(client.get("/login").text)
    r = client.post(
        "/setup/account",
        data={
            "email": "evil@example.com",
            "password": PASSWORD,
            "password2": PASSWORD,
            "csrf_token": token,
        },
    )
    assert r.status_code == 404
    assert auth.get_admin(app.state.db)["username"] == USER
    assert client.get("/setup").headers["location"] == "/login"


def test_qr_svg_encodes_the_url_with_white_background():
    import segno

    url = auth.provisioning_uri(auth.new_totp_secret(), USER)
    svg = auth.qr_svg(url)
    assert svg.startswith("<svg") and 'fill="#fff"' in svg and "viewBox" in svg
    assert svg != auth.qr_svg(auth.provisioning_uri(auth.new_totp_secret(), USER))
    assert segno.make(url, error="m").svg_inline  # same lib, same input is accepted


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


def test_session_cookie_not_secure_over_plain_http(config):
    from dataclasses import replace

    # No https base URL and a plain http request: the cookie must work on http://localhost.
    _, client = make_client(replace(config, public_base_url=""), "http://testserver")
    setup_admin(client)
    cookie = [
        h for h in client.get("/").headers.get_list("set-cookie") if h.startswith(auth.COOKIE_NAME)
    ] or [h for h in client.cookies.jar if h.name == auth.COOKIE_NAME]
    assert cookie
    r = client.post("/logout", data={"csrf_token": csrf_from(client.get("/").text)})
    low = [h for h in r.headers.get_list("set-cookie") if h.startswith(auth.COOKIE_NAME)][0].lower()
    assert "secure" not in low and "httponly" in low and "samesite=strict" in low
    assert [c for c in client.cookies.jar] == []  # the unsecured cookie was cleared too


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


def test_password_stored_as_argon2(env, config):
    app, _, _ = env
    row = auth.get_admin(app.state.db)
    assert row["password_hash"].startswith("$argon2")
    assert PASSWORD not in row["password_hash"]


def test_secrets_not_in_logs(config, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        _, client = make_client(config)
        secret = setup_admin(client, totp=True)
        client.cookies.clear()
        login(client, secret, password="wrong wrong wrong")
    assert secret not in caplog.text and PASSWORD not in caplog.text


def test_session_for_deleted_admin_is_invalid(env):
    app, client, secret = env
    login(client, secret)
    app.state.db.execute("DELETE FROM admin")
    assert client.get("/").status_code == 303


# ---------- no setup code, optional 2-step login (round 4, E2) ----------


def test_no_setup_code_anywhere(config, capsys):
    app = create_app(config)
    assert "SETUP CODE" not in capsys.readouterr().out
    tables = {r[0] for r in app.state.db.execute("SELECT name FROM sqlite_master")}
    assert "setup_code" not in tables
    assert not hasattr(auth, "issue_setup_code") and not hasattr(auth, "check_setup_code")


def test_login_without_totp(env_plain):
    app, client = env_plain
    assert 'name="code"' not in client.get("/login").text  # no code field while off
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200
    client.cookies.clear()
    assert login(client, password="nope nope nope").status_code == 401
    assert login(client, code="123456").status_code == 303  # an extra code is simply ignored


def test_login_page_shows_code_field_only_when_on(env):
    _, client, _ = env
    assert 'name="code"' in client.get("/login").text


def test_enable_then_login_needs_the_code(env_plain):
    app, client = env_plain
    login(client)
    secret = enable_totp(client)
    assert auth.totp_enabled(app.state.db)
    client.cookies.clear()
    assert 'name="code"' in client.get("/login").text
    assert login(client).status_code == 401  # no code
    assert login(client, code="000000").status_code == 401
    assert login(client, secret).status_code == 303


def test_enable_wrong_code_keeps_it_off_and_same_secret(env_plain):
    app, client = env_plain
    login(client)
    token = csrf_from(client.get("/settings").text)
    page = client.post("/settings/two-step/start", data={"csrf_token": token})
    secret = totp_secret_from(page.text)
    r = client.post(
        "/settings/two-step/enable", data={"code": "000000", "csrf_token": csrf_from(page.text)}
    )
    assert r.status_code == 400 and totp_secret_from(r.text) == secret and "<svg" in r.text
    assert not auth.totp_enabled(app.state.db)
    assert "<img" not in r.text and 'src="http' not in r.text  # QR is inline, no service


def test_enable_without_start_is_refused(env_plain):
    app, client = env_plain
    login(client)
    token = csrf_from(client.get("/settings").text)
    r = client.post("/settings/two-step/enable", data={"code": "123456", "csrf_token": token})
    assert r.status_code == 400 and not auth.totp_enabled(app.state.db)


def test_disable_needs_password_and_code(env):
    app, client, secret = env
    login(client, secret)
    token = csrf_from(client.get("/settings").text)
    post = lambda **d: client.post(  # noqa: E731
        "/settings/two-step/disable", data={"csrf_token": token, **d}
    )
    assert post(password=PASSWORD, code="000000").status_code == 403
    assert post(password="wrong wrong wrong", code=pyotp.TOTP(secret).now()).status_code == 403
    assert post(password=PASSWORD, code="").status_code == 403
    assert auth.totp_enabled(app.state.db)
    r = post(password=PASSWORD, code=pyotp.TOTP(secret).now())
    assert r.status_code == 200 and "Two-step login is off" in r.text
    assert not auth.totp_enabled(app.state.db)
    client.cookies.clear()
    assert login(client).status_code == 303  # no code needed any more


def test_totp_secret_stored_encrypted_after_enable(env, config):
    app, _, secret = env
    row = auth.get_admin(app.state.db)
    assert secret not in row["totp_secret_enc"]
    assert crypto.decrypt(config.secret_key, row["totp_secret_enc"]) == secret


def test_migration_keeps_old_admin_with_totp(tmp_path):
    """An admin made before 2-step became optional keeps working (rows survive the rebuild)."""
    import sqlite3
    from pathlib import Path

    from app import db as dbm

    old = tmp_path / "old"
    old.mkdir()
    for p in sorted(dbm.MIGRATIONS_DIR.glob("*.sql")):
        if int(p.name[:3]) < 29:
            (old / p.name).write_text(p.read_text())
    conn = sqlite3.connect(str(tmp_path / "x.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    dbm.migrate(conn, old)
    conn.execute(
        "INSERT INTO admin(id, username, password_hash, totp_secret_enc, created_at) "
        "VALUES (1, 'a@example.com', 'h', 'enc', '2026-01-01T00:00:00+00:00')"
    )
    assert dbm.migrate(conn, Path(dbm.MIGRATIONS_DIR)) == [29]
    row = conn.execute("SELECT * FROM admin").fetchone()
    assert row["username"] == "a@example.com" and row["totp_secret_enc"] == "enc"
    conn.execute("UPDATE admin SET totp_secret_enc = NULL")  # nullable now


# ---------- reminder banner ----------


def _age_admin(app, days):
    when = (auth.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    app.state.db.execute("UPDATE admin SET created_at = ?", (when,))


def _skip(client, back="/"):
    token = csrf_from(client.get("/").text)
    return client.post("/settings/two-step/skip", data={"csrf_token": token, "back": back})


def test_reminder_not_shown_in_the_first_week(env_plain):
    app, client = env_plain
    login(client)
    _age_admin(app, 6)
    assert "Turn on 2-step login" not in client.get("/").text


def test_reminder_shown_after_seven_days_on_every_page(env_plain):
    app, client = env_plain
    login(client)
    _age_admin(app, 8)
    for path in ("/", "/settings", "/approvals"):
        html = client.get(path).text
        assert "Turn on 2-step login" in html and "/settings#two-step" in html
        assert "/settings/two-step/skip" in html


def test_reminder_never_shown_when_totp_is_on(env):
    app, client, secret = env
    login(client, secret)
    _age_admin(app, 30)
    assert "Turn on 2-step login" not in client.get("/").text


def test_reminder_days_come_from_settings(env_plain):
    app, client = env_plain
    login(client)
    app.state.settings.set("security.totp_reminder_days", 2)
    _age_admin(app, 3)
    assert "Turn on 2-step login" in client.get("/").text


def test_skip_hides_then_shows_again_after_the_wait(env_plain, monkeypatch):
    app, client = env_plain
    login(client)
    _age_admin(app, 8)
    r = _skip(client, "/settings")
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert app.state.settings.get("security.totp_reminder_skipped_at")
    assert "Turn on 2-step login" not in client.get("/").text
    real = auth.utcnow
    again = app.state.settings.get("security.totp_reminder_again_days")
    monkeypatch.setattr(auth, "utcnow", lambda: real() + timedelta(days=again - 1))
    assert "Turn on 2-step login" not in client.get("/").text
    monkeypatch.setattr(auth, "utcnow", lambda: real() + timedelta(days=again + 1))
    assert "Turn on 2-step login" in client.get("/").text


def test_skip_with_again_zero_never_shows_again(env_plain, monkeypatch):
    app, client = env_plain
    login(client)
    _age_admin(app, 8)
    app.state.settings.set("security.totp_reminder_again_days", 0)
    _skip(client)
    real = auth.utcnow
    monkeypatch.setattr(auth, "utcnow", lambda: real() + timedelta(days=3650))
    assert "Turn on 2-step login" not in client.get("/").text


def test_skip_needs_csrf_and_login_and_ignores_outside_targets(env_plain):
    app, client = env_plain
    assert client.post("/settings/two-step/skip", data={}).status_code == 403
    login(client)
    assert client.post("/settings/two-step/skip", data={"back": "/"}).status_code == 403
    for bad in ("https://evil.test", "//evil.test", "/\\evil.test"):
        assert _skip(client, bad).headers["location"] == "/"


def test_skip_requires_login(env_plain):
    _, client = env_plain
    token = csrf_from(client.get("/login").text)
    r = client.post("/settings/two-step/skip", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"
