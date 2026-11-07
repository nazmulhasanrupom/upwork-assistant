"""Settings page /settings (plan J3): typed save, switches, export, delete all data."""

import json
import re
from pathlib import Path

import pyotp
import pytest

from app import settings_routes as sr
from app.settings import load_defaults
from tests.test_auth import PASSWORD, csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    client.totp = setup_admin(client, totp=True)  # the delete form then asks for a code too
    return app, client


def post(client, path, **data):
    token = csrf_from(client.get("/settings").text)
    return client.post(path, data={"csrf_token": token, **data})


# ---------- page ----------


def test_needs_login(config):
    _, client = make_web_client(config)
    assert client.get("/settings").status_code == 303


def test_every_default_is_shown_or_secret(web):
    app, client = web
    html = client.get("/settings").text
    for key in load_defaults():
        if sr.is_secret(key):
            assert f'name="{key}"' not in html and f'id="ro-{key}"' not in html
        elif sr.is_readonly(key):
            assert f'id="ro-{key}"' in html, key
        else:
            assert f'name="{key}"' in html, key


def test_secrets_never_shown(web):
    app, client = web
    app.state.settings.set("email.smtp_password", "hunter2-secret-value")
    app.state.settings.set("email.gmail_client_secret_enc", "ENCRYPTEDBLOB")
    html = client.get("/settings").text
    assert "hunter2-secret-value" not in html and "ENCRYPTEDBLOB" not in html
    assert "smtp_password" not in html


def test_switches_at_top_and_toggle(web):
    app, client = web
    html = client.get("/settings").text
    assert html.index('id="switches"') < html.index('id="section-')
    assert "Dry-run is ON" in html and "Kill switch is OFF" in html
    r = post(client, "/settings/toggle/kill_switch", value="on")
    assert "Kill switch is ON" in r.text and app.state.settings.get("general.kill_switch") is True
    r = post(client, "/settings/toggle/dry_run", value="off")
    assert app.state.settings.get("general.dry_run") is False
    r = post(client, "/settings/toggle/dry_run", value="on")
    assert app.state.settings.get("general.dry_run") is True
    assert post(client, "/settings/toggle/nope", value="on").status_code == 404


def test_writes_need_csrf(web):
    _, client = web
    assert client.post("/settings/save/pay", data={"pay.fit_min": "50"}).status_code == 403
    assert client.post("/settings/toggle/dry_run", data={"value": "off"}).status_code == 403
    assert (
        client.post("/settings/delete-all", data={"phrase": sr.CONFIRM_PHRASE}).status_code == 403
    )


# ---------- save and validate ----------


def test_save_section_typed_values(web):
    app, client = web
    st = app.state.settings
    r = post(
        client,
        "/settings/save/pay",
        **{"pay.fit_min": "70", "pay.good_spending": "6000"},
    )
    assert r.status_code == 200 and "Saved the pay settings" in r.text
    assert st.get("pay.fit_min") == 70 and st.get("pay.good_spending") == 6000
    r = post(client, "/settings/save/claude", **{"claude.think_max_budget_usd": "1.25"})
    assert st.get("claude.think_max_budget_usd") == 1.25
    r = post(client, "/settings/save/search", **{"search.keywords": "n8n\n Make.com \n\nzapier"})
    assert st.get("search.keywords") == ["n8n", "Make.com", "zapier"]
    r = post(client, "/settings/save/profile_loop", **{"profile_loop.writable_tactics": "1, 2\n5"})
    assert st.get("profile_loop.writable_tactics") == [1, 2, 5]


def test_bool_checkbox_pattern(web):
    app, client = web
    st = app.state.settings
    assert st.get("weekly.enabled") is True
    token = csrf_from(client.get("/settings").text)
    # unchecked box: only the hidden 0 is sent; checked: 0 then 1
    client.post("/settings/save/weekly", data={"csrf_token": token, "weekly.enabled": "0"})
    assert st.get("weekly.enabled") is False
    client.post("/settings/save/weekly", data={"csrf_token": token, "weekly.enabled": ["0", "1"]})
    assert st.get("weekly.enabled") is True


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("pay.fit_min", "abc"),
        ("pay.fit_min", "1.5"),
        ("pay.fit_min", "-1"),
        ("pay.fit_min", "101"),
        ("pay.good_spending", ""),
        ("claude.think_max_budget_usd", "nan"),
        ("claude.think_max_budget_usd", "-2"),
        ("security.session_hours", "0"),
        ("security.password_min_length", "3"),
        ("schedules.daily", "not a cron"),
        ("general.timezone", "Mars/Base"),
        ("profile_loop.writable_tactics", "1, x"),
        ("pay.good_spending", "99999999999"),
    ],
)
def test_invalid_values_refused_and_nothing_saved(web, key, bad):
    app, client = web
    st = app.state.settings
    before = st.get(key)
    r = post(client, f"/settings/save/{key.split('.')[0]}", **{key: bad})
    assert r.status_code == 400 and "Nothing was saved" in r.text
    assert st.get(key) == before
    assert bad == "" or f"{bad}" in r.text  # the wrong input is kept in the form


