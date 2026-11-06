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
