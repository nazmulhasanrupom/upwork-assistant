"""Run now buttons (plan T1): lock, detached start, dashboard states. A fake task module
(tests/fake_tasks) stands in for the real tasks. No network, no Claude."""

import os
import re
import time

import pytest

from app.tasks import daily, lock, trigger
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client


@pytest.fixture
def web(config, monkeypatch):
    monkeypatch.setattr(trigger, "PACKAGE", "tests.fake_tasks")
    monkeypatch.setenv("FAKE_TASK_SLEEP", "1")
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def token(client):
    return csrf_from(client.get("/").text)


def post_run(client, name="daily", csrf=True):
    data = {"csrf_token": token(client)} if csrf else {}
    return client.post(f"/tasks/{name}/run", data=data)


def wait_until(check, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def runs(app):
    return app.state.db.execute("SELECT * FROM runs WHERE task = 'daily'").fetchall()


def log_files(config):
    d = config.data_dir / "logs"
    return sorted(d.glob("daily-*.log")) if d.exists() else []


def idle(config):
    return lock.running_since(config.data_dir, "daily") is None


def test_start_runs_detached_and_shows_status(web, config):
    app, client = web
    r = post_run(client)
    assert r.status_code == 303 and r.headers["location"] == "/?msg=started"
    # The lock is handed to the new process, so the very next page already says running.
    page = client.get("/?msg=started")
    assert "Started." in page.text
    assert "running since" in page.text or "just started" in page.text
    assert 'hx-get="/tasks/runs"' in page.text and 'hx-trigger="every 5s"' in page.text
    assert re.search(r"<button[^>]*disabled[^>]*>Running</button>", page.text)
    assert wait_until(lambda: len(runs(app)) == 1)
    assert wait_until(lambda: idle(config))
    row = runs(app)[0]
    assert row["status"] == "ok"
    page = client.get("/")
    assert "hx-trigger" not in page.text  # nothing runs: polling stops
    assert '<span class="status ok">ok</span>' in page.text
    [log] = log_files(config)
    assert "fake daily start" in log.read_text()


def test_busy_lock_blocks_second_start(web, config):
    app, client = web
    fd = lock.acquire(config.data_dir, "daily")  # as if the scheduler runs it
    try:
        r = post_run(client)
        assert r.headers["location"] == "/?msg=busy"
        page = client.get("/?msg=busy")
        assert "already running" in page.text and "running since" in page.text
        assert re.search(r"<button[^>]*disabled[^>]*>Running</button>", page.text)
        assert 'hx-trigger="every 5s"' in page.text
        time.sleep(0.5)
        assert log_files(config) == [] and runs(app) == []  # no second process was started
    finally:
        os.close(fd)
    assert idle(config)
    assert "hx-trigger" not in client.get("/tasks/runs").text


def test_task_main_exits_quietly_when_busy(config, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    fd = lock.acquire(config.data_dir, "daily")
    try:
        assert daily.main() == 0
    finally:
        os.close(fd)
    from app.db import init_db

    assert init_db(config.db_path).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_lock_is_released_when_holder_ends(config):
    with lock.task_lock(config.data_dir, "daily"):
        assert lock.running_since(config.data_dir, "daily")
        with pytest.raises(lock.TaskBusy):
            lock.acquire(config.data_dir, "daily")
    assert lock.running_since(config.data_dir, "daily") is None
    os.close(lock.acquire(config.data_dir, "daily"))


@pytest.mark.parametrize("name", ["digest", "weekly", "monthly"])
def test_missing_module_is_disabled(web, config, name):
    app, client = web
    page = client.get("/")
    row = re.search(rf'id="task-{name}".*?</tr>', page.text, re.S).group(0)
    assert "not built yet" in row and "disabled" in row and "<form" not in row
    r = post_run(client, name)
    assert r.headers["location"] == "/?msg=unbuilt"
    assert "not built yet" in client.get("/?msg=unbuilt").text
    assert not (config.data_dir / "logs").exists()


def test_unknown_task_is_404(web):
    _, client = web
    assert post_run(client, "nope").status_code == 404


def test_csrf_missing_is_403(web, config):
    app, client = web
    assert post_run(client, csrf=False).status_code == 403
    r = client.post("/tasks/daily/run", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    time.sleep(0.3)
    assert log_files(config) == [] and runs(app) == []


def test_login_needed(web):
    _, client = web
    client.cookies.clear()
    assert client.get("/tasks/runs").status_code == 303


def test_kill_switch_disables_buttons(web, config):
    app, client = web
    app.state.settings.set("general.kill_switch", True)
    page = client.get("/")
    row = re.search(r'id="task-daily".*?</tr>', page.text, re.S).group(0)
    assert "kill switch is on" in row and "disabled" in row and "<form" not in row
    assert 'id="kill-note"' in page.text
    r = post_run(client)
    assert r.headers["location"] == "/?msg=killed"
    assert "kill switch is on" in client.get("/?msg=killed").text
    time.sleep(0.3)
    assert log_files(config) == [] and runs(app) == []


def test_partial_endpoint(web, config):
    _, client = web
    r = client.get("/tasks/runs")
    assert r.status_code == 200
    assert "<html" not in r.text and 'id="runs"' in r.text and "never run" in r.text
    assert "hx-trigger" not in r.text
    assert 'action="/tasks/daily/run"' in r.text
    fd = lock.acquire(config.data_dir, "daily")
    try:
        r = client.get("/tasks/runs")
        assert 'hx-get="/tasks/runs"' in r.text and 'hx-trigger="every 5s"' in r.text
    finally:
        os.close(fd)


def test_finished_child_is_reaped(web, config, monkeypatch):
    app, client = web
    monkeypatch.setenv("FAKE_TASK_SLEEP", "0")
    post_run(client)
    assert wait_until(lambda: len(runs(app)) == 1 and idle(config))

    if not os.path.exists(f"/proc/self/task/{os.getpid()}/children"):
        pytest.skip("this kernel has no /proc children list")

    def zombies():
        out = []
        for task in os.listdir("/proc/self/task"):
            with open(f"/proc/self/task/{task}/children") as f:
                for pid in f.read().split():
                    with open(f"/proc/{pid}/stat") as s:
                        out.append(s.read().rsplit(")", 1)[1].split()[0])
        return [state for state in out if state == "Z"]

    assert wait_until(lambda: not zombies(), 5)
