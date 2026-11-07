"""Setup wizard /setup (plan J1, SPEC 4.2). TestClient, fakes only."""

import pytest

from app import auth
from app.memory import memory_file
from app.setup_routes import PAY_KEYS, SCHEDULE_KEYS, STEPS
from tests.test_auth import PASSWORD, USER, csrf_from, make_client, setup_admin


def post(client, path, data=None):
    """Click. A queued action (the Claude check) is followed to the page that shows its result."""
    token = csrf_from(client.get("/setup").text)
    r = client.post(path, data={**(data or {}), "csrf_token": token})
    if r.status_code == 303 and "/setup?step=claude&action=" in r.headers["location"]:
        return client.get(r.headers["location"])
    return r


def pay_form(app):
    return {k: str(app.state.settings.get(k)) for k in PAY_KEYS}


def schedule_form(app):
    form = {k: str(app.state.settings.get(k)) for k in SCHEDULE_KEYS}
    form["general.timezone"] = "Europe/Berlin"
    return form


@pytest.fixture
def wiz(config):
    app, client = make_client(config)
    setup_admin(client)  # logged in, on the checklist
    app.state.worker.sync = True  # each action runs inside the click (plan W3)
    return app, client


# ---------- step 1 ----------


def test_fresh_data_shows_step_one(config):
    app, client = make_client(config)
    r = client.get("/setup")
    assert r.status_code == 200 and "/setup/account" in r.text and "Create your account" in r.text
    assert 'id="setup-steps"' not in r.text  # no checklist before the admin exists


def test_unauthenticated_pages_go_to_setup_until_admin_exists(config):
    app, client = make_client(config)
    r = client.get("/jobs")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = client.get("/login")
    assert r.headers["location"] == "/setup"


def test_old_urls_redirect(config):
    app, client = make_client(config)
    assert client.get("/setup/admin").headers["location"] == "/setup"
    assert client.get("/setup/admin/totp").headers["location"] == "/setup"
    assert client.get("/setup/totp").headers["location"] == "/setup"


def test_no_takeover_after_admin_exists(wiz):
    app, client = wiz
    token = csrf_from(client.get("/setup").text)
    data = {
        "email": "e@example.com",
        "password": PASSWORD,
        "password2": PASSWORD,
        "csrf_token": token,
    }
    assert client.post("/setup/account", data=data).status_code == 404
    # Also without a session
    client.cookies.clear()
    assert client.get("/setup").headers["location"] == "/login"
    assert auth.get_admin(app.state.db)["username"] == USER


def test_password_stored_as_hash_only(wiz, caplog):
    app, _ = wiz
    stored = auth.get_admin(app.state.db)["password_hash"]
    assert stored.startswith("$argon2") and PASSWORD not in stored


# ---------- the checklist ----------


def test_hub_lists_nine_steps_without_an_admin_step(wiz):
    app, client = wiz
    html = client.get("/setup").text
    assert len(STEPS) == 9 and STEPS[0]["key"] == "claude"
    assert 'id="step-admin"' not in html
    for st in STEPS:
        assert f'id="step-{st["key"]}"' in html
    assert app.state.settings.get("setup.finished") is False
    r = post(client, "/setup/finish")
    assert r.status_code == 400 and "Not finished yet" in r.text
    assert app.state.settings.get("setup.finished") is False


def test_setup_needs_login_after_admin(wiz):
    app, client = wiz
    client.cookies.clear()
    for path in ("/setup/claude", "/setup/pay", "/setup/keywords"):
        assert client.post(path).status_code == 403  # CSRF first
    assert client.get("/setup").headers["location"] == "/login"


def test_claude_step_ok_and_errors(wiz, fake_claude, monkeypatch):
    app, client = wiz
    r = post(client, "/setup/claude")
    assert "Failed" in r.text and "NO TOKEN" in r.text
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    fake_claude.mode("auth")
    r = post(client, "/setup/claude")
    assert "Failed" in r.text and "BAD TOKEN" in r.text and "tok-not-real" not in r.text
    fake_claude.mode("ok")
    r = post(client, "/setup/claude")
    assert r.status_code == 200 and "Claude works." in r.text
    assert "claude" in app.state.settings.get("setup.done_steps")


