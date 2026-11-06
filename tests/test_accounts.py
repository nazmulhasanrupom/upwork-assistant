"""Auto-detect the freelancer account (plan F0a). Fake MCP only, no network."""

import json
import re

import pytest

from app.db import init_db
from app.settings import Settings
from app.tasks import daily
from app.upwork import accounts
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.test_auth import csrf_from, login, make_client, setup_admin
from tests.test_daily import ORG
from tests.test_daily import make_client as make_daily_client
from tests.upwork_fakes import FakeAccountsMCP, accounts_payload, load

ONE = accounts_payload(("TALENT", "Sample Freelancer", "111"), ("CLIENT", "Co", "222"))
TWO = accounts_payload(
    ("TALENT", "Freelancer A", "111"), ("TALENT", "Freelancer B", "333"), ("FL_AGENCY", "Ag", "444")
)
ZERO = accounts_payload(("CLIENT", "Co", "222"), ("FL_AGENCY", "Ag", "444"))


# ---------- the helper ----------


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    return Settings(conn)


def test_fixture_file_has_one_talent():
    assert accounts.parse_accounts(load("list_accounts.json")) == [
        {"org_uid": "1000000000000000001", "name": "Sample Freelancer"}
    ]


def test_one_talent_is_saved(settings):
    mcp = FakeAccountsMCP(ONE)
    res = accounts.detect_and_save(mcp, settings)
    assert res.status == "saved"
    assert mcp.calls == [("list_accounts", {})]  # no arguments
    assert settings.get("upwork.talent_org_uid") == "111"
    assert settings.get("upwork.talent_name") == "Sample Freelancer"
    assert accounts.current(settings) == {"org_uid": "111", "name": "Sample Freelancer"}


def test_two_talents_wait_for_a_pick(settings):
    res = accounts.detect_and_save(FakeAccountsMCP(TWO), settings)
    assert res.status == "choose"
    assert settings.get("upwork.talent_org_uid") == ""
    assert [c["org_uid"] for c in accounts.candidates(settings)] == ["111", "333"]
    with pytest.raises(accounts.AccountError):
        accounts.pick_candidate(settings, "444")  # an agency, not offered
    accounts.pick_candidate(settings, "333")
    assert settings.get("upwork.talent_org_uid") == "333"
    assert settings.get("upwork.talent_name") == "Freelancer B"
    assert accounts.candidates(settings) == []


def test_zero_talents_is_an_error_and_keeps_old_value(settings):
    settings.set("upwork.talent_org_uid", "old")
    res = accounts.detect_and_save(FakeAccountsMCP(ZERO), settings)
    assert res.status == "none"
    assert res.message == "No freelancer account found. Agency accounts are not supported."
    assert settings.get("upwork.talent_org_uid") == "old"


def test_odd_answer(settings):
    with pytest.raises(accounts.AccountError):
        accounts.detect_and_save(FakeAccountsMCP({"oops": 1}), settings)


# ---------- the web pages ----------


@pytest.fixture
def web(config):
    app, c = make_client(config)
    secret = setup_admin(c)
    c.cookies.clear()
    login(c, secret)
    return app, c


def use(app, payload):
    fake = FakeAccountsMCP(payload)
    app.state.mcp_factory = lambda: fake
    return fake


def post(c, path, **data):
    token = csrf_from(c.get("/upwork").text)
    return c.post(path, data={"csrf_token": token, **data})


def test_page_has_button_login_help_and_no_oauth(web):
    app, c = web
    page = c.get("/upwork").text
    assert "Test connection" in page and 'action="/upwork/test"' in page
    assert "How to log in" in page and "claude mcp add" in page and "/mcp" in page
    assert "No freelancer account is selected" in page and "no fetch yet" in page
    assert "/oauth/" not in page  # "Connect Upwork" is now the pty login button (plan P5)


def test_oauth_routes_are_gone(web):
    app, c = web
    assert c.get("/oauth/client-metadata.json", follow_redirects=False).status_code in (303, 404)
    c.cookies.clear()
    assert c.get("/oauth/client-metadata.json", follow_redirects=False).status_code in (303, 404)
    assert c.get("/oauth/callback", follow_redirects=False).status_code in (303, 404)
    from app import auth

    assert not any("oauth" in p for p in auth.PUBLIC_PATHS)


def test_test_connection_one_account(web):
    app, c = web
    fake = use(app, ONE)
    r = post(c, "/upwork/test")
    assert r.status_code == 200 and "Sample Freelancer" in r.text
    assert fake.calls == [("list_accounts", {})]  # one list_accounts batch
    assert fake.closed
    assert app.state.settings.get("upwork.talent_org_uid") == "111"
    page = c.get("/upwork").text
    assert "In use:" in page and "Sample Freelancer" in page and "111" in page
    assert "Client" not in page.split("Freelancer account")[1].split("How to log in")[0]


def test_page_shows_last_fetch_cost(web):
    app, c = web
    from app.llm.runner import save_call

    save_call(app.state.db, "fetch-1", "claude-haiku-5-5", {"total_cost_usd": 0.0123}, "fetch")
    page = c.get("/upwork").text
    assert "$0.0123" in page and "claude-haiku-5-5" in page


