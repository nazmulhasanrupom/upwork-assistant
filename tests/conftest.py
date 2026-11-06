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


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put tests/fakes/claude first on PATH. Returns a helper to set mode and read the log."""
    import json
    import os
    import shutil
    import stat
    from pathlib import Path

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    target = bindir / "claude"
    shutil.copy(Path(__file__).parent / "fakes" / "claude", target)
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
            return json.loads(log.read_text())

    return Fake()
