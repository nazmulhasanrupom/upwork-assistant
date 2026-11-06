"""Profile test loop (plan O3): the pick, the draft and its checks, the approvals item, approve
(previews only), the confirm click, the experiment, the batch button, the judge.
Fake bridge, fake writer, fake claude: no network, no real Claude, no Upwork."""

import contextlib
import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest

from app import approvals, mail, playbook, profile_loop
from app import profile_versions as pv
from app.db import init_db
from app.settings import Settings
from app.tasks import daily, lock, scheduler, trigger
from app.upwork import writes
from app.upwork.client import ToolResult, UpworkAuthError
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.test_profile_versions import payload
from tests.upwork_fakes import load, to_result

ORG = "1000000000000000001"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
EM = "—"


class Bridge:
    """The READ bridge: get_profile and find_freelancers search."""

    def __init__(self, profile=None, people=None):
        self.profile = payload() if profile is None else profile
        self.people = load("find_freelancers_search.json") if people is None else people
        self.calls = []
        self.budgets = []
        self.closed = 0

    def call_budget(self, key):
        self.budgets.append(key)
        return contextlib.nullcontext()

    def close(self):
        self.closed += 1

    def call_many(self, calls):
        out = []
        for tool, args in calls:
            self.calls.append((tool, args))
            data = self.profile if tool == "get_profile" else self.people
            if isinstance(data, UpworkAuthError):
                raise data
            if isinstance(data, Exception):
                out.append(ToolResult(tool, [{"type": "text", "text": str(data)}], is_error=True))
            else:
                out.append(to_result(tool, data))
        return out


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    s.set("rank.keywords", ["n8n automation", "Zapier"])
    s.set("keywords.baseline_started", (TODAY - timedelta(days=30)).isoformat())
    return s


@pytest.fixture
def user_md(config):
    mem = config.data_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "USER.md").write_text("# Me\nI build n8n and Make.com flows for marketing teams.\n")


def run(conn, settings, config, bridge=None):
    return profile_loop.run_profile_test(conn, settings, config, bridge or Bridge(), now=NOW)


def count(conn, table, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


# ---------------------------------------------------------------- when and candidates


def test_defaults_exist(settings):
    assert settings.get("profile_loop.every_days") == 14
    assert settings.get("profile_loop.title_max_chars") > 0
    assert settings.get("profile_loop.description_max_chars") > 0
    assert settings.get("profile_loop.batch_confirms") is True


def test_due_uses_every_days(settings):
    assert profile_loop.due(settings, NOW)  # never tried
    settings.set("profile_loop.last_run", (NOW - timedelta(days=13)).isoformat())
    assert not profile_loop.due(settings, NOW)
    settings.set("profile_loop.last_run", (NOW - timedelta(days=14)).isoformat())
    assert profile_loop.due(settings, NOW)
    settings.set("profile_loop.every_days", 30)
    assert not profile_loop.due(settings, NOW)


def test_candidates_are_writable_untested_and_not_alert_or_manual(config, settings):
    nos = [c["no"] for c in profile_loop.candidates(config, settings)]
    assert nos and set(nos) <= {1, 2, 3, 4, 9}
    settings.set("profile_loop.writable_tactics", [2])
    assert [c["no"] for c in profile_loop.candidates(config, settings)] == [2]
    settings.set("profile_loop.eligible_statuses", ["worked"])
    assert profile_loop.candidates(config, settings) == []


def test_summarize_competitors_gives_patterns_and_titles_apart():
    rows = load("find_freelancers_search.json")["freelancers"]
    s = profile_loop.summarize_competitors(rows)
    assert s["profiles"] == len(rows) and "titles" in s
    assert profile_loop.summarize_competitors([1, None, "x"])["profiles"] == 0


# ---------------------------------------------------------------- validate pick


CANDS = [
    {"no": 2, "tactic": "Title formula", "type": "Ask first", "status": "untested"},
    {"no": 3, "tactic": "Skills", "type": "Auto", "status": "untested"},
]


def test_validate_pick_takes_text_from_the_playbook(settings):
    got = profile_loop.validate_pick(
        {"tactic_no": 2, "field": "title", "reason": f"Low views {EM} test"}, CANDS, settings
    )
    assert got["tactic"] == "Title formula" and got["field"] == "title"
    assert EM not in got["reason"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"tactic_no": 99, "field": "title"},
        {"tactic_no": True, "field": "title"},
        {"tactic_no": "2", "field": "title"},
        {"tactic_no": 2, "field": "rate"},
        {"tactic_no": 2},
    ],
)
def test_validate_pick_refuses_bad_answers(settings, bad):
    with pytest.raises(profile_loop.ProfileLoopError):
        profile_loop.validate_pick(bad, CANDS, settings)


