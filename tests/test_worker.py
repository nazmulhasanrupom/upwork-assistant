"""In-app worker (plan W1): submit, dedup, kill switch, failures, restart, routes, nav badge."""

import logging
import threading
from pathlib import Path

import pytest

from app.main import create_app
from app.worker import GENERIC_ERROR, WorkerError
from tests.test_auth import make_client, setup_admin

APP = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture
def web(config):
    app, client = make_client(config)
    setup_admin(client)
    app.state.worker.sync = True  # tests run each action inside submit
    return app, client


@pytest.fixture
def threaded(config):
    app, client = make_client(config)
    setup_admin(client)
    app.state.settings.set("worker.threads", 1)
    yield app, client
    app.state.worker.stop()


def row(app, action_id):
    return app.state.worker.get(action_id)


def wait_for(app, action_id, status, timeout=5):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if row(app, action_id)["status"] == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"{action_id} is {row(app, action_id)['status']}, wanted {status}")


# ---------- submit and status ----------


def test_submit_returns_at_once_and_status_moves(threaded):
    app, _ = threaded
    w = app.state.worker
    go, inside = threading.Event(), threading.Event()
    seen = {}

    def slow(ctx):
        seen["status"] = ctx.conn.execute(
            "SELECT status FROM actions WHERE id=?", (ctx.action_id,)
        ).fetchone()[0]
        seen["own_conn"] = ctx.conn is not app.state.db
        inside.set()
        go.wait(5)
        return "/jobs", "All good"

    a1 = w.submit("t", "x", "Slow one", slow, "k1")
    a2 = w.submit("t", "y", "Second", lambda: None, "k2")  # one thread: waits in the queue
    assert inside.wait(5)
    assert row(app, a1)["status"] == "running" and row(app, a1)["started"]
    assert row(app, a2)["status"] == "queued"
    go.set()
    wait_for(app, a1, "done")
    wait_for(app, a2, "done")
    r = row(app, a1)
    assert r["result_url"] == "/jobs" and r["message"] == "All good" and r["ended"]
    assert seen == {"status": "running", "own_conn": True}


def test_dedup_returns_same_id_while_active(threaded):
    app, _ = threaded
    w = app.state.worker
    go, inside = threading.Event(), threading.Event()
    runs = []

    def fn():
        runs.append(1)
        inside.set()
        go.wait(5)

    a1 = w.submit("prepare", "j1", "Prepare", fn, "prepare:j1")
    assert inside.wait(5)
    assert w.submit("prepare", "j1", "Prepare", fn, "prepare:j1") == a1
    assert w.submit("prepare", "j2", "Prepare", lambda: None, "prepare:j2") != a1
    go.set()
    wait_for(app, a1, "done")
    a3 = w.submit("prepare", "j1", "Prepare", lambda: None, "prepare:j1")  # finished: new run
    assert a3 != a1
    assert len(runs) == 1


def test_kill_switch_blocks_the_function(web):
    app, _ = web
    app.state.settings.set("general.kill_switch", True)
    ran = []
    aid = app.state.worker.submit("t", "", "Do it", lambda: ran.append(1), None)
    r = row(app, aid)
    assert r["status"] == "failed" and r["error"] == "Stopped by kill switch"
    assert ran == []


def test_failure_is_short_and_safe(web, caplog):
    app, client = web
    secret = "sk-ant-SECRET-TOKEN-123"

    def boom():
        raise RuntimeError(f"bad key {secret}")

    with caplog.at_level(logging.ERROR, logger="worker"):
        aid = app.state.worker.submit("t", "", "Boom", boom, None)
    r = row(app, aid)
    assert r["status"] == "failed" and r["error"] == GENERIC_ERROR
    assert secret not in str(r)
    assert secret in caplog.text and "Traceback" in caplog.text  # full trace: log only
    for url in (f"/actions/{aid}", "/actions"):
        text = client.get(url).text
        assert secret not in text and "Traceback" not in text and "RuntimeError" not in text
    assert GENERIC_ERROR in client.get(f"/actions/{aid}").text


def test_worker_error_text_is_shown(web):
    app, client = web

    def nope():
        raise WorkerError("Upwork is not connected.")

    aid = app.state.worker.submit("t", "", "Try", nope, None)
    assert "Upwork is not connected." in client.get(f"/actions/{aid}").text


def test_restart_marks_unfinished_as_stopped_and_never_reruns(config):
    app, _ = make_client(config)
    conn = app.state.db
    for i, status in enumerate(("queued", "running", "done")):
        conn.execute(
            "INSERT INTO actions(id, kind, target, label, status, created) "
            "VALUES (?, 'k', '', 'L', ?, '2026-01-01T00:00:00+00:00')",
            (f"a{i}", status),
        )
    app2 = create_app(config)  # a new process on the same database
    w = app2.state.worker
    assert w.get("a0")["status"] == "stopped" and w.get("a0")["error"] == "Stopped by restart"
    assert w.get("a1")["status"] == "stopped"
    assert w.get("a2")["status"] == "done"
    assert w.running_count() == 0


