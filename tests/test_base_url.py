"""The panel address: env, then setting, then the first real request. No real calls."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import mail
from app.base_url import detect_origin, get_base_url
from app.db import init_db
from app.main import create_app
from app.settings import Settings
from app.upwork.oauth import UpworkOAuth


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


@pytest.fixture
def settings(conn):
    return Settings(conn)


@pytest.fixture
def bare(config):
    return replace(config, public_base_url="")


def test_env_wins_over_setting(settings, config):
    settings.set("general.public_base_url", "https://saved.example")
    assert get_base_url(settings, config) == "https://test.example"


def test_setting_used_without_env(settings, bare):
    settings.set("general.public_base_url", "https://saved.example/")
    assert get_base_url(settings, bare) == "https://saved.example"


def test_empty_when_nothing_known(settings, bare):
    assert get_base_url(settings, bare) == ""
    assert get_base_url(None, bare) == ""


def make(bare, base_url="http://testserver"):
    app = create_app(bare)
    return app, TestClient(app, base_url=base_url, follow_redirects=False)


def test_first_request_origin_is_saved(bare):
    app, c = make(bare, "http://panel.lan:8000")
    c.get("/login")
    assert get_base_url(app.state.settings, bare) == "http://panel.lan:8000"


def test_forwarded_headers_are_respected(bare):
    app, c = make(bare)
    c.get("/login", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "up.example.com"})
    assert get_base_url(app.state.settings, bare) == "https://up.example.com"


def test_first_origin_is_kept(bare):
    app, c = make(bare, "http://one.example")
    c.get("/login")
    c.get("/login", headers={"X-Forwarded-Host": "two.example"})
    assert get_base_url(app.state.settings, bare) == "http://one.example"


def test_health_check_and_static_do_not_set_it(bare):
    app, c = make(bare, "http://127.0.0.1:8000")
    c.get("/healthz")
    c.get("/static/app.css")
    assert get_base_url(app.state.settings, bare) == ""


def test_env_value_is_not_overwritten(config):
    app, c = make(config, "http://other.example")
    c.get("/login")
    assert app.state.settings.get("general.public_base_url") == ""
    assert get_base_url(app.state.settings, config) == "https://test.example"


def test_bad_host_is_ignored(bare):
    app, c = make(bare)
    c.get("/login", headers={"X-Forwarded-Host": "evil.example/<x>"})
    assert get_base_url(app.state.settings, bare) == ""


def test_detect_origin_takes_first_forwarded_value():
    class R:
        headers = {"x-forwarded-proto": "https, http", "x-forwarded-host": "a.example, b.example"}

        class url:
            scheme = "http"

    assert detect_origin(R) == "https://a.example"


def test_oauth_and_gmail_use_the_saved_address(conn, settings, bare):
    settings.set("general.public_base_url", "https://saved.example")
    oauth = UpworkOAuth(bare, settings, conn)
    assert oauth.redirect_uri == "https://saved.example/oauth/callback"
    assert oauth.client_id == "https://saved.example/oauth/client-metadata.json"


def test_emails_use_the_saved_address(conn, settings, bare, monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send_event", lambda *a, **k: sent.append(a) or "sent")
    mail.notify_connects_low(conn, bare, "Low", "2026-01-01")
    assert "http" not in sent[0][5].replace("w3.org", "")  # no link: address unknown
    settings.set("general.public_base_url", "http://panel.lan:8000")
    mail.notify_connects_low(conn, bare, "Low", "2026-01-02")
    assert "http://panel.lan:8000/" in sent[1][4]  # plain text body
