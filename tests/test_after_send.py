"""After-send review (plan R3): sync of sent proposals, one review each, draft vs sent, email,
daily wiring and the /review list. Real sample answers, a fake bridge and a fake claude."""

import copy
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app import mail
from app.db import init_db
from app.review import after_send
from app.settings import Settings
from app.tasks import daily
from app.upwork.client import CallBudgetExceeded, UpworkAuthError, UpworkToolError
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin
from tests.test_mail import SEND_URL, connect_gmail
from tests.upwork_fakes import FakeMCP, load, to_result

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
ORG = "1000000000000000001"
P1, P2 = "3000000000000000001", "3000000000000000002"
JOB1, JOB2 = "4000000000000000001", "4000000000000000002"
USER_FACTS = "Built 12 n8n flows for small shops.\nRate: 35 per hour."


class FakeProposals:
    """Stands in for the bridge: list and get for list_freelancer_proposals."""

    def __init__(self, list_payload=None, fail=None):
        self.list_payload = list_payload or load("list_proposals.json")
        self.fail = fail or {}  # proposal id -> exception, or "list" -> exception
        self.calls: list[tuple[str, dict]] = []

    def detail(self, pid):
        base = load("get_proposal.json")
        node = base["data"]["vendorProposal"]
        if pid == P2:
            node.update(
                id=P2,
                proposalCoverLetter="I automate recruiting flows with n8n. Quick call this week?",
                questionsAnswers=[],
            )
            node["marketplaceJobPosting"].update(id=JOB2, content={"title": "AI Automation"})
            node["terms"]["chargeRate"]["rawValue"] = "100.0"
            node["viewedByClient"] = True
            node["boosted"] = True
        return base

    def close(self):
        pass

    def call_many(self, calls):
        out = []
        for name, args in calls:
            self.calls.append((name, copy.deepcopy(args)))
            assert name == "list_freelancer_proposals"
            assert args["org_uid"] == ORG
            key = "list" if args["action"] == "list" else args["params"]["id"]
            if key in self.fail:
                out.append(to_result(name, self.fail[key]))
            elif args["action"] == "list":
                out.append(to_result(name, copy.deepcopy(self.list_payload)))
            else:
                out.append(to_result(name, self.detail(key)))
        return out

    def actions(self):
        return [a["action"] for _, a in self.calls]


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


@pytest.fixture
def with_user_md(config):
    mem = config.data_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "USER.md").write_text(USER_FACTS)


def proposals(conn):
    return {r["proposal_id"]: r for r in conn.execute("SELECT * FROM proposals")}


def add_draft(conn, job_id, cover, answers=()):
    conn.execute(
        "INSERT INTO drafts(job_id, cover_letter, answers) VALUES (?,?,?)",
        (job_id, cover, json.dumps(list(answers))),
    )


# ---------------------------------------------------------------- sync


def test_sync_stores_list_then_gets_text(conn, settings):
    bridge = FakeProposals()
    res = after_send.sync_proposals(bridge, conn, settings, now=NOW)
    assert (res.listed, res.new_ids, res.fetched) == (2, [P1, P2], 2)
    assert bridge.calls[0][1] == {"action": "list", "org_uid": ORG, "params": {"limit": 25}}
    assert bridge.actions() == ["list", "get", "get"]
    assert bridge.calls[1][1]["params"] == {"id": P1}
    rows = proposals(conn)
    p1, p2 = rows[P1], rows[P2]
    assert p1["job_id"] == JOB1 and p1["job_title"] == "AI Agent Operations Assistant (Part-Time)"
    assert p1["created_at"] == "2026-10-10T15:03:09+00:00"
    assert p1["rate"] == 20.0 and p1["status"] == "Accepted"
    assert (p1["viewed_by_client"], p1["boosted"], p1["connects_bid"]) == (0, 0, None)
    assert (p2["viewed_by_client"], p2["boosted"], p2["connects_bid"]) == (1, 1, 5)
    assert p1["cover_letter"] == "Bots"
    answers = json.loads(p1["answers_json"])
    assert len(answers) == 3
    assert (
        answers[0]["question"]
        == "Which AI tools do you use every day, and what do you use each one for?"
    )
    assert "untrusted" not in p1["answers_json"]
    assert p1["review_id"] is None and p1["draft_id"] is None