def test_one_bad_value_blocks_the_whole_section(web):
    app, client = web
    st = app.state.settings
    r = post(client, "/settings/save/pay", **{"pay.fit_min": "55", "pay.good_spending": "nope"})
    assert r.status_code == 400
    assert st.get("pay.fit_min") == load_defaults()["pay.fit_min"]


def test_valid_cron_and_timezone_saved(web):
    app, client = web
    post(client, "/settings/save/schedules", **{"schedules.daily": "0 6 * * *"})
    assert app.state.settings.get("schedules.daily") == "0 6 * * *"
    post(client, "/settings/save/general", **{"general.timezone": "Europe/Berlin"})
    assert app.state.settings.get("general.timezone") == "Europe/Berlin"


def test_secret_and_readonly_keys_cannot_be_saved(web):
    app, client = web
    st = app.state.settings
    post(
        client,
        "/settings/save/email",
        **{"email.smtp_password": "x", "email.gmail_address": "someone@example.com"},
    )
    assert st.get("email.smtp_password") == ""
    assert st.get("email.gmail_address") == ""
    post(client, "/settings/save/setup", **{"setup.finished": "1"})
    assert st.get("setup.finished") is False
    assert post(client, "/settings/save/nosuchsection", x="1").status_code == 404
    assert post(client, "/settings/save/scheduler", x="1").status_code == 404  # all read-only


def test_parse_value_unit():
    assert sr.parse_value("a.b", 3, " 7 ") == 7
    assert sr.parse_value("a.b", 3.0, "2") == 2.0
    assert sr.parse_value("a.b", "x", "  a   b ") == "a b"
    assert sr.parse_value("a.b", True, "1") is True
    assert sr.parse_value("a.b", -1, "-5") == -5
    with pytest.raises(sr.Invalid):
        sr.parse_value("a.b", 3, "-5")
    with pytest.raises(sr.Invalid):
        sr.parse_value("a.b", "x", "y" * 5000)


# ---------- export ----------