def test_validate_pick_cuts_reason(settings):
    settings.set("profile_loop.reason_max_chars", 10)
    got = profile_loop.validate_pick(
        {"tactic_no": 3, "field": "skills", "reason": "x" * 50}, CANDS, settings
    )
    assert len(got["reason"]) == 10


# ---------------------------------------------------------------- validate draft


PICK_T = {"field": "title"}
PICK_D = {"field": "description"}
PICK_S = {"field": "skills"}


def draft(pick, output, current, settings, sources="n8n Make.com 25", titles=()):
    return profile_loop.validate_draft(output, pick, current, settings, sources, list(titles))


def test_draft_title_ok_and_em_dash_cleaned(settings):
    value, summary = draft(
        PICK_T,
        {"field": "title", "text": f"n8n {EM} Make.com expert", "summary": f"Short {EM} clear"},
        "Old",
        settings,
    )
    assert EM not in value and EM not in summary and value.startswith("n8n")


@pytest.mark.parametrize(
    "output,message",
    [
        ({"field": "description", "text": "x"}, "wrong field"),
        ({"field": "title"}, "no text"),
        ({"field": "title", "text": "  "}, "no text"),
        ({"field": "title", "text": "Old"}, "does not change"),
        ({"field": "title", "text": "n8n <b>expert</b>"}, "markup"),
        ({"field": "title", "text": "n8n expert 87 flows"}, "numbers"),
        ({"field": "title", "text": "x" * 71}, "limit is 70"),
        ("text", "wrong field"),
    ],
)
def test_draft_title_refusals(settings, output, message):
    with pytest.raises(profile_loop.ProfileLoopError, match=message):
        draft(PICK_T, output, "Old", settings)


def test_draft_limits_come_from_settings(settings):
    settings.set("profile_loop.title_max_chars", 5)
    with pytest.raises(profile_loop.ProfileLoopError, match="limit is 5"):
        draft(PICK_T, {"field": "title", "text": "Too long"}, "Old", settings)
    settings.set("profile_loop.description_max_chars", 20)
    with pytest.raises(profile_loop.ProfileLoopError, match="limit is 20"):
        draft(PICK_D, {"field": "description", "text": "x" * 21}, "Old", settings)
    ok, _ = draft(PICK_D, {"field": "description", "text": "n8n " * 5}, "Old", settings)
    assert ok.startswith("n8n")


def test_draft_refuses_copy_of_a_competitor_title(settings):
    titles = ["Senior Automation Engineer for Zapier and Make experts"]
    with pytest.raises(profile_loop.ProfileLoopError, match="another freelancer"):
        draft(
            PICK_T,
            {"field": "title", "text": "Senior Automation Engineer for Zapier today"},
            "Old",
            settings,
            titles=titles,
        )
    ok, _ = draft(
        PICK_T,
        {"field": "title", "text": "Senior Automation Engineer"},
        "Old",
        settings,
        titles=titles,
    )
    assert ok


def test_draft_skills_checks(settings):
    out = {"field": "skills", "skills": ["n8n", "n8n", " Make.com "]}
    ok, _ = draft(PICK_S, out, ["a"], settings)
    assert ok == ["n8n", "Make.com"]  # trimmed, no duplicates
    for bad in (None, "n8n", [1], []):
        with pytest.raises(profile_loop.ProfileLoopError):
            draft(PICK_S, {"field": "skills", "skills": bad}, ["a"], settings)
    with pytest.raises(profile_loop.ProfileLoopError, match="does not change"):
        draft(PICK_S, {"field": "skills", "skills": ["a"]}, ["a"], settings)
    settings.set("profile_loop.skills_max", 2)
    with pytest.raises(profile_loop.ProfileLoopError, match="more skills"):
        draft(PICK_S, {"field": "skills", "skills": ["a", "b", "c"]}, ["x"], settings)
    settings.set("profile_loop.skill_max_chars", 3)
    with pytest.raises(profile_loop.ProfileLoopError, match="length limit"):
        draft(PICK_S, {"field": "skills", "skills": ["abcd"]}, ["x"], settings)


# ---------------------------------------------------------------- the run