def test_stop_marks_waiting_actions_stopped(threaded):
    app, _ = threaded
    w = app.state.worker
    go, inside = threading.Event(), threading.Event()
    app.state.settings.set("worker.shutdown_wait_seconds", 0)

    def fn():
        inside.set()
        go.wait(5)

    a1 = w.submit("t", "", "One", fn, None)
    a2 = w.submit("t", "", "Two", lambda: None, None)
    assert inside.wait(5)
    w.stop()
    assert row(app, a1)["status"] == "stopped" and row(app, a2)["status"] == "stopped"
    go.set()


# ---------- routes ----------


def test_polling_partial_while_working(web):
    app, client = web
    conn = app.state.db
    conn.execute(
        "INSERT INTO actions(id, kind, target, label, status, created) "
        "VALUES ('w1', 'k', '', 'Preparing', 'running', '2026-01-01T00:00:00+00:00')"
    )
    r = client.get("/actions/w1", headers={"HX-Request": "true"})
    poll = app.state.settings.get("worker.poll_seconds")
    assert r.status_code == 200
    assert 'hx-get="/actions/w1"' in r.text and f"every {poll}s" in r.text
    assert "Preparing" in r.text
    assert "HX-Redirect" not in r.headers and "HX-Refresh" not in r.headers


def test_done_redirects_to_result_url(web):
    app, client = web
    aid = app.state.worker.submit("t", "", "Make", lambda: ("/jobs/abc", "Ready"), None)
    r = client.get(f"/actions/{aid}", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"] == "/jobs/abc"
    assert "hx-trigger" not in r.text and "Ready" in r.text


def test_done_without_url_refreshes(web):
    app, client = web
    aid = app.state.worker.submit("t", "", "Make", lambda: (None, "Ready"), None)
    r = client.get(f"/actions/{aid}", headers={"HX-Request": "true"})
    assert r.headers["HX-Refresh"] == "true" and "HX-Redirect" not in r.headers


def test_done_with_outside_url_is_not_followed(web):
    app, client = web
    aid = app.state.worker.submit(
        "t", "", "Make", lambda: ("https://evil.example/x", "Ready"), None
    )
    r = client.get(f"/actions/{aid}", headers={"HX-Request": "true"})
    assert "HX-Redirect" not in r.headers and r.headers["HX-Refresh"] == "true"
    assert "evil.example" not in r.text


def test_failed_does_not_refresh(web):
    app, client = web
    aid = app.state.worker.submit("t", "", "Make", lambda: 1 / 0, None)
    r = client.get(f"/actions/{aid}", headers={"HX-Request": "true"})
    assert "HX-Refresh" not in r.headers and "HX-Redirect" not in r.headers
    assert "Failed" in r.text


def test_unknown_action_is_404(web):
    _, client = web
    assert client.get("/actions/nope").status_code == 404


@pytest.mark.parametrize("path", ["/actions", "/actions/abc"])
def test_actions_need_login(config, path):
    _, client = make_client(config)
    setup_admin(client)
    client.cookies.clear()
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.get(path, headers={"HX-Request": "true"})
    assert r.status_code == 401


# ---------- nav badge and toast ----------


def test_nav_badge_counts_active_actions(web):
    app, client = web
    assert "Working (" not in client.get("/").text
    conn = app.state.db
    for i, status in enumerate(("queued", "running", "done")):
        conn.execute(
            "INSERT INTO actions(id, kind, target, label, status, created) "
            "VALUES (?, 'k', '', ?, ?, '2026-01-01T00:00:00+00:00')",
            (f"n{i}", f"Label {i}", status),
        )
    page = client.get("/").text
    assert "Working (2)" in page and "Label 0" in page and "Label 1" in page
    assert "Label 2" not in page
    assert 'hx-get="/actions"' in page
    part = client.get("/actions").text
    assert "Working (2)" in part and "<details" in part
    conn.execute("UPDATE actions SET status='done' WHERE status!='done'")
    assert "Working (" not in client.get("/actions").text


def test_finished_action_shows_a_toast_once_per_page(web):
    app, client = web
    aid = app.state.worker.submit("t", "", "Sync profile", lambda: ("/profile", "Saved"), None)
    part = client.get("/actions").text
    assert f'data-toast-id="{aid}"' in part and "data-notice" in part
    assert "Sync profile" in part and "Saved" in part and "hx-trigger" not in part
    assert "sessionStorage" in (APP / "static" / "notice.js").read_text()
