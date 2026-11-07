import pytest

from app.config import Config


@pytest.fixture
def config(tmp_path):
    return Config(
        secret_key="k" * 40,
        public_base_url="https://test.example",
        data_dir=tmp_path,
        tz="UTC",
        log_level="info",
        claude_code_oauth_token="",
        anthropic_api_key="",
    )


@pytest.fixture(autouse=True)
def no_real_claude(tmp_path_factory, monkeypatch):
    """Tests must never start the real `claude`. A stub that fails goes first on PATH;
    the fake_claude fixtures put their own fake in front of it."""
    import os
    import stat

    bindir = tmp_path_factory.mktemp("stubbin")
    stub = bindir / "claude"
    stub.write_text("#!/bin/sh\necho 'real claude must not run in tests' >&2\nexit 99\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _install_fake_claude(tmp_path, monkeypatch, script):
    """Put tests/fakes/<script> first on PATH as `claude`. Returns a helper (mode, log)."""
    import json
    import os
    import shutil
    import stat
    from pathlib import Path

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    target = bindir / "claude"
    shutil.copy(Path(__file__).parent / "fakes" / script, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "fake_claude_log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Fake:
        def mode(self, name):
            monkeypatch.setenv("FAKE_CLAUDE_MODE", name)

        def called(self):
            return log.exists()

        def log(self):
            """Log of the first call (the stream fake writes one JSON line per call)."""
            return json.loads(log.read_text().splitlines()[0])

        def logs(self):
            return [json.loads(x) for x in log.read_text().splitlines()]

        def env(self, name, value):
            monkeypatch.setenv(name, str(value))

    return Fake()


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    return _install_fake_claude(tmp_path, monkeypatch, "claude")


@pytest.fixture
def fake_claude_stream(tmp_path, monkeypatch):
    return _install_fake_claude(tmp_path, monkeypatch, "claude_stream")


@pytest.fixture
def fake_claude_gen(tmp_path, monkeypatch):
    """Fake `claude` for judge and write calls (see tests/fakes/claude_gen)."""
    return _install_fake_claude(tmp_path, monkeypatch, "claude_gen")


@pytest.fixture
def fake_claude_chat(tmp_path, monkeypatch):
    """Fake `claude` for the Chat page (tests/fakes/claude_chat). Helper: replies(list)."""
    import json

    fake = _install_fake_claude(tmp_path, monkeypatch, "claude_chat")
    counter = tmp_path / "chat_counter"
    monkeypatch.setenv("FAKE_CHAT_COUNTER", str(counter))

    def replies(items):
        path = tmp_path / "chat_replies.json"
        path.write_text(json.dumps(items))
        monkeypatch.setenv("FAKE_CHAT_REPLIES", str(path))

    fake.replies = replies
    fake.count = lambda: int(counter.read_text()) if counter.exists() else 0
    return fake


@pytest.fixture
def fake_claude_sleep(tmp_path, monkeypatch):
    """Fake `claude` that sleeps (tests/fakes/claude_sleep): for the live dock and Cancel."""
    return _install_fake_claude(tmp_path, monkeypatch, "claude_sleep")


@pytest.fixture
def fake_claude_login(tmp_path, monkeypatch):
    """Fake `claude mcp login upwork --no-browser` (needs a pty). The session is killed after."""
    from app.upwork import mcp_login

    fake = _install_fake_claude(tmp_path, monkeypatch, "claude_mcp_login")
    mcp_login.manager.cancel()
    yield fake
    mcp_login.manager.cancel()


@pytest.fixture(autouse=True)
def clean_claude_env(monkeypatch):
    """The image sets CLAUDE_CONFIG_DIR=/data/claude. Tests choose their own folder."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("UPWORK_MCP_URL", raising=False)


@pytest.fixture(autouse=True)
def no_judging_in_daily(request, monkeypatch):
    """The daily task's judge/write step (plan G2) is off in tests that are about something
    else. Tests marked `@pytest.mark.judging` run the real step (with a fake claude)."""
    if request.node.get_closest_marker("judging"):
        return
    from app.llm.judging import JudgeResult
    from app.tasks import daily

    monkeypatch.setattr(daily, "judge_and_draft", lambda *a, **k: JudgeResult())


@pytest.fixture(autouse=True)
def no_after_send_in_daily(request, monkeypatch):
    """The daily task's after-send proposal review (plan R3) is off in tests about something
    else (their fake Upwork client only knows find_jobs). Tests marked `@pytest.mark.after_send`
    run the real part."""
    if request.node.get_closest_marker("after_send"):
        return
    from app.tasks import daily

    monkeypatch.setattr(daily, "after_send_part", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def no_metrics_in_daily(request, monkeypatch):
    """The daily task's metrics snapshot (plan X1) is off in tests about something else (their
    fake Upwork client only knows find_jobs). Tests marked `@pytest.mark.metrics` run the real
    part."""
    if request.node.get_closest_marker("metrics"):
        return
    from app.tasks import daily

    monkeypatch.setattr(daily, "metrics_part", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def no_profile_versions_in_daily(request, monkeypatch):
    """The daily profile copy (plan O1) is off in tests about something else (their fake Upwork
    client only knows find_jobs). Tests marked `@pytest.mark.profile_versions` run the real
    part."""
    if request.node.get_closest_marker("profile_versions"):
        return
    from app.tasks import daily

    monkeypatch.setattr(daily, "profile_versions_part", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def no_rank_in_daily(request, monkeypatch):
    """The daily task's keyword rank check (plan X2) is off in tests about something else (their
    fake Upwork client only knows find_jobs). Tests marked `@pytest.mark.rank` run the real
    part."""
    if request.node.get_closest_marker("rank"):
        return
    from app.tasks import daily

    monkeypatch.setattr(daily, "rank_part", lambda *a, **k: [])


@pytest.fixture
def gate_open():
    """The noise gate open (plan L4), for tests that write MEMORY.md or skills directly."""
    from app import noise_gate

    with noise_gate.open_gate("consolidation"):
        yield