def test_run_makes_one_profile_change_item(conn, settings, config, fake_claude_gen, user_md):
    bridge = Bridge()
    res = run(conn, settings, config, bridge)
    assert res.status == "ok" and res.item_id
    # reads only, with the task's own budget
    assert {t for t, _ in bridge.calls} == {"get_profile", "find_freelancers"}
    search = next(a for t, a in bridge.calls if t == "find_freelancers")
    assert search["action"] == "search" and search["params"]["limit"] == 10
    assert search["params"]["query"] == "n8n automation" and search["org_uid"] == ORG
    assert bridge.budgets == ["profile_loop.max_mcp_calls"]
    [item] = approvals.pending_items(conn, settings, NOW)
    assert item["kind"] == "profile_change" and item["id"] == res.item_id
    assert item["before_data"]["title"].startswith("AI Automation")
    assert item["after_data"]["title"].startswith("New title")
    meta = profile_loop.meta(profile_loop.pending_change(conn))
    assert meta["field"] == "title" and meta["tactic_no"] and meta["tactic"] and meta["reason"]
    # nothing was written or previewed
    assert count(conn, "profile_previews") == 0 and count(conn, "experiments") == 0
    assert settings.get("profile_loop.last_run")
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["status"] == "ok"


def test_run_prompts_follow_the_rules(conn, settings, config, fake_claude_gen, user_md):
    run(conn, settings, config)
    first, second = fake_claude_gen.logs()
    blob1, blob2 = json.dumps(first), json.dumps(second)
    assert "Task: pick tactic" in blob1 and "Task: draft profile change" in blob2
    assert settings.get("models.think") in blob1 and settings.get("models.write") in blob2
    assert "Facts about the user" in blob1 and "Facts about the user" in blob2
    assert "untrusted_competitor_data" in blob1 and "profile_data" in blob1
    # the draft call gets patterns only, no competitor text
    assert "untrusted_competitor_data" not in blob2


def test_draft_prompt_carries_the_matching_skill(conn, settings, config, fake_claude_gen, user_md):
    from app import skills

    skills.ensure_skills(config, settings)
    run(conn, settings, config)
    first, second = fake_claude_gen.logs()
    assert "# Skill for this task" not in first["prompt"]  # the pick step has no skill
    assert "# Skill for this task: draft-profile-change" in second["prompt"]
    assert "- review-proposal: " in second["prompt"]  # the other skills: name and summary only


def test_run_sends_email_event_once(conn, settings, config, fake_claude_gen, user_md, monkeypatch):
    seen = []
    monkeypatch.setattr(mail, "send_event", lambda *a, **k: seen.append((a[1], a[2])) or mail.SENT)
    res = run(conn, settings, config)
    assert seen == [(profile_loop.EVENT, str(res.item_id))] and res.email == mail.SENT


def test_run_is_skipped_when_blocked(conn, settings, config, fake_claude_gen):
    cases = [
        ("general.kill_switch", True),
        ("safety.jss_guard", True),
        ("keywords.baseline_started", ""),
        ("keywords.baseline_started", TODAY.isoformat()),
        ("profile_loop.writable_tactics", []),
    ]
    for key, value in cases:
        old = settings.get(key)
        settings.set(key, value)
        bridge = Bridge()
        res = run(conn, settings, config, bridge)
        assert res.status == "skipped" and res.error, key
        assert bridge.calls == [], key
        settings.set(key, old)
    assert not fake_claude_gen.called() and count(conn, "approvals") == 0


def test_run_is_skipped_while_a_test_runs(conn, settings, config, fake_claude_gen):
    playbook.start_experiment(conn, settings, config, "Title formula", 2, NOW)
    res = run(conn, settings, config)
    assert res.status == "skipped" and "already running" in res.error
    assert not fake_claude_gen.called()


def test_run_is_skipped_while_a_change_waits(conn, settings, config, fake_claude_gen, user_md):
    assert run(conn, settings, config).status == "ok"
    res = run(conn, settings, config)
    assert res.status == "skipped" and "still waits" in res.error
    assert count(conn, "approvals", "kind = 'profile_change'") == 1


@pytest.mark.parametrize(
    "env,message",
    [
        ("FAKE_TACTIC_FAIL", "not on the candidate list"),
        ("FAKE_TACTIC_BAD", "not on the candidate list"),
        ("FAKE_DRAFT_SAME", "does not change"),
        ("FAKE_DRAFT_NUM", "numbers"),
        ("FAKE_DRAFT_HTML", "markup"),
        ("FAKE_DRAFT_BAD", "wrong field"),
    ],
)
def test_run_refuses_bad_claude_answers(
    conn, settings, config, fake_claude_gen, user_md, env, message
):
    fake_claude_gen.env(env, 1)
    res = run(conn, settings, config)
    assert res.status == "error" and message in res.error
    assert count(conn, "approvals") == 0
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["status"] == "error"


