"""No long running text: a <p> outside .notice, .warning and .empty is at most MAX_CHARS long.

Longer words belong in an "i" tooltip (app/help.toml). Later phases add their pages to PAGES.
"""

from html.parser import HTMLParser

import pytest

from app import approvals
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin

MAX_CHARS = 120
EXEMPT_CLASSES = {"notice", "warning", "empty", "empty-state", "flash"}
VOID = {"br", "hr", "img", "input", "meta", "link", "use", "path", "circle", "line", "rect"}

# Add a path here when a page is redesigned. Each page is fetched after `seed` ran.
PAGES = ["/", "/approvals"]


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
        "INSERT INTO jobs(job_id, first_seen, title, status, tier, fit_score) "
        "VALUES ('9000000000000000001', datetime('now'), 'n8n CRM flow', 'candidate', 'HIGH', 80)"
    )


@pytest.fixture
def pages(config):
    app, client = make_web_client(config)
    setup_admin(client)
    seed(app)
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
