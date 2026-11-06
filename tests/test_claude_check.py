from app.tools import claude_check


def test_ok(fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    assert claude_check.main() == 0
    assert capsys.readouterr().out.strip() == "OK"
    argv = fake_claude.log()["argv"]
    assert argv[:2] == ["-p", "say ok"]
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_api_key_also_accepted(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-not-real")
    assert claude_check.check()[0] is True


def test_no_token(fake_claude, capsys):
    assert claude_check.main() == 1
    err = capsys.readouterr().err
    assert "NO TOKEN" in err
    assert not fake_claude.called()


def test_bad_token(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    fake_claude.mode("auth")
    ok, msg = claude_check.check()
    assert not ok and msg.startswith("BAD TOKEN")
    assert "tok-not-real" not in msg


def test_rate_limit(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    fake_claude.mode("rate_limit")
    ok, msg = claude_check.check()
    assert not ok and msg.startswith("RATE LIMIT")


def test_other_error(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    fake_claude.mode("error_turns")
    ok, msg = claude_check.check()
    assert not ok and msg.startswith("ERROR")


def test_claude_missing(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-not-real")
    monkeypatch.setenv("PATH", "/nonexistent")
    ok, msg = claude_check.check()
    assert not ok and msg.startswith("CLAUDE NOT FOUND")