def test_test_connection_two_accounts_shows_form_then_pick(web):
    app, c = web
    use(app, TWO)
    r = post(c, "/upwork/test")
    assert r.status_code == 200
    assert 'action="/upwork/account"' in r.text and "Freelancer A" in r.text
    assert "Freelancer B" in r.text and "Ag" not in re.findall(r"\(<code>444", r.text)
    assert app.state.settings.get("upwork.talent_org_uid") == ""
    r = post(c, "/upwork/account", org_uid="333")
    assert r.status_code == 200 and "Freelancer B" in r.text
    assert app.state.settings.get("upwork.talent_org_uid") == "333"
    assert 'action="/upwork/account"' not in c.get("/upwork").text


def test_pick_rejects_unknown_account(web):
    app, c = web
    use(app, TWO)
    post(c, "/upwork/test")
    r = post(c, "/upwork/account", org_uid="444")
    assert r.status_code == 400 and "not one of the detected" in r.text
    assert app.state.settings.get("upwork.talent_org_uid") == ""


def test_test_connection_zero_accounts_error(web):
    app, c = web
    use(app, ZERO)
    r = post(c, "/upwork/test")
    assert r.status_code == 400
    assert "No freelancer account found. Agency accounts are not supported." in r.text


@pytest.mark.parametrize("exc", [UpworkToolError("boom <b>x</b>"), RuntimeError("secret")])
def test_test_connection_failure_is_shown(web, exc):
    app, c = web
    use(app, exc)
    r = post(c, "/upwork/test")
    assert r.status_code == 400 and "Test failed" in r.text
    assert "<b>x</b>" not in r.text and "secret" not in r.text  # escaped / not leaked


def test_test_connection_claude_errors_are_shown(web):
    from app.llm.runner import ClaudeRateLimited

    app, c = web
    use(app, ClaudeRateLimited("Claude is rate limited"))
    r = post(c, "/upwork/test")
    assert r.status_code == 400 and "rate limited" in r.text


def test_test_connection_login_expired(web):
    app, c = web
    use(app, UpworkAuthError("Upwork login in Claude Code is missing or expired."))
    r = post(c, "/upwork/test")
    assert r.status_code == 400 and "missing or expired" in r.text


def test_test_and_pick_need_csrf_and_login(web):
    app, c = web
    assert c.post("/upwork/test").status_code == 403
    assert c.post("/upwork/account", data={"org_uid": "1"}).status_code == 403
    c.cookies.clear()
    assert c.post("/upwork/test").status_code in (303, 403)
    assert app.state.settings.get("upwork.talent_org_uid") == ""


def test_test_connection_with_the_real_bridge_and_fake_claude(
    web, fake_claude_stream, tmp_path, monkeypatch
):
    app, c = web
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"tool": "list_accounts", "text": json.dumps(ONE)}]))
    monkeypatch.setenv("FAKE_UPWORK_RULES", str(rules))
    r = post(c, "/upwork/test")
    assert r.status_code == 200 and "Sample Freelancer" in r.text
    assert "$0.0050" in c.get("/upwork").text  # cost of the fake run
    argv = fake_claude_stream.log()["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__upwork__upwork__list_accounts"


def test_test_connection_when_claude_says_login_needed(web, fake_claude_stream, monkeypatch):
    app, c = web
    monkeypatch.setenv("FAKE_SERVER_STATUS", "needs-auth")
    r = post(c, "/upwork/test")
    assert r.status_code == 400 and "Upwork login in Claude Code" in r.text


# ---------- the daily task ----------


def daily_client(settings, payload):
    return make_daily_client(settings, accounts=payload)


def test_daily_detects_single_account_first(conn, settings, config):
    client = daily_client(settings, ONE)
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "ok", res.error
    assert settings.get("upwork.talent_org_uid") == "111"
    assert [n for n, _ in client.calls].count("list_accounts") == 1
    assert client.calls[0][0] == "list_accounts"
    assert {a["org_uid"] for n, a in client.calls if n == "find_jobs"} == {"111"}


def test_daily_skips_detect_when_org_is_set(conn, settings, config):
    settings.set("upwork.talent_org_uid", ORG)
    client = daily_client(settings, ONE)
    assert daily.run_daily(conn, settings, config, client).status == "ok"
    assert all(n != "list_accounts" for n, _ in client.calls)


@pytest.mark.parametrize(
    "payload, text",
    [(TWO, "More than one"), (ZERO, "No freelancer account found")],
)
def test_daily_fails_clearly_without_a_single_account(conn, settings, config, payload, text):
    client = daily_client(settings, payload)
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "error" and text in res.error
    assert settings.get("upwork.talent_org_uid") == ""
    assert all(n != "find_jobs" for n, _ in client.calls)


def test_daily_list_accounts_login_expired(conn, settings, config):
    client = daily_client(settings, UpworkAuthError("login expired"))
    assert daily.run_daily(conn, settings, config, client).status == "auth_expired"
