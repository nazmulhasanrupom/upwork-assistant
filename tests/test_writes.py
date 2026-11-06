"""The Upwork write path (plan H1). A fake `claude` prints stream-json; no real Upwork or Claude.

Every hard safety rule of app/upwork/writes.py has a test here."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Request

from app.db import init_db
from app.llm.runner import ClaudeFailed
from app.settings import Settings
from app.upwork import bridge, writes
from app.upwork.bridge import ClaudeUpworkBridge, ToolNotAllowed
from app.upwork.client import ToolResult
from app.upwork.writes import (
    AlreadyConfirmed,
    ClaudeUpworkWriter,
    ClickRequired,
    ExistingProposal,
    PreviewExpired,
    UserClick,
    WriteRefused,
)
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.upwork_fakes import load

ORG = "1000000000000000001"
JOB = "4000000000000000999"  # no proposal, no invitation
INVITED_JOB = "4000000000000000101"  # invitation 7000000000000000001 (invitations_two.json)
SENT_JOB = "4000000000000000001"  # already in list_proposals.json
LETTER = "Hello, I build n8n flows for CRM syncs. Here is how I would do it."
PREVIEW = json.dumps(load("proposal_preview.json"))


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


@pytest.fixture
def writer(config, settings, conn):
    return ClaudeUpworkWriter(config, settings, conn)


class FakeReader:
    """Stands in for the READ bridge: list and invitations answered from fixtures."""

    def __init__(self, proposals="list_proposals.json", invitations="invitations_empty.json"):
        self.proposals = proposals
        self.invitations = invitations
        self.calls = []
        self.error = False

    def call_many(self, calls):
        self.calls.extend(calls)
        out = []
        for tool, args in calls:
            assert tool == "list_freelancer_proposals"
            name = self.proposals if args["action"] == "list" else self.invitations
            text = json.dumps(load(name))
            content = [{"type": "text", "text": text}]
            out.append(ToolResult(tool, content, is_error=self.error))
        return out


def click(action="preview"):
    return UserClick._issue("tester", action)


def rules(tmp_path, monkeypatch, *items):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(list(items)))
    monkeypatch.setenv("FAKE_UPWORK_RULES", str(path))


def preview_rule(preview_id="pv-0000-aaaa", **kw):
    payload = json.loads(PREVIEW)
    payload["data"]["preview_id"] = preview_id
    return {"tool": "manage_proposals", "text": json.dumps(payload), **kw}


def confirm_rule(text='{"status":"ok","data":{"submitted":true}}', **kw):
    return {"tool": "confirm_preview", "text": text, **kw}


@pytest.fixture
def fake(fake_claude_stream, tmp_path, monkeypatch):
    rules(tmp_path, monkeypatch, preview_rule(), confirm_rule())
    return fake_claude_stream


_MAKE = object()


def go(conn, config, settings, writer, reader=None, c=_MAKE, **kw):
    args = {
        "job_id": JOB,
        "cover_letter": LETTER,
        "charged_amount": 25,
        "attachments_asked": True,
        "highlights_asked": True,
    }
    args.update(kw)
    return writes.preview_proposal(
        click() if c is _MAKE else c,
        conn=conn,
        config=config,
        settings=settings,
        reader=reader or FakeReader(),
        writer=writer,
        **args,
    )


def confirm(conn, config, settings, writer, row_id, c=_MAKE):
    return writes.confirm_preview(
        click("confirm") if c is _MAKE else c,
        conn=conn,
        config=config,
        settings=settings,
        writer=writer,
        row_id=row_id,
    )


def prompt_call(log):
    """The tool call line inside the prompt claude was given."""
    argv = log["argv"]
    prompt = argv[argv.index("-p") + 1]
    line = prompt.strip().splitlines()[-1]
    num, name, args = line.split(" ", 2)
    return name, json.loads(args)


# ---------------------------------------------------------------- the click


def test_no_click_raises_and_nothing_runs(fake, conn, config, settings, writer):
    with pytest.raises(ClickRequired):
        go(conn, config, settings, writer, c=None)
    with pytest.raises(ClickRequired):
        go(conn, config, settings, writer, c=object())
    with pytest.raises(ClickRequired):
        confirm(conn, config, settings, writer, 1, c=None)
    with pytest.raises(ClickRequired):
        writer.call(None, "manage_proposals", {"action": "create", "org_uid": ORG, "params": {}})
    assert not fake.called()
    assert conn.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 0


def test_a_click_cannot_be_built_by_hand(settings):
    with pytest.raises(ClickRequired):
        UserClick(user="x", action="preview", click_id="1", issued_at=0.0)
    with pytest.raises(ClickRequired):
        UserClick(user="x", action="preview", click_id="1", issued_at=0.0, _seal=object())


def test_wrong_old_or_used_click_is_refused(fake, conn, config, settings, writer):
    with pytest.raises(ClickRequired):  # a confirm click cannot make a preview
        go(conn, config, settings, writer, c=click("confirm"))
    old = click()
    old.issued_at -= 10_000
    with pytest.raises(ClickRequired):
        go(conn, config, settings, writer, c=old)
    used = click()
    go(conn, config, settings, writer, c=used)
    assert used.used
    with pytest.raises(ClickRequired):  # one click is one write
        go(conn, config, settings, writer, c=used)
    assert len(fake.logs()) == 1


def test_issue_click_only_in_a_logged_in_csrf_checked_post(config):
    app, client = make_web_client(config)
    setup_admin(client)

    @app.post("/_t/click")
    async def post_click(request: Request):
        return {"user": writes.issue_click(request, "preview").user}

    @app.get("/_t/click")
    async def get_click(request: Request):
        return {"user": writes.issue_click(request, "preview").user}

    token = csrf_from(client.get("/").text)
    assert client.post("/_t/click").status_code == 403  # no CSRF
    ok = client.post("/_t/click", data={"csrf_token": token})
    assert ok.status_code == 200 and ok.json()["user"]
    with pytest.raises(ClickRequired):
        client.get("/_t/click")  # a page load is not a click
    client.cookies.clear()
    assert client.post("/_t/click", data={"csrf_token": token}).status_code in (303, 403)


# ---------------------------------------------------------------- one tool, one call


def test_one_tool_in_allowed_tools_and_locked_down(fake, conn, config, settings, writer):
    settings.set("models.fetch", "claude-haiku-test")
    res = go(conn, config, settings, writer)
    assert res.status == "previewed"
    log = fake.log()
    argv = log["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__upwork__upwork__manage_proposals"
    assert "," not in argv[argv.index("--allowedTools") + 1]
    disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
    for tool in ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "Read", "Glob", "Grep", "Task"):
        assert tool in disallowed
    assert argv[argv.index("--model") + 1] == "claude-haiku-test"
    assert argv[argv.index("--max-turns") + 1] == "4"
    assert "--bare" not in argv
    assert log["stdin"] == ""  # /dev/null
    assert Path(log["cwd"]).parent == config.runs_dir


def test_confirm_run_allows_only_confirm_preview(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    confirm(conn, config, settings, writer, row)
    argv = fake.logs()[1]["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__upwork__upwork__confirm_preview"


def test_prompt_has_one_call_and_nothing_from_the_job(fake, conn, config, settings, writer):
    go(conn, config, settings, writer, answers=[{"question": "Why you?", "answer": "I fit."}])
    argv = fake.log()["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith(writes.PROMPT_HEAD)
    body = prompt[len(writes.PROMPT_HEAD) :]
    assert len(body.strip().splitlines()) == 1  # exactly one call line
    name, args = prompt_call(fake.log())
    assert name == "mcp__upwork__upwork__manage_proposals"
    assert args == {
        "action": "create",
        "org_uid": ORG,
        "params": {
            "job_reference": JOB,
            "cover_letter": LETTER,
            "charged_amount": 25,
            "answers": [{"question": "Why you?", "answer": "I fit."}],
        },
    }


def test_duplicate_write_calls_are_noticed(
    fake_claude_stream, conn, config, settings, writer, tmp_path, monkeypatch
):
    args = {
        "action": "create",
        "org_uid": ORG,
        "params": {"job_reference": JOB, "cover_letter": LETTER, "charged_amount": 25},
    }
    name = "mcp__upwork__upwork__manage_proposals"
    lines = [{"type": "system", "subtype": "init", "mcp_servers": []}]
    for i in (1, 2):
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": args}]
                },
            }
        )
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i}",
                            "content": [{"type": "text", "text": PREVIEW}],
                        }
                    ]
                },
            }
        )
    lines.append({"type": "result", "subtype": "success", "is_error": False,
                  "total_cost_usd": 0.01, "usage": {"output_tokens": 3}})  # fmt: skip
    stream = tmp_path / "dup.jsonl"
    stream.write_text("\n".join(json.dumps(x) for x in lines))
    fake_claude_stream.mode("replay")
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", str(stream))
    res = go(conn, config, settings, writer)
    assert res.extra_calls == 1 and "extra write calls" in res.message


def test_each_write_run_logs_an_audit_row(fake, conn, config, settings, writer):
    go(conn, config, settings, writer)
    rows = conn.execute("SELECT task, model, run_id FROM claude_calls").fetchall()
    assert [r["task"] for r in rows] == ["write"] and rows[0]["run_id"].startswith("write-")
    assert conn.execute("SELECT run_id FROM previews").fetchone()[0] == rows[0]["run_id"]


def test_audit_row_even_when_claude_fails(fake_claude_stream, conn, config, settings, writer):
    fake_claude_stream.mode("no_result")
    with pytest.raises(ClaudeFailed):
        go(conn, config, settings, writer)
    assert conn.execute("SELECT COUNT(*) FROM claude_calls WHERE task='write'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "tool,args",
    [
        ("update_profile", {"action": "update", "org_uid": ORG, "params": {}}),
        ("manage_proposals", {"action": "withdraw", "org_uid": ORG, "params": {}}),
        ("manage_proposals", {"action": "create", "params": {}}),
        ("manage_proposals", {"action": "create", "org_uid": ORG, "params": {}, "mode": "x"}),
        ("confirm_preview", {"action": "confirm", "org_uid": ORG,
                             "params": {"type": "proposal", "preview_id": "p", "extra": 1}}),
        ("confirm_preview", {"action": "confirm", "org_uid": ORG,
                             "params": {"type": "contract", "preview_id": "p"}}),
        ("confirm_preview", {"action": "confirm", "org_uid": ORG,
                             "params": {"type": "proposal"}}),
        ("confirm_preview", {"action": "cancel", "org_uid": ORG,
                             "params": {"type": "proposal", "preview_id": "p"}}),
    ],
)  # fmt: skip
def test_only_the_two_tools_with_allowed_actions(fake, writer, tool, args):
    with pytest.raises(WriteRefused):
        writer.call(click("confirm" if tool == "confirm_preview" else "preview"), tool, args)
    assert not fake.called()


def test_write_tools_are_exactly_two():
    assert writes.WRITE_TOOLS == ("manage_proposals", "confirm_preview", "update_profile")
    assert set(writes.WRITE_TOOLS).isdisjoint(bridge.READ_TOOLS)
    with pytest.raises(WriteRefused):
        writes.build_command("p", "find_jobs", "m", 4, 0.25)


# ---------------------------------------------------------------- the read bridge still blocks


@pytest.mark.parametrize("tool", ["manage_proposals", "confirm_preview"])
def test_read_bridge_still_blocks_write_tools(fake_claude_stream, config, settings, conn, tool):
    br = ClaudeUpworkBridge(config, settings, conn)
    with pytest.raises(ToolNotAllowed):
        br.call_many([(tool, {"action": "confirm", "org_uid": ORG, "params": {}})])
    assert not fake_claude_stream.called()
    assert tool not in bridge.READ_TOOLS
    assert bridge._FORBIDDEN_RE.match(tool)
    with pytest.raises(ToolNotAllowed):
        bridge.build_command("p", [tool], "m", 4, 0.25)


# ---------------------------------------------------------------- preview rules


def test_preview_stores_everything(fake, conn, config, settings, writer):
    res = go(conn, config, settings, writer, boost_connects=None)
    assert (res.status, res.kind, res.preview_id) == ("previewed", "create", "pv-0000-aaaa")
    assert (res.connects_cost, res.connects_balance, res.can_afford) == (16, 40, True)
    assert res.boost["recommended_connects"] == 10
    row = conn.execute("SELECT * FROM previews").fetchone()
    assert row["job_id"] == JOB and row["status"] == "previewed" and row["confirmed_at"] is None
    assert row["connects_cost"] == 16 and row["connects_balance"] == 40 and row["created_at"]
    assert json.loads(row["request_json"])["params"]["job_reference"] == JOB
    assert json.loads(row["boost_json"])["max_boost_connects"] == 30
    assert json.loads(row["response_json"])["data"]["preview_id"] == "pv-0000-aaaa"
    assert row["click_id"] and row["attachments_asked"] == 1 and row["highlights_asked"] == 1


def test_preview_is_allowed_in_dry_run(fake, conn, config, settings, writer):
    assert settings.get("general.dry_run") is True
    assert go(conn, config, settings, writer).status == "previewed"


def test_preview_error_or_no_id_is_failed_and_not_confirmable(
    fake_claude_stream, tmp_path, monkeypatch, conn, config, settings, writer
):
    rules(tmp_path, monkeypatch, {"tool": "manage_proposals", "text": "nope", "is_error": True})
    res = go(conn, config, settings, writer)
    assert res.status == "failed" and "nope" in res.message and res.preview_id is None
    settings.set("general.dry_run", False)
    with pytest.raises(WriteRefused):
        confirm(conn, config, settings, writer, res.row_id)
    rules(tmp_path, monkeypatch, {"tool": "manage_proposals", "text": '{"data":{}}'})
    assert go(conn, config, settings, writer).status == "failed"


def test_cannot_apply_is_failed(fake_claude_stream, tmp_path, monkeypatch, conn, config,
                                settings, writer):  # fmt: skip
    payload = json.loads(PREVIEW)
    payload["data"]["can_apply"] = False
    rules(tmp_path, monkeypatch, {"tool": "manage_proposals", "text": json.dumps(payload)})
    res = go(conn, config, settings, writer)
    assert res.status == "failed" and res.can_apply is False


def test_cover_letter_over_5000_refused_not_cut(fake, conn, config, settings, writer):
    with pytest.raises(WriteRefused, match="5000"):
        go(conn, config, settings, writer, cover_letter="a" * 5001)
    assert not fake.called()
    assert go(conn, config, settings, writer, cover_letter="a" * 5000).status == "previewed"
    _, args = prompt_call(fake.log())
    assert len(args["params"]["cover_letter"]) == 5000


@pytest.mark.parametrize(
    "kw",
    [
        {"cover_letter": ""},
        {"cover_letter": "It works — really."},
        {"cover_letter": "En – dash."},
        {"charged_amount": 0},
        {"charged_amount": "25"},
        {"job_id": "~02abcdef"},
        {"answers": [{"question": "q", "answer": "  "}]},
        {"answers": [{"question": "q", "answer": "a — b"}]},
        {"extra": {"attachments": ["x"]}},
        {"extra": {"anything": 1}},
    ],
)
def test_bad_input_is_refused_before_claude(fake, conn, config, settings, writer, kw):
    with pytest.raises(WriteRefused):
        go(conn, config, settings, writer, **kw)
    assert not fake.called()


def test_pass_through_params(fake, conn, config, settings, writer):
    go(
        conn, config, settings, writer,
        extra={"estimated_duration": "1_to_3_months", "payment_mode": "milestone",
               "milestones": [{"description": "a", "amount": 10}], "certificate_ids": ["c1"]},
    )  # fmt: skip
    params = prompt_call(fake.log())[1]["params"]
    assert params["payment_mode"] == "milestone" and params["certificate_ids"] == ["c1"]


@pytest.mark.parametrize("boost", [9, 31, 0, -1, True, 12.5, "12"])
def test_boost_out_of_range_refused(fake, conn, config, settings, writer, boost):
    limits = {"available": True, "recommended_connects": 10, "max_boost_connects": 30}
    with pytest.raises(WriteRefused):
        go(conn, config, settings, writer, boost_connects=boost, boost_limits=limits)
    assert not fake.called()


def test_boost_needs_known_limits_and_availability(fake, conn, config, settings, writer):
    with pytest.raises(WriteRefused, match="not known"):
        go(conn, config, settings, writer, boost_connects=12)
    off = {"available": False, "recommended_connects": 10, "max_boost_connects": 30}
    with pytest.raises(WriteRefused, match="not available"):
        go(conn, config, settings, writer, boost_connects=12, boost_limits=off)
    assert not fake.called()


def test_boost_in_range_is_sent_and_limits_come_from_the_last_preview(
    fake, conn, config, settings, writer, tmp_path, monkeypatch
):
    go(conn, config, settings, writer)  # learns the boost range (10..30)
    with pytest.raises(WriteRefused):
        go(conn, config, settings, writer, boost_connects=31)
    rules(tmp_path, monkeypatch, preview_rule("pv-0000-boost"))
    res = go(conn, config, settings, writer, boost_connects=10)
    assert res.status == "previewed"
    assert prompt_call(fake.logs()[-1])[1]["params"]["boost_connects"] == 10


def test_kill_switch_blocks_writes(fake, conn, config, settings, writer):
    settings.set("general.kill_switch", True)
    with pytest.raises(WriteRefused, match="kill switch"):
        go(conn, config, settings, writer)
    assert not fake.called()


# ---------------------------------------------------------------- existing proposal / invitation


def test_existing_proposal_is_refused(fake, conn, config, settings, writer):
    reader = FakeReader()
    with pytest.raises(ExistingProposal):
        go(conn, config, settings, writer, reader=reader, job_id=SENT_JOB)
    assert not fake.called()
    assert [a["action"] for _, a in reader.calls] == ["list", "invitations"]
    assert all(a["org_uid"] == ORG for _, a in reader.calls)


def test_existing_proposal_in_my_own_records_is_refused(fake, conn, config, settings, writer):
    conn.execute("INSERT INTO proposals(proposal_id, job_id) VALUES ('p1', ?)", (JOB,))
    with pytest.raises(ExistingProposal):
        go(conn, config, settings, writer)
    assert not fake.called()


def test_invite_exists_uses_accept_invitation(fake, conn, config, settings, writer):
    reader = FakeReader(invitations="invitations_two.json")
    res = go(conn, config, settings, writer, reader=reader, job_id=INVITED_JOB, charged_amount=30)
    assert res.kind == "accept_invitation"
    name, args = prompt_call(fake.log())
    assert name == "mcp__upwork__upwork__manage_proposals"
    assert args == {
        "action": "accept_invitation",
        "org_uid": ORG,
        "params": {"invitation_uid": "7000000000000000001", "cover_letter": LETTER, "bid": 30},
    }
    assert "job_reference" not in json.dumps(args)
    row = conn.execute("SELECT * FROM previews").fetchone()
    assert row["kind"] == "accept_invitation" and row["invitation_uid"] == "7000000000000000001"


def test_confirm_type_follows_the_kind(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    reader = FakeReader(invitations="invitations_two.json")
    row = go(conn, config, settings, writer, reader=reader, job_id=INVITED_JOB).row_id
    confirm(conn, config, settings, writer, row)
    assert prompt_call(fake.logs()[1])[1] == {
        "action": "confirm",
        "org_uid": ORG,
        "params": {"type": "proposal_accept_invitation", "preview_id": "pv-0000-aaaa"},
    }


def test_the_check_failing_means_no_write(fake, conn, config, settings, writer):
    reader = FakeReader()
    reader.error = True
    with pytest.raises(WriteRefused, match="Could not check"):
        go(conn, config, settings, writer, reader=reader)
    assert not fake.called()


# ---------------------------------------------------------------- dry run, double confirm, expiry


def test_dry_run_blocks_confirm_and_records_it(fake, conn, config, settings, writer, caplog):
    row = go(conn, config, settings, writer).row_id
    with caplog.at_level("WARNING", logger="upwork.writes"):
        res = confirm(conn, config, settings, writer, row)
    assert res.sent is False and res.status == "dry_run_blocked"
    assert res.message == "dry-run: not sent"
    assert len(fake.logs()) == 1  # only the preview ran; confirm_preview never reached claude
    got = conn.execute("SELECT * FROM previews WHERE id = ?", (row,)).fetchone()
    assert got["status"] == "dry_run_blocked" and got["confirmed_at"] is None
    assert json.loads(got["confirm_result_json"])["dry_run"] is True
    assert "dry-run" in caplog.text


def test_writer_itself_refuses_confirm_in_dry_run(fake, writer):
    args = {"action": "confirm", "org_uid": ORG,
            "params": {"type": "proposal", "preview_id": "pv-1"}}  # fmt: skip
    with pytest.raises(WriteRefused, match="dry-run"):
        writer.call(click("confirm"), "confirm_preview", args)
    assert not fake.called()


def test_after_dry_run_a_real_confirm_can_follow(fake, conn, config, settings, writer):
    row = go(conn, config, settings, writer).row_id
    confirm(conn, config, settings, writer, row)  # blocked
    settings.set("general.dry_run", False)
    res = confirm(conn, config, settings, writer, row)
    assert res.sent and res.status == "confirmed"


def test_confirm_sends_exactly_type_and_preview_id(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    res = confirm(conn, config, settings, writer, row)
    assert res.sent and res.response == {"status": "ok", "data": {"submitted": True}}
    assert prompt_call(fake.logs()[1]) == (
        "mcp__upwork__upwork__confirm_preview",
        {
            "action": "confirm",
            "org_uid": ORG,
            "params": {"type": "proposal", "preview_id": "pv-0000-aaaa"},
        },
    )
    got = conn.execute("SELECT * FROM previews WHERE id = ?", (row,)).fetchone()
    assert got["status"] == "confirmed" and got["confirmed_at"]
    assert json.loads(got["confirm_result_json"])["click_id"]
    tasks = [r[0] for r in conn.execute("SELECT task FROM claude_calls")]
    assert tasks == ["write", "write"]


def test_double_confirm_is_blocked(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    confirm(conn, config, settings, writer, row)
    with pytest.raises(AlreadyConfirmed):
        confirm(conn, config, settings, writer, row)
    assert len(fake.logs()) == 2  # preview + ONE confirm
    # the DB itself refuses a second row with the same preview_id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO previews(kind, preview_id, request_json, created_at) "
            "VALUES ('create', 'pv-0000-aaaa', '{}', 'x')"
        )


def test_confirm_started_elsewhere_is_blocked(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    conn.execute("UPDATE previews SET confirmed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
                 (row,))  # fmt: skip
    with pytest.raises(AlreadyConfirmed):
        confirm(conn, config, settings, writer, row)
    assert len(fake.logs()) == 1


def test_failed_confirm_is_never_retried(
    fake, conn, config, settings, writer, tmp_path, monkeypatch
):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    rules(tmp_path, monkeypatch, confirm_rule("preview gone", is_error=True))
    res = confirm(conn, config, settings, writer, row)
    assert res.status == "failed" and not res.sent and "preview gone" in res.message
    with pytest.raises(AlreadyConfirmed):
        confirm(conn, config, settings, writer, row)
    assert len(fake.logs()) == 2


def test_claude_crash_during_confirm_is_marked_and_not_retried(
    fake, fake_claude_stream, conn, config, settings, writer
):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    fake.mode("no_result")
    with pytest.raises(ClaudeFailed):
        confirm(conn, config, settings, writer, row)
    got = conn.execute("SELECT * FROM previews WHERE id = ?", (row,)).fetchone()
    assert (
        got["status"] == "failed" and got["confirmed_at"] and "error" in got["confirm_result_json"]
    )
    fake.mode("echo")
    with pytest.raises(AlreadyConfirmed):
        confirm(conn, config, settings, writer, row)


def test_attachment_and_highlight_questions_must_be_asked(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer, attachments_asked=False).row_id
    with pytest.raises(WriteRefused, match="attachments"):
        confirm(conn, config, settings, writer, row)
    writes.mark_asked(conn, row, attachments=True, highlights=True)
    assert confirm(conn, config, settings, writer, row).sent
    assert len(fake.logs()) == 2


def test_expired_preview_cannot_be_confirmed(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    old = (datetime.now(UTC) - timedelta(minutes=26)).isoformat(timespec="seconds")
    conn.execute("UPDATE previews SET created_at = ? WHERE id = ?", (old, row))
    with pytest.raises(PreviewExpired):
        confirm(conn, config, settings, writer, row)
    assert conn.execute("SELECT status FROM previews").fetchone()[0] == "expired"
    with pytest.raises(PreviewExpired):  # still expired the second time
        confirm(conn, config, settings, writer, row)
    assert len(fake.logs()) == 1


def test_max_age_is_a_setting(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    settings.set("writes.preview_max_age_minutes", 5)
    row = go(conn, config, settings, writer).row_id
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat(timespec="seconds")
    conn.execute("UPDATE previews SET created_at = ?", (old,))
    with pytest.raises(PreviewExpired):
        confirm(conn, config, settings, writer, row)


def test_remake_preview_makes_a_new_one_then_confirms(
    fake, conn, config, settings, writer, tmp_path, monkeypatch
):
    settings.set("general.dry_run", False)
    first = go(conn, config, settings, writer, answers=[{"question": "q", "answer": "a"}]).row_id
    old = (datetime.now(UTC) - timedelta(minutes=40)).isoformat(timespec="seconds")
    conn.execute("UPDATE previews SET created_at = ? WHERE id = ?", (old, first))
    with pytest.raises(PreviewExpired):
        confirm(conn, config, settings, writer, first)
    rules(tmp_path, monkeypatch, preview_rule("pv-0000-bbbb"), confirm_rule())
    new = writes.remake_preview(
        click(), conn=conn, config=config, settings=settings, reader=FakeReader(),
        writer=writer, row_id=first,
    )  # fmt: skip
    assert new.status == "previewed" and new.preview_id == "pv-0000-bbbb" and new.row_id != first
    assert conn.execute("SELECT status FROM previews WHERE id = ?", (first,)).fetchone()[0] == (
        "expired"
    )
    assert prompt_call(fake.logs()[-1])[1]["params"]["answers"] == [
        {"question": "q", "answer": "a"}
    ]
    assert confirm(conn, config, settings, writer, new.row_id).sent
    with pytest.raises(PreviewExpired):  # the old one stays dead
        confirm(conn, config, settings, writer, first)


def test_remake_needs_a_click_and_refuses_a_confirmed_preview(fake, conn, config, settings, writer):
    settings.set("general.dry_run", False)
    row = go(conn, config, settings, writer).row_id
    kw = dict(conn=conn, config=config, settings=settings, reader=FakeReader(),
              writer=writer, row_id=row)  # fmt: skip
    with pytest.raises(ClickRequired):
        writes.remake_preview(None, **kw)
    confirm(conn, config, settings, writer, row)
    with pytest.raises(AlreadyConfirmed):
        writes.remake_preview(click(), **kw)


def test_expire_old_sweeps_only_old_unconfirmed(fake, conn, config, settings, writer):
    a = go(conn, config, settings, writer).row_id
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute("UPDATE previews SET created_at = ? WHERE id = ?", (old, a))
    assert writes.expire_old(conn, settings) == 1
    assert writes.expire_old(conn, settings) == 0


def test_previews_table_has_the_planned_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(previews)")}
    assert cols >= {
        "id", "kind", "job_id", "invitation_uid", "preview_id", "request_json", "response_json",
        "connects_cost", "connects_balance", "boost_json", "created_at", "confirmed_at",
        "confirm_result_json", "status", "click_id",
    }  # fmt: skip
