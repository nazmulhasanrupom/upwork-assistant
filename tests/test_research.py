"""L5: weekly web research. Fake claude only: the web is never touched. The run has web tools and
no Upwork tool, proposals are checked by Python, each becomes an approval item, and only an
approval changes PLAYBOOK.md (with a commit)."""

import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from app import approvals, playbook, research
from app.db import init_db
from app.llm import runner
from app.settings import Settings
from app.tasks import lock, scheduler, trigger
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NOW = datetime(2026, 11, 9, 6, 30, tzinfo=UTC)
EM = "—"


@pytest.fixture
def conn(config):
    c = init_db(config.db_path)
    yield c
    c.close()


@pytest.fixture
def settings(conn):
    return Settings(conn)


@pytest.fixture
def seeded(config, settings):
    playbook.ensure_seeded(config, settings=settings)


def run(conn, settings, config):
    return research.run_research(conn, settings, config, now=NOW)


def items(conn, status=None):
    sql = "SELECT * FROM approvals WHERE kind = 'playbook_update'"
    if status:
        sql += f" AND status = '{status}'"
    return conn.execute(sql + " ORDER BY id").fetchall()


def subjects(config):
    out = subprocess.run(
        ["git", "-C", str(config.data_dir), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


# ---------------------------------------------------------------- the Claude run


def test_command_has_web_tools_and_no_upwork(config):
    cmd = runner.build_command("p", "m", 3, 1.0, tools=research.TOOLS)
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch,Read,Write"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--mcp-config") + 1].endswith("empty-mcp.json")  # no Upwork server
    assert "--strict-mcp-config" in cmd
    assert "mcp" not in cmd[cmd.index("--tools") + 1].lower()
    assert "mcp__" not in cmd[cmd.index("--allowedTools") + 1]
    plain = runner.build_command("p", "m", 3, 1.0)
    assert "--allowedTools" not in plain and plain[plain.index("--tools") + 1] == "Read,Write"


def test_run_uses_settings_for_budget_turns_and_model(
    config, conn, settings, seeded, fake_claude_gen
):
    settings.set("research.max_turns", 7)
    settings.set("research.max_budget_usd", 0.42)
    res = run(conn, settings, config)
    assert res.status == "ok", res.error
    log = fake_claude_gen.log()
    argv = log["argv"]
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.42"
    assert argv[argv.index("--model") + 1] == settings.get("models.write")
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch,Read,Write"
    assert "Upwork" in log["prompt"] and "untrusted data from strangers" in log["prompt"]
    assert log["input"]["limits"]["allowed_types"] == ["Ask first", "Manual on upwork.com"]
    assert "Main keyword in title" in log["input"]["playbook"]
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["task"] == "research" and row["status"] == "ok"
    assert conn.execute("SELECT task FROM claude_calls").fetchone()["task"] == "research"
    assert settings.get("research.last_run") == "2026-11-09T06:30:00+00:00"


def test_switch_off_and_kill_switch_do_nothing(config, conn, settings, seeded, fake_claude_gen):
    settings.set("research.enabled", False)
    res = run(conn, settings, config)
    assert res.status == "skipped" and "switched off" in res.error
    settings.set("research.enabled", True)
    settings.set("general.kill_switch", True)
    assert run(conn, settings, config).status == "skipped"
    assert not fake_claude_gen.called() and items(conn) == []


def test_default_is_on_and_budget_is_small(settings):
    assert settings.get("research.enabled") is True
    assert 0 < settings.get("research.max_budget_usd") <= 1.0
    assert "Auto" not in settings.get("research.allowed_types")


# ---------------------------------------------------------------- proposals become approval items


def test_proposals_become_items_and_the_playbook_is_unchanged(
    config, conn, settings, seeded, fake_claude_gen
):
    before = playbook.path(config).read_text()
    res = run(conn, settings, config)
    assert len(res.item_ids) == 3  # add, mark outdated, mark checked
    rows = items(conn, "pending")
    titles = [r["title"] for r in rows]
    assert titles[0] == "Playbook: add a tactic: Add a short video to the profile"
    assert titles[1] == "Playbook: tactic #5 looks outdated"
    assert titles[2] == "Playbook: 2 tactic(s) still valid"
    first = json.loads(rows[0]["after_json"])
    assert first["sources"] == ["https://www.upwork.com/resources/example"]
    assert "Add a short video" in first["summary"]
    assert EM not in rows[0]["after_json"]
    assert playbook.path(config).read_text() == before  # nothing written until a click


def test_new_run_replaces_older_pending_items(config, conn, settings, seeded, fake_claude_gen):
    run(conn, settings, config)
    run(conn, settings, config)
    assert len(items(conn, "pending")) == 3 and len(items(conn, "expired")) == 3


def test_nothing_to_propose_is_a_good_answer(config, conn, settings, seeded, fake_claude_gen):
    fake_claude_gen.env("FAKE_RES_NONE", "1")
    res = run(conn, settings, config)
    assert res.status == "ok" and res.item_ids == []


