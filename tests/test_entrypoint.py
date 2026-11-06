"""docker/entrypoint.sh with a fake `claude` (plan P4)."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"

# The fake `add` writes .claude.json like the real CLI does, and logs each call.
FAKE = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
if [ "$FAKE_FAIL" = "1" ]; then echo "boom" >&2; exit 1; fi
if [ "$1 $2" = "mcp add" ]; then
  url=$(eval echo \\${$#})
  printf '{"mcpServers": {"upwork": {"type": "http", "url": "%s"}}}' "$url" \\
    > "$CLAUDE_CONFIG_DIR/.claude.json"
fi
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    e = dict(os.environ)
    e.update(
        PATH=f"{bindir}:{e['PATH']}",
        CLAUDE_CONFIG_DIR=str(tmp_path / "data" / "claude"),
        FAKE_LOG=str(tmp_path / "log.txt"),
    )
    e.pop("UPWORK_MCP_URL", None)
    e.pop("FAKE_FAIL", None)
    return e


def run(env, *cmd):
    return subprocess.run(
        [str(SCRIPT), *(cmd or ("echo", "started"))], env=env, capture_output=True, text=True
    )


def calls(env):
    p = Path(env["FAKE_LOG"])
    return p.read_text().splitlines() if p.exists() else []


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_first_start_makes_dir_adds_server_and_runs_command(env):
    r = run(env)
    assert r.returncode == 0 and r.stdout.strip().endswith("started")
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o700
    assert calls(env) == ["mcp add --transport http --scope user upwork https://mcp.upwork.com/mcp"]
    assert "upwork" in json.loads((cfg / ".claude.json").read_text())["mcpServers"]


def test_second_start_does_not_add_again(env):
    run(env)
    r = run(env)
    assert r.returncode == 0
    assert len(calls(env)) == 1


def test_server_in_config_means_no_call(env):
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(parents=True)
    (cfg / ".claude.json").write_text('{"mcpServers": {"upwork": {"url": "x"}}, "other": 1}')
    assert run(env).returncode == 0
    assert calls(env) == []


def test_other_server_only_still_adds_upwork(env):
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(parents=True)
    (cfg / ".claude.json").write_text('{"mcpServers": {"x": {}}}')
    run(env)
    assert len(calls(env)) == 1


def test_broken_config_json_adds_server(env):
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(parents=True)
    (cfg / ".claude.json").write_text("{nope")
    assert run(env).returncode == 0
    assert len(calls(env)) == 1


def test_url_from_env(env):
    env["UPWORK_MCP_URL"] = "https://mcp.example.test/mcp"
    run(env)
    assert calls(env)[0].endswith(" upwork https://mcp.example.test/mcp")


def test_claude_failure_warns_but_starts(env):
    env["FAKE_FAIL"] = "1"
    r = run(env)
    assert r.returncode == 0 and r.stdout.strip().endswith("started")
    assert "warning" in r.stderr


def test_missing_claude_warns_but_starts(env, tmp_path):
    (tmp_path / "bin" / "claude").unlink()
    env["PATH"] = f"{tmp_path / 'bin'}:/usr/bin:/bin"
    r = run(env)
    assert r.returncode == 0 and r.stdout.strip().endswith("started")
    assert "warning" in r.stderr


def test_unwritable_dir_warns_but_starts(env, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    env["CLAUDE_CONFIG_DIR"] = str(blocker / "claude")  # parent is a file
    r = run(env)
    assert r.returncode == 0 and r.stdout.strip().endswith("started")
    assert "warning" in r.stderr


def test_command_exit_code_and_args_pass_through(env):
    r = run(env, "sh", "-c", 'echo "$1"; exit 7', "sh", "a b")
    assert r.returncode == 7 and r.stdout.strip().endswith("a b")
