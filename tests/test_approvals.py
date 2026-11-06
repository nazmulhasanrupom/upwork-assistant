"""Approvals queue /approvals (plan H3). No Upwork, no Claude."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app import approvals
from app.upwork import writes
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def post(client, path, **data):
    token = csrf_from(client.get("/approvals").text)
    return client.post(path, data={"csrf_token": token, **data})


def iso(minutes_ago=0, days_ago=0):
    dt = datetime.now(UTC) - timedelta(minutes=minutes_ago, days=days_ago)
    return dt.isoformat(timespec="seconds")


def add_job(conn, job_id, title="n8n CRM flow"):
    conn.execute(
        "INSERT INTO jobs(job_id, first_seen, title, status) VALUES (?, ?, ?, 'drafted')",
        (job_id, iso(), title),
    )


def add_preview(conn, job_id, status="previewed", minutes_ago=2, confirmed=False, cost=16):
    cur = conn.execute(
        "INSERT INTO previews(kind, job_id, preview_id, request_json, connects_cost, "
        "connects_balance, created_at, status, confirmed_at) VALUES "
        "('create', ?, ?, '{}', ?, 40, ?, ?, ?)",
        (
            job_id,
            f"pv-{job_id}-{minutes_ago}-{status}",
            cost,
            iso(minutes_ago),
            status,
            iso() if confirmed else None,
        ),
    )
    return cur.lastrowid


def row(conn, item_id):
    return conn.execute("SELECT * FROM approvals WHERE id = ?", (item_id,)).fetchone()


@pytest.fixture(autouse=True)
def clean_handlers(monkeypatch):
    monkeypatch.setattr(approvals, "HANDLERS", {})


# ---------- page ----------


def test_needs_login(config):
    _, client = make_web_client(config)
    setup_admin(client)
    client.cookies.clear()
    r = client.get("/approvals")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_empty_page_and_nav(web):
    _, client = web
    r = client.get("/approvals")
    assert r.status_code == 200
    assert "Nothing is waiting for you" in r.text
    assert 'href="/approvals"' in r.text and 'aria-current="page"' in r.text
    assert 'nav-label">To do' in r.text
    assert "nav-count" not in r.text  # nothing pending: no number


def test_pending_count_and_nav_number(web):
    app, client = web
    conn, settings = app.state.db, app.state.settings
    assert approvals.pending_count(conn, settings) == 0
    add_job(conn, "4000000000000000001")
    add_preview(conn, "4000000000000000001")
    approvals.add(conn, "keywords", "Add keyword n8n expert", after=["n8n expert"])
    approvals.add(conn, "profile", "New title", before="Old", after="New")
    assert approvals.pending_count(conn, settings) == 3
    r = client.get("/")
    assert '<span class="nav-count" aria-label="3 waiting">3</span>' in r.text


def test_nav_count_drops_after_decision(web):
    app, client = web
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    assert 'aria-label="1 waiting"' in client.get("/").text
    post(client, f"/approvals/{item}/approve")
    assert "nav-count" not in client.get("/").text


# ---------- proposals ----------


def test_open_preview_is_listed_with_link_not_confirm(web, monkeypatch):
    app, client = web
    conn = app.state.db
    add_job(conn, "4000000000000000001")
    pid = add_preview(conn, "4000000000000000001")

    def boom(*a, **k):
        raise AssertionError("confirm_preview must never be called from /approvals")

    monkeypatch.setattr(writes, "confirm_preview", boom)
    r = client.get("/approvals")
    assert r.status_code == 200
    assert 'href="/jobs/4000000000000000001"' in r.text and "n8n CRM flow" in r.text
    assert "Review and approve" in r.text and "Preview ready" in r.text
    assert "/approve" not in r.text  # no POST form for a proposal
    assert conn.execute("SELECT status, confirmed_at FROM previews WHERE id=?", (pid,)).fetchone()[
        :
    ] == ("previewed", None)


def test_proposal_cannot_be_decided_here(web, monkeypatch):
    app, client = web
    conn = app.state.db
    add_job(conn, "4000000000000000001")
    pid = add_preview(conn, "4000000000000000001")
    monkeypatch.setattr(writes, "confirm_preview", lambda *a, **k: pytest.fail("confirmed"))
    # a proposal has no row in `approvals`: the generic route knows nothing about it
    r = post(client, f"/approvals/{pid}/approve")
    assert r.status_code == 409 and "does not exist" in r.text
    assert conn.execute("SELECT status FROM previews WHERE id=?", (pid,)).fetchone()[0] == (
        "previewed"
    )


def test_dry_run_blocked_preview_is_listed(web):
    app, client = web
    add_job(app.state.db, "4000000000000000001")
    add_preview(app.state.db, "4000000000000000001", status="dry_run_blocked")
    r = client.get("/approvals")
    assert "Blocked by dry-run" in r.text


def test_expired_previews_are_hidden(web):
    app, client = web
    conn = app.state.db
    for jid in ("4000000000000000001", "4000000000000000002", "4000000000000000003"):
        add_job(conn, jid, title=f"Job title {jid[-1]}")
    add_preview(conn, "4000000000000000001", minutes_ago=90)  # too old by time
    add_preview(conn, "4000000000000000002", status="expired")
    add_preview(conn, "4000000000000000003", status="confirmed", confirmed=True)
    r = client.get("/approvals")
    assert not any(f"Job title {n}" in r.text for n in "123")
    assert "Nothing is waiting for you" in r.text
    assert approvals.pending_count(conn, app.state.settings) == 0


def test_only_newest_preview_per_job(web):
    app, client = web
    conn = app.state.db
    add_job(conn, "4000000000000000001")
    add_preview(conn, "4000000000000000001", minutes_ago=10, cost=10)
    add_preview(conn, "4000000000000000001", status="confirmed", confirmed=True, minutes_ago=1)
    assert approvals.open_previews(conn, app.state.settings) == []
    add_job(conn, "4000000000000000002")
    add_preview(conn, "4000000000000000002", minutes_ago=10, cost=10)
    add_preview(conn, "4000000000000000002", minutes_ago=1, cost=12)
    items = approvals.open_previews(conn, app.state.settings)
    assert len(items) == 1 and items[0]["connects_cost"] == 12


def test_preview_without_job_row_still_links(web):
    app, _ = web
    add_preview(app.state.db, "4000000000000000009")
    [item] = approvals.open_previews(app.state.db, app.state.settings)
    assert item["link"] == "/jobs/4000000000000000009" and item["title"].startswith("Job ")


# ---------- generic items ----------


def test_generic_item_shows_before_after_escaped(web):
    app, client = web
    approvals.add(
        app.state.db,
        "profile",
        "Change <b>title</b>",
        ref_id="p1",
        before={"title": "Old"},
        after="New <script>x</script>",
    )
    r = client.get("/approvals")
    assert "Change &lt;b&gt;title&lt;/b&gt;" in r.text and "<b>title</b>" not in r.text
    assert "&lt;script&gt;x&lt;/script&gt;" in r.text
    assert '"title": "Old"' in r.text.replace("&#34;", '"')
    assert "Only recorded" in r.text or "only recorded" in r.text
    assert 'action="/approvals/1/approve"' in r.text and 'action="/approvals/1/reject"' in r.text


def test_approve_records_decision(web):
    app, client = web
    conn = app.state.db
    item = approvals.add(conn, "keywords", "Add keyword", after=["n8n"])
    r = post(client, f"/approvals/{item}/approve")
    assert r.status_code == 303 and r.headers["location"] == "/approvals?done=approved"
    got = row(conn, item)
    assert got["status"] == "approved" and got["decided_at"]
    page = client.get(r.headers["location"])
    assert "Approved. The decision is recorded." in page.text
    assert "Recently decided" in page.text and "Nothing is waiting for you" in page.text


def test_reject_records_decision(web):
    app, client = web
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    r = post(client, f"/approvals/{item}/reject")
    assert r.status_code == 303 and r.headers["location"] == "/approvals?done=rejected"
    assert row(app.state.db, item)["status"] == "rejected"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_decision_needs_csrf(web, action):
    app, client = web
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    r = client.post(f"/approvals/{item}/{action}")
    assert r.status_code == 403
    r = client.post(f"/approvals/{item}/{action}", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    assert row(app.state.db, item)["status"] == "pending"


def test_decision_needs_post_and_login(web, config):
    app, client = web
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    assert client.get(f"/approvals/{item}/approve").status_code == 405
    assert row(app.state.db, item)["status"] == "pending"
    client.cookies.clear()
    r = client.post(f"/approvals/{item}/approve", data={"csrf_token": "x"})
    assert r.status_code in (303, 403) and row(app.state.db, item)["status"] == "pending"


def test_decision_uses_issue_click(web, monkeypatch):
    app, client = web
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    seen = []
    real = writes.issue_click

    def spy(request, action):
        seen.append((request.method, action))
        return real(request, action)

    monkeypatch.setattr(writes, "issue_click", spy)
    post(client, f"/approvals/{item}/approve")
    assert seen == [("POST", "confirm")]


def test_second_decision_is_refused(web):
    app, client = web
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    post(client, f"/approvals/{item}/approve")
    r = post(client, f"/approvals/{item}/reject")
    assert r.status_code == 409 and "already decided (approved)" in r.text
    assert row(app.state.db, item)["status"] == "approved"


def test_unknown_item(web):
    _, client = web
    r = post(client, "/approvals/999/approve")
    assert r.status_code == 409 and "does not exist" in r.text


def test_registry_is_empty_and_unknown_kind_records_only(web):
    app, client = web
    assert approvals.HANDLERS == {}
    item = approvals.add(app.state.db, "never-heard-of", "Something new")
    r = post(client, f"/approvals/{item}/approve")
    assert r.status_code == 303 and row(app.state.db, item)["status"] == "approved"


def test_handler_runs_for_its_kind(web):
    app, client = web
    calls = []
    approvals.register("keywords", lambda it, decision, conn: calls.append((it["title"], decision)))
    a = approvals.add(app.state.db, "keywords", "Add keyword")
    b = approvals.add(app.state.db, "other", "Other thing")
    post(client, f"/approvals/{a}/approve")
    post(client, f"/approvals/{b}/approve")
    assert calls == [("Add keyword", "approved")]


def test_failing_handler_keeps_item_pending(web):
    app, client = web

    def bad(it, decision, conn):
        raise RuntimeError("disk full")

    approvals.register("keywords", bad)
    item = approvals.add(app.state.db, "keywords", "Add keyword")
    r = post(client, f"/approvals/{item}/approve")
    assert r.status_code == 409 and "could not be applied" in r.text
    got = row(app.state.db, item)
    assert got["status"] == "pending" and got["decided_at"] is None


def test_old_items_expire(web):
    app, client = web
    conn, settings = app.state.db, app.state.settings
    days = settings.get("approvals.expire_days")
    then = datetime.now(UTC) - timedelta(days=days + 1)
    old = approvals.add(conn, "keywords", "Old one", now=then)
    new = approvals.add(conn, "keywords", "New one")
    assert approvals.pending_count(conn, settings) == 1  # the old one is not counted
    r = client.get("/approvals")
    assert "New one" in r.text and row(conn, old)["status"] == "expired"
    assert row(conn, new)["status"] == "pending"
    assert post(client, f"/approvals/{old}/approve").status_code == 409


def test_before_after_json_stored(web):
    app, _ = web
    item = approvals.add(app.state.db, "profile", "T", before={"a": 1}, after=["x"])
    got = row(app.state.db, item)
    assert json.loads(got["before_json"]) == {"a": 1} and json.loads(got["after_json"]) == ["x"]
    with pytest.raises(sqlite3.IntegrityError):  # status is checked by the table
        app.state.db.execute("UPDATE approvals SET status='weird' WHERE id=?", (item,))
