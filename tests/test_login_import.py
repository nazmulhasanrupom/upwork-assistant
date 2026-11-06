"""Import of the Claude Code Upwork login (plan P4). No real tokens: all values are fakes."""

import json
import os
import stat

import pytest

from app.upwork import login_import
from app.upwork.login_import import LoginImportError
from tests.test_auth import csrf_from, login, make_client, setup_admin

URL = "https://mcp.upwork.com/mcp"
SECRET_A = "fake-access-token-AAAA"
SECRET_B = "fake-refresh-token-BBBB"


def creds(url=URL, **extra):
    data = {
        "mcpOAuth": {
            "upwork|abc123": {
                "serverName": "upwork",
                "serverUrl": url,
                "accessToken": SECRET_A,
                "refreshToken": SECRET_B,
                "expiresAt": 1,
            }
        },
        "claudeAiOauth": {"accessToken": "should-not-be-imported"},
    }
    data.update(extra)
    return json.dumps(data).encode()


@pytest.fixture
def web(config):
    app, c = make_client(config)
    secret = setup_admin(c)
    c.cookies.clear()
    login(c, secret)
    return app, c, config.data_dir / "claude"


def upload(c, raw, name=".credentials.json", token=True):
    data = {"csrf_token": csrf_from(c.get("/upwork").text)} if token else {}
    return c.post("/upwork/import", data=data, files={"file": (name, raw, "application/json")})


# ---------- the helper ----------


def test_extract_keeps_only_upwork_entries():
    raw = json.dumps(
        {
            "mcpOAuth": {
                "upwork|1": {"serverUrl": URL + "/", "accessToken": "x"},
                "other|2": {"serverUrl": "https://other.example/mcp", "accessToken": "y"},
            },
            "claudeAiOauth": {"accessToken": "z"},
        }
    ).encode()
    assert list(login_import.extract_upwork_entries(raw, URL)) == ["upwork|1"]


@pytest.mark.parametrize(
    "raw",
    [b"", b"   ", b"not json", b"\xff\xfe", b"[1]", b'"x"', b"{}", b'{"mcpOAuth": []}',
     b'{"mcpOAuth": {}}', b'{"mcpOAuth": {"a": "str"}}',
     creds(url="https://evil.example/mcp")],
)  # fmt: skip
def test_extract_refuses_bad_files(raw):
    with pytest.raises(LoginImportError) as e:
        login_import.extract_upwork_entries(raw, URL)
    assert SECRET_A not in str(e.value) and SECRET_B not in str(e.value)


def test_extract_refuses_too_big():
    with pytest.raises(LoginImportError, match="too big"):
        login_import.extract_upwork_entries(b" " * (login_import.MAX_UPLOAD_BYTES + 1), URL)


# ---------- the form ----------


def test_page_has_import_form_and_laptop_steps(web):
    app, c, _ = web
    page = c.get("/upwork").text
    assert 'action="/upwork/import"' in page and 'enctype="multipart/form-data"' in page
    assert 'type="file"' in page and "Import Claude login" in page
    assert "export CLAUDE_CONFIG_DIR=$HOME/upwork-claude-test" in page
    assert "claude mcp add --transport http --scope user upwork https://mcp.upwork.com/mcp" in page
    assert "/exit" in page and "Authenticate" in page
    assert "$HOME/upwork-claude-test/.credentials.json" in page
    assert "holds a login token" in page and "delete" in page


def test_valid_file_is_saved_private_without_claude_ai_oauth(web, caplog):
    app, c, cfg = web
    with caplog.at_level("DEBUG"):
        r = upload(c, creds())
    assert r.status_code == 200 and "Login imported. Click Test connection." in r.text
    assert SECRET_A not in r.text and SECRET_B not in r.text
    assert SECRET_A not in caplog.text and SECRET_B not in caplog.text
    path = cfg / ".credentials.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o700
    saved = json.loads(path.read_text())
    assert list(saved) == ["mcpOAuth"]  # claudeAiOauth was NOT imported
    assert saved["mcpOAuth"]["upwork|abc123"]["accessToken"] == SECRET_A
    assert not [p for p in cfg.iterdir() if p.name != ".credentials.json"]  # no temp files left


def test_merge_keeps_existing_keys_and_other_servers(web):
    app, c, cfg = web
    cfg.mkdir(parents=True)
    (cfg / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "keep-me"},
                "mcpOAuth": {
                    "old-upwork": {"serverUrl": URL, "accessToken": "old"},
                    "other|9": {"serverUrl": "https://other.example/mcp", "accessToken": "o"},
                },
                "extra": 5,
            }
        )
    )
    r = upload(c, creds())
    assert r.status_code == 200
    saved = json.loads((cfg / ".credentials.json").read_text())
    assert saved["claudeAiOauth"] == {"accessToken": "keep-me"}
    assert saved["extra"] == 5
    assert set(saved["mcpOAuth"]) == {"upwork|abc123", "other|9"}  # old Upwork entry replaced
    assert stat.S_IMODE(os.stat(cfg / ".credentials.json").st_mode) == 0o600


def test_broken_existing_file_is_replaced(web):
    app, c, cfg = web
    cfg.mkdir(parents=True)
    (cfg / ".credentials.json").write_text("{broken")
    assert upload(c, creds()).status_code == 200
    assert "upwork|abc123" in json.loads((cfg / ".credentials.json").read_text())["mcpOAuth"]


def test_config_dir_follows_env(web, monkeypatch, tmp_path):
    app, c, _ = web
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert upload(c, creds()).status_code == 200
    assert (tmp_path / "elsewhere" / ".credentials.json").exists()


@pytest.mark.parametrize(
    "raw",
    [creds(url="https://evil.example/mcp"), b"not json", b"[]", b'{"claudeAiOauth": {}}'],
    ids=["wrong-server", "not-json", "not-object", "no-mcpoauth"],
)
def test_bad_files_are_refused_and_nothing_is_written(web, raw):
    app, c, cfg = web
    r = upload(c, raw)
    assert r.status_code == 400 and "Login imported" not in r.text
    assert SECRET_A not in r.text
    assert not (cfg / ".credentials.json").exists()


def test_too_big_file_is_refused(web):
    app, c, cfg = web
    big = creds(pad="x" * (login_import.MAX_UPLOAD_BYTES + 10))
    r = upload(c, big)
    assert r.status_code == 413 and "too big" in r.text
    assert not (cfg / ".credentials.json").exists()


def test_no_file_chosen(web):
    app, c, cfg = web
    token = csrf_from(c.get("/upwork").text)
    r = c.post("/upwork/import", data={"csrf_token": token})
    assert r.status_code == 400 and "Choose" in r.text


def test_csrf_missing_is_403_and_login_is_required(web):
    app, c, cfg = web
    assert upload(c, creds(), token=False).status_code == 403
    assert not (cfg / ".credentials.json").exists()
    token = csrf_from(c.get("/upwork").text)
    c.cookies.clear()
    r = c.post(
        "/upwork/import",
        data={"csrf_token": token},
        files={"file": (".credentials.json", creds(), "application/json")},
    )
    assert r.status_code in (303, 403)
    assert not (cfg / ".credentials.json").exists()
