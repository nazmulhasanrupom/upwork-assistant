"""Round 3 A2: countdown, Approve/Stop, auto-send with system consent, emails.

No real Upwork, Claude or Gmail: the READ bridge and the write runs are fakes, emails are
collected in a list."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import auto_send, auto_send_flow, mail
from app import job_routes as routes
from app.db import init_db
from app.llm.runner import ClaudeFailed
from app.settings import Settings
from app.tasks import scheduler, trigger
from app.upwork import writes
from app.upwork.client import ToolResult
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.test_h2_job_actions import FakeReader as BaseReader
from tests.test_writes import JOB, ORG, SENT_JOB

LETTER = "Your CRM and billing do not talk to each other. I build the n8n sync that fixes it."
JOB2 = "4000000000000000777"


class Reader(BaseReader):
    swap = None  # an id that should show up in the proposal list ("already applied")

    def call_many(self, calls):
        out = super().call_many(calls)
        if self.swap:
            for r in out:
                for c in r.content:
                    c["text"] = c["text"].replace(SENT_JOB, self.swap)
        return out


class FakeWriter(writes.ClaudeUpworkWriter):
    """The real click / kill-switch / dry-run gates, with no claude run behind them."""

    calls: list = []
    count = 0
    balance = 60
    confirm_error: Exception | None = None

    def _run(self, tool, args, click):
        type(self).calls.append((tool, args["action"], click.user))
        if tool == writes.MANAGE_PROPOSALS:
            type(self).count += 1
            data = {
                "preview_id": f"pv-{type(self).count}",
                "connects_cost": 16,
                "connects_balance": type(self).balance,
                "can_apply": True,
            }
            text = json.dumps({"status": "ok", "data": data})
        else:
            if type(self).confirm_error:
                raise type(self).confirm_error
            text = json.dumps({"status": "ok", "data": {"submitted": True}})
        return writes.WriteCall(
            result=ToolResult(tool, [{"type": "text", "text": text}]), run_id=f"fake-{tool}"
        )


@pytest.fixture(autouse=True)
def reset_writer():
    FakeWriter.calls, FakeWriter.count = [], 0
    FakeWriter.balance, FakeWriter.confirm_error = 60, None


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    s.set("general.dry_run", False)
    return s


@pytest.fixture
def writer(config, settings, conn):
    return FakeWriter(config, settings, conn)


@pytest.fixture
def emails(monkeypatch):
    sent = []

    def fake_send(conn, event, ref_id, subject, text, html, **kw):
        sent.append({"event": event, "ref": ref_id, "subject": subject, "text": text})
        return mail.SENT

    monkeypatch.setattr(mail, "send_event", fake_send)
    return sent


def seed(conn, job_id=JOB, letter=LETTER, rules_ok=1, status="drafted"):
    conn.execute(
        "INSERT INTO jobs(job_id, first_seen, title, url, job_type, connects_cost, status, "
        "client_rating, client_spend, activity_label, details) VALUES (?, ?, 'n8n CRM flow', ?, "
        "'hourly', 16, ?, 4.9, 50000, 'GOOD', ?)",
        (
            job_id,
            "2026-11-06T10:00:00+00:00",
            f"https://www.upwork.com/jobs/~02{job_id}",
            status,
            json.dumps({"payment_verified": True, "applied": False}),
        ),
    )
    conn.execute(
        "INSERT INTO judgements(job_id, fit_score, why_apply, risks, plan, suggested_rate, "
        "skills_match_all, win_chance, client_quality) VALUES (?, 95, 'Great fit', '', '{}', 25, "
        "1, 'high', 'good')",
        (job_id,),
    )
    conn.execute(
        "INSERT INTO drafts(job_id, cover_letter, answers, materials, suggested_rate, flags, "
        "created_at, rules_ok) VALUES (?, ?, '[]', '[]', 25, '[]', ?, ?)",
        (job_id, letter, "2026-11-06T10:05:00+00:00", rules_ok),
    )
    conn.execute(
        "INSERT INTO connects(balance, needed, seen_at) "
        "VALUES (60, 16, '2026-11-07T00:00:00+00:00')"
    )


def start(conn, settings, config, writer, job_ids=(JOB,), reader=None, now=None):
    return auto_send_flow.start_countdowns(
        conn, settings, config, reader or Reader(), list(job_ids), writer=writer, now=now
    )


def make_due(conn, row_id):
    """Pretend the 2 hours are over (the clock the gate reads is the real one)."""
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE auto_sends SET deadline = ? WHERE id = ?", (past, row_id))


def run(conn, settings, config, writer, reader=None):
    return auto_send_flow.run_due(
        conn,
        settings,
        config,
        reader_factory=lambda: reader or Reader(),
        writer_factory=lambda: writer,
    )


def row(conn, row_id=1):
    return conn.execute("SELECT * FROM auto_sends WHERE id = ?", (row_id,)).fetchone()


def tools(kind=None):
    return [c for c in FakeWriter.calls if kind is None or c[0] == kind]


# ------------------------------------------------------------------ creation


def test_eligible_job_creates_a_countdown_and_emails(conn, settings, config, writer, emails):
    seed(conn)
    assert start(conn, settings, config, writer) == [1]
    r = row(conn)
    assert r["status"] == "waiting" and r["job_id"] == JOB and r["decided_by"] is None
    created = datetime.fromisoformat(r["created"])
    assert datetime.fromisoformat(r["deadline"]) - created == timedelta(minutes=120)
    assert [e["event"] for e in emails] == ["auto_send_waiting"]
    assert "Sends on Upwork automatically when the timer ends." in emails[0]["text"]
    assert LETTER in emails[0]["text"] and "/jobs/" + JOB in emails[0]["text"]
    assert [c[1] for c in tools()] == ["create"]  # one preview, nothing confirmed
    assert tools()[0][2] == "system"
    pv = conn.execute("SELECT * FROM previews").fetchone()
    assert pv["attachments_asked"] == 1 and pv["highlights_asked"] == 1
    assert "portfolio_project_ids" in pv["request_json"]  # the highlights flow adds a project


def test_nothing_starts_in_dry_run_or_with_the_kill_switch(conn, settings, config, writer, emails):
    seed(conn)
    settings.set("general.dry_run", True)
    assert start(conn, settings, config, writer) == []
    settings.set("general.dry_run", False)
    settings.set("general.kill_switch", True)
    assert start(conn, settings, config, writer) == []
    settings.set("general.kill_switch", False)
    settings.set("auto_send.enabled", False)
    assert start(conn, settings, config, writer) == []
    assert FakeWriter.calls == [] and emails == []


def test_a_job_gets_one_countdown_even_after_stop(conn, settings, config, writer, emails):
    seed(conn)
    assert start(conn, settings, config, writer) == [1]
    assert start(conn, settings, config, writer) == []  # one waiting already
    assert auto_send.stop(conn, 1)
    assert start(conn, settings, config, writer) == []  # a Stop is not undone by the next run


def test_daily_cap(conn, settings, config, writer, emails):
    seed(conn)
    seed(conn, JOB2)
    settings.set("auto_send.max_per_day", 1)
    assert start(conn, settings, config, writer, (JOB, JOB2)) == [1]
    assert conn.execute("SELECT COUNT(*) FROM auto_sends").fetchone()[0] == 1


def test_a_draft_that_breaks_the_rules_never_starts(conn, settings, config, writer, emails):
    seed(conn, rules_ok=0)
    seed(conn, JOB2, letter="Read **this** at example.com", rules_ok=1)  # flag stale, text bad
    assert start(conn, settings, config, writer, (JOB, JOB2)) == []
    assert FakeWriter.calls == [] and emails == []


def test_not_enough_connects_after_the_fresh_numbers(conn, settings, config, writer, emails):
    seed(conn)
    FakeWriter.balance = 30  # 16 + 20 to keep = 36 needed
    assert start(conn, settings, config, writer) == []
    assert conn.execute("SELECT COUNT(*) FROM auto_sends").fetchone()[0] == 0


# ------------------------------------------------------------------ the race and the gate


def test_stop_wins_if_first_and_claim_wins_if_first(conn, settings, config, writer, emails):
    seed(conn)
    seed(conn, JOB2)
    start(conn, settings, config, writer, (JOB, JOB2))
    make_due(conn, 1)
    make_due(conn, 2)
    assert auto_send.stop(conn, 1) is True
    assert auto_send.claim_due(conn, 1) is False  # Stop landed first
    assert auto_send.claim_due(conn, 2) is True
    assert auto_send.stop(conn, 2) is False  # the claim landed first: too late
    assert auto_send.claim_due(conn, 2) is False  # no second claim
    assert row(conn, 1)["status"] == "stopped" and row(conn, 2)["status"] == "sending"


def test_no_claim_before_the_deadline(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    assert auto_send.claim_due(conn, 1) is False
    assert run(conn, settings, config, writer) == []


def test_system_consent_needs_an_ended_countdown(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    with pytest.raises(writes.ClickRequired):
        writes.issue_system_click(conn, settings, "confirm", auto_send_id=1)  # still waiting
    make_due(conn, 1)
    with pytest.raises(writes.ClickRequired):  # ended, but not claimed by the system
        writes.issue_system_click(conn, settings, "confirm", auto_send_id=1)
    with pytest.raises(writes.ClickRequired):
        writes.issue_system_click(conn, settings, "confirm")
    assert auto_send.claim_due(conn, 1)
    click = writes.issue_system_click(conn, settings, "confirm", auto_send_id=1)
    assert click.user == "system" and click.action == "confirm"
    settings.set("general.dry_run", True)
    with pytest.raises(writes.ClickRequired):
        writes.issue_system_click(conn, settings, "confirm", auto_send_id=1)
    settings.set("general.dry_run", False)
    settings.set("general.kill_switch", True)
    with pytest.raises(writes.ClickRequired):
        writes.issue_system_click(conn, settings, "preview", job_id=JOB)


# ------------------------------------------------------------------ sending at the deadline


def test_deadline_sends_with_system_consent_and_emails(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    FakeWriter.calls.clear()
    make_due(conn, 1)
    assert run(conn, settings, config, writer) == ["1: sent (Sent to Upwork.)"]
    r = row(conn)
    assert r["status"] == "sent" and r["decided_by"] == "system" and r["sent_at"] and r["click_id"]
    assert tools() == [
        ("manage_proposals", "create", "system"),  # the fresh preview
        ("confirm_preview", "confirm", "system"),
    ]
    assert conn.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()[0] == "sent"
    assert conn.execute("SELECT status FROM previews ORDER BY id DESC").fetchone()[0] == "confirmed"
    assert [e["event"] for e in emails] == ["auto_send_waiting", "auto_send_sent"]
    assert LETTER in emails[1]["text"]


def test_a_double_tick_sends_once(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    make_due(conn, 1)
    run(conn, settings, config, writer)
    run(conn, settings, config, writer)
    assert len(tools("confirm_preview")) == 1
    assert [e["event"] for e in emails].count("auto_send_sent") == 1


@pytest.mark.parametrize(
    ("flag", "text"),
    [("general.dry_run", "dry-run"), ("general.kill_switch", "kill switch")],
)
def test_dry_run_or_kill_switch_at_the_deadline_cancels(
    conn, settings, config, writer, emails, flag, text
):
    seed(conn)
    start(conn, settings, config, writer)
    FakeWriter.calls.clear()
    make_due(conn, 1)
    settings.set(flag, True)
    run(conn, settings, config, writer)
    assert row(conn)["status"] == "cancelled" and text in row(conn)["reason"]
    assert FakeWriter.calls == []
    assert emails[-1]["event"] == "auto_send_cancelled"
    run(conn, settings, config, writer)  # and it stays cancelled
    assert row(conn)["status"] == "cancelled"


def test_already_applied_is_cancelled(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    FakeWriter.calls.clear()
    make_due(conn, 1)
    applied = Reader()
    applied.swap = JOB  # Upwork now lists a proposal for this job
    run(conn, settings, config, writer, reader=applied)
    assert row(conn)["status"] == "cancelled" and "already applied" in row(conn)["reason"]
    assert tools("confirm_preview") == []
    assert emails[-1]["event"] == "auto_send_cancelled"


def test_job_applied_by_hand_or_skipped_is_cancelled(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    FakeWriter.calls.clear()
    make_due(conn, 1)
    conn.execute("UPDATE jobs SET status = 'sent' WHERE job_id = ?", (JOB,))
    run(conn, settings, config, writer)
    assert row(conn)["status"] == "cancelled" and FakeWriter.calls == []


def test_eligibility_is_checked_again_at_the_deadline(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    FakeWriter.calls.clear()
    make_due(conn, 1)
    FakeWriter.balance = 20  # Connects were spent meanwhile: 16 + 20 to keep is not there
    run(conn, settings, config, writer)
    r = row(conn)
    assert r["status"] == "cancelled" and "no longer eligible" in r["reason"]
    assert tools("confirm_preview") == []


def test_the_daily_cap_does_not_count_the_countdown_itself(conn, settings, config, writer, emails):
    seed(conn)
    settings.set("auto_send.max_per_day", 1)
    start(conn, settings, config, writer)
    make_due(conn, 1)
    run(conn, settings, config, writer)
    assert row(conn)["status"] == "sent"


def test_rules_failing_text_never_auto_sends(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    FakeWriter.calls.clear()
    make_due(conn, 1)
    pv = conn.execute("SELECT * FROM previews WHERE id = 1").fetchone()
    bad = json.loads(pv["request_json"])
    bad["params"]["cover_letter"] = "See https://example.com now"
    conn.execute("UPDATE previews SET request_json = ? WHERE id = 1", (json.dumps(bad),))
    conn.execute("UPDATE drafts SET cover_letter = ?", (bad["params"]["cover_letter"],))
    run(conn, settings, config, writer)
    r = row(conn)
    assert r["status"] == "cancelled" and "rules" in r["reason"]
    assert FakeWriter.calls == []
    assert emails[-1]["event"] == "auto_send_cancelled"


def test_a_changed_draft_cancels(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    make_due(conn, 1)
    conn.execute("UPDATE drafts SET cover_letter = 'A different letter.'")
    run(conn, settings, config, writer)
    assert row(conn)["status"] == "cancelled" and "draft changed" in row(conn)["reason"]


def test_a_failed_send_is_emailed_and_never_retried(conn, settings, config, writer, emails):
    seed(conn)
    start(conn, settings, config, writer)
    make_due(conn, 1)
    FakeWriter.confirm_error = ClaudeFailed("claude exploded")
    run(conn, settings, config, writer)
    r = row(conn)
    assert r["status"] == "failed" and "claude exploded" in r["error"]
    assert emails[-1]["event"] == "auto_send_failed"
    confirms = len(tools("confirm_preview"))
    FakeWriter.confirm_error = None
    run(conn, settings, config, writer)
    run(conn, settings, config, writer)
    assert len(tools("confirm_preview")) == confirms == 1  # no automatic retry
    assert row(conn)["status"] == "failed"


# ------------------------------------------------------------------ the task and the scheduler


def test_task_module_and_schedule(conn, settings, config, monkeypatch):
    assert trigger.is_built("auto_send") and "auto_send" in trigger.SCHEDULED_ONLY
    assert "auto_send" not in trigger.MANUAL_TASKS  # no button can start it
    assert scheduler._cron(settings, "auto_send") == "*/5 * * * *"
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    scheduler.tick(settings, config, datetime.now(UTC))
    assert "auto_send" in started


# ------------------------------------------------------------------ web: Stop, Approve, cards


@pytest.fixture
def web(config, monkeypatch):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.worker.sync = True
    s = app.state.settings
    s.set("upwork.talent_org_uid", ORG)
    s.set("general.dry_run", False)
    app.state.mcp_factory = lambda: Reader()
    monkeypatch.setattr(
        routes, "_make_writer", lambda cfg, ctx: FakeWriter(cfg, ctx.settings, ctx.conn)
    )
    seed(app.state.db)
    assert start(app.state.db, s, config, FakeWriter(config, s, app.state.db)) == [1]
    FakeWriter.calls.clear()
    return app, client


def post(client, path, **data):
    token = csrf_from(client.get("/approvals").text)
    r = client.post(path, data={"csrf_token": token, **data})
    while r.status_code == 303:
        r = client.get(r.headers["location"])
    return r


def test_cards_and_pill_show_the_countdown(web):
    app, client = web
    todo = client.get("/approvals").text
    assert "Sends on Upwork automatically when the timer ends." in todo
    assert "Approve and send now" in todo and ">Stop<" in todo and "auto-timer" in todo
    assert "Auto-send in" in todo  # the top bar pill
    job = client.get(f"/jobs/{JOB}").text
    assert "Sends on Upwork automatically when the timer ends." in job
    assert "Costs 16 Connects of 60" in job
    assert "Preview ready" not in todo  # the countdown owns this preview
    assert app.state.templates.env.globals["approvals_pending"]() >= 1


def test_stop_button(web):
    app, client = web
    r = post(client, f"/jobs/{JOB}/auto/stop")
    assert "Stopped" in r.text
    assert row(app.state.db)["status"] == "stopped" and row(app.state.db)["decided_by"] == "user"
    assert "Auto-send in" not in client.get("/approvals").text
    late = post(client, f"/jobs/{JOB}/auto/stop")
    assert "Too late" in late.text


def test_approve_button_sends_now_with_the_users_clicks(web, emails):
    app, client = web
    r = post(client, f"/jobs/{JOB}/auto/approve")
    assert r.status_code == 200 and "Sent to Upwork" in r.text
    got = row(app.state.db)
    assert got["status"] == "sent" and got["decided_by"] == "user"
    users = {c[2] for c in FakeWriter.calls}
    assert users and "system" not in users  # the logged-in user's own clicks
    assert [c[0] for c in FakeWriter.calls] == ["manage_proposals", "confirm_preview"]
    assert "auto_send_sent" in [e["event"] for e in emails]
    assert "Too late" in post(client, f"/jobs/{JOB}/auto/approve").text


def test_job_page_approve_button_is_the_same_path(web):
    app, client = web
    post(client, f"/jobs/{JOB}/approve", preview=1)
    assert row(app.state.db)["status"] == "sent" and row(app.state.db)["decided_by"] == "user"


def test_approve_in_dry_run_sends_nothing(web):
    app, client = web
    app.state.settings.set("general.dry_run", True)
    r = post(client, f"/jobs/{JOB}/auto/approve")
    assert "Dry-run" in r.text or "dry-run" in r.text
    assert row(app.state.db)["status"] == "waiting" and FakeWriter.calls == []


def test_taking_over_cancels_the_countdown(web):
    app, client = web
    post(client, f"/jobs/{JOB}/skip")
    assert row(app.state.db)["status"] == "cancelled"


# ------------------------------------------------------------------ the rules text


def test_hard_rules_allow_only_the_owner_approved_exception():
    from app.llm import prompting

    text = prompting.hard_rules()
    assert "auto-send" in text and "countdown ended with no Stop" in text
    assert "counts as my approval" in text
    for kept in (
        "Never break Upwork's Terms of Service",
        "Never change title, overview or rate without my approval.",
        "Never copy text from other freelancers' profiles",
        "Never invent facts about me",
        "No em dashes",
        "Every change is logged and can be rolled back.",
        "If JSS drops, stop all profile tests and alert me.",
    ):
        assert kept in text
    assert "—" not in text and "–" not in text


def test_the_write_prompt_carries_no_rule_text():
    prompt = writes.build_prompt(
        "confirm_preview", {"action": "confirm", "org_uid": "1", "params": {}}
    )
    assert "without my approval" not in prompt and "Never send" not in prompt
