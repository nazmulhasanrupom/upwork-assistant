"""One-click Upwork login (plan P5). Uses the fake pty-aware claude; the real one never runs."""

import os
import time

import pytest

from app.upwork import mcp_login
from app.upwork.client import UpworkAuthError
from app.upwork.mcp_login import AuthLink, LoginError, manager
from tests.test_accounts import ONE
from tests.test_auth import csrf_from, login, make_client, setup_admin
from tests.upwork_fakes import FakeAccountsMCP

PORT = 41234
STATE = "STATE123"
GOOD = f"http://localhost:{PORT}/callback?code=GOOD&state={STATE}"
LINK_URL = (
    "https://www.upwork.com/ab/account-security/oauth2/authorize?response_type=code"
    f"&redirect_uri=http%3A%2F%2Flocalhost%3A{PORT}%2Fcallback&state={STATE}"
)
LINK = AuthLink(LINK_URL, PORT, STATE)


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir()
    return str(cfg)


def start(cfg, **extra):
    """Start a login. The environment is read now, so fixtures that set env vars count."""
    return manager.start({**os.environ, "CLAUDE_CONFIG_DIR": cfg, **extra}, cfg)


def pid_dead(pid: int) -> bool:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # a zombie still answers kill(0); read its state
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


# ---------- URL extraction ----------


def test_extract_url_from_osc8_and_plain_text():
    raw = (
        'Starting authentication for "upwork"…\r\nVisit this URL to authorize:\r\n'
        f"\x1b]8;;{LINK_URL}\x1b\\{LINK_URL}\x1b]8;;\x1b\\\r\nWaiting\r\n"
    )
    assert mcp_login.extract_authorize_url(raw) == LINK_URL
    bel = raw.replace("\x1b\\", "\x07")
    assert mcp_login.extract_authorize_url(bel) == LINK_URL
    plain = f"\x1b[1mVisit this URL:\x1b[0m\r\n{LINK_URL}\r\nWaiting\r\n"
    assert mcp_login.extract_authorize_url(plain) == LINK_URL


def test_extract_url_waits_for_the_complete_link():
    assert mcp_login.extract_authorize_url(f"Visit:\r\n{LINK_URL[:60]}") is None
    assert mcp_login.extract_authorize_url("Starting authentication\r\n") is None
    other = "https://evil.example/ab/account-security/oauth2/authorize?x=1\r\n"
    assert mcp_login.extract_authorize_url(other) is None


def test_parse_authorize_url_reads_port_and_state():
    link = mcp_login.parse_authorize_url(LINK_URL)
    assert (link.port, link.state) == (PORT, STATE)
    with pytest.raises(LoginError):
        mcp_login.parse_authorize_url(LINK_URL.replace("localhost", "evil.example"))
    with pytest.raises(LoginError):
        mcp_login.parse_authorize_url(LINK_URL.replace("www.upwork.com", "evil.example"))
    with pytest.raises(LoginError):
        mcp_login.parse_authorize_url(LINK_URL.replace(f"&state={STATE}", ""))


# ---------- pasted address checks ----------


def check(pasted):
    return mcp_login.validate_pasted(pasted, LINK)


def test_valid_paste_passes():
    assert check(f"  {GOOD}  ") == GOOD
    assert check(f"http://127.0.0.1:{PORT}/callback?code=x&state={STATE}")


@pytest.mark.parametrize(
    "bad,word",
    [
        (f"http://localhost:{PORT + 1}/callback?code=x&state={STATE}", "port"),
        (f"http://evil.example:{PORT}/callback?code=x&state={STATE}", "localhost"),
        (f"https://localhost:{PORT}/callback?code=x&state={STATE}", "http://"),
        (f"http://localhost:{PORT}/callback?state={STATE}", "code"),
        (f"http://localhost:{PORT}/callback?code=&state={STATE}", "code"),
        (f"http://localhost:{PORT}/callback?code=x", "state"),
        (f"http://localhost:{PORT}/callback?code=x&state=OTHER", "different login"),
        (f"http://localhost:{PORT}/other?code=x&state={STATE}", "/callback"),
        (f"http://localhost/callback?code=x&state={STATE}", "port"),
        ("", "Paste"),
        (f"{GOOD}\nhttp://x", "Paste"),
        ("notaurl", "http://"),
    ],
)
def test_bad_paste_is_refused(bad, word):
    with pytest.raises(LoginError) as e:
        check(bad)
    assert word in str(e.value)


def test_clean_text_and_safe_lines_hide_links_and_tokens():
    raw = (
        "\x1b[31mError\x1b[0m: failed\r\n"
        f"\x1b]8;;{LINK_URL}\x1b\\{LINK_URL}\x1b]8;;\x1b\\\r\n"
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnopqrstuvwxyz0123\r\n"
    )
    out = mcp_login.safe_lines(raw)
    assert "Error: failed" in out and "[link]" in out and "[hidden]" in out
    assert "eyJ" not in out and "upwork.com" not in out and "\x1b" not in out