def test_keywords_validation_and_save(wiz):
    app, client = wiz
    assert post(client, "/setup/keywords", {"keywords": "  \n"}).status_code == 400
    too_many = "\n".join(f"k{i}" for i in range(app.state.settings.get("setup.keywords_max") + 1))
    assert post(client, "/setup/keywords", {"keywords": too_many}).status_code == 400
    r = post(client, "/setup/keywords", {"keywords": "n8n  automation\nZapier, zapier\nRAG"})
    assert r.status_code == 200
    assert app.state.settings.get("search.keywords") == ["n8n automation", "Zapier", "RAG"]


def test_pay_validation_and_save(wiz):
    app, client = wiz
    form = pay_form(app)
    assert post(client, "/setup/pay", {**form, "pay.fit_min": "abc"}).status_code == 400
    assert post(client, "/setup/pay", {**form, "pay.mid_rate_min": "30"}).status_code == 400
    r = post(client, "/setup/pay", {**form, "pay.fit_min": "70"})
    assert r.status_code == 200 and app.state.settings.get("pay.fit_min") == 70


def test_schedule_validation_and_save(wiz):
    app, client = wiz
    form = schedule_form(app)
    assert post(client, "/setup/schedule", {**form, "schedules.daily": "nope"}).status_code == 400
    r = post(client, "/setup/schedule", {**form, "general.timezone": "Mars/Base"})
    assert r.status_code == 400
    r = post(client, "/setup/schedule", {**form, "schedules.daily": "0 8 * * *"})
    assert r.status_code == 200
    assert app.state.settings.get("schedules.daily") == "0 8 * * *"
    assert app.state.settings.get("general.timezone") == "Europe/Berlin"


def test_skip_only_optional(wiz):
    app, client = wiz
    assert post(client, "/setup/skip/pay").status_code == 404
    assert post(client, "/setup/skip/email").status_code == 200
    assert "email" in app.state.settings.get("setup.done_steps")


def test_dryrun_step_turns_it_on_never_off(wiz):
    app, client = wiz
    app.state.settings.set("general.dry_run", False)
    assert post(client, "/setup/dryrun").status_code == 200
    assert app.state.settings.get("general.dry_run") is True


def test_link_steps_detect_state(wiz):
    app, client = wiz
    html = client.get("/setup").text
    assert 'id="step-upwork"' in html
    app.state.settings.set("upwork.talent_org_uid", "1000000000000000001")
    memory_file(app.state.config, "USER.md").parent.mkdir(parents=True, exist_ok=True)
    memory_file(app.state.config, "USER.md").write_text("I build automations.\n")
    from app.setup_routes import done_keys

    class R:  # a request stand-in with just app.state
        pass

    r = R()
    r.app = app
    assert {"upwork", "profile"} <= done_keys(r)


# ---------- end to end ----------


