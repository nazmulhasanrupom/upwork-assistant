"""L2: git history of memory/ and skills/. Real local git in a temp DATA_DIR (no network)."""

import shutil
import subprocess

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import auth, crypto, memory, playbook, versioning
from app.db import init_db
from app.main import create_app
from app.settings import Settings

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("gate_open"),
]


@pytest.fixture
def conn(config):
    return init_db(config.db_path)


@pytest.fixture
def settings(conn):
    return Settings(conn)


def tracked(config):
    out = subprocess.run(
        ["git", "-C", str(config.data_dir), "ls-files"], capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def test_ensure_repo_creates_once_with_generic_author(config, settings):
    assert versioning.ensure_repo(config, settings) is True
    assert versioning.ensure_repo(config, settings) is False
    commits = versioning.list_commits(config, settings)
    assert [c["subject"] for c in commits] == ["Start version history"]
    out = subprocess.run(
        ["git", "-C", str(config.data_dir), "log", "-1", "--format=%an <%ae>"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out == "Upwork Assistant <assistant@localhost>"


def test_secrets_are_never_tracked(config, settings):
    (config.data_dir / "app.db").write_bytes(b"db")
    (config.data_dir / "tokens.enc").write_text("secret")
    (config.data_dir / "runs").mkdir()
    (config.data_dir / "runs" / "r.json").write_text("{}")
    (config.data_dir / "memory").mkdir()
    (config.data_dir / "memory" / "MEMORY.md").write_text("hi\n")
    (config.data_dir / "memory" / ".x.tmp").write_text("tmp")
    versioning.ensure_repo(config, settings)
    assert versioning.commit(config, settings, "x") is None  # nothing new
    assert sorted(tracked(config)) == [".gitignore", "memory/MEMORY.md"]
    for bad in ("app.db", "tokens.enc", "runs/r.json"):
        assert bad not in tracked(config)
    # a path outside memory/ and skills/ is refused (commit returns None, never raises)
    assert versioning.commit(config, settings, "x", ["app.db"]) is None
    assert "app.db" not in tracked(config)


def test_write_memory_file_makes_one_commit(config, settings):
    memory.write_memory_file(config, settings, "MEMORY.md", "one\n")
    memory.write_memory_file(config, settings, "MEMORY.md", "two\n", message="Consolidate")
    subjects = [c["subject"] for c in versioning.list_commits(config, settings)]
    assert subjects[:2] == ["Consolidate", "Update MEMORY.md"]
    # same text again: no new commit
    memory.write_memory_file(config, settings, "MEMORY.md", "two\n")
    assert len(versioning.list_commits(config, settings)) == len(subjects)


def test_playbook_writes_are_committed(config, conn, settings):
    playbook.ensure_seeded(config, settings=settings)
    subjects = [c["subject"] for c in versioning.list_commits(config, settings)]
    assert "Seed the playbook" in subjects
    assert "memory/PLAYBOOK.md" in tracked(config)


def test_show_diff_and_sha_validation(config, settings):
    memory.write_memory_file(config, settings, "MEMORY.md", "hello world\n")
    sha = versioning.list_commits(config, settings)[0]["sha"]
    assert "+hello world" in versioning.show_diff(config, settings, sha)
    assert "+hello world" in versioning.show_diff(config, settings, sha[:8])
    for bad in ("", "xyz", "--help", "HEAD", sha + ";rm", "a" * 41, "1234"):
        with pytest.raises(versioning.VersioningError):
            versioning.show_diff(config, settings, bad)
    with pytest.raises(versioning.VersioningError, match="does not exist"):
        versioning.show_diff(config, settings, "deadbeef")


def test_revert_restores_text(config, settings):
    memory.write_memory_file(config, settings, "MEMORY.md", "one\n")
    memory.write_memory_file(config, settings, "MEMORY.md", "two\n")
    sha = versioning.list_commits(config, settings)[0]["sha"]
    versioning.revert(config, settings, sha)
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "one\n"
    assert versioning.list_commits(config, settings)[0]["subject"].startswith('Revert "')


def test_revert_conflict_aborts_and_keeps_repo_clean(config, settings):
    memory.write_memory_file(config, settings, "MEMORY.md", "one\n")
    memory.write_memory_file(config, settings, "MEMORY.md", "two\n")
    older = versioning.list_commits(config, settings)[1]["sha"]
    memory.write_memory_file(config, settings, "MEMORY.md", "three\n")
    before = versioning.list_commits(config, settings)
    with pytest.raises(versioning.VersioningError, match="Nothing was changed"):
        versioning.revert(config, settings, older)
    assert versioning.list_commits(config, settings) == before
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "three\n"
    assert not (config.data_dir / ".git" / "REVERT_HEAD").exists()
    status = subprocess.run(
        ["git", "-C", str(config.data_dir), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""
    memory.write_memory_file(config, settings, "MEMORY.md", "four\n")  # still works


def test_revert_saves_hand_edits_first(config, settings):
    memory.write_memory_file(config, settings, "MEMORY.md", "one\n")
    memory.write_memory_file(config, settings, "USER.md", "u\n")
    sha = versioning.list_commits(config, settings)[0]["sha"]  # the USER.md commit
    memory.memory_file(config, "MEMORY.md").write_text("edited by hand\n")
    versioning.revert(config, settings, sha)
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "edited by hand\n"


def test_commit_never_raises_and_alerts(config, conn, settings, monkeypatch):
    memory.write_memory_file(config, settings, "MEMORY.md", "one\n")

    def boom(*a, **k):
        raise versioning.VersioningError("git broke")

    monkeypatch.setattr(versioning, "_git", boom)
    assert versioning.commit(config, settings, "x") is None
    row = conn.execute(
        "SELECT message FROM alerts WHERE key = ?", (versioning.ALERT_KEY,)
    ).fetchone()
    assert "git broke" in row["message"]
    # the caller's write still succeeds
    memory.write_memory_file(config, settings, "MEMORY.md", "two\n")
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "two\n"
    monkeypatch.undo()
    memory.write_memory_file(config, settings, "MEMORY.md", "three\n")
    assert (
        conn.execute("SELECT 1 FROM alerts WHERE key = ?", (versioning.ALERT_KEY,)).fetchone()
        is None
    )


def test_git_missing_does_not_break_callers(config, settings, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert versioning.commit(config, settings, "x") is None
    memory.write_memory_file(config, settings, "MEMORY.md", "ok\n")


def test_timeout_comes_from_settings(config, settings, monkeypatch):
    settings.set("versioning.git_timeout_seconds", 7)
    seen = {}
    real = subprocess.run

    def spy(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        assert kw.get("shell") is not True and isinstance(cmd, list)
        return real(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    versioning.ensure_repo(config, settings)
    assert seen["timeout"] == 7


# ---------------------------------------------------------------- page


@pytest.fixture
def client(config):
    import re

    app = create_app(config)
    conn = app.state.db
    secret = pyotp.random_base32()
    auth.create_admin(
        conn,
        "owner",
        auth.hash_password("correct horse battery"),
        crypto.encrypt(config.secret_key, secret),
    )
    auth.finish_setup(conn)
    c = TestClient(app, base_url="https://testserver")
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post(
        "/login",
        data={
            "username": "owner",
            "password": "correct horse battery",
            "code": pyotp.TOTP(secret).now(),
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:200]
    return c


def csrf(client):
    import re

    html = client.get("/learning").text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_learning_requires_login(config):
    c = TestClient(create_app(config), base_url="https://testserver")
    r = c.get("/learning", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)
    r = c.post("/learning/abcdef1/undo", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)


def test_learning_lists_diffs_and_undo(client, config):
    s = client.app.state
    memory.write_memory_file(config, s.settings, "MEMORY.md", "first fact\n")
    memory.write_memory_file(config, s.settings, "MEMORY.md", "second fact\n")
    page = client.get("/learning")
    assert page.status_code == 200
    assert "Update MEMORY.md" in page.text and "+second fact" in page.text
    assert 'href="/learning"' in page.text
    token = csrf(client)
    sha = versioning.list_commits(config, s.settings)[0]["sha"]
    r = client.post(f"/learning/{sha}/undo", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/learning?done=undone"
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "first fact\n"
    assert "Undone" in client.get("/learning?done=undone").text


def test_undo_needs_csrf_and_valid_sha(client, config):
    s = client.app.state
    memory.write_memory_file(config, s.settings, "MEMORY.md", "x\n")
    sha = versioning.list_commits(config, s.settings)[0]["sha"]
    assert client.post(f"/learning/{sha}/undo", data={}).status_code == 403
    token = csrf(client)
    r = client.post("/learning/not-a-sha/undo", data={"csrf_token": token})
    assert r.status_code == 409 and "not a valid version id" in r.text
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "x\n"


def test_app_start_baselines_existing_files(config):
    (config.data_dir / "memory").mkdir()
    (config.data_dir / "memory" / "USER.md").write_text("u\n")
    create_app(config)
    assert "memory/USER.md" in tracked(config)
