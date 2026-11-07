"""Job page write UI (plan H2): edit draft, prepare, questions, approve, re-make, skip.

No real Upwork or Claude: the READ bridge is a fake (`app.state.mcp_factory`) and the write path
runs the fake `claude` (tests/fakes/claude_stream) like tests/test_writes.py."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from app import job_actions as ja
from app.upwork.client import ToolResult, UpworkToolError
from app.upwork.jobs import CLOSE_TAG, OPEN_TAG
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.test_writes import (
    INVITED_JOB,
    JOB,
    LETTER,
    ORG,
    SENT_JOB,
    confirm_rule,
    preview_rule,
    rules,
)
from tests.upwork_fakes import load

DASH = "—"
HIGHLIGHTS = {
    "status": "ok",
    "data": {
        "portfolio": [
            {
                "id": "pf-1",
                "title": f"{OPEN_TAG}CRM sync demo{CLOSE_TAG}",
            }
        ],
        "certificates": [{"id": "ce-1", "title": "n8n expert"}],
        "other": [{"title": "Loose item without id"}],
    },
}


class FakeReader:
    """The READ bridge: proposals/invitations from fixtures, highlights from HIGHLIGHTS."""

    def __init__(self, proposals="list_proposals.json", invitations="invitations_empty.json"):
        self.proposals = proposals
        self.invitations = invitations
        self.highlights = HIGHLIGHTS
        self.calls = []
        self.closed = 0

    def call_many(self, calls):
        out = []
        for tool, args in calls:
            self.calls.append((tool, args))
            if tool == "get_profile":
                assert args["action"] == "list_highlights"
                if isinstance(self.highlights, Exception):
                    out.append(ToolResult(tool, [{"type": "text", "text": "nope"}], is_error=True))
                    continue
                payload = self.highlights
            else:
                assert tool == "list_freelancer_proposals"
                payload = load(self.proposals if args["action"] == "list" else self.invitations)
            out.append(ToolResult(tool, [{"type": "text", "text": json.dumps(payload)}]))
        return out

    def close(self):
        self.closed += 1


@pytest.fixture
def web(config, fake_claude_stream, tmp_path, monkeypatch):
    rules(tmp_path, monkeypatch, preview_rule(), confirm_rule())
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.worker.sync = True  # each action runs inside the click (plan W2)
    app.state.settings.set("upwork.talent_org_uid", ORG)
    app.state.reader = FakeReader()
    app.state.mcp_factory = lambda: app.state.reader
    seed(app.state.db)
    app.state.claude = fake_claude_stream
    return app, client


def seed(conn, job_id=JOB, status="drafted", rate=25.0):
    conn.execute(
        "INSERT INTO jobs(job_id, first_seen, title, url, job_type, connects_cost, status) "
        "VALUES (?, ?, 'n8n CRM flow', ?, 'hourly', 16, ?)",
        (job_id, "2026-11-06T10:00:00+00:00", f"https://www.upwork.com/jobs/~02{job_id}", status),
    )
    conn.execute(
        "INSERT INTO drafts(job_id, cover_letter, answers, materials, suggested_rate, flags, "
        "created_at) VALUES (?, ?, ?, '[]', ?, '[]', ?)",
        (
            job_id,
            LETTER,
            json.dumps([{"question": "Which tools?", "answer": "n8n and Python."}]),
            rate,
            "2026-11-06T10:05:00+00:00",
        ),
    )


def page(client, job_id=JOB):
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    return r


def post(client, path, job_id=JOB, token=None, **data):
    if token is None:
        token = csrf_from(page(client, job_id).text)
    return client.post(f"/jobs/{job_id}/{path}", data={"csrf_token": token, **data})


def follow(client, r):
    """Follow the click's redirect. A finished action (the tests run them inline) redirects once
    more, to its result page."""
    assert r.status_code == 303, r.text[:300]
    r = client.get(r.headers["location"])
    while r.status_code == 303:
        r = client.get(r.headers["location"])
    return r


def preview_row(conn, job_id=JOB):
    return conn.execute(
        "SELECT * FROM previews WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


def claude_tools(fake):
    """The tool names the fake claude was asked to call, in order."""
    out = []
    if not fake.called():
        return out
    for log in fake.logs():
        out.append(
            re.search(
                r"mcp__upwork__upwork__(\w+) ", log["argv"][log["argv"].index("-p") + 1]
            ).group(1)
        )
    return out


def prepare(client):
    r = post(client, "prepare")
    return follow(client, r)


def answer_both(client, app, **extra):
    row = preview_row(app.state.db)
    return post(client, "answers", preview=row["id"], attachments="none", **extra)


def disabled(html, form_id):
    form = re.search(rf'<form[^>]*id="{form_id}".*?</form>', html, re.S)
    assert form, f"no form {form_id}"
    return "disabled" in form.group(0)


# ---------------------------------------------------------------- edit draft


def test_edit_saves_letter_answers_and_rate(web):
    app, client = web
    r = post(
        client,
        "draft",
        cover_letter="New letter about n8n.",
        answers="Updated answer.",
        rate="31.5",
    )
    assert r.status_code == 303 and r.headers["location"].endswith("msg=saved")
    row = app.state.db.execute("SELECT * FROM drafts WHERE job_id = ?", (JOB,)).fetchone()
    assert row["cover_letter"] == "New letter about n8n."
    assert json.loads(row["answers"]) == [{"question": "Which tools?", "answer": "Updated answer."}]
    assert row["suggested_rate"] == 31.5 and row["edited_by_user"] == 1
    html = follow(client, r).text
    assert "Draft saved." in html and "New letter about n8n." in html and "Edited by you" in html


def test_edit_with_em_dash_warns_and_does_not_save(web):
    app, client = web
    bad = f"Hello {DASH} I can help."
    r = post(client, "draft", cover_letter=bad, answers="Fine answer.", rate="30")
    assert r.status_code == 422
    assert "has an em dash or en dash" in r.text and "Cover letter" in r.text
    assert bad in r.text  # what the user typed stays in the form
    assert 'name="clean"' in r.text  # the explicit fix button
    row = app.state.db.execute("SELECT * FROM drafts WHERE job_id = ?", (JOB,)).fetchone()
    assert row["cover_letter"] == LETTER and row["edited_by_user"] == 0  # nothing saved


def test_edit_in_an_answer_is_found_too(web):
    _, client = web
    r = post(client, "draft", cover_letter="ok", answers=f"a {DASH} b", rate="30")
    assert r.status_code == 422 and "Answer 1" in r.text


def test_clean_and_save_replaces_dashes_only_on_request(web):
    app, client = web
    r = post(
        client, "draft", cover_letter=f"Hello {DASH} I can help.", answers="x", rate="30", clean="1"
    )
    assert r.status_code == 303 and r.headers["location"].endswith("msg=saved_clean")
    row = app.state.db.execute(
        "SELECT cover_letter FROM drafts WHERE job_id = ?", (JOB,)
    ).fetchone()
    assert DASH not in row["cover_letter"] and row["cover_letter"].startswith("Hello")
    assert "Em dashes were replaced" in follow(client, r).text


def test_edit_refuses_bad_input(web):
    app, client = web
    limit = app.state.settings.get("writes.cover_letter_max_chars")
    for kw, text in [
        ({"cover_letter": "  ", "answers": "a", "rate": "20"}, "cover letter is empty"),
        ({"cover_letter": "x" * (limit + 1), "answers": "a", "rate": "20"}, "the limit is"),
        ({"cover_letter": "ok", "answers": "a", "rate": "free"}, "rate must be a number"),
        ({"cover_letter": "ok", "answers": "a", "rate": "-3"}, "rate must be a number"),
    ]:
        r = post(client, "draft", **kw)
        assert r.status_code == 422 and text in r.text
    # wrong number of answers
    r = client.post(
        f"/jobs/{JOB}/draft",
        data={"csrf_token": csrf_from(page(client).text), "cover_letter": "ok", "rate": "20"},
    )
    assert r.status_code == 422 and "number of answers" in r.text
    assert app.state.db.execute("SELECT cover_letter FROM drafts").fetchone()[0] == LETTER


# ---------------------------------------------------------------- prepare


def test_prepare_shows_cost_balance_afford_boost_and_unmet(web, tmp_path, monkeypatch):
    app, client = web
    payload = json.loads(json.dumps(load("proposal_preview.json")))
    payload["data"]["unmet_preferred_qualifications"] = ["Located in the US"]
    rules(tmp_path, monkeypatch, {"tool": "manage_proposals", "text": json.dumps(payload)})
    html = prepare(client).text
    assert "Preview made. Nothing was sent." in html
    facts = re.search(r'id="preview-facts".*?</dl>', html, re.S).group(0)
    assert "Connects cost" in facts and ">16<" in facts
    assert "Connects balance" in facts and ">40<" in facts
    assert "Can afford" in facts and "Yes" in facts
    assert 'id="boost-block"' in html and "<strong>10</strong> to <strong>30</strong>" in html
    assert 'id="unmet"' in html and "Located in the US" in html
    row = preview_row(app.state.db)
    assert row["status"] == "previewed" and row["kind"] == "create"
    assert row["attachments_asked"] == 0 and row["highlights_asked"] == 0
    assert (
        app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()[0]
        == "prepared"
    )
    # the write call: one manage_proposals run with the draft as it is
    assert claude_tools(app.state.claude) == ["manage_proposals"]
    args = json.loads(row["request_json"])
    assert args["action"] == "create" and args["org_uid"] == ORG
    assert args["params"]["job_reference"] == JOB and args["params"]["cover_letter"] == LETTER
    assert args["params"]["charged_amount"] == 25.0
    assert args["params"]["answers"] == [{"question": "Which tools?", "answer": "n8n and Python."}]
    assert "boost_connects" not in args["params"]  # boost never by default
    assert app.state.reader.closed >= 1


def test_prepare_cannot_afford_blocks_approve(web, tmp_path, monkeypatch):
    app, client = web
    payload = load("proposal_preview.json")
    payload["data"]["connects_cost"], payload["data"]["connects_balance"] = 16, 3
    rules(tmp_path, monkeypatch, {"tool": "manage_proposals", "text": json.dumps(payload)})
    html = prepare(client).text
    assert "You can&#39;t afford this one: it costs 16 Connects and you have 3." in html
    row = preview_row(app.state.db)
    writes_asked(app, row["id"])
    html = page(client).text
    assert disabled(html, "approve-form") and "Not enough Connects" in html


def writes_asked(app, row_id):
    app.state.db.execute(
        "UPDATE previews SET attachments_asked = 1, highlights_asked = 1 WHERE id = ?", (row_id,)
    )


def test_prepare_already_applied_stops_before_any_preview(web):
    app, client = web
    seed(app.state.db, SENT_JOB)  # SENT_JOB is in list_proposals.json
    token = csrf_from(page(client, SENT_JOB).text)
    r = client.post(f"/jobs/{SENT_JOB}/prepare", data={"csrf_token": token})
    assert str(follow(client, r).url).endswith("msg=applied")
    assert "Already applied" in follow(client, r).text
    assert not app.state.claude.called()  # no write run at all
    assert preview_row(app.state.db, SENT_JOB) is None


def test_prepare_with_invitation_previews_accept_invitation(web):
    app, client = web
    seed(app.state.db, INVITED_JOB)
    app.state.reader = FakeReader(invitations="invitations_two.json")
    token = csrf_from(page(client, INVITED_JOB).text)
    r = client.post(f"/jobs/{INVITED_JOB}/prepare", data={"csrf_token": token})
    html = follow(client, r).text
    assert "accepts the invitation" in html
    assert preview_row(app.state.db, INVITED_JOB)["kind"] == "accept_invitation"


def test_prepare_failure_shows_the_reason_and_no_ready_preview(web, tmp_path, monkeypatch):
    app, client = web
    rules(
        tmp_path,
        monkeypatch,
        {"tool": "manage_proposals", "text": "Job not found", "is_error": True},
    )
    r = follow(client, post(client, "prepare"))
    assert r.status_code == 200 and "Job not found" in r.text
    assert preview_row(app.state.db)["status"] == "failed"
    assert not ja.build_view(app.state.db, app.state.settings, *_job_draft(app))["live"]


def _job_draft(app, job_id=JOB):
    conn = app.state.db
    return (
        conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone(),
        conn.execute("SELECT * FROM drafts WHERE job_id = ?", (job_id,)).fetchone(),
    )


def test_prepare_needs_a_rate(web):
    app, client = web
    app.state.db.execute("UPDATE drafts SET suggested_rate = NULL")
    html = page(client).text
    assert disabled(html, "prepare-form") and "has no rate" in html
    r = post(client, "prepare")
    assert r.status_code == 409 and "has no rate" in r.text
    assert not app.state.claude.called()


def test_boost_only_after_a_preview_showed_the_range(web, tmp_path, monkeypatch):
    app, client = web
    html = page(client).text
    assert "boost_connects" not in html  # nothing known yet
    prepare(client)
    html = page(client).text
    assert 'name="boost_connects"' in html and "10 to 30" in html
    # in range: goes into the next preview
    rules(tmp_path, monkeypatch, preview_rule("pv-boost-1"))
    r = post(client, "prepare", boost_connects="15")
    assert r.status_code == 303
    row = preview_row(app.state.db)
    assert json.loads(row["request_json"])["params"]["boost_connects"] == 15
    assert "includes a boost of 15" in follow(client, r).text


def test_boost_out_of_range_is_refused_with_the_range(web):
    app, client = web
    prepare(client)
    n = len(app.state.claude.logs())
    r = follow(client, post(client, "prepare", boost_connects="99"))
    assert r.status_code == 200 and "between 10 and 30" in r.text
    assert len(app.state.claude.logs()) == n  # refused before claude ran
    r = post(client, "prepare", boost_connects="lots")
    assert r.status_code == 200 and "whole number" in r.text  # refused in the route, no action


# ---------------------------------------------------------------- the two questions


def test_questions_show_highlights_and_approve_needs_the_answers(web):
    app, client = web
    html = prepare(client).text
    assert 'id="q-attachments"' in html and 'id="q-highlights"' in html
    assert "CRM sync demo" in html and "n8n expert" in html
    assert "<untrusted" not in html  # tags stripped from client-side titles
    assert "Loose item without id" in html and "cannot be added from here" in html
    assert app.state.reader.calls[-1] == (
        "get_profile",
        {"action": "list_highlights", "org_uid": ORG, "params": {}},
    )
    assert disabled(html, "approve-form") and "Answer the attachments and highlights" in html
    # a direct POST is refused too, and nothing runs
    row = preview_row(app.state.db)
    n = len(app.state.claude.logs())
    r = post(client, "approve", preview=row["id"])
    assert r.status_code == 409 and "Answer the attachments and highlights" in r.text
    assert len(app.state.claude.logs()) == n
    assert preview_row(app.state.db)["status"] == "previewed"


def test_answering_no_files_no_highlights_marks_asked(web):
    app, client = web
    prepare(client)
    r = answer_both(client, app)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=asked")
    row = preview_row(app.state.db)
    assert row["attachments_asked"] == 1 and row["highlights_asked"] == 1
    assert not disabled(follow(client, r).text, "approve-form")


def test_wanting_files_does_not_mark_asked_and_points_to_upwork(web):
    app, client = web
    prepare(client)
    row = preview_row(app.state.db)
    r = post(client, "answers", preview=row["id"], attachments="want")
    assert r.status_code == 200 and "cannot attach files" in r.text
    row = preview_row(app.state.db)
    assert row["attachments_asked"] == 0 and row["highlights_asked"] == 0
    assert post(client, "answers", preview=row["id"]).status_code == 422  # unanswered


def test_picked_highlight_makes_a_new_preview_with_its_id(web, tmp_path, monkeypatch):
    app, client = web
    rules(
        tmp_path,
        monkeypatch,
        preview_rule("pv-with-hl", args={"params": {"portfolio_project_ids": ["pf-1"]}}),
        preview_rule("pv-0000-aaaa"),
    )
    prepare(client)
    first = preview_row(app.state.db)
    key = next(h["key"] for h in json.loads(first["highlights_json"]) if h["id"] == "pf-1")
    r = answer_both(client, app, highlight=key)
    assert str(follow(client, r).url).endswith("msg=asked")
    new = preview_row(app.state.db)
    assert new["id"] != first["id"] and new["preview_id"] == "pv-with-hl"
    assert json.loads(new["request_json"])["params"]["portfolio_project_ids"] == ["pf-1"]
    assert new["attachments_asked"] == 1 and new["highlights_asked"] == 1
    assert (
        app.state.db.execute("SELECT status FROM previews WHERE id = ?", (first["id"],)).fetchone()[
            0
        ]
        == "expired"
    )
    assert claude_tools(app.state.claude) == ["manage_proposals", "manage_proposals"]
    assert "checked" in re.search(
        r'<input type="checkbox"[^>]*value="h0"[^>]*>', page(client).text
    ).group(0)


def test_highlights_unreadable_is_said_and_does_not_block(web):
    app, client = web
    app.state.reader.highlights = UpworkToolError("no such action")
    html = prepare(client).text
    assert "Highlights could not be read" in html
    assert answer_both(client, app).status_code == 303


# ---------------------------------------------------------------- approve


def test_approve_in_dry_run_does_not_send_and_is_logged(web, caplog):
    app, client = web
    assert app.state.settings.get("general.dry_run") is True  # the default
    prepare(client)
    answer_both(client, app)
    row = preview_row(app.state.db)
    with caplog.at_level("WARNING"):
        r = post(client, "approve", preview=row["id"])
    assert "?action=" in r.headers["location"]
    done = follow(client, r)
    assert str(done.url).endswith("msg=dry_run")
    html = done.text
    assert "Dry-run: not sent" in html
    row = preview_row(app.state.db)
    assert row["status"] == "dry_run_blocked" and row["confirmed_at"] is None
    assert json.loads(row["confirm_result_json"])["dry_run"] is True
    assert claude_tools(app.state.claude) == ["manage_proposals"]  # confirm_preview never ran
    assert "dry-run" in caplog.text
    assert (
        app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()[0]
        == "prepared"
    )


def test_get_never_confirms(web):
    app, client = web
    prepare(client)
    answer_both(client, app)
    row = preview_row(app.state.db)
    r = client.get(f"/jobs/{JOB}/approve")
    assert r.status_code in (404, 405)
    page(client)
    assert claude_tools(app.state.claude) == ["manage_proposals"]
    assert preview_row(app.state.db)["id"] == row["id"] and row["status"] == "previewed"


def test_approve_with_dry_run_off_sends_once(web):
    app, client = web
    app.state.settings.set("general.dry_run", False)
    prepare(client)
    answer_both(client, app)
    row = preview_row(app.state.db)
    r = post(client, "approve", preview=row["id"])
    done = follow(client, r)
    assert str(done.url).endswith("msg=sent")
    assert "Sent to Upwork." in done.text
    assert claude_tools(app.state.claude) == ["manage_proposals", "confirm_preview"]
    assert preview_row(app.state.db)["status"] == "confirmed"
    assert (
        app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()[0]
        == "sent"
    )
    # a second click does nothing: the page has no Approve for it any more
    r = post(client, "approve", preview=row["id"])
    assert r.status_code == 409
    assert claude_tools(app.state.claude) == ["manage_proposals", "confirm_preview"]


def test_approve_failed_confirm_shows_error(web, tmp_path, monkeypatch):
    app, client = web
    app.state.settings.set("general.dry_run", False)
    rules(tmp_path, monkeypatch, preview_rule(), confirm_rule("Upwork said no", is_error=True))
    prepare(client)
    answer_both(client, app)
    r = post(client, "approve", preview=preview_row(app.state.db)["id"])
    assert r.status_code == 303 and "?action=" in r.headers["location"]
    r = follow(client, r)  # the failed action shows its message in the Apply card
    assert r.status_code == 200 and "Upwork said no" in r.text and "Failed" in r.text
    assert (
        app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()[0]
        == "prepared"
    )


# ---------------------------------------------------------------- expired preview


def age(app, row_id, minutes=40):
    old = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    app.state.db.execute("UPDATE previews SET created_at = ? WHERE id = ?", (old, row_id))


def test_expired_preview_shows_remake_and_approve_says_expired(web, tmp_path, monkeypatch):
    app, client = web
    prepare(client)
    answer_both(client, app)
    old = preview_row(app.state.db)
    age(app, old["id"])
    html = page(client).text
    assert "Preview expired" in html and 'id="remake-form"' in html
    assert "Re-make preview" in html and 'id="approve-form"' not in html
    # approving the old one is refused: it says expired and no confirm runs
    r = post(client, "approve", preview=old["id"])
    assert r.status_code == 409 and "Preview expired" in r.text
    assert claude_tools(app.state.claude) == ["manage_proposals"]
    # re-make: a new preview, same letter, flags carried over
    rules(tmp_path, monkeypatch, preview_rule("pv-0000-bbbb"), confirm_rule())
    r = post(client, "remake", preview=old["id"])
    assert r.status_code == 303 and "?action=" in r.headers["location"]
    done = follow(client, r)
    assert str(done.url).endswith("msg=remade")
    new = preview_row(app.state.db)
    assert new["id"] != old["id"] and new["preview_id"] == "pv-0000-bbbb"
    assert new["attachments_asked"] == 1 and new["highlights_asked"] == 1
    assert new["highlights_json"] == old["highlights_json"]
    assert (
        app.state.db.execute("SELECT status FROM previews WHERE id = ?", (old["id"],)).fetchone()[0]
        == "expired"
    )
    html = follow(client, r).text
    assert (
        "Fresh preview made" in html
        and 'id="approve-form"' in html
        and "Preview expired" not in html
    )


def test_remake_is_refused_for_a_live_preview(web):
    app, client = web
    prepare(client)
    r = post(client, "remake", preview=preview_row(app.state.db)["id"])
    assert r.status_code == 409 and "has not expired" in r.text
    assert claude_tools(app.state.claude) == ["manage_proposals"]


def test_stale_preview_after_edit_needs_prepare_again(web):
    app, client = web
    prepare(client)
    answer_both(client, app)
    post(client, "draft", cover_letter="A different letter.", answers="n8n and Python.", rate="25")
    html = page(client).text
    assert "The draft changed after this preview" in html
    assert disabled(html, "approve-form")
    r = post(client, "approve", preview=preview_row(app.state.db)["id"])
    assert r.status_code == 409 and "draft changed" in r.text
    assert claude_tools(app.state.claude) == ["manage_proposals"]


def test_out_of_date_page_is_refused(web):
    app, client = web
    prepare(client)
    answer_both(client, app)
    r = post(client, "approve", preview=preview_row(app.state.db)["id"] + 7)
    assert r.status_code == 409 and "out of date" in r.text


# ---------------------------------------------------------------- skip


def test_skip_and_unskip(web):
    app, client = web
    r = post(client, "skip")
    assert r.status_code == 303 and r.headers["location"].endswith("msg=skipped")
    html = follow(client, r).text
    assert "Job skipped." in html and "This job is skipped." in html

    def status():
        return app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()[
            0
        ]

    assert status() == "skipped"
    assert disabled(html, "prepare-form")
    r = post(client, "prepare")  # the server refuses too
    assert r.status_code == 409 and not app.state.claude.called()
    follow(client, post(client, "unskip"))
    assert status() == "drafted"
    assert not disabled(page(client).text, "prepare-form")


def test_a_sent_job_cannot_be_skipped(web):
    app, client = web
    app.state.db.execute("UPDATE jobs SET status = 'sent'")
    r = post(client, "skip")
    assert r.status_code == 409 and "already sent" in r.text


# ---------------------------------------------------------------- kill switch


def test_kill_switch_disables_every_write_button(web):
    app, client = web
    prepare(client)
    answer_both(client, app)
    row = preview_row(app.state.db)
    age(app, row["id"])  # so the Re-make button shows as well
    app.state.settings.set("general.kill_switch", True)
    html = page(client).text
    assert "The kill switch is on" in html
    for form in ("prepare-form", "remake-form"):
        assert disabled(html, form)
    assert html.count("nothing is written to Upwork") >= 2
    n = len(app.state.claude.logs())
    for path in ("prepare", "remake", "approve", "answers"):
        r = post(client, path, preview=row["id"], attachments="none")
        assert r.status_code == 409 and "kill switch" in r.text
    assert len(app.state.claude.logs()) == n


def test_kill_switch_disables_approve_and_questions(web):
    app, client = web
    prepare(client)
    answer_both(client, app)
    app.state.settings.set("general.kill_switch", True)
    html = page(client).text
    assert disabled(html, "approve-form") and disabled(html, "ask-form")


# ---------------------------------------------------------------- CSRF and login


@pytest.mark.parametrize(
    "path", ["draft", "prepare", "answers", "approve", "remake", "skip", "unskip"]
)
def test_post_without_csrf_is_rejected(web, path):
    app, client = web
    r = client.post(f"/jobs/{JOB}/{path}", data={"cover_letter": "x"})
    assert r.status_code == 403
    r = client.post(f"/jobs/{JOB}/{path}", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    assert not app.state.claude.called()
    assert app.state.db.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 0


def test_posts_need_login(web):
    app, client = web
    token = csrf_from(page(client).text)
    client.cookies.clear()
    r = client.post(f"/jobs/{JOB}/prepare", data={"csrf_token": token})
    assert r.status_code in (303, 403)
    assert not app.state.claude.called()


def test_unknown_job_is_404(web):
    _, client = web
    token = csrf_from(page(client).text)
    r = client.post("/jobs/999/skip", data={"csrf_token": token})
    assert r.status_code == 404


def test_every_form_on_the_page_carries_the_csrf_token(web):
    app, client = web
    prepare(client)
    html = page(client).text
    forms = re.findall(r"<form[^>]*method=\"post\".*?</form>", html, re.S)
    assert len(forms) >= 5
    assert all('name="csrf_token"' in f for f in forms)


# ---------------------------------------------------------------- highlights parser


def test_parse_highlights_kinds_ids_and_limit():
    items = ja.parse_highlights(HIGHLIGHTS, 10)
    by_title = {i["title"]: i for i in items}
    assert by_title["CRM sync demo"]["param"] == "portfolio_project_ids"
    assert by_title["n8n expert"]["param"] == "certificate_ids"
    assert by_title["Loose item without id"]["param"] is None
    assert len(ja.parse_highlights(HIGHLIGHTS, 2)) == 2
    assert ja.parse_highlights("nothing", 5) == [] and ja.parse_highlights(None, 5) == []


def test_highlight_limit_is_a_setting(web):
    app, client = web
    app.state.settings.set("writes.highlights_max_shown", 1)
    html = prepare(client).text
    assert "CRM sync demo" in html and "n8n expert" not in html


# ---------------------------------------------------------------- the worker (plan W2)


class HeldPool:
    """Stands in for the thread pool: remembers the work so a test can run it later."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)