def test_wizard_end_to_end_ends_with_dry_run_on(config, fake_claude, monkeypatch):
    """Fresh data dir, only env filled: admin, Claude, Upwork, profile, keywords, pay, style,
    email skipped, schedule, dry-run, finish."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    app, client = make_client(config)
    app.state.worker.sync = True
    assert client.get("/setup").status_code == 200
    setup_admin(client)
    s = app.state.settings
    assert s.get("general.dry_run") is True

    assert post(client, "/setup/claude").status_code == 200
    # Steps 3 and 4 use their own pages (tested elsewhere); put their results in place.
    s.set("upwork.talent_org_uid", "1000000000000000001")
    memory_file(config, "USER.md").parent.mkdir(parents=True, exist_ok=True)
    memory_file(config, "USER.md").write_text("I build automations.\n")
    assert post(client, "/setup/keywords", {"keywords": "n8n\nZapier"}).status_code == 200
    assert post(client, "/setup/pay", pay_form(app)).status_code == 200
    assert post(client, "/setup/style").status_code == 200
    assert post(client, "/setup/skip/email").status_code == 200
    assert post(client, "/setup/schedule", schedule_form(app)).status_code == 200
    assert post(client, "/setup/dryrun").status_code == 200

    r = post(client, "/setup/finish")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert s.get("setup.finished") is True
    assert s.get("general.dry_run") is True
    assert "tok-not-real" not in str(s.all())


def test_claude_check_returns_at_once_and_runs_later(wiz, fake_claude, monkeypatch):
    app, client = wiz
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    fake_claude.mode("ok")
    app.state.worker.sync = False
    held = []

    class Held:
        def submit(self, fn, *args):
            held.append((fn, args))

    app.state.worker._pool = lambda: Held()
    token = csrf_from(client.get("/setup").text)
    r = client.post("/setup/claude", data={"csrf_token": token})
    assert r.status_code == 303 and "action=" in r.headers["location"]
    assert "claude" not in app.state.settings.get("setup.done_steps")
    assert "Waiting" in client.get(r.headers["location"]).text
    again = client.post("/setup/claude", data={"csrf_token": token})
    assert again.headers["location"] == r.headers["location"] and len(held) == 1
    held[0][0](*held[0][1])
    assert "claude" in app.state.settings.get("setup.done_steps")
    assert "Claude works." in client.get(r.headers["location"]).text


# ---------- Claude token step, setup bar (round 4, E3) ----------

TOKEN = "tok-abcdefgh-wxyz"


def stored_raw(app, key="claude.oauth_token_enc"):
    return app.state.settings.get(key)


def test_token_saved_encrypted_used_by_claude_env_never_shown(wiz, fake_claude, monkeypatch):
    from app.llm.runner import claude_env

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app, client = wiz
    fake_claude.mode("ok")
    r = post(client, "/setup/claude", {"oauth_token": TOKEN, "api_key": "sk-ant-key-1234"})
    assert "Claude works." in r.text
    raw = stored_raw(app)
    assert raw and TOKEN not in raw and "sk-ant" not in stored_raw(app, "claude.api_key_enc")
    env = claude_env(app.state.config, app.state.settings)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and env["ANTHROPIC_API_KEY"] == "sk-ant-key-1234"
    for page in ("/setup?step=claude", "/settings"):
        html = client.get(page).text
        assert TOKEN not in html and "sk-ant-key-1234" not in html and raw not in html
        assert "ends with ...wxyz" in html
    export = client.get("/settings/export.json").text
    assert raw not in export and TOKEN not in export


def test_env_token_wins_over_saved(wiz, monkeypatch):
    from app import claude_auth
    from app.llm.runner import claude_env

    app, _ = wiz
    claude_auth.save(app.state.settings, app.state.config, claude_auth.TOKEN_KEY, TOKEN)
    from dataclasses import replace

    cfg = replace(app.state.config, claude_code_oauth_token="env-token-wins")
    assert claude_env(cfg, app.state.settings)["CLAUDE_CODE_OAUTH_TOKEN"] == "env-token-wins"


def test_settings_replace_and_remove(wiz):
    app, client = wiz
    token = csrf_from(client.get("/settings").text)
    client.post("/settings/claude", data={"csrf_token": token, "oauth_token": TOKEN})
    assert stored_raw(app)
    client.post("/settings/claude", data={"csrf_token": token, "remove": "token"})
    assert stored_raw(app) == ""
    assert "ends with" not in client.get("/settings").text


def test_check_message_does_not_mention_env_file(wiz, fake_claude, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _, client = wiz
    r = post(client, "/setup/claude")
    assert "NO TOKEN" in r.text and ".env" not in r.text


def test_setup_bar_shows_until_finished(wiz):
    app, client = wiz
    html = client.get("/").text
    assert 'class="setup-bar"' in html and "Setup 0/" in html
    app.state.settings.set("setup.finished", True)
    assert 'class="setup-bar"' not in client.get("/").text


def test_setup_claude_step_passes_saved_token_to_child(wiz, fake_claude, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _, client = wiz
    fake_claude.mode("ok")
    r = post(client, "/setup/claude", {"oauth_token": TOKEN})
    assert "Claude works." in r.text
    log = fake_claude.log()
    assert log["has_oauth"] and TOKEN not in str(log)