# ---------- the manager with the fake claude ----------


def test_start_returns_link_and_uses_config_dir(fake_claude_login, env):
    cwd = env
    link = start(env)
    assert (link.port, link.state) == (PORT, STATE)
    assert link.url.startswith("https://www.upwork.com/ab/account-security/oauth2/authorize?")
    log = fake_claude_login.log()
    assert log["argv"] == ["mcp", "login", "upwork", "--no-browser"]
    assert log["config_dir"] == cwd
    assert manager.active() == link
    assert manager.remaining_seconds() > 500


def test_finish_success(fake_claude_login, env):
    start(env)
    ok, msg = manager.finish(GOOD)
    assert ok and "saved" in msg
    assert manager.active() is None


def test_finish_failure_shows_cleaned_text_without_tokens(fake_claude_login, env):
    start(env)
    bad = f"http://localhost:{PORT}/callback?code=BAD&state={STATE}"
    ok, msg = manager.finish(bad)
    assert not ok and "authorization failed" in msg
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in msg
    assert "code=BAD" not in msg and "\x1b" not in msg
    assert manager.active() is None


def test_bad_paste_keeps_session_open_for_another_try(fake_claude_login, env):
    start(env)
    ok, msg = manager.finish(f"http://localhost:{PORT + 5}/callback?code=GOOD&state={STATE}")
    assert not ok and "port" in msg
    assert manager.active() is not None
    ok, _ = manager.finish(GOOD)
    assert ok


def test_finish_without_session(fake_claude_login):
    ok, msg = manager.finish(GOOD)
    assert not ok and "no login in progress" in msg


def test_start_twice_kills_the_first(fake_claude_login, env):
    start(env)
    first = fake_claude_login.logs()[0]["pid"]
    start(env)
    assert len(fake_claude_login.logs()) == 2
    assert pid_dead(first)
    assert manager.active() is not None
    ok, _ = manager.finish(GOOD)
    assert ok


def test_cancel_kills_the_process(fake_claude_login, env):
    start(env)
    pid = fake_claude_login.logs()[0]["pid"]
    manager.cancel()
    assert pid_dead(pid)
    assert manager.active() is None
    manager.cancel()  # nothing to cancel is fine


def test_session_timeout_kills_the_process(fake_claude_login, env, monkeypatch):
    monkeypatch.setattr(manager, "session_seconds", 0.4)
    start(env)
    pid = fake_claude_login.logs()[0]["pid"]
    assert pid_dead(pid)
    for _ in range(50):
        if manager.active() is None:
            break
        time.sleep(0.05)
    assert manager.active() is None
    ok, msg = manager.finish(GOOD)
    assert not ok


def test_no_link_is_an_error(fake_claude_login, env):
    fake_claude_login.mode("no_url")
    with pytest.raises(LoginError) as e:
        start(env)
    assert "did not give an Upwork login link" in str(e.value) and "not found" in str(e.value)
    assert manager.active() is None


def test_waiting_for_the_link_times_out(fake_claude_login, env, monkeypatch):
    fake_claude_login.mode("hang_start")
    monkeypatch.setattr(manager, "url_wait", 0.5)
    with pytest.raises(LoginError) as e:
        start(env)
    assert "in time" in str(e.value)
    assert pid_dead(fake_claude_login.logs()[0]["pid"])


def test_finish_times_out_and_kills(fake_claude_login, env, monkeypatch):
    fake_claude_login.mode("hang")
    monkeypatch.setattr(manager, "finish_wait", 0.5)
    start(env)
    pid = fake_claude_login.logs()[0]["pid"]
    ok, msg = manager.finish(GOOD)
    assert not ok and "in time" in msg
    assert pid_dead(pid)
    assert manager.active() is None