def test_second_sync_updates_status_and_skips_get(conn, settings):
    after_send.sync_proposals(FakeProposals(), conn, settings, now=NOW)
    payload = load("list_proposals.json")
    first = payload["data"]["vendorProposals"]["edges"][0]["node"]
    first["viewedByClient"] = True
    first["status"] = {"status": "Hired", "status_label": "Hired"}
    bridge = FakeProposals(payload)
    res = after_send.sync_proposals(bridge, conn, settings, now=NOW)
    assert bridge.actions() == ["list"]
    assert res.new_ids == [] and res.fetched == 0
    p1 = proposals(conn)[P1]
    assert p1["viewed_by_client"] == 1 and p1["status"] == "Hired"
    assert p1["cover_letter"] == "Bots"  # text is kept
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 2


def test_failed_get_is_retried_next_run(conn, settings):
    bridge = FakeProposals(fail={P2: UpworkToolError("proposal not found")})
    res = after_send.sync_proposals(bridge, conn, settings, now=NOW)
    assert res.fetched == 1 and any("could not be read" in n for n in res.notes)
    assert proposals(conn)[P2]["cover_letter"] is None
    bridge = FakeProposals()
    after_send.sync_proposals(bridge, conn, settings, now=NOW)
    assert bridge.actions() == ["list", "get"] and bridge.calls[1][1]["params"] == {"id": P2}
    assert proposals(conn)[P2]["cover_letter"].startswith("I automate")


def test_gets_run_in_batches(conn, settings):
    settings.set("claude.fetch_batch_size", 1)
    bridge = FakeProposals()
    after_send.sync_proposals(bridge, conn, settings, now=NOW)
    assert bridge.actions() == ["list", "get", "get"]
    assert len(bridge.calls) == 3


def test_call_limit_leaves_texts_for_later(conn, settings):
    bridge = FakeProposals(fail={P1: CallBudgetExceeded("limit"), P2: CallBudgetExceeded("limit")})
    res = after_send.sync_proposals(bridge, conn, settings, now=NOW)
    assert res.fetched == 0 and any("call limit" in n for n in res.notes)
    assert all(r["cover_letter"] is None for r in proposals(conn).values())


def test_list_error_is_a_note(conn, settings):
    res = after_send.sync_proposals(
        FakeProposals(fail={"list": UpworkToolError("boom")}), conn, settings, now=NOW
    )
    assert res.listed == 0 and "Proposal list failed" in res.notes[0]


def test_auth_error_is_raised(conn, settings):
    with pytest.raises(UpworkAuthError):
        after_send.sync_proposals(
            FakeProposals(fail={"list": UpworkAuthError("expired")}), conn, settings, now=NOW
        )


def test_no_account_means_no_calls(conn):
    bridge = FakeProposals()
    res = after_send.sync_proposals(bridge, conn, Settings(conn), now=NOW)
    assert bridge.calls == [] and "no freelancer account" in res.notes[0]


# ---------------------------------------------------------------- draft vs sent


def test_draft_diff_for_same_job(conn, settings):
    add_draft(
        conn, JOB1, "Bots. I build n8n flows for recruiters.", [{"question": "q", "answer": "ok"}]
    )
    res = after_send.sync_proposals(FakeProposals(), conn, settings, now=NOW)
    p1 = proposals(conn)[P1]
    assert p1["draft_id"] == 1
    data = json.loads(p1["draft_diff"])
    assert data["cover_letter"]["changed"] is True
    assert 0 <= data["cover_letter"]["similarity"] < 1
    assert data["cover_letter"]["removed_words"] > 0
    kinds = {op[0] for op in data["cover_letter"]["ops"]}
    assert "del" in kinds
    assert len(data["answers"]) == 3
    assert (
        res.draft_notes and "Draft vs sent for 'AI Agent Operations Assistant" in res.draft_notes[0]
    )
    assert proposals(conn)[P2]["draft_id"] is None  # no draft for that job


def test_draft_sent_as_written(conn, settings):
    add_draft(conn, JOB1, "Bots")
    res = after_send.sync_proposals(FakeProposals(), conn, settings, now=NOW)
    data = json.loads(proposals(conn)[P1]["draft_diff"])
    assert data["cover_letter"]["changed"] is False and data["cover_letter"]["similarity"] == 1
    assert "sent as written" in res.draft_notes[0]


# ---------------------------------------------------------------- review once