def hold(app):
    pool = HeldPool()
    app.state.worker.sync = False
    app.state.worker._pool = lambda: pool
    return pool


def action_id(r):
    assert r.status_code == 303, r.text[:300]
    loc = r.headers["location"]
    assert loc.startswith(f"/jobs/{JOB}?action=")
    return loc.split("action=")[1]


def test_click_returns_at_once_with_an_action_and_the_work_runs_later(web):
    app, client = web
    pool = hold(app)
    aid = action_id(post(client, "prepare"))
    assert len(pool.jobs) == 1
    assert preview_row(app.state.db) is None  # nothing ran inside the click
    assert not app.state.claude.called()
    row = app.state.worker.get(aid)
    assert row["status"] == "queued" and row["kind"] == "job_prepare" and row["target"] == JOB
    html = page_with_action(client, aid).text
    assert f'hx-get="/actions/{aid}"' in html and "Waiting" in html
    assert "Prepare makes a preview on Upwork and sends nothing" in html  # safety text stays
    pool.run_all()
    assert app.state.worker.get(aid)["status"] == "done"
    assert preview_row(app.state.db)["status"] == "previewed"
    r = client.get(f"/actions/{aid}", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"] == f"/jobs/{JOB}?msg=prepared"


def page_with_action(client, aid):
    r = client.get(f"/jobs/{JOB}?action={aid}")
    assert r.status_code == 200
    return r


def test_double_click_starts_the_work_once(web):
    app, client = web
    pool = hold(app)
    token = csrf_from(page(client).text)
    first = action_id(post(client, "prepare", token=token))
    second = action_id(post(client, "prepare", token=token))
    assert first == second and len(pool.jobs) == 1
    assert app.state.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1


def test_kill_switch_blocks_before_submit(web):
    app, client = web
    pool = hold(app)
    app.state.settings.set("general.kill_switch", True)
    for path in ("prepare", "remake", "approve", "answers"):
        r = post(client, path, preview=1, attachments="none")
        assert r.status_code == 409 and "kill switch" in r.text.lower()
    assert not pool.jobs
    assert app.state.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_kill_switch_turned_on_while_queued_stops_the_work(web):
    app, client = web
    pool = hold(app)
    aid = action_id(post(client, "prepare"))
    app.state.settings.set("general.kill_switch", True)
    pool.run_all()
    row = app.state.worker.get(aid)
    assert row["status"] == "failed" and "kill switch" in row["error"]
    assert not app.state.claude.called() and preview_row(app.state.db) is None
    assert "Stopped by kill switch" in page_with_action(client, aid).text


def test_csrf_missing_starts_no_action(web):
    app, client = web
    pool = hold(app)
    r = client.post(f"/jobs/{JOB}/prepare", data={})
    assert r.status_code in (400, 403)
    assert not pool.jobs and app.state.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_action_page_of_another_job_is_not_shown(web):
    app, client = web
    hold(app)
    aid = action_id(post(client, "prepare"))
    seed(app.state.db, "4000000000000000777")
    html = client.get(f"/jobs/4000000000000000777?action={aid}").text
    assert f'id="action-{aid}"' not in html


def test_dry_run_turned_on_while_the_approve_waits_blocks_the_send(web):
    app, client = web
    app.state.settings.set("general.dry_run", False)
    prepare(client)
    answer_both(client, app)
    row = preview_row(app.state.db)
    pool = hold(app)
    aid = action_id(post(client, "approve", preview=row["id"]))
    app.state.settings.set("general.dry_run", True)  # changed while the action waited
    pool.run_all()
    assert claude_tools(app.state.claude) == ["manage_proposals"]  # no confirm_preview
    assert preview_row(app.state.db)["status"] == "dry_run_blocked"
    r = client.get(f"/actions/{aid}", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"] == f"/jobs/{JOB}?msg=dry_run"