def test_missing_program(env, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(LoginError) as err:
        start(env, PATH="/nonexistent")
    assert "not found" in str(err.value)


# ---------- the routes ----------


@pytest.fixture
def web(config, fake_claude_login):
    app, c = make_client(config)
    secret = setup_admin(c)
    c.cookies.clear()
    login(c, secret)
    return app, c


def post(c, path, **data):
    token = csrf_from(c.get("/upwork").text)
    return c.post(path, data={"csrf_token": token, **data})


def test_page_before_login_has_connect_button_and_collapsed_import(web):
    app, c = web
    page = c.get("/upwork").text
    assert "Upwork login" in page and "Connect Upwork" in page
    assert 'action="/upwork/login/start"' in page and "Not checked yet" in page
    assert "Step 1" not in page
    assert '<details class="advanced"' in page and "Advanced: import a login file" in page
    assert '<details class="advanced" id="advanced-import" open' not in page
    assert 'action="/upwork/import"' in page


def test_start_shows_steps_and_link(web):
    app, c = web
    r = post(c, "/upwork/login/start")
    assert r.status_code == 200
    assert "Step 1: Open Upwork login" in r.text
    assert (
        "does not" in r.text and "load (localhost)" in r.text and "Copy the full address" in r.text
    )
    assert 'target="_blank"' in r.text and "noopener" in r.text
    assert "https://www.upwork.com/ab/account-security/oauth2/authorize?" in r.text
    assert 'name="pasted"' in r.text and 'action="/upwork/login/finish"' in r.text
    assert 'action="/upwork/login/cancel"' in r.text
    # a reload of the page still shows the open login
    assert "Step 1" in c.get("/upwork").text


def test_start_failure_shows_error_and_try_again(web, fake_claude_login):
    app, c = web
    fake_claude_login.mode("no_url")
    r = post(c, "/upwork/login/start")
    assert r.status_code == 400
    assert "did not give an Upwork login link" in r.text and "Try again" in r.text


def test_finish_success_runs_test_connection(web):
    app, c = web
    fake = FakeAccountsMCP(ONE)
    app.state.mcp_factory = lambda: fake
    post(c, "/upwork/login/start")
    r = post(c, "/upwork/login/finish", pasted=GOOD)
    assert r.status_code == 200
    assert "Upwork login saved" in r.text and "Sample Freelancer" in r.text
    assert fake.calls == [("list_accounts", {})] and fake.closed
    assert "Connected" in r.text
    assert app.state.settings.get("upwork.last_test_status") == "ok"
    assert "GOOD" not in r.text
    assert "Step 1" not in r.text


def test_finish_success_but_test_fails(web):
    app, c = web
    app.state.mcp_factory = lambda: FakeAccountsMCP(UpworkAuthError("Upwork needs a login."))
    post(c, "/upwork/login/start")
    r = post(c, "/upwork/login/finish", pasted=GOOD)
    assert r.status_code == 400
    assert "login was saved, but Test connection failed" in r.text
    assert "Needs login" in c.get("/upwork").text


def test_finish_failure_shows_error_and_try_again(web):
    app, c = web
    fake = FakeAccountsMCP(ONE)
    app.state.mcp_factory = lambda: fake
    post(c, "/upwork/login/start")
    r = post(
        c, "/upwork/login/finish", pasted=f"http://localhost:{PORT}/callback?code=BAD&state={STATE}"
    )
    assert r.status_code == 400
    assert "The login did not work" in r.text and "Try again" in r.text
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in r.text
    assert fake.calls == []  # no test connection after a failed login


def test_finish_with_bad_address_keeps_the_steps(web):
    app, c = web
    post(c, "/upwork/login/start")
    r = post(
        c, "/upwork/login/finish", pasted=f"http://localhost:1/callback?code=GOOD&state={STATE}"
    )
    assert r.status_code == 400 and "port" in r.text
    assert "Step 1" in r.text and "Try again" not in r.text


def test_cancel_route(web, fake_claude_login):
    app, c = web
    post(c, "/upwork/login/start")
    pid = fake_claude_login.logs()[0]["pid"]
    r = post(c, "/upwork/login/cancel")
    assert r.status_code == 200 and "Login cancelled" in r.text and "Step 1" not in r.text
    assert pid_dead(pid)


def test_start_twice_via_routes_kills_first(web, fake_claude_login):
    app, c = web
    post(c, "/upwork/login/start")
    post(c, "/upwork/login/start")
    logs = fake_claude_login.logs()
    assert len(logs) == 2 and pid_dead(logs[0]["pid"])


@pytest.mark.parametrize("path", ["start", "finish", "cancel"])
def test_post_without_csrf_is_refused(web, fake_claude_login, path):
    app, c = web
    r = c.post(f"/upwork/login/{path}", data={"pasted": GOOD})
    assert r.status_code == 403
    assert not fake_claude_login.called()


@pytest.mark.parametrize("path", ["start", "finish", "cancel"])
def test_post_needs_login(config, path):
    app, c = make_client(config)
    r = c.post(f"/upwork/login/{path}", data={})
    assert r.status_code in (303, 403)


def test_test_connection_records_status(web):
    app, c = web
    app.state.mcp_factory = lambda: FakeAccountsMCP(ONE)
    post(c, "/upwork/test")
    page = c.get("/upwork").text
    assert "Connected" in page and "Last checked" in page
    app.state.mcp_factory = lambda: FakeAccountsMCP(UpworkAuthError("needs login"))
    post(c, "/upwork/test")
    assert "Needs login" in c.get("/upwork").text