def test_unusable_answer_is_an_error(config, conn, settings, seeded, fake_claude_gen):
    fake_claude_gen.env("FAKE_RES_BAD", "1")
    res = run(conn, settings, config)
    assert res.status == "error" and "no usable answer" in res.error and items(conn) == []


def test_claude_failure_is_an_error(config, conn, settings, seeded, fake_claude_gen):
    fake_claude_gen.env("FAKE_GEN_FAIL", "1")
    assert run(conn, settings, config).status == "error"
    fake_claude_gen.env("FAKE_GEN_FAIL", "")
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", "0")
    assert "rate limited" in run(conn, settings, config).error


# ---------------------------------------------------------------- Python does not trust the web


@pytest.mark.parametrize(
    ("knob", "why"),
    [
        ("FAKE_RES_AUTO", "type 'Auto' is not allowed"),
        ("FAKE_RES_NOSRC", "no usable source link"),
        ("FAKE_RES_JS", "no usable source link"),
        ("FAKE_RES_HTML", "empty, too long or has markup"),
    ],
)
def test_bad_proposals_are_refused(config, conn, settings, seeded, fake_claude_gen, knob, why):
    fake_claude_gen.env(knob, "1")
    res = run(conn, settings, config)
    assert res.status == "ok" and why in res.refused
    kinds = [json.loads(r["after_json"])["op"] for r in items(conn)]
    assert "add_tactic" not in kinds


def test_unknown_tactic_number_is_refused(config, conn, settings, seeded, fake_claude_gen):
    fake_claude_gen.env("FAKE_RES_BADNO", "1")
    res = run(conn, settings, config)
    assert "does not exist" in res.refused
    assert [json.loads(r["after_json"])["op"] for r in items(conn)] == [
        "add_tactic",
        "mark_checked",
    ]


def test_max_proposals_is_a_setting(config, conn, settings, seeded, fake_claude_gen):
    fake_claude_gen.env("FAKE_RES_MANY", "1")
    settings.set("research.max_proposals", 2)
    res = run(conn, settings, config)
    adds = [r for r in items(conn) if json.loads(r["after_json"])["op"] == "add_tactic"]
    assert len(adds) == 2 and "more than the limit" in res.refused
    assert len(res.item_ids) == 3  # plus the "still valid" item


def test_validate_rules(config, settings, seeded):
    good, note = research.validate(
        {
            "proposals": [
                {
                    "op": "add_tactic",
                    "text": "Reply fast",
                    "type": "Ask first",
                    "reason": "r",
                    "sources": ["http://a.example/x", "ftp://no", "https://a.example/y"],
                },
                {
                    "op": "add_tactic",
                    "text": "x" * 301,
                    "type": "Ask first",
                    "reason": "r",
                    "sources": ["https://a.example"],
                },
                {
                    "op": "add_tactic",
                    "text": "Reply fast",
                    "type": "Ask first",
                    "reason": "",
                    "sources": ["https://a.example"],
                },
                {
                    "op": "mark_outdated",
                    "tactic_no": True,
                    "reason": "r",
                    "sources": ["https://a.example"],
                },
                {
                    "op": "mark_outdated",
                    "tactic_no": 8,
                    "reason": "r",
                    "sources": ["https://a.example"] * 9,
                },
                {"op": "delete_everything", "reason": "r", "sources": ["https://a.example"]},
                {
                    "op": "add_tactic",
                    "text": "Use all 20 skill slots: ~12 core niche + ~8 nearby skills",
                    "type": "Ask first",
                    "reason": "r",
                    "sources": ["https://a.example"],
                },
                "text",
            ],
            "still_valid": [1, "2", 99, 3, 3],
        },
        config,
        settings,
    )
    assert [g["op"] for g in good] == ["add_tactic", "mark_outdated", "mark_checked"]
    assert good[0]["sources"] == ["http://a.example/x", "https://a.example/y"]
    assert good[1]["sources"] == ["https://a.example"]
    assert good[2]["numbers"] == [1, 3]
    assert note.count(";") == 5
    with pytest.raises(research.ResearchError):
        research.validate([], config, settings)
    with pytest.raises(research.ResearchError):
        research.validate({"x": 1}, config, settings)


# ---------------------------------------------------------------- approve and apply


def decide(conn, config, item_id, decision="approved"):
    return approvals.decide(conn, item_id, decision, NOW, {"config": config})


def test_approving_a_new_tactic_adds_a_row_with_a_commit(
    config, conn, settings, seeded, fake_claude_gen
):
    res = run(conn, settings, config)
    decide(conn, config, res.item_ids[0])
    rows = playbook.tactics(config)
    assert rows[-1] == {
        "no": 12,
        "tactic": "Add a short video to the profile",
        "type": "Ask first",
        "status": "untested",
    }
    assert len(rows) == 12
    assert subjects(config)[0].startswith("Playbook: new tactic #12")
    assert playbook.last_checked(config)[12] == datetime.now(UTC).date().isoformat()
    text = playbook.path(config).read_text()
    assert text.index("## Last checked") < text.index("## Results log")