def test_run_cleans_em_dashes_in_the_draft(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_DRAFT_EM", 1)
    fake_claude_gen.env("FAKE_TACTIC_EM", 1)
    assert run(conn, settings, config).status == "ok"
    [item] = approvals.pending_items(conn, settings, NOW)
    assert EM not in json.dumps(item["after_data"], ensure_ascii=False)


def test_run_can_draft_skills(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_TACTIC_FIELD", "skills")
    fake_claude_gen.env("FAKE_TACTIC_NO", 3)
    assert run(conn, settings, config).status == "ok"
    [item] = approvals.pending_items(conn, settings, NOW)
    assert "Zapier" in item["after_data"]["skills"]
    assert "Zapier" not in item["before_data"]["skills"]


def test_run_read_errors_and_auth(conn, settings, config, fake_claude_gen, user_md):
    res = run(conn, settings, config, Bridge(people={"freelancers": []}))
    assert res.status == "error" and "no freelancers" in res.error
    res = run(conn, settings, config, Bridge(profile=UpworkAuthError("log in again")))
    assert res.status == "auth_expired" and "log in again" in res.error
    assert not fake_claude_gen.called() and count(conn, "approvals") == 0


def test_run_needs_an_account_and_a_keyword(conn, settings, config, fake_claude_gen, user_md):
    settings.set("upwork.talent_org_uid", "")
    assert run(conn, settings, config).status == "error"
    settings.set("upwork.talent_org_uid", ORG)
    settings.set("rank.keywords", [])
    res = run(conn, settings, config)
    assert res.status == "error" and "keyword" in res.error


def test_run_closes_a_bridge_it_made(conn, settings, config, fake_claude_gen, user_md, monkeypatch):
    made = []

    class Made(Bridge):
        def __init__(self, *a):
            super().__init__()
            made.append(self)

    from app.upwork import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "ClaudeUpworkBridge", Made)
    res = profile_loop.run_profile_test(conn, settings, config, now=NOW)
    assert res.status == "ok" and made[0].closed == 1


# ---------------------------------------------------------------- web: approve, confirm


class FakeWriter:
    """Stands in for ClaudeUpworkWriter: checks the click like the real one, records the call."""

    calls: list = []
    fail_confirm = False

    def __init__(self, config, settings, conn):
        self.settings = settings

    def call(self, click, tool, args):
        writes.check_write_call(tool, args)
        action = "confirm" if tool == writes.CONFIRM_PREVIEW else "preview"
        writes.require_click(click, action, self.settings)
        click.consume(action, self.settings)
        FakeWriter.calls.append((tool, args))
        n = len(FakeWriter.calls)
        bad = action == "confirm" and FakeWriter.fail_confirm
        body = json.dumps({"preview_id": f"pv-{n}", "ok": True})
        res = ToolResult(tool, [{"type": "text", "text": "no" if bad else body}], is_error=bad)
        return writes.WriteCall(result=res, run_id=f"write-{n}")


@pytest.fixture
def fake_writer(monkeypatch):
    FakeWriter.calls, FakeWriter.fail_confirm = [], False
    monkeypatch.setattr(writes, "ClaudeUpworkWriter", FakeWriter)
    return FakeWriter


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    s = app.state.settings
    s.set("upwork.talent_org_uid", ORG)
    s.set("rank.keywords", ["n8n automation"])
    s.set("keywords.baseline_started", (date.today() - timedelta(days=30)).isoformat())
    app.state.mcp = Bridge()
    app.state.mcp_factory = lambda: app.state.mcp
    return app, client


def post(client, path, page="/approvals", **data):
    token = csrf_from(client.get(page).text)
    return client.post(path, data={"csrf_token": token, **data}, follow_redirects=False)


def add_item(conn, field="title", value="n8n and Make.com Expert", tactic_no=2, title="Test: x"):
    return approvals.add(
        conn,
        "profile_change",
        title,
        ref_id="run-1",
        before={field: "AI Automation for Marketing"},
        after={
            field: value,
            "_meta": {"tactic_no": tactic_no, "tactic": "Title formula", "field": field,
                      "reason": "Low views", "summary": "Shorter"},
        },
    )  # fmt: skip


def previews(conn):
    return conn.execute("SELECT * FROM profile_previews ORDER BY id").fetchall()


def item_status(conn, item):
    return conn.execute("SELECT status FROM approvals WHERE id = ?", (item,)).fetchone()["status"]


def test_approvals_page_shows_the_change_and_the_batch_button(web):
    app, client = web
    add_item(app.state.db)
    r = client.get("/approvals")
    assert r.status_code == 200
    assert "Test: x" in r.text and "Low views" in r.text and "difftext" in r.text
    assert "/approvals/small/approve" in r.text and "Approve all small changes" in r.text
    assert "/approvals/profile-test/run" not in r.text  # a change waits: no new test button
    assert "A change is waiting for your decision" in r.text


def test_page_without_small_items_has_no_batch_button_but_a_start_button(web):
    _, client = web
    page = client.get("/approvals").text
    assert "Approve all small changes" not in page
    assert "/approvals/profile-test/run" in page


def test_approve_makes_one_preview_and_never_confirms(web, fake_writer, monkeypatch):
    app, client = web
    app.state.settings.set("general.dry_run", False)
    monkeypatch.setattr(
        writes, "confirm_preview", lambda *a, **k: pytest.fail("confirm_preview called")
    )
    item = add_item(app.state.db)
    r = post(client, f"/approvals/{item}/approve")
    assert r.status_code == 303 and r.headers["location"].startswith("/profile/versions")
    assert [(t, a["action"]) for t, a in fake_writer.calls] == [("update_profile", "update_title")]
    assert fake_writer.calls[0][1]["params"] == {"title": "n8n and Make.com Expert"}
    [p] = previews(app.state.db)
    assert p["status"] == "previewed" and p["approval_id"] == item and p["before_version_id"]
    assert p["confirmed_at"] is None
    assert count(app.state.db, "experiments") == 0
    assert pv.latest(app.state.db)["reason"] == "before_change"  # a copy came first
    page = client.get("/profile/versions").text
    assert f"/profile/previews/{p['id']}/confirm" in page
    assert "not wired" not in page and "Phase O3" not in page


def test_confirm_needs_a_click_and_dry_run_blocks_it(web, fake_writer):
    app, client = web
    item = add_item(app.state.db)
    post(client, f"/approvals/{item}/approve")
    [p] = previews(app.state.db)
    assert app.state.settings.get("general.dry_run") is True  # default
    r = post(client, f"/profile/previews/{p['id']}/confirm", page="/profile/versions")
    assert r.status_code == 303 and "done=dry_run" in r.headers["location"]
    assert all(t != writes.CONFIRM_PREVIEW for t, _ in fake_writer.calls)
    assert previews(app.state.db)[0]["confirm_status"] == "dry_run_blocked"
    assert count(app.state.db, "experiments") == 0
    # without CSRF nothing happens
    assert client.post(f"/profile/previews/{p['id']}/confirm", data={}).status_code in (400, 403)


def test_confirm_starts_the_experiment_with_baseline(web, fake_writer):
    app, client = web
    conn = app.state.db
    app.state.settings.set("general.dry_run", False)
    conn.execute(
        "INSERT INTO metrics_daily(date, created_at, jss, proposals_sent) VALUES (?,?,?,?)",
        (date.today().isoformat(), NOW.isoformat(), 98.0, 12),
    )
    item = add_item(conn)
    post(client, f"/approvals/{item}/approve")
    [p] = previews(conn)
    r = post(client, f"/profile/previews/{p['id']}/confirm", page="/profile/versions")
    assert r.status_code == 303 and "done=test_started" in r.headers["location"]
    confirm_call = fake_writer.calls[-1]
    assert confirm_call[0] == writes.CONFIRM_PREVIEW
    assert confirm_call[1]["params"] == {"type": "profile", "preview_id": p["preview_id"]}
    exp = playbook.active_experiment(conn)
    assert exp["tactic_no"] == 2 and exp["approval_id"] == item and exp["field"] == "title"
    assert exp["version_id"] == p["before_version_id"]
    base = json.loads(exp["baseline_json"])
    assert base["metrics"]["jss"] == 98.0 and base["metrics"]["proposals_sent"] == 12
    assert playbook.tactics(app.state.config)[1]["status"] == "testing"
    # the same preview cannot be confirmed twice
    r = post(client, f"/profile/previews/{p['id']}/confirm", page="/profile/versions")
    assert r.status_code == 409 and count(conn, "experiments") == 1


def test_confirm_failure_starts_no_test(web, fake_writer):
    app, client = web
    app.state.settings.set("general.dry_run", False)
    fake_writer.fail_confirm = True
    item = add_item(app.state.db)
    post(client, f"/approvals/{item}/approve")
    [p] = previews(app.state.db)
    r = post(client, f"/profile/previews/{p['id']}/confirm", page="/profile/versions")
    assert r.status_code == 502
    assert count(app.state.db, "experiments") == 0


def test_approve_is_blocked_while_a_test_runs(web, fake_writer):
    app, client = web
    playbook.start_experiment(app.state.db, app.state.settings, app.state.config, "Other", 3)
    item = add_item(app.state.db)
    r = post(client, f"/approvals/{item}/approve")
    assert r.status_code == 409 and "already running" in r.text
    assert fake_writer.calls == [] and item_status(app.state.db, item) == "pending"


def test_jss_guard_blocks_approve(web, fake_writer):
    app, client = web
    app.state.settings.set("safety.jss_guard", True)
    item = add_item(app.state.db)
    assert post(client, f"/approvals/{item}/approve").status_code == 409
    assert fake_writer.calls == []


def test_reject_changes_nothing(web, fake_writer):
    app, client = web
    item = add_item(app.state.db)
    post(client, f"/approvals/{item}/reject")
    assert fake_writer.calls == [] and count(app.state.db, "profile_previews") == 0
    assert item_status(app.state.db, item) == "rejected"


def test_failed_preview_keeps_the_item_pending(web, monkeypatch):
    app, client = web

    class Failing(FakeWriter):
        def call(self, click, tool, args):
            click.consume("preview", self.settings)
            res = ToolResult(tool, [{"type": "text", "text": "Upwork said no"}], is_error=True)
            return writes.WriteCall(result=res, run_id="w")

    monkeypatch.setattr(writes, "ClaudeUpworkWriter", Failing)
    item = add_item(app.state.db)
    r = post(client, f"/approvals/{item}/approve")
    assert r.status_code == 409 and "Upwork said no" in r.text
    assert item_status(app.state.db, item) == "pending"


def test_rollback_preview_is_confirmed_the_same_way(web, fake_writer):
    app, client = web
    conn, s, cfg = app.state.db, app.state.settings, app.state.config
    s.set("general.dry_run", False)
    pv.snapshot(Bridge(payload(title="Old title")), conn, s, cfg, NOW - timedelta(days=2))
    pv.snapshot(Bridge(payload(title="New title")), conn, s, cfg, NOW - timedelta(days=1))
    old = pv.list_versions(conn)[1]["id"]
    app.state.mcp = Bridge(payload(title="New title"))
    post(client, f"/profile/versions/{old}/rollback", page="/profile/versions")
    [it] = approvals.pending_items(conn, s)
    post(client, f"/approvals/{it['id']}/approve")
    [p] = previews(conn)
    r = post(client, f"/profile/previews/{p['id']}/confirm", page="/profile/versions")
    assert r.status_code == 303 and "done=confirmed" in r.headers["location"]
    assert fake_writer.calls[-1][0] == writes.CONFIRM_PREVIEW
    assert count(conn, "experiments") == 0  # a roll back starts no test


# ---------------------------------------------------------------- batch button


def test_batch_approves_only_small_items_and_blocks_confirm_in_dry_run(web, fake_writer):
    app, client = web
    conn = app.state.db
    small = add_item(conn, title="Small")
    big_text = " ".join(f"w{i}" for i in range(80))
    big = add_item(conn, field="description", value=big_text, title="Big")
    r = post(client, "/approvals/small/approve")
    loc = r.headers["location"]
    assert r.status_code == 303 and "ap=1" in loc and "bl=1" in loc and "cf=0" in loc
    assert item_status(conn, small) == "approved" and item_status(conn, big) == "pending"
    [p] = previews(conn)
    assert p["confirm_status"] == "dry_run_blocked"
    assert [t for t, _ in fake_writer.calls] == ["update_profile"]  # no confirm_preview sent
    assert count(conn, "experiments") == 0


def test_batch_confirms_each_item_after_the_click_when_dry_run_is_off(web, fake_writer):
    app, client = web
    conn = app.state.db
    app.state.settings.set("general.dry_run", False)
    add_item(conn, title="Small one")
    r = post(client, "/approvals/small/approve")
    assert "ap=1" in r.headers["location"] and "cf=1" in r.headers["location"]
    assert [t for t, _ in fake_writer.calls] == ["update_profile", writes.CONFIRM_PREVIEW]
    assert playbook.active_experiment(conn) is not None


def test_batch_can_be_preview_only(web, fake_writer):
    app, client = web
    app.state.settings.set("general.dry_run", False)
    app.state.settings.set("profile_loop.batch_confirms", False)
    add_item(app.state.db)
    post(client, "/approvals/small/approve")
    assert [t for t, _ in fake_writer.calls] == ["update_profile"]
    assert previews(app.state.db)[0]["confirmed_at"] is None


def test_batch_with_nothing_small_and_without_csrf(web, fake_writer):
    app, client = web
    r = post(client, "/approvals/small/approve")
    assert r.status_code == 303 and "batch_none" in r.headers["location"]
    add_item(app.state.db)
    assert client.post("/approvals/small/approve", data={}).status_code in (400, 403)
    assert fake_writer.calls == []


def test_batch_limit_comes_from_settings(web):
    app, _ = web
    conn, s = app.state.db, app.state.settings
    add_item(conn)
    assert len(profile_loop.small_items(conn, s)) == 1
    s.set("profile_loop.small_change_max_words", 0)
    assert profile_loop.small_items(conn, s) == []


def test_batch_blocked_item_stays_pending_and_the_rest_go_on(web, fake_writer):
    app, client = web
    conn = app.state.db
    first = add_item(conn, title="First")
    second = add_item(conn, title="Second", value="Another Title Here")
    app.state.settings.set("general.dry_run", False)
    r = post(client, "/approvals/small/approve")
    loc = r.headers["location"]
    assert "ap=1" in loc and "cf=1" in loc and "er=1" in loc
    # the first test starts; one test at a time, so the second item waits for a decision later
    assert sorted([item_status(conn, first), item_status(conn, second)]) == ["approved", "pending"]
    assert count(conn, "experiments") == 1


# ---------------------------------------------------------------- the Run button and the task


def test_run_button_starts_the_task(web, monkeypatch):
    app, client = web
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    r = post(client, "/approvals/profile-test/run")
    assert r.headers["location"] == "/approvals?done=pt_started" and started == ["profile_test"]
    assert "Profile test started" in client.get("/approvals?done=pt_started").text
    app.state.settings.set("general.kill_switch", True)
    r = post(client, "/approvals/profile-test/run")
    assert r.headers["location"] == "/approvals?done=pt_killed" and started == ["profile_test"]


def test_run_button_busy_and_unbuilt(web, monkeypatch):
    _, client = web

    def busy(cfg, name):
        raise lock.TaskBusy(name)

    monkeypatch.setattr(trigger, "start", busy)
    assert post(client, "/approvals/profile-test/run").headers["location"].endswith("pt_busy")

    def unbuilt(cfg, name):
        raise trigger.NotBuilt(name)

    monkeypatch.setattr(trigger, "start", unbuilt)
    assert post(client, "/approvals/profile-test/run").headers["location"].endswith("pt_unbuilt")


def test_profile_test_is_a_manual_and_scheduled_task():
    assert "profile_test" in trigger.MANUAL_TASKS and "profile_test" in trigger.SCHEDULED_MANUAL
    assert trigger.is_built("profile_test")


def test_scheduler_waits_for_the_interval(settings):
    assert scheduler._interval_passed(settings, "profile_test", NOW)
    settings.set("profile_loop.last_run", (NOW - timedelta(days=3)).isoformat())
    assert not scheduler._interval_passed(settings, "profile_test", NOW)
    settings.set("profile_loop.last_run", (NOW - timedelta(days=15)).isoformat())
    assert scheduler._interval_passed(settings, "profile_test", NOW)


def test_task_main_runs_and_reports(config, monkeypatch):
    from app.tasks import profile_test

    monkeypatch.setattr(profile_test, "load_config", lambda: config)

    class R:
        status = "ok"

    monkeypatch.setattr(profile_test.profile_loop, "run_profile_test", lambda *a, **k: R())
    assert profile_test.main() == 0
    R.status = "error"
    assert profile_test.main() == 1
    R.status = "skipped"
    assert profile_test.main() == 0


def test_task_main_when_busy(config, monkeypatch):
    from app.tasks import profile_test

    monkeypatch.setattr(profile_test, "load_config", lambda: config)
    fd = lock.acquire(config.data_dir, profile_test.TASK)
    try:
        assert profile_test.main() == 0
    finally:
        os.close(fd)


# ---------------------------------------------------------------- judge


def start_test(conn, settings, config, days_ago, version_id=None):
    exp_id = playbook.start_experiment(
        conn, settings, config, "Title formula", 2, NOW - timedelta(days=days_ago)
    )
    base = {
        "metrics": {"jss": 98.0, "proposals_viewed": 10.0, "interviews": 2.0},
        "rank": {"n8n automation": 20.0},
    }
    conn.execute(
        "UPDATE experiments SET baseline_json = ?, field = 'title', version_id = ? WHERE id = ?",
        (json.dumps(base), version_id, exp_id),
    )
    return exp_id


def set_now_metrics(monkeypatch, metrics, rank):
    monkeypatch.setattr(
        playbook, "baseline_metrics", lambda *a, **k: {"metrics": metrics, "rank": rank}
    )


def test_compare_worked_hurt_and_no_effect(settings):
    base = {"metrics": {"jss": 98, "interviews": 2.0}, "rank": {"k": 20.0}}

    def cmp(jss, interviews, rank):
        now = {"metrics": {"jss": jss, "interviews": interviews}, "rank": {"k": rank}}
        return profile_loop.compare(base, now, settings)

    assert cmp(98, 3.0, 10).outcome == "worked"
    assert cmp(98, 2.1, 20).outcome == "no effect"
    assert cmp(98, 1.0, 40).outcome == "hurt"
    assert cmp(94, 3.0, 10).outcome == "hurt"  # a JSS drop of 3 points or more is hurt at once
    empty = profile_loop.compare({}, {}, settings)
    assert empty.outcome == "no effect" and "Not enough" in empty.text


def test_compare_thresholds_come_from_settings(settings):
    base = {"metrics": {"interviews": 2.0}, "rank": {}}
    new = {"metrics": {"interviews": 2.4}, "rank": {}}  # +20%
    assert profile_loop.compare(base, new, settings).outcome == "worked"
    settings.set("profile_loop.change_pct", 50.0)
    assert profile_loop.compare(base, new, settings).outcome == "no effect"


def test_judge_does_nothing_before_the_end_or_without_a_test(conn, settings, config):
    assert profile_loop.judge_due(conn, settings, config, NOW) == []
    start_test(conn, settings, config, days_ago=5)
    assert profile_loop.judge_due(conn, settings, config, NOW) == []
    assert playbook.active_experiment(conn) is not None


def test_judge_keeps_a_change_that_worked(conn, settings, config, monkeypatch):
    start_test(conn, settings, config, days_ago=15)
    now_vals = {"jss": 98.0, "proposals_viewed": 20.0, "interviews": 4.0}
    set_now_metrics(monkeypatch, now_vals, {"n8n automation": 8.0})
    [line] = profile_loop.judge_due(conn, settings, config, NOW)
    assert "worked" in line
    assert playbook.active_experiment(conn) is None
    exp = conn.execute("SELECT * FROM experiments").fetchone()
    assert exp["status"] == "finished" and exp["outcome"] == "worked" and exp["result"]
    assert playbook.tactics(config)[1]["status"] == "worked"
    assert "Title formula: worked" in playbook.read(config)
    assert count(conn, "approvals") == 0


def test_judge_queues_a_rollback_item_when_it_hurt(conn, settings, config, monkeypatch):
    day = timedelta(days=1)
    pv.snapshot(Bridge(payload(title="Old title")), conn, settings, config, NOW - 16 * day)
    old = pv.latest(conn)["id"]
    pv.snapshot(Bridge(payload(title="New title")), conn, settings, config, NOW - 15 * day)
    start_test(conn, settings, config, days_ago=15, version_id=old)
    now_vals = {"jss": 98.0, "proposals_viewed": 4.0, "interviews": 0.5}
    set_now_metrics(monkeypatch, now_vals, {"n8n automation": 40.0})
    [line] = profile_loop.judge_due(conn, settings, config, NOW)
    assert "hurt" in line and "roll back waits in approvals" in line
    [item] = approvals.pending_items(conn, settings, NOW)
    assert item["kind"] == "profile_rollback" and item["after_data"]["title"] == "Old title"
    assert count(conn, "profile_previews") == 0  # queued only, nothing written
    assert conn.execute("SELECT outcome FROM experiments").fetchone()[0] == "hurt"
    assert playbook.tactics(config)[1]["status"] == "hurt"


def test_judge_hurt_without_a_saved_version(conn, settings, config, monkeypatch):
    start_test(conn, settings, config, days_ago=15, version_id=None)
    set_now_metrics(monkeypatch, {"jss": 90.0}, {})
    [line] = profile_loop.judge_due(conn, settings, config, NOW)
    assert "no roll back possible" in line and count(conn, "approvals") == 0


def test_judge_no_effect_lets_the_next_test_pick_another(conn, settings, config, monkeypatch):
    start_test(conn, settings, config, days_ago=15)
    now_vals = {"jss": 98.0, "proposals_viewed": 10.0, "interviews": 2.0}
    set_now_metrics(monkeypatch, now_vals, {"n8n automation": 20.0})
    [line] = profile_loop.judge_due(conn, settings, config, NOW)
    assert "no effect" in line and "another tactic" in line
    assert 2 not in [c["no"] for c in profile_loop.candidates(config, settings)]


def test_judge_reads_the_real_seven_day_averages(conn, settings, config):
    start_test(conn, settings, config, days_ago=15)
    for i in range(7):
        day = (TODAY - timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO metrics_daily(date, created_at, jss, proposals_viewed, interviews) "
            "VALUES (?,?,?,?,?)",
            (day, NOW.isoformat(), 98.0, 30, 6),
        )
    [line] = profile_loop.judge_due(conn, settings, config, NOW)
    assert "worked" in line


def test_daily_part_never_raises(conn, settings, config, monkeypatch):
    assert daily.profile_loop_part(conn, settings, config, NOW) == []

    def boom(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(playbook, "active_experiment", boom)
    [line] = daily.profile_loop_part(conn, settings, config, NOW)
    assert "judge failed" in line


def test_apply_change_needs_the_web_context(conn, settings):
    item = add_item(conn)
    with pytest.raises(approvals.ApprovalError, match="web page"):
        approvals.decide(conn, item, "approved")
    assert item_status(conn, item) == "pending"
