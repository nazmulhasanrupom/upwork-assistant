"""Profile versions (plan O1). Fake bridge, fake writer: no network, no Claude, no Upwork."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import approvals
from app import profile_versions as pv
from app.db import init_db
from app.settings import Settings
from app.tasks import daily
from app.upwork import writes
from app.upwork.client import ToolResult, UpworkAuthError
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.upwork_fakes import FakeMCP, load

ORG = "1000000000000000001"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def payload(title=None, description=None, skills=None, rate=None):
    p = copy.deepcopy(load("get_profile.json"))
    pd = p["data"]["personalData"]
    if title is not None:
        pd["title"] = f"<untrusted_participant_content>\n{title}\n</untrusted_participant_content>"
    if description is not None:
        pd["description"] = (
            f"<untrusted_participant_content>\n{description}\n</untrusted_participant_content>"
        )
    if skills is not None:
        p["data"]["skills"] = [{"id": str(i), "prettyName": n} for i, n in enumerate(skills)]
    if rate is not None:
        pd["chargeRate"]["rawValue"] = rate
    return p


class FakeProfile:
    """The READ bridge: answers get_profile with `self.payload` (or raises/returns an error)."""

    def __init__(self, data=None):
        self.payload = payload() if data is None else data
        self.calls = []
        self.closed = 0

    def call_many(self, calls):
        out = []
        for tool, args in calls:
            self.calls.append((tool, args))
            if isinstance(self.payload, UpworkAuthError):
                raise self.payload
            if isinstance(self.payload, Exception):
                out.append(
                    ToolResult(tool, [{"type": "text", "text": str(self.payload)}], is_error=True)
                )
            else:
                text = json.dumps(self.payload)
                out.append(ToolResult(tool, [{"type": "text", "text": text}]))
        return out

    def close(self):
        self.closed += 1


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    return Settings(conn)


@pytest.fixture
def ready(settings):
    settings.set("upwork.talent_org_uid", ORG)
    return settings


def files(config):
    folder = pv.versions_dir(config)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ---------------------------------------------------------------- extract and snapshot


def test_extract_fields_strips_tags_and_reads_skills_and_rate():
    f = pv.extract_fields(load("get_profile.json"))
    assert f["title"].startswith("AI Automation for Marketing")
    assert "untrusted" not in f["title"] and "untrusted" not in f["description"]
    assert f["description"].startswith("I build AI automations")
    assert f["skills"][:3] == ["n8n", "Make.com", "Automation"]
    assert f["rate"] == "25.0"


def test_extract_fields_survives_junk():
    assert pv.extract_fields(None) == {"title": "", "description": "", "skills": [], "rate": ""}
    assert pv.extract_fields({"data": {"skills": "x", "personalData": []}})["skills"] == []


def test_snapshot_saves_file_and_row(conn, ready, config):
    bridge = FakeProfile()
    res = pv.snapshot(bridge, conn, ready, config, NOW, reason="daily")
    assert res.saved and res.note == "saved"
    assert bridge.calls == [("get_profile", {"action": "get", "org_uid": ORG, "params": {}})]
    assert files(config) == ["20261106T120000Z.json"]
    saved = json.loads((pv.versions_dir(config) / files(config)[0]).read_text())
    assert saved["data"]["identity"]["ciphertext"] == "~01sample"
    row = pv.latest(conn)
    assert row["reason"] == "daily" and row["file"] == files(config)[0]
    assert row["title"].startswith("AI Automation") and row["rate"] == "25.0"
    assert len(json.loads(row["skills_json"])) == 8


def test_snapshot_skips_equal_content_even_when_other_fields_change(conn, ready, config):
    pv.snapshot(FakeProfile(), conn, ready, config, NOW)
    other = payload()
    other["data"]["profileAggregates"]["totalJobs"] = 99  # not an editable field
    res = pv.snapshot(FakeProfile(other), conn, ready, config, NOW + timedelta(days=1))
    assert not res.saved and res.note == "unchanged"
    assert conn.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0] == 1
    assert len(files(config)) == 1


def test_snapshot_saves_when_content_changes_and_files_never_collide(conn, ready, config):
    pv.snapshot(FakeProfile(), conn, ready, config, NOW)
    res = pv.snapshot(FakeProfile(payload(title="New title")), conn, ready, config, NOW)
    assert res.saved
    assert files(config) == ["20261106T120000Z-2.json", "20261106T120000Z.json"]
    assert pv.latest(conn)["title"] == "New title"


def test_snapshot_failures_save_nothing(conn, ready, config):
    with pytest.raises(pv.ProfileVersionError):
        pv.snapshot(FakeProfile({"data": {}}), conn, ready, config, NOW)
    with pytest.raises(Exception, match="boom"):
        pv.snapshot(FakeProfile(RuntimeError("boom")), conn, ready, config, NOW)
    assert conn.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0] == 0
    assert files(config) == []


def test_snapshot_needs_an_account(conn, settings, config):
    settings.set("upwork.talent_org_uid", "")
    bridge = FakeProfile()
    with pytest.raises(pv.ProfileVersionError, match="account"):
        pv.snapshot(bridge, conn, settings, config, NOW)
    assert bridge.calls == []


def test_retention_keeps_newest_rows_and_files(conn, ready, config):
    ready.set("profile_versions.keep", 2)
    for i in range(4):
        pv.snapshot(
            FakeProfile(payload(title=f"T{i}")), conn, ready, config, NOW + timedelta(minutes=i)
        )
    titles = [r["title"] for r in pv.list_versions(conn)]
    assert titles == ["T3", "T2"]
    assert len(files(config)) == 2


def test_defaults_exist(settings):
    assert settings.get("profile_versions.daily") is True
    assert settings.get("profile_versions.keep") > 0
    assert settings.get("profile_versions.diff_max_chars") > 0


# ---------------------------------------------------------------- diff


def test_fields_diff_uses_compare(conn, ready, config):
    pv.snapshot(
        FakeProfile(payload(title="Old title here", skills=["a", "b"])), conn, ready, config, NOW
    )
    pv.snapshot(
        FakeProfile(payload(title="New title here", skills=["a", "b", "c"])),
        conn,
        ready,
        config,
        NOW + timedelta(days=1),
    )
    new, old = pv.list_versions(conn)
    d = {f["field"]: f for f in pv.fields_diff(pv.row_fields(old), pv.row_fields(new), ready)}
    assert d["title"]["changed"] and ["del", "Old"] in d["title"]["ops"]
    assert ["ins", "New"] in d["title"]["ops"]
    assert d["skills"]["changed"] and d["skills"]["added_words"] == 1
    assert not d["description"]["changed"] and not d["rate"]["changed"]
    assert d["title"]["restorable"] and not d["rate"]["restorable"]


# ---------------------------------------------------------------- web


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.worker.sync = True  # each action runs inside the click (plan W3)
    app.state.settings.set("upwork.talent_org_uid", ORG)
    app.state.mcp = FakeProfile()
    app.state.mcp_factory = lambda: app.state.mcp
    return app, client


def post(client, path, **data):
    token = csrf_from(client.get("/profile/versions").text)
    return client.post(path, data={"csrf_token": token, **data}, follow_redirects=True)


def two_versions(app, old_title="Old title", new_title="New title"):
    conn, s, cfg = app.state.db, app.state.settings, app.state.config
    pv.snapshot(FakeProfile(payload(title=old_title, skills=["n8n"])), conn, s, cfg, NOW)
    pv.snapshot(
        FakeProfile(payload(title=new_title, skills=["n8n", "Make"])),
        conn,
        s,
        cfg,
        NOW + timedelta(days=1),
    )
    new, old = pv.list_versions(conn)
    return int(old["id"]), int(new["id"])


def test_page_needs_login(config):
    _, client = make_web_client(config)
    r = client.get("/profile/versions", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_page_empty_and_nav_link(web):
    _, client = web
    r = client.get("/profile/versions")
    assert r.status_code == 200
    assert "No copy is saved yet" in r.text
    assert 'href="/profile/versions"' in r.text and 'aria-current="page">History' in r.text


def test_page_lists_versions_and_shows_diff(web):
    app, client = web
    old, new = two_versions(app)
    r = client.get("/profile/versions")
    assert r.status_code == 200
    assert "<del>Old</del>" in r.text and "<ins>New</ins>" in r.text  # default: newest vs previous
    assert 'id="diff-title"' in r.text and 'id="diff-skills"' in r.text
    assert f"/profile/versions/{old}/rollback" in r.text
    assert f"/profile/versions/{new}/rollback" not in r.text  # the newest has no Roll back
    swapped = client.get(f"/profile/versions?a={new}&b={old}")
    assert "<del>New</del>" in swapped.text and "<ins>Old</ins>" in swapped.text


def test_page_diff_escapes_text(web):
    app, client = web
    two_versions(app, old_title="Fine", new_title="<script>alert(1)</script>")
    r = client.get("/profile/versions")
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text


def test_page_ignores_bad_query(web):
    app, client = web
    two_versions(app)
    assert client.get("/profile/versions?a=zzz&b=99999").status_code == 200


def test_snapshot_button_saves_then_reports_unchanged(web, config):
    app, client = web
    r = post(client, "/profile/versions/snapshot")
    assert r.status_code == 200 and "A new version of the profile was saved" in r.text
    assert app.state.mcp.closed == 1 and len(files(config)) == 1
    assert pv.latest(app.state.db)["reason"] == "manual"
    r = post(client, "/profile/versions/snapshot")
    assert "has not changed" in r.text and len(files(config)) == 1


def test_snapshot_button_shows_errors(web):
    app, client = web
    app.state.mcp = FakeProfile(UpworkAuthError("log in again"))
    r = post(client, "/profile/versions/snapshot")
    assert "Failed" in r.text and "log in again" in r.text
    app.state.mcp = FakeProfile(RuntimeError("boom"))
    assert "Could not read the profile" in post(client, "/profile/versions/snapshot").text


def test_snapshot_needs_csrf(web):
    _, client = web
    assert client.post("/profile/versions/snapshot", data={}).status_code in (400, 403)


# ---------------------------------------------------------------- roll back request


def test_rollback_creates_one_approval_item(web):
    app, client = web
    old, _ = two_versions(app)
    r = post(client, f"/profile/versions/{old}/rollback")
    assert r.status_code == 200 and "waiting below" in r.text
    [item] = approvals.pending_items(app.state.db, app.state.settings)
    assert item["kind"] == "profile_rollback" and item["ref_id"] == str(old)
    assert (
        item["before_data"]["title"] == "New title" and item["after_data"]["title"] == "Old title"
    )
    assert item["has_handler"]
    assert 'class="difftext"' in r.text and "<del>New</del>" in r.text
    post(client, f"/profile/versions/{old}/rollback")  # again: still one item
    assert len(approvals.pending_items(app.state.db, app.state.settings)) == 1
    # nothing was written or previewed
    assert app.state.db.execute("SELECT COUNT(*) FROM profile_previews").fetchone()[0] == 0


def test_rollback_refuses_newest_and_unknown(web):
    app, client = web
    _, new = two_versions(app)
    assert post(client, f"/profile/versions/{new}/rollback").status_code == 409
    assert post(client, "/profile/versions/9999/rollback").status_code == 409
    assert approvals.pending_items(app.state.db, app.state.settings) == []


# ---------------------------------------------------------------- roll back approval


class FakeWriter:
    """Stands in for ClaudeUpworkWriter: checks the click like the real one, records the call."""

    calls: list = []
    fail = False
    confirm_seen = False

    def __init__(self, config, settings, conn):
        self.settings = settings

    def call(self, click, tool, args):
        if tool == writes.CONFIRM_PREVIEW:
            FakeWriter.confirm_seen = True
        writes.check_write_call(tool, args)
        writes.require_click(click, "preview", self.settings)
        click.consume("preview", self.settings)
        FakeWriter.calls.append((tool, args))
        n = len(FakeWriter.calls)
        if FakeWriter.fail:
            res = ToolResult(tool, [{"type": "text", "text": "Upwork said no"}], is_error=True)
        else:
            body = json.dumps({"preview_id": f"pv-{n}", "ok": True})
            res = ToolResult(tool, [{"type": "text", "text": body}])
        return writes.WriteCall(result=res, run_id=f"write-{n}")


@pytest.fixture
def fake_writer(monkeypatch):
    FakeWriter.calls, FakeWriter.fail, FakeWriter.confirm_seen = [], False, False
    monkeypatch.setattr(writes, "ClaudeUpworkWriter", FakeWriter)
    return FakeWriter


def queue(app, client):
    old, _ = two_versions(app)
    post(client, f"/profile/versions/{old}/rollback")
    [item] = approvals.pending_items(app.state.db, app.state.settings)
    return old, item["id"]


def approve(client, item_id):
    token = csrf_from(client.get("/approvals").text)
    r = client.post(
        f"/approvals/{item_id}/approve", data={"csrf_token": token}, follow_redirects=False
    )
    assert r.status_code == 303 and "/approvals?action=" in r.headers["location"]
    # a finished action answers with the redirect to its result, a failed one shows its message
    return client.get(r.headers["location"], follow_redirects=False)


def test_approve_makes_previews_only_and_links_to_them(web, fake_writer):
    app, client = web
    old, item_id = queue(app, client)
    app.state.mcp = FakeProfile(payload(title="New title", skills=["n8n", "Make"]))
    versions_before = len(pv.list_versions(app.state.db))
    r = approve(client, item_id)
    assert r.status_code == 303 and r.headers["location"].startswith(
        "/profile/versions?done=previewed"
    )
    # one update_profile preview per changed field (title and skills), one click each
    assert [(t, a["action"]) for t, a in fake_writer.calls] == [
        ("update_profile", "update_title"),
        ("update_profile", "set_skills"),
    ]
    assert fake_writer.calls[0][1]["params"] == {"title": "Old title"}
    assert fake_writer.calls[1][1]["params"] == {"skills": ["n8n"]}
    assert not fake_writer.confirm_seen
    rows = app.state.db.execute("SELECT * FROM profile_previews ORDER BY id").fetchall()
    assert [(r["field"], r["status"], r["preview_id"]) for r in rows] == [
        ("title", "previewed", "pv-1"),
        ("skills", "previewed", "pv-2"),
    ]
    assert rows[0]["approval_id"] == item_id and rows[0]["version_id"] == old
    # a copy of the current profile was kept first (content equals the newest: nothing new saved)
    assert len(pv.list_versions(app.state.db)) == versions_before
    status = app.state.db.execute(
        "SELECT status FROM approvals WHERE id = ?", (item_id,)
    ).fetchone()
    assert status["status"] == "approved"
    page = client.get("/profile/versions")
    assert "Preview ready" in page.text and "Phase O3" not in page.text
    assert page.text.count("Preview ready") == 2


def test_approve_saves_a_copy_before_when_profile_moved_on(web, fake_writer):
    app, client = web
    _, item_id = queue(app, client)
    app.state.mcp = FakeProfile(payload(title="Edited on Upwork meanwhile", skills=["n8n", "Make"]))
    approve(client, item_id)
    assert pv.latest(app.state.db)["reason"] == "before_change"
    assert pv.latest(app.state.db)["title"] == "Edited on Upwork meanwhile"
    assert len(fake_writer.calls) == 2


def test_approve_never_confirms_even_with_dry_run_off(web, fake_writer, monkeypatch):
    app, client = web
    app.state.settings.set("general.dry_run", False)

    def boom(*a, **k):
        raise AssertionError("confirm_preview must never be called from a roll back")

    monkeypatch.setattr(writes, "confirm_preview", boom)
    _, item_id = queue(app, client)
    approve(client, item_id)
    assert all(t == "update_profile" for t, _ in fake_writer.calls) and fake_writer.calls
    assert not fake_writer.confirm_seen


def test_all_previews_failing_keeps_the_item_pending(web, fake_writer):
    app, client = web
    _, item_id = queue(app, client)
    fake_writer.fail = True
    r = approve(client, item_id)
    assert "Failed" in r.text
    assert "Upwork said no" in r.text
    row = app.state.db.execute("SELECT status FROM approvals WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "pending"
    failed = app.state.db.execute("SELECT status FROM profile_previews").fetchall()
    assert failed and all(f["status"] == "failed" for f in failed)


def test_no_copy_no_preview(web, fake_writer):
    app, client = web
    _, item_id = queue(app, client)
    app.state.mcp = FakeProfile(RuntimeError("read failed"))
    r = approve(client, item_id)
    assert "Failed" in r.text and "copy of the current profile" in r.text
    assert fake_writer.calls == []
    row = app.state.db.execute("SELECT status FROM approvals WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "pending"


def test_kill_switch_blocks_previews(web, monkeypatch):
    app, client = web
    _, item_id = queue(app, client)
    app.state.settings.set("general.kill_switch", True)
    r = approve(client, item_id)  # the real writer refuses before it starts claude
    assert "Failed" in r.text and "kill switch" in r.text.lower()
    row = app.state.db.execute("SELECT status FROM approvals WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "pending"


def test_reject_makes_nothing(web, fake_writer):
    app, client = web
    _, item_id = queue(app, client)
    token = csrf_from(client.get("/approvals").text)
    client.post(f"/approvals/{item_id}/reject", data={"csrf_token": token})
    assert fake_writer.calls == []
    assert app.state.db.execute("SELECT COUNT(*) FROM profile_previews").fetchone()[0] == 0
    row = app.state.db.execute("SELECT status FROM approvals WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "rejected"


def test_approve_without_a_web_click_stays_pending(web):
    app, client = web
    _, item_id = queue(app, client)
    with pytest.raises(approvals.ApprovalError, match="web page"):
        approvals.decide(app.state.db, item_id, "approved")
    row = app.state.db.execute("SELECT status FROM approvals WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "pending"


# ---------------------------------------------------------------- write helper rules


def test_write_list_allows_only_three_profile_actions():
    ok = {"org_uid": ORG, "params": {"title": "x"}}
    for action in ("update_title", "update_overview", "set_skills"):
        writes.check_write_call("update_profile", {"action": action, **ok})
    for action in ("add_employment", "remove_education", "set_video", "update_availability"):
        with pytest.raises(writes.WriteRefused):
            writes.check_write_call("update_profile", {"action": action, **ok})
    assert "update_profile" not in writes.CONFIRM_TYPES.values()


def test_preview_helper_needs_a_click_and_valid_values(conn, ready, config):
    class NeverCalled:
        def call(self, *a, **k):
            raise AssertionError("no call expected")

    with pytest.raises(writes.WriteRefused):
        writes.preview_profile_update(None, conn, ready, NeverCalled(), "rate", "30")
    with pytest.raises(writes.WriteRefused):
        writes.preview_profile_update(None, conn, ready, NeverCalled(), "title", "  ")
    with pytest.raises(writes.WriteRefused):
        writes.preview_profile_update(None, conn, ready, NeverCalled(), "skills", "n8n")
    real = writes.ClaudeUpworkWriter(config, ready, conn)
    with pytest.raises(writes.ClickRequired):  # a real writer without a UserClick
        writes.preview_profile_update(None, conn, ready, real, "title", "New")
    assert conn.execute("SELECT COUNT(*) FROM profile_previews").fetchone()[0] == 0


def test_read_bridge_still_refuses_update_profile():
    from app.upwork import bridge

    assert "update_profile" not in bridge.READ_TOOLS
    with pytest.raises(bridge.ToolNotAllowed):
        bridge.check_tools(["update_profile"])


# ---------------------------------------------------------------- daily


@pytest.mark.profile_versions
def test_daily_part_saves_then_reports_unchanged(conn, ready, config):
    lines = daily.profile_versions_part(conn, ready, config, FakeProfile(), NOW)
    assert lines == ["Profile copy saved (version 1)."]
    lines = daily.profile_versions_part(conn, ready, config, FakeProfile(), NOW)
    assert lines == ["Profile unchanged since the last copy."]
    assert len(pv.list_versions(conn)) == 1


@pytest.mark.profile_versions
def test_daily_part_never_raises_and_sets_then_clears_the_alert(conn, ready, config):
    for err in (RuntimeError("bridge exploded"), UpworkAuthError("login"), {"data": {}}):
        lines = daily.profile_versions_part(conn, ready, config, FakeProfile(err), NOW)
        assert lines and "failed" in lines[0]
    alert = conn.execute(
        "SELECT message FROM alerts WHERE key = 'profile_snapshot_failed'"
    ).fetchone()
    assert alert
    daily.profile_versions_part(conn, ready, config, FakeProfile(), NOW)
    assert (
        conn.execute("SELECT 1 FROM alerts WHERE key = 'profile_snapshot_failed'").fetchone()
        is None
    )


@pytest.mark.profile_versions
def test_daily_part_off_by_setting(conn, ready, config):
    ready.set("profile_versions.daily", False)
    bridge = FakeProfile()
    assert daily.profile_versions_part(conn, ready, config, bridge, NOW) == []
    assert bridge.calls == []


class DailyClient(FakeMCP):
    def __init__(self, settings, profile):
        super().__init__(settings, search={}, details={})
        self.profile = profile

    def call_many(self, calls):
        if calls and calls[0][0] == "get_profile":
            return self.profile.call_many(calls)
        return super().call_many(calls)


@pytest.mark.profile_versions
def test_daily_run_saves_a_copy_and_a_failure_does_not_fail_the_run(conn, ready, config):
    res = daily.run_daily(conn, ready, config, DailyClient(ready, FakeProfile()), now=NOW)
    assert res.status == "ok" and len(pv.list_versions(conn)) == 1
    note = (config.data_dir / "memory" / "daily" / "2026-11-06.md").read_text()
    assert "Profile copy saved" in note
    res = daily.run_daily(
        conn, ready, config, DailyClient(ready, FakeProfile(RuntimeError("nope"))), now=NOW
    )
    assert res.status == "ok"
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'profile_snapshot_failed'").fetchone()