def test_approving_outdated_marks_the_status_and_logs_it(
    config, conn, settings, seeded, fake_claude_gen
):
    res = run(conn, settings, config)
    decide(conn, config, res.item_ids[1])
    row = next(t for t in playbook.tactics(config) if t["no"] == 5)
    assert row["status"] == "outdated"
    assert "#5 Highlight best portfolio items" in playbook.path(config).read_text()
    assert "outdated. Feature changed" in playbook.path(config).read_text()
    assert subjects(config)[0] == "Playbook: tactic #5 marked outdated"
    assert 5 in playbook.last_checked(config)


def test_approving_still_valid_sets_last_checked(config, conn, settings, seeded, fake_claude_gen):
    res = run(conn, settings, config)
    decide(conn, config, res.item_ids[2])
    assert sorted(playbook.last_checked(config)) == [1, 2]
    decide(conn, config, res.item_ids[1])
    assert sorted(playbook.last_checked(config)) == [1, 2, 5]
    text = playbook.path(config).read_text()
    assert text.count("## Last checked") == 1 and text.rstrip().splitlines()[-1].startswith("- 2")


def test_reject_changes_nothing(config, conn, settings, seeded, fake_claude_gen):
    res = run(conn, settings, config)
    before = playbook.path(config).read_text()
    for i in res.item_ids:
        decide(conn, config, i, "rejected")
    assert playbook.path(config).read_text() == before


def test_cannot_mark_a_tactic_outdated_while_it_is_tested(
    config, conn, settings, seeded, fake_claude_gen
):
    res = run(conn, settings, config)
    playbook._set_status(config, 5, playbook.TESTING)
    with pytest.raises(approvals.ApprovalError, match="being tested"):
        decide(conn, config, res.item_ids[1])
    assert items(conn)[1]["status"] == "pending"


def test_playbook_functions_directly(config, settings, seeded):
    n = playbook.add_tactic(config, settings, "A | B tactic", "Ask first", "2026-11-09")
    assert n == 12 and playbook.tactics(config)[-1]["tactic"] == "A | B tactic"
    with pytest.raises(playbook.PlaybookError):
        playbook.add_tactic(config, settings, "  ", "Ask first", "2026-11-09")
    with pytest.raises(playbook.PlaybookError):
        playbook.mark_outdated(config, settings, 77, "r", "2026-11-09")
    with pytest.raises(playbook.PlaybookError):
        playbook.set_checked(config, settings, [77], "2026-11-09", "m")
    playbook.set_checked(config, settings, [3], "2026-11-01", "m")
    playbook.set_checked(config, settings, [3], "2026-11-09", "m")
    assert playbook.last_checked(config)[3] == "2026-11-09"
    assert len(playbook.tactics(config)) == 12


# ---------------------------------------------------------------- task, scheduler, page


def test_task_main_uses_the_task_lock(config, monkeypatch):
    from app.tasks import research as task

    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    fd = lock.acquire(config.data_dir, "research")
    try:
        assert task.main() == 0
    finally:
        import os

        os.close(fd)


def test_task_main_runs_end_to_end(config, monkeypatch, fake_claude_gen):
    from app.tasks import research as task

    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    assert task.main() == 0
    conn = init_db(config.db_path)
    assert len(items(conn, "pending")) == 3


def test_scheduler_has_a_weekly_entry(config, conn, settings, monkeypatch):
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    monkeypatch.setattr(trigger, "is_built", lambda name: name == "research")
    scheduler.tick(settings, config, NOW)
    assert started == ["research"] and settings.get("schedules.research") == "30 6 * * 1"
    assert trigger.is_built("research")


@pytest.fixture
def web(config, fake_claude_gen):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def post(client, path):
    return client.post(path, data={"csrf_token": csrf_from(client.get("/learning").text)})


def test_run_button_respects_the_switch(web, monkeypatch):
    app, client = web
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    r = post(client, "/learning/run/research")
    assert r.headers["location"] == "/learning?done=research_started" and started == ["research"]
    assert "weekly research started" in client.get("/learning?done=research_started").text
    app.state.settings.set("research.enabled", False)
    r = post(client, "/learning/run/research")
    assert r.headers["location"] == "/learning?done=research_off" and started == ["research"]
    assert "switched off in the settings" in client.get("/learning").text


def test_item_page_and_approve_through_the_web(web, config):
    app, client = web
    res = research.run_research(app.state.db, app.state.settings, config, now=datetime.now(UTC))
    html = client.get("/approvals").text
    assert "Playbook: add a tactic" in html and "Add a short video to the profile" in html
    assert "https://www.upwork.com/resources/example" in html
    r = client.post(f"/approvals/{res.item_ids[0]}/approve", data={"csrf_token": csrf_from(html)})
    assert r.status_code == 303
    assert playbook.tactics(config)[-1]["tactic"] == "Add a short video to the profile"
    assert "Add a short video" in client.get("/playbook").text