def reviews(conn):
    return conn.execute("SELECT * FROM reviews ORDER BY id").fetchall()


def synced(conn, settings):
    after_send.sync_proposals(FakeProposals(), conn, settings, now=NOW)


def test_each_proposal_is_reviewed_once(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert [r["proposal_id"] for r in batch.reviewed] == [P1, P2]  # newest first: P1 is October
    rows = proposals(conn)
    assert rows[P1]["review_id"] and rows[P2]["review_id"]
    assert len(reviews(conn)) == 2
    assert batch.reviewed[0]["score"] == 72 and batch.reviewed[0]["top_issue"] == "problem 1, x"
    again = after_send.review_new(conn, config, settings, now=NOW)
    assert again.reviewed == [] and len(reviews(conn)) == 2
    assert len(fake_claude_gen.logs()) == 2


def test_questions_stay_inside_the_untrusted_tags(
    conn, settings, config, with_user_md, fake_claude_gen
):
    synced(conn, settings)
    after_send.review_new(conn, config, settings, now=NOW)
    log = next(x for x in fake_claude_gen.logs() if "Which AI tools" in json.dumps(x["input"]))
    job_block = log["input"]["job"]["untrusted_job_post"]
    assert job_block.startswith("<untrusted_job_post>") and "Which AI tools" in job_block
    assert "Which AI tools" not in log["input"]["draft"]
    assert "screening_questions[0]" in log["input"]["draft"]
    assert "Only the job title is known" in job_block  # job not stored
    assert log["task"] == "review"
    # the stored review keeps the real question text for the page
    original = json.loads(reviews(conn)[0]["original"])
    assert original["answers"][0]["question"].startswith("Which AI tools")
    assert original["cover_letter"] == "Bots"


def test_stored_job_is_used(conn, settings, config, with_user_md, fake_claude_gen):
    conn.execute(
        "INSERT INTO jobs(job_id, first_seen, title, status, tier, job_type, budget_min, "
        "budget_max, details) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            JOB1,
            "2026-10-09",
            "Stored title",
            "candidate",
            "MID",
            "hourly",
            30,
            40,
            json.dumps({"description": "Fix the CRM. Start with Bots."}),
        ),
    )
    synced(conn, settings)
    after_send.review_new(conn, config, settings, now=NOW)
    row = (
        reviews(conn)[0]
        if json.loads(reviews(conn)[0]["original"])["cover_letter"] == "Bots"
        else reviews(conn)[1]
    )
    assert row["job_id"] == JOB1
    pre = json.loads(row["prechecks"])
    assert any(f["check"] == "rate" and "below" in f["message"] for f in pre)
    log = next(x for x in fake_claude_gen.logs() if "Fix the CRM" in json.dumps(x["input"]))
    assert "Fix the CRM" in log["input"]["job"]["untrusted_job_post"]


