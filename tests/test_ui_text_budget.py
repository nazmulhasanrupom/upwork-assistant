"""No long running text: a <p> outside .notice, .warning and .empty is at most MAX_CHARS long.

Longer words belong in an "i" tooltip (app/help.toml). Later phases add their pages to PAGES.
"""

import json
from html.parser import HTMLParser

import pytest

from app import approvals
from app.setup_routes import STEPS
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client

STEP_KEYS = [st["key"] for st in STEPS]
MAX_CHARS = 120
EXEMPT_CLASSES = {"notice", "warning", "empty", "empty-state", "flash"}
VOID = {"br", "hr", "img", "input", "meta", "link", "use", "path", "circle", "line", "rect"}

# Add a path here when a page is redesigned. Each page is fetched after `seed` ran.
PAGES = ["/", "/approvals", "/jobs", "/jobs/9000000000000000001", "/review",
         "/inbox", "/context", "/profile/versions", "/playbook", "/trends", "/reports", "/learning",
         "/settings", "/settings/email", "/upwork", "/runs", "/setup", "/setup?step=finish"]
PAGES += [f"/setup?step={key}" for key in STEP_KEYS]


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []  # (tag, exempt)
        self.buf: list[str] | None = None
        self.exempt_p = False
        self.found: list[str] = []
        self.skip = 0  # inside the tooltip spans, which hold help text

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        classes = set((dict(attrs).get("class") or "").split())
        role = dict(attrs).get("role")
        exempt = bool(classes & EXEMPT_CLASSES) or role == "tooltip"
        self.stack.append((tag, exempt))
        if tag == "p":
            self.buf = []
            self.exempt_p = any(e for _, e in self.stack)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == "p" and self.buf is not None:
            text = " ".join("".join(self.buf).split())
            if not self.exempt_p and len(text) > MAX_CHARS:
                self.found.append(text)
            self.buf = None
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if self.buf is not None:
            self.buf.append(data)


def long_paragraphs(html: str) -> list[str]:
    parser = Paragraphs()
    parser.feed(html)
    return parser.found


def seed(app):
    """Fake data so the busy states render too."""
    conn = app.state.db
    approvals.add(conn, "keywords", "Add keyword n8n expert", after=["n8n expert"])
    approvals.add(conn, "profile", "New title", before="Old", after="New")
    conn.execute(
        "INSERT INTO jobs(job_id, first_seen, title, status, tier) "
        "VALUES ('9000000000000000001', datetime('now'), 'n8n CRM flow', 'candidate', 'HIGH')"
    )
    conn.execute("INSERT INTO judgements(job_id, fit_score) VALUES ('9000000000000000001', 80)")


@pytest.fixture
def pages(config):
    app, client = make_web_client(config)
    setup_admin(client)
    seed(app)
    client.app_state = app.state
    return client


def test_checker_flags_long_text():
    assert long_paragraphs("<p>" + "x" * 121 + "</p>")
    assert not long_paragraphs("<p>" + "x" * 120 + "</p>")
    assert not long_paragraphs('<div class="notice"><p>' + "x" * 300 + "</p></div>")
    assert not long_paragraphs('<div class="empty-state"><p>' + "x" * 300 + "</p></div>")


@pytest.mark.parametrize("path", PAGES)
def test_no_long_paragraphs(pages, path):
    r = pages.get(path)
    assert r.status_code == 200
    assert long_paragraphs(r.text) == []


def test_review_result_has_no_long_paragraphs(pages):
    result = {
        "issues": [{"check": "Opening", "problem": "Starts with I.", "fix": "Lead with them."}],
        "fixed_version": {"cover_letter": "Bots. Hello there.", "answers": []},
        "instructions_found": ["Start with the word Bots"],
        "instructions_missed": [],
        "first_two_lines_ok": True,
        "proof_found": "12 n8n flows",
    }
    original = {"cover_letter": "Hello there.", "answers": [], "job_title": "n8n CRM flow"}
    pages.app_state.db.execute(
        "INSERT INTO reviews(job_id, job_text_hash, original, prechecks, result, score, model, "
        "cost, created_at) VALUES (NULL, 'h', ?, '[]', ?, 72, 'm', 0.01, datetime('now'))",
        (json.dumps(original), json.dumps(result)),
    )
    r = pages.get("/review/1")
    assert r.status_code == 200 and 'id="score-badge"' in r.text
    assert long_paragraphs(r.text) == []


def test_settings_sections_have_no_long_paragraphs(pages):
    names = ["general", "pay", "search"]
    for name in names:
        r = pages.get(f"/settings?section={name}")
        assert r.status_code == 200 and long_paragraphs(r.text) == []


def test_run_log_has_no_long_paragraphs(pages):
    logs = pages.app_state.config.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "daily-1.log").write_text("line one\n")
    r = pages.get("/runs/log/daily-1.log")
    assert r.status_code in (200, 404)
    assert long_paragraphs(r.text) == []


def test_gmail_result_has_no_long_paragraphs(pages):
    r = pages.get("/gmail/callback?error=access_denied&state=x")
    assert r.status_code == 400 and long_paragraphs(r.text) == []


def test_login_page_has_no_long_paragraphs(config):
    app, client = make_web_client(config)
    setup_admin(client)
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/").text)})
    r = client.get("/login")
    assert r.status_code == 200 and 'name="code"' in r.text and 'name="username"' in r.text
    assert long_paragraphs(r.text) == []
    bad = client.post("/login", data={"username": "x", "password": "y", "code": "1",
                                       "csrf_token": csrf_from(r.text)})
    assert bad.status_code == 401 and "Wrong user name" in bad.text
    assert long_paragraphs(bad.text) == []


def test_setup_admin_and_totp_have_no_long_paragraphs(config):
    app, client = make_web_client(config)
    r = client.get("/setup")
    assert r.status_code == 200 and "step 1 of 10" in r.text and long_paragraphs(r.text) == []
    token = csrf_from(r.text)
    client.post("/setup/account", data={"username": "u", "password": "a-long-password-12",
                                        "password2": "a-long-password-12",
                                        "setup_code": client.setup_code, "csrf_token": token})
    r = client.get("/setup/totp")
    assert r.status_code == 200 and long_paragraphs(r.text) == []


def test_report_detail_has_no_long_paragraphs(pages):
    pages.app_state.db.execute(
        "INSERT INTO reports(kind, period, created_at, title, body) "
        "VALUES ('weekly', '2026-W01', datetime('now'), 'Week', '# Head\n\n- a\n- b\n\ntext')"
    )
    r = pages.get("/reports/1")
    assert r.status_code == 200 and long_paragraphs(r.text) == []
