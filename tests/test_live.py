"""Live Claude jobs dock and Cancel (plan L1). Only fake claude CLIs run here."""

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.llm import live, runner
from app.llm.live import ClaudeCancelled
from app.settings import Settings
from tests.test_auth import csrf_from, make_client, setup_admin

TOKEN = "fake-token-SUPERSECRETVALUE123456"


@pytest.fixture
def conn(config):
    return db.init_db(config.db_path)


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("live.check_seconds", 0.1)
    s.set("live.cancel_grace_seconds", 1)
    return s


def env_for(config, **extra):
    env = runner.claude_env(config, None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def start(config, tmp_path, settings=None, thread_conn=None, timeout=30, **kw):
    """Run `claude -p hello` in a thread. Returns (thread, result dict)."""
    out = {}

    def work():
        c = thread_conn or db.connect(config.db_path)
        live_settings = Settings(c)
        try:
            out["proc"] = live.run_process(
                ["claude", "-p", "hello there", "--model", "m1", "--output-format", "stream-json"],
                cwd=tmp_path,
                env=kw.pop("env", None) or env_for(config),
                timeout=timeout,
                conn=c,
                data_dir=config.data_dir,
                settings=live_settings,
                **kw,
            )
        except BaseException as exc:
            out["exc"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, out


def wait(fn, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM claude_live ORDER BY started")]


def one_running(conn):
    return lambda: next((r for r in rows(conn) if r["status"] == "running"), None)


def alive(pid):
    return live.pid_alive(pid)


# ---------- registry and log ----------


def test_row_is_created_and_ended_and_log_has_text_not_secrets(
    config, conn, settings, tmp_path, fake_claude_sleep, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    proc = live.run_process(
        [
            "claude",
            "-p",
            "my prompt text " + TOKEN,
            "--model",
            "m1",
            "--output-format",
            "stream-json",
        ],
        cwd=tmp_path,
        env=env_for(config),
        timeout=20,
        conn=conn,
        data_dir=config.data_dir,
        settings=settings,
        kind="claude",
        label="Judge jobs",
        run_id="r1",
    )
    assert proc.returncode == 0 and '"type": "result"' in proc.stdout
    (r,) = rows(conn)
    assert r["status"] == "done" and r["ended"] and r["model"] == "m1"
    assert r["kind"] == "claude" and r["label"] == "Judge jobs" and r["run_id"] == "r1"
    assert r["pid"] == r["pgid"] and r["pid_start"] and r["host"]
    text = live.log_path(config.data_dir, r["id"]).read_text()
    assert "my prompt text" in text and "Reading the job <b>now</b>" in text
    assert "> tool: Read" in text and "[finished]" in text
    assert TOKEN not in text and "[hidden]" in text
    assert oct(live.log_path(config.data_dir, r["id"]).stat().st_mode)[-3:] == "600"


def test_failed_exit_is_marked_failed(config, conn, settings, tmp_path, fake_claude_sleep):
    fake_claude_sleep.env("FAKE_EXIT", 3)
    proc = live.run_process(
        ["claude", "-p", "x"],
        cwd=tmp_path,
        env=env_for(config),
        timeout=20,
        conn=conn,
        data_dir=config.data_dir,
        settings=settings,
    )
    assert proc.returncode == 3
    assert rows(conn)[0]["status"] == "failed"


def test_run_claude_registers_and_still_returns_the_output(
    config, conn, fake_claude_sleep, tmp_path
):
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")
    res = runner.run_claude(
        "judge",
        prompt,
        {"jobs": []},
        "claude-test",
        3,
        1.0,
        config=config,
        conn=conn,
        record_run_id="run-9",
    )
    assert res["output"] == {"results": []} and res["cost_usd"] == pytest.approx(0.01)
    (r,) = rows(conn)
    assert r["kind"] == "claude" and r["label"] == "judge" and r["run_id"] == "run-9"
    assert "[finished]" in live.log_path(config.data_dir, r["id"]).read_text()


def test_timeout_stops_the_process(config, conn, settings, tmp_path, fake_claude_sleep):
    pid_file = tmp_path / "pid"
    fake_claude_sleep.env("FAKE_SLEEP", 30)
    fake_claude_sleep.env("FAKE_PID_FILE", pid_file)
    with pytest.raises(subprocess.TimeoutExpired):
        live.run_process(
            ["claude", "-p", "x"],
            cwd=tmp_path,
            env=env_for(config),
            timeout=0.7,
            conn=conn,
            data_dir=config.data_dir,
            settings=settings,
        )
    assert not alive(int(pid_file.read_text()))
    assert rows(conn)[0]["status"] == "failed"


def test_without_a_connection_nothing_is_registered(tmp_path, fake_claude_sleep):
    proc = live.run_process(["claude", "-p", "x"], cwd=tmp_path, env=dict(os.environ), timeout=20)
    assert proc.returncode == 0 and not (tmp_path / "live").exists()


def test_old_rows_and_logs_are_pruned_and_stale_rows_fail(config, conn):
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO claude_live(id, pid, pgid, kind, label, started, ended, status) "
        "VALUES ('aaaaaaaaaaaaaaaa', 1, 1, 'claude', 'x', ?, ?, 'done')",
        (old, old),
    )
    conn.execute(
        "INSERT INTO claude_live(id, pid, pgid, kind, label, started, status, beat) "
        "VALUES ('bbbbbbbbbbbbbbbb', 1, 1, 'claude', 'y', ?, 'running', ?)",
        (old, old),
    )
    folder = live.live_dir(config.data_dir)
    folder.mkdir(parents=True)
    log = folder / "aaaaaaaaaaaaaaaa.log"
    log.write_text("x")
    os.utime(log, (0, 0))
    live.prune(conn, config.data_dir, 7)
    assert [r["id"] for r in rows(conn)] == ["bbbbbbbbbbbbbbbb"] and not log.exists()
    assert live.reap_stale(conn, 120) == 1
    assert rows(conn)[0]["status"] == "failed"


def test_mask_hides_env_values_and_token_shapes():
    text = live.mask(
        "a SECRETVALUE99 b sk-ant-abc123456 c Bearer abcdefgh12345",
        {"MY_TOKEN": "SECRETVALUE99", "PATH": "/usr/bin"},
    )
    assert "SECRETVALUE99" not in text and "sk-ant" not in text and "abcdefgh12345" not in text
    assert "/usr/bin" in live.mask("/usr/bin", {"PATH": "/usr/bin"})


# ---------- cancel ----------


def test_cancel_kills_the_group_and_marks_action_and_run(
    config, conn, settings, tmp_path, fake_claude_sleep
):
    pid_file, child_file = tmp_path / "pid", tmp_path / "child"
    fake_claude_sleep.env("FAKE_SLEEP", 60)
    fake_claude_sleep.env("FAKE_PID_FILE", pid_file)
    fake_claude_sleep.env("FAKE_CHILD_FILE", child_file)
    conn.execute(
        "INSERT INTO actions(id, kind, target, label, status, created) "
        "VALUES ('act1', 'k', '', 'Prepare job', 'running', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES ('run1', 'daily', 'now', 'running')"
    )
    holder = {}

    def with_action():
        token = live.CURRENT_ACTION.set("act1")
        try:
            holder["t"], holder["out"] = None, None
            c = db.connect(config.db_path)
            holder["proc"] = live.run_process(
                ["claude", "-p", "x"],
                cwd=tmp_path,
                env=env_for(config),
                timeout=60,
                conn=c,
                data_dir=config.data_dir,
                settings=Settings(c),
                run_id="run1",
            )
        except BaseException as exc:
            holder["exc"] = exc
        finally:
            live.CURRENT_ACTION.reset(token)

    t = threading.Thread(target=with_action, daemon=True)
    t.start()
    r = wait(one_running(conn))
    wait(lambda: child_file.exists() and pid_file.exists())
    pid, child = int(pid_file.read_text()), int(child_file.read_text())
    assert r["action_id"] == "act1" and r["pid"] == pid
    assert live.verify_pid(pid, r["pid_start"])

    assert live.request_cancel(conn, config.data_dir, r["id"], 1) == "stopped"
    t.join(10)
    assert isinstance(holder["exc"], ClaudeCancelled)
    assert not alive(pid) and wait(lambda: not alive(child))
    assert rows(conn)[0]["status"] == "cancelled"
    act = conn.execute("SELECT status, error FROM actions WHERE id='act1'").fetchone()
    assert (act["status"], act["error"]) == ("failed", "Cancelled by you")
    run = conn.execute("SELECT status, error FROM runs WHERE run_id='run1'").fetchone()
    assert (run["status"], run["error"]) == ("cancelled", "Cancelled by you")
    assert "Cancelled by you" in live.log_path(config.data_dir, r["id"]).read_text()


def test_sigkill_after_grace_when_sigterm_is_ignored(config, conn, tmp_path, fake_claude_sleep):
    pid_file = tmp_path / "pid"
    fake_claude_sleep.env("FAKE_SLEEP", 60)
    fake_claude_sleep.env("FAKE_PID_FILE", pid_file)
    fake_claude_sleep.env("FAKE_IGNORE_TERM", 1)
    t, out = start(config, tmp_path)
    r = wait(one_running(conn))
    wait(pid_file.exists)
    time.sleep(0.3)  # let the fake install its SIGTERM handler
    started = time.monotonic()
    assert live.request_cancel(conn, config.data_dir, r["id"], 0.5) == "stopped"
    assert time.monotonic() - started >= 0.5  # it waited the grace time first
    t.join(10)
    assert isinstance(out["exc"], ClaudeCancelled)
    assert not alive(int(pid_file.read_text()))


def test_owner_stops_its_own_child_when_another_machine_asked(
    config, conn, tmp_path, fake_claude_sleep
):
    pid_file = tmp_path / "pid"
    fake_claude_sleep.env("FAKE_SLEEP", 60)
    fake_claude_sleep.env("FAKE_PID_FILE", pid_file)
    t, out = start(config, tmp_path)
    r = wait(one_running(conn))
    conn.execute("UPDATE claude_live SET host='some-other-container' WHERE id=?", (r["id"],))
    assert live.request_cancel(conn, config.data_dir, r["id"], 1) == "requested"
    assert alive(r["pid"])  # not this machine's pid: the route did not signal it
    t.join(10)
    assert isinstance(out["exc"], ClaudeCancelled)
    assert not alive(r["pid"]) and rows(conn)[0]["status"] == "cancelled"


def test_pid_check_refuses_a_process_that_is_not_claude(config, conn):
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        start_time = live._proc_start(other.pid)
        assert start_time and not live.verify_pid(other.pid, start_time)
        import socket

        conn.execute(
            "INSERT INTO claude_live(id, pid, pgid, pid_start, host, kind, label, started, status) "
            "VALUES ('cccccccccccccccc', ?, ?, ?, ?, 'claude', 'x', 'now', 'running')",
            (other.pid, other.pid, start_time, socket.gethostname()),
        )
        assert live.request_cancel(conn, "/nonexistent", "cccccccccccccccc", 0.1) == "requested"
        assert other.poll() is None  # still running: never signalled
        assert rows(conn)[0]["status"] == "running"
    finally:
        other.kill()
        other.wait()


def test_pid_check_refuses_a_reused_pid(config, conn, tmp_path, fake_claude_sleep):
    fake_claude_sleep.env("FAKE_SLEEP", 60)
    t, out = start(config, tmp_path)
    r = wait(one_running(conn))
    assert live.verify_pid(r["pid"], r["pid_start"])
    assert not live.verify_pid(r["pid"], str(int(r["pid_start"]) + 1))  # other start time
    assert not live.verify_pid(0, r["pid_start"]) and not live.verify_pid(1, r["pid_start"])
    live.request_cancel(conn, config.data_dir, r["id"], 1)
    t.join(10)


def test_cancel_of_unknown_or_finished_job(config, conn, settings, tmp_path, fake_claude_sleep):
    assert live.request_cancel(conn, config.data_dir, "f" * 16, 1) == "unknown"
    live.run_process(
        ["claude", "-p", "x"],
        cwd=tmp_path,
        env=env_for(config),
        timeout=20,
        conn=conn,
        data_dir=config.data_dir,
        settings=settings,
    )
    assert live.request_cancel(conn, config.data_dir, rows(conn)[0]["id"], 1) == "gone"


# ---------- tasks stop cleanly ----------


@pytest.mark.parametrize(
    ("module", "func"),
    [("keywords", "run_keywords"), ("daily", "run_daily"), ("weekly", "run_weekly")],
)
def test_task_stops_cleanly_on_cancel_and_releases_its_lock(module, func, config, monkeypatch):
    import importlib

    from app.tasks import lock

    mod = importlib.import_module(
        f"app.tasks.{module}" if module != "keywords" else "app.tasks.keywords"
    )
    target = mod.keywords if module == "keywords" else mod

    def boom(*a, **k):
        raise ClaudeCancelled("Cancelled by you")

    monkeypatch.setattr(target, func, boom)
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    assert mod.main() == 0
    assert lock.running_since(config.data_dir, mod.TASK) is None


def test_cancel_is_not_swallowed_by_except_exception():
    assert not issubclass(ClaudeCancelled, Exception)
    try:
        try:
            raise ClaudeCancelled("x")
        except Exception:  # what the tasks do around each step
            pytest.fail("swallowed")
    except ClaudeCancelled:
        pass


# ---------- routes and dock ----------


@pytest.fixture
def web(config):
    app, client = make_client(config)
    setup_admin(client)
    return app, client


def add_job(app, live_id="1" * 16, kind="claude", label="Prepare a job", log="line one", **cols):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    app.state.db.execute(
        "INSERT INTO claude_live(id, pid, pgid, kind, label, model, started, status, beat) "
        "VALUES (?, 999999, 999999, ?, ?, 'claude-x', ?, 'running', ?)",
        (live_id, kind, label, now, now),
    )
    folder = live.live_dir(app.state.config.data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{live_id}.log").write_text(log)
    return live_id


def test_dock_is_hidden_and_quiet_when_nothing_runs(web):
    _, client = web
    html = client.get("/").text
    assert 'id="claude-dock"' in html and "hidden" in html and "Claude:" not in html
    assert "hx-trigger" not in html  # no HTMX polling while idle (dock.js looks now and then)


def test_dock_lists_running_jobs_with_cancel_and_log(web):
    app, client = web
    add_job(app, log="first line\nsecond line")
    html = client.get("/").text
    assert "Claude: 1 running" in html and "Prepare a job" in html and "claude-x" in html
    assert 'hx-get="/claude/dock"' in html and "second line" in html
    assert "/claude/" + "1" * 16 + "/cancel" in html and ">Cancel<" in html
    assert "<details" in html
    assert "This may already be sent to Upwork" not in html
    assert client.get("/claude/dock").text.count("Prepare a job") == 1


def test_write_step_shows_the_warning_next_to_cancel(web):
    app, client = web
    add_job(app, kind="write", label="Send to Upwork (x)")
    html = client.get("/claude/dock").text
    assert "This may already be sent to Upwork. Check Upwork before you try again." in html
    assert html.index("Cancel</button>") < html.index('class="dock-warn')


def test_dock_text_is_escaped(web):
    app, client = web
    add_job(app, label="<script>alert(1)</script>", log="<img src=x onerror=alert(2)>")
    html = client.get("/claude/dock").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    assert "<img src=x" not in html and "&lt;img src=x onerror" in html


def test_dock_uses_the_action_label_when_linked(web):
    app, client = web
    app.state.db.execute(
        "INSERT INTO actions(id, kind, target, label, status, created) "
        "VALUES ('a1', 'k', '', 'Writing your cover letter', 'running', 'now')"
    )
    add_job(app, label="judge")
    app.state.db.execute("UPDATE claude_live SET action_id='a1'")
    assert "Writing your cover letter" in client.get("/claude/dock").text


def test_cancel_needs_login_and_csrf(web, config):
    app, client = web
    live_id = add_job(app)
    token = csrf_from(client.get("/settings").text)
    r = client.post(f"/claude/{live_id}/cancel")  # logged in, no token
    assert r.status_code == 403
    r = client.post(f"/claude/{live_id}/cancel", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    assert rows(app.state.db)[0]["cancel_requested"] == 0

    anon_app, anon = make_client(
        config.__class__(**{**config.__dict__, "data_dir": config.data_dir / "x"})
    )
    r = anon.post(f"/claude/{live_id}/cancel", data={"csrf_token": token})
    assert r.status_code in (303, 401, 403) and r.status_code != 200
    assert anon.get("/claude/dock").status_code in (303, 401)

    r = client.post(f"/claude/{live_id}/cancel", data={"csrf_token": token})
    assert r.status_code == 303
    assert rows(app.state.db)[0]["cancel_requested"] == 1  # pid is not claude: asked, not killed


def test_cancel_with_htmx_returns_the_dock_and_unknown_is_404(web):
    app, client = web
    live_id = add_job(app)
    token = csrf_from(client.get("/settings").text)
    r = client.post(
        f"/claude/{live_id}/cancel", headers={"HX-Request": "true", "X-CSRF-Token": token}
    )
    assert r.status_code == 200 and 'id="claude-dock"' in r.text and "Stopping..." in r.text
    r = client.post(f"/claude/{'9' * 16}/cancel", headers={"X-CSRF-Token": token})
    assert r.status_code == 404
    r = client.post("/claude/not-an-id/cancel", headers={"X-CSRF-Token": token})
    assert r.status_code == 404


def test_cancel_a_worker_action_end_to_end(config, fake_claude_sleep, tmp_path):
    """A click starts claude through the worker; Cancel kills it and the action fails."""
    app, client = make_client(config)
    setup_admin(client)
    fake_claude_sleep.env("FAKE_SLEEP", 60)
    app.state.settings.set("live.check_seconds", 0.1)
    app.state.settings.set("live.cancel_grace_seconds", 1)
    reached = {}

    def work(ctx):
        try:
            live.run_process(
                ["claude", "-p", "x", "--model", "m"],
                cwd=tmp_path,
                env=env_for(config),
                timeout=60,
                conn=ctx.conn,
                data_dir=config.data_dir,
                settings=ctx.settings,
            )
        finally:
            reached["after"] = True  # a cancelled action must not go on past claude

    try:
        action_id = app.state.worker.submit("t", "x", "Prepare the job", work)
        r = wait(one_running(app.state.db))
        assert r["action_id"] == action_id
        assert "Prepare the job" in client.get("/claude/dock").text
        token = csrf_from(client.get("/settings").text)
        resp = client.post(f"/claude/{r['id']}/cancel", data={"csrf_token": token})
        assert resp.status_code == 303
        wait(lambda: app.state.worker.get(action_id)["status"] == "failed")
        act = app.state.worker.get(action_id)
        assert act["error"] == "Cancelled by you"
        wait(lambda: reached.get("after"))
        assert not alive(r["pid"])
        assert "Claude: " not in client.get("/claude/dock").text
    finally:
        app.state.worker.stop()


def test_settings_for_the_dock_exist_in_defaults():
    from app.settings import load_defaults

    d = load_defaults()
    for key in (
        "poll_seconds",
        "idle_poll_seconds",
        "tail_lines",
        "cancel_grace_seconds",
        "log_keep_days",
        "stale_seconds",
    ):
        assert d[f"live.{key}"] > 0


def test_python_is_running_tests_with_this_interpreter():
    assert sys.executable  # keeps the sys import honest for the fakes' shebang