def test_export_has_settings_and_counts_but_no_secrets(web, config):
    app, client = web
    st = app.state.settings
    st.set("email.smtp_password", "hunter2-secret-value")
    st.set("email.gmail_client_secret_enc", "ENCRYPTEDBLOB")
    st.set("pay.fit_min", 77)
    (config.data_dir / "tokens.enc").write_text("TOKENBLOB")
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("my private facts")
    r = client.get("/settings/export.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["settings"]["pay.fit_min"] == 77
    assert body["data_summary"]["files"]["memory"] >= 1
    assert "jobs" in body["data_summary"]["tables"]
    assert "admin" not in body["data_summary"]["tables"]
    text = r.text
    for leak in ("hunter2-secret-value", "ENCRYPTEDBLOB", "TOKENBLOB", "my private facts"):
        assert leak not in text
    assert "email.smtp_password" not in body["settings"]
    json.loads(text)


# ---------- path guard ----------


def test_inside_accepts_children_only(tmp_path):
    root = tmp_path / "data" / "x"
    root.mkdir(parents=True)
    child = root / "memory"
    assert sr.inside(root, child) == root.resolve() / "memory"
    for bad in (root, root.parent, root / ".." / "other", Path("/etc/passwd"), Path("/")):
        with pytest.raises(sr.UnsafePath):
            sr.inside(root, bad)


def test_inside_refuses_a_root_near_the_file_system_root():
    with pytest.raises(sr.UnsafePath):
        sr.inside(Path("/"), Path("/etc"))
    with pytest.raises(sr.UnsafePath):
        sr.inside(Path("/data"), Path("/data/x"))  # too shallow to be a safe target


def test_wipe_files_keeps_db_and_never_follows_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    data = tmp_path / "work" / "data"
    (data / "memory" / ".git").mkdir(parents=True)
    (data / "memory" / "USER.md").write_text("x")
    (data / "runs").mkdir()
    (data / "runs" / "r1.json").write_text("{}")
    (data / "tokens.enc").write_text("t")
    (data / "app.db").write_text("db")
    (data / "link_to_dir").symlink_to(outside, target_is_directory=True)
    (data / "link_to_file").symlink_to(outside / "precious.txt")
    (data / "memory" / "inner_link").symlink_to(outside, target_is_directory=True)
    removed = sr.wipe_files(data)
    assert removed == 5
    assert sorted(p.name for p in data.iterdir()) == ["app.db"]
    assert (outside / "precious.txt").read_text() == "keep me"  # untouched
    assert data.is_dir()


def test_wipe_files_missing_dir_is_fine(tmp_path):
    assert sr.wipe_files(tmp_path / "a" / "b" / "nope") == 0


# ---------- delete all data ----------


def seed(app, config):
    app.state.settings.set("pay.fit_min", 80)
    app.state.settings.set("setup.finished", True)
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("private")
    (config.data_dir / "runs").mkdir(exist_ok=True)
    (config.data_dir / "runs" / "x.json").write_text("{}")
    (config.data_dir / "tokens.enc").write_text("tok")
    (config.data_dir / "gmail.enc").write_text("tok")


def count(app, table):
    return app.state.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def wipe(client, **over):
    code = pyotp.TOTP(client.totp).now() if getattr(client, "totp", "") else ""
    data = {"phrase": sr.CONFIRM_PHRASE, "password": PASSWORD, "code": code}
    data.update(over)
    return post(client, "/settings/delete-all", **data)


def test_delete_with_password(web, config):
    app, client = web
    seed(app, config)
    r = wipe(client)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert (
        sorted(p.name for p in config.data_dir.iterdir() if not p.name.startswith("app.db")) == []
    )
    assert count(app, "admin") == 0 and count(app, "settings") == 0
    assert app.state.settings.get("pay.fit_min") == load_defaults()["pay.fit_min"]
    assert count(app, "auth_state") == 1  # fixed row kept so login works again
    # logged out, and the wizard opens again
    assert client.get("/settings").headers["location"] == "/setup"
    page = client.get("/setup")
    assert page.status_code == 200 and "/setup/account" in page.text


def test_delete_with_totp_needs_password_and_code(web, config):
    app, client = web
    seed(app, config)
    code = pyotp.TOTP(client.totp).now
    for pw, c in (("", code()), (PASSWORD, ""), (PASSWORD, "000000"), ("wrong wrong", code())):
        assert wipe(client, password=pw, code=c).status_code == 403
    assert count(app, "admin") == 1 and (config.data_dir / "tokens.enc").exists()
    r = wipe(client, password=PASSWORD, code=code())
    assert r.status_code == 303
    assert count(app, "admin") == 0 and not (config.data_dir / "tokens.enc").exists()


def test_delete_without_totp_needs_only_the_password(config):
    app, client = make_web_client(config)
    setup_admin(client)
    assert 'name="code"' not in client.get("/settings").text.split("delete-form")[1]
    assert wipe(client, password="wrong wrong").status_code == 403
    assert wipe(client, password="").status_code == 403
    assert count(app, "admin") == 1
    assert wipe(client).status_code == 303 and count(app, "admin") == 0


def test_after_delete_the_next_visitor_sees_create_account(web, config):
    app, client = web
    wipe(client, code=pyotp.TOTP(client.totp).now())
    client.cookies.clear()
    page = client.get("/setup")
    assert page.status_code == 200 and "Create your account" in page.text
    assert "setup_code" not in page.text
    setup_admin(client)  # a new owner, no code from any log
    assert count(app, "admin") == 1


def test_wrong_phrase_deletes_nothing(web, config):
    app, client = web
    seed(app, config)
    for phrase in ("", "delete all my data", "DELETE"):
        r = wipe(client, phrase=phrase)
        assert r.status_code == 400
    assert count(app, "admin") == 1 and (config.data_dir / "tokens.enc").exists()


def test_wrong_password_and_code_delete_nothing(web, config):
    app, client = web
    seed(app, config)
    assert wipe(client, password="wrong password!!", code="").status_code == 403
    assert wipe(client, password="", code="000000").status_code == 403
    assert wipe(client, password="", code="").status_code == 403
    assert count(app, "admin") == 1 and (config.data_dir / "tokens.enc").exists()
    assert app.state.settings.get("pay.fit_min") == 80


def test_wrong_tries_lock_the_check(web, config):
    app, client = web
    seed(app, config)
    tries = app.state.settings.get("security.max_login_tries")
    for _ in range(tries):
        wipe(client, password="wrong password!!")
    r = wipe(client)  # correct now, but locked
    assert r.status_code == 429
    assert count(app, "admin") == 1


def test_delete_needs_login(config):
    app, client = make_web_client(config)
    r = client.post("/settings/delete-all", data={"phrase": sr.CONFIRM_PHRASE})
    assert r.status_code in (303, 403)
    assert count(app, "settings") >= 0


def test_unsafe_data_dir_refuses_and_keeps_data(web, config, monkeypatch):
    app, client = web
    seed(app, config)

    def boom(data_dir, keep=()):
        raise sr.UnsafePath("outside the data folder")

    monkeypatch.setattr(sr, "wipe_files", boom)
    r = wipe(client)
    assert r.status_code == 400 and "outside the data folder" in r.text
    assert count(app, "admin") == 1  # DB untouched when the file step is refused


def test_wipe_db_clears_fts_and_keeps_schema(web):
    app, client = web
    db = app.state.db
    db.execute("INSERT INTO search_idx(kind, ref, body) VALUES ('note','d','hello')")
    sr.wipe_db(db)
    assert count(app, "search_idx") == 0
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 21


def test_no_secret_word_in_page_for_phrase_regex(web):
    html = web[1].get("/settings").text
    assert re.search(r"Type <code>DELETE ALL MY DATA</code>", html)
