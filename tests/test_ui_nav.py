"""Shell: five main items plus a gear menu, status pills, active item, toast."""

import re

import pytest

from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def main_nav(html):
    return re.search(r'<nav aria-label="Main".*?</nav>', html, re.S).group(0)


def active(html):
    pat = r'aria-current="page">(?:<svg.*?</svg>)?<span class="nav-label">([^<]+)'
    return re.findall(pat, main_nav(html), re.S)


def test_at_most_five_main_items_and_a_gear_menu(web):
    _, client = web
    html = client.get("/").text
    assert len(re.findall(r"<a ", main_nav(html))) <= 5
    for href in ("/settings", "/settings/email", "/upwork", "/runs", "/setup"):
        assert f'<a href="{href}">' in html.split('class="gear"')[1]
    assert 'action="/logout"' in html and 'name="csrf_token"' in html


@pytest.mark.parametrize(
    "path,label",
    [
        ("/", "Home"),
        ("/jobs", "Jobs"),
        ("/review", "Jobs"),
        ("/approvals", "To do"),
        ("/inbox", "Inbox"),
        ("/context", "Growth"),
        ("/profile/versions", "Growth"),
        ("/playbook", "Growth"),
        ("/trends", "Growth"),
        ("/reports", "Growth"),
        ("/learning", "Growth"),
    ],
)
def test_active_item(web, path, label):
    _, client = web
    assert active(client.get(path).text) == [label]


@pytest.mark.parametrize("path", ["/settings", "/settings/email", "/upwork", "/runs", "/setup"])
def test_gear_pages_have_no_active_main_item(web, path):
    _, client = web
    r = client.get(path)
    assert r.status_code == 200 and active(r.text) == []


def test_tabs_on_jobs_and_growth_pages(web):
    _, client = web
    assert 'class="tabs"' in client.get("/jobs").text
    assert 'class="tabs"' in client.get("/review").text
    for p in ("/context", "/profile/versions", "/playbook", "/trends", "/reports", "/learning"):
        assert 'class="tabs"' in client.get(p).text


def test_dry_run_and_stopped_pills(web):
    app, client = web
    app.state.settings.set("general.dry_run", True)
    app.state.settings.set("general.kill_switch", False)
    html = client.get("/").text
    assert ">Dry-run</a>" in html and ">Stopped</a>" not in html
    app.state.settings.set("general.dry_run", False)
    app.state.settings.set("general.kill_switch", True)
    html = client.get("/").text
    assert ">Dry-run</a>" not in html and ">Stopped</a>" in html


def test_notice_from_msg(web):
    _, client = web
    html = client.get("/?msg=Saved").text
    assert "data-notice" in html and "Saved" in html and "notice flash ok" in html
    assert "notice flash error" in client.get("/?msg=Bad&err=1").text
    assert "data-notice" not in client.get("/").text