def test_old_proposals_are_not_reviewed(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    settings.set("review.after_send_max_age_days", 30)  # P1 sent 27 days ago, P2 65 days ago
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert [r["proposal_id"] for r in batch.reviewed] == [P1]
    assert proposals(conn)[P2]["review_id"] is None
    assert len(fake_claude_gen.logs()) == 1


def test_max_per_run(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    settings.set("review.after_send_max_per_run", 1)
    assert len(after_send.review_new(conn, config, settings, now=NOW).reviewed) == 1
    assert len(after_send.review_new(conn, config, settings, now=NOW).reviewed) == 1
    assert len(after_send.review_new(conn, config, settings, now=NOW).reviewed) == 0
    assert len(reviews(conn)) == 2


def test_rate_limit_stops_and_retries_later(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", 1)
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert [r["proposal_id"] for r in batch.reviewed] == [P1]
    assert batch.stopped and "rate limited" in batch.stopped
    assert proposals(conn)[P2]["review_id"] is None
    assert len(reviews(conn)) == 1  # no junk row for the stopped one
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", 99)
    later = after_send.review_new(conn, config, settings, now=NOW)
    assert [r["proposal_id"] for r in later.reviewed] == [P2]


def test_monthly_cap_stops_before_claude(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    settings.set("claude.monthly_cap_usd", 0.01)
    conn.execute(
        "INSERT INTO claude_calls(run_id, model, cost_usd, created_at) VALUES ('x','m',5,?)",
        (NOW.isoformat(),),
    )
    batch = after_send.review_new(conn, config, settings, now=datetime.now(UTC))
    assert batch.reviewed == [] and batch.stopped
    assert not fake_claude_gen.called() and len(reviews(conn)) == 0
    assert all(r["review_id"] is None for r in proposals(conn).values())


def test_missing_user_md_marks_nothing(conn, settings, config, fake_claude_gen):
    synced(conn, settings)
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert batch.reviewed == [] and batch.stopped and "USER.md" in batch.stopped
    assert len(reviews(conn)) == 0 and not fake_claude_gen.called()


def test_unusable_answer_is_kept_and_not_repeated(
    conn, settings, config, with_user_md, fake_claude_gen
):
    synced(conn, settings)
    fake_claude_gen.env("FAKE_REVIEW_BAD", 1)
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert batch.reviewed == [] and len(batch.notes) == 2
    assert all(r["review_id"] for r in proposals(conn).values())
    assert after_send.review_new(conn, config, settings, now=NOW).notes == []


def test_empty_text_is_not_reviewed(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    conn.execute("UPDATE proposals SET cover_letter = '' WHERE proposal_id = ?", (P1,))
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert [r["proposal_id"] for r in batch.reviewed] == [P2]


# ---------------------------------------------------------------- email


class Captured:
    def __init__(self, monkeypatch):
        self.sent = []

        def fake(conn, event, ref, subject, text, html, **kw):
            self.sent.append(
                {"event": event, "ref": ref, "subject": subject, "text": text, "html": html}
            )
            return mail.SENT

        monkeypatch.setattr(mail, "send_event", fake)


def test_one_email_per_run_lists_reviews(
    conn, settings, config, with_user_md, fake_claude_gen, monkeypatch
):
    cap = Captured(monkeypatch)
    synced(conn, settings)
    batch = after_send.review_new(conn, config, settings, now=NOW)
    assert after_send.notify_reviews(conn, config, settings, batch.reviewed, now=NOW) == mail.SENT
    [m] = cap.sent
    assert m["event"] == "after_send_review" and m["ref"] == f"{P1},{P2}"
    assert m["subject"] == "2 sent proposals reviewed"
    assert "Score 72 / 100" in m["text"] and "Top issue: problem 1, x" in m["text"]
    ids = [r["review_id"] for r in batch.reviewed]
    for rid in ids:
        assert f"https://test.example/review/{rid}" in m["text"]
    assert "AI Automation" in m["html"]


def test_no_reviews_no_email(conn, settings, config, monkeypatch):
    cap = Captured(monkeypatch)
    assert after_send.notify_reviews(conn, config, settings, []) is None
    assert cap.sent == []


def test_email_is_deduped_and_has_a_switch(conn, settings, config, with_user_md, fake_claude_gen):
    synced(conn, settings)
    batch = after_send.review_new(conn, config, settings, now=NOW)
    connect_gmail(config, settings, conn)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
        assert (
            after_send.notify_reviews(conn, config, settings, batch.reviewed, now=NOW) == mail.SENT
        )
        assert (
            after_send.notify_reviews(conn, config, settings, batch.reviewed, now=NOW)
            == mail.DUPLICATE
        )
        assert route.call_count == 1
        settings.set("email.events.after_send_review", False)
        other = [dict(batch.reviewed[0], proposal_id="999")]
        assert after_send.notify_reviews(conn, config, settings, other, now=NOW) == mail.OFF
        assert route.call_count == 1
    ref = conn.execute("SELECT ref_id FROM emails WHERE event = 'after_send_review'").fetchone()[0]
    assert ref == f"{P1},{P2}"


def test_email_never_raises(conn, settings, config, with_user_md, fake_claude_gen, monkeypatch):
    synced(conn, settings)
    batch = after_send.review_new(conn, config, settings, now=NOW)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(mail, "build_email", boom)
    assert after_send.notify_reviews(conn, config, settings, batch.reviewed) == mail.FAILED


# ---------------------------------------------------------------- daily wiring


class DailyClient(FakeMCP):
    """FakeMCP for jobs plus the proposals tool."""

    def __init__(self, settings, proposals_bridge):
        super().__init__(settings, search={}, details={})
        self.proposals = proposals_bridge

    def call_many(self, calls):
        if calls and calls[0][0] == "list_freelancer_proposals":
            return self.proposals.call_many(calls)
        return super().call_many(calls)


@pytest.mark.after_send
def test_daily_runs_the_after_send_part(
    conn, settings, config, with_user_md, fake_claude_gen, monkeypatch
):
    cap = Captured(monkeypatch)
    add_draft(conn, JOB1, "Bots. Draft text.")
    client = DailyClient(settings, FakeProposals())
    res = daily.run_daily(conn, settings, config, client, now=NOW)
    assert res.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 2
    assert len(reviews(conn)) == 2
    assert [m["event"] for m in cap.sent] == ["after_send_review"]
    note = (config.data_dir / "memory" / "daily" / "2026-11-06.md").read_text()
    assert "Sent proposals:" in note
    assert "Proposals synced: 2 listed, 2 new, 2 reviewed" in note
    assert "Draft vs sent for 'AI Agent Operations Assistant" in note
    # review cost lands on the daily run
    cost_rows = conn.execute("SELECT run_id FROM claude_calls WHERE task = 'review'").fetchall()
    assert cost_rows and all(r["run_id"] == res.run_id for r in cost_rows)
    # second run: nothing new
    daily.run_daily(conn, settings, config, DailyClient(settings, FakeProposals()), now=NOW)
    assert len(reviews(conn)) == 2 and len(cap.sent) == 1


@pytest.mark.after_send
def test_after_send_error_does_not_fail_the_run(conn, settings, config):
    client = DailyClient(settings, FakeProposals(fail={"list": RuntimeError("bridge exploded")}))
    res = daily.run_daily(conn, settings, config, client, now=NOW)
    assert res.status == "ok"
    alert = conn.execute("SELECT message FROM alerts WHERE key = 'after_send_failed'").fetchone()
    assert alert and "bridge exploded" in alert["message"]
    note = (config.data_dir / "memory" / "daily" / "2026-11-06.md").read_text()
    assert "after-send proposal review failed" in note
    # a good run clears the alert
    daily.run_daily(conn, settings, config, DailyClient(settings, FakeProposals()), now=NOW)
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'after_send_failed'").fetchone() is None


@pytest.mark.after_send
def test_setting_turns_it_off(conn, settings, config):
    settings.set("review.after_send_enabled", False)
    proposals_bridge = FakeProposals()
    res = daily.run_daily(conn, settings, config, DailyClient(settings, proposals_bridge), now=NOW)
    assert res.status == "ok" and proposals_bridge.calls == []


@pytest.mark.after_send
def test_without_user_md_proposals_are_stored_and_waiting(conn, settings, config, fake_claude_gen):
    res = daily.run_daily(conn, settings, config, DailyClient(settings, FakeProposals()), now=NOW)
    assert res.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM proposals WHERE review_id IS NULL").fetchone()[0] == 2
    note = (config.data_dir / "memory" / "daily" / "2026-11-06.md").read_text()
    assert "After-send reviews stopped" in note


# ---------------------------------------------------------------- the /review list


def test_review_page_lists_synced_proposals(config, with_user_md, fake_claude_gen):
    app, client = make_web_client(config)
    setup_admin(client)
    conn = app.state.db
    settings = app.state.settings
    settings.set("upwork.talent_org_uid", ORG)
    add_draft(conn, JOB1, "Bots. Draft text.")
    page = client.get("/review").text
    assert 'id="sent-proposals"' in page and "No sent proposals yet" in page
    after_send.sync_proposals(FakeProposals(), conn, settings, now=NOW)
    after_send.review_new(conn, app.state.config, settings, now=NOW)
    page = client.get("/review").text
    assert "AI Agent Operations Assistant (Part-Time)" in page
    assert "AI Automation" in page and "Submitted" in page and "Viewed" in page
    assert 'class="score-badge ok">72' in page
    rid = proposals(conn)[P1]["review_id"]
    assert f'href="/review/{rid}"' in page
    assert "% the same" in page or "sent as written" in page
    assert client.get(f"/review/{rid}").status_code == 200


def test_review_page_shows_unread_and_unreviewed(config):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.db.execute(
        "INSERT INTO proposals(proposal_id, job_title, created_at, status) VALUES (?,?,?,?)",
        ("77", "<b>Evil</b> title", "2026-11-01T10:00:00+00:00", "Accepted"),
    )
    page = client.get("/review").text
    assert "text not read yet" in page and "&lt;b&gt;Evil&lt;/b&gt; title" in page
    assert "<b>Evil</b>" not in page
