"""Manual job add (plan M1). Fakes only: the READ bridge and judging are replaced."""

import json

import pytest

from app import manual_add as M
from app.llm import judging
from app.llm.judging import JudgeResult
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.upwork_fakes import load, to_result

ORG = "1000000000000000001"
JOB = "2118524057353559796"  # the id inside tests/fixtures/upwork/get_job.json


class FakeBridge:
    def __init__(self, detail_error=None):
        self.calls = []
        self.detail_error = detail_error
        self.closed = 0

    def call_many(self, calls):
        out = []
        for tool, args in calls:
            self.calls.append((tool, args))
            assert tool == "find_jobs"
            if args["action"] == "get":
                payload = self.detail_error or load("get_job.json")
            else:
                payload = load("smart_search_best_match.json")
            out.append(to_result(tool, payload))
        return out

    def close(self):
        self.closed += 1


@pytest.fixture
def web(config, monkeypatch):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.worker.sync = True
    app.state.settings.set("upwork.talent_org_uid", ORG)
    app.state.bridge = FakeBridge()
    app.state.mcp_factory = lambda: app.state.bridge
    judged_calls = []

    def fake_judge(conn, candidates, **kw):
        judged_calls.append([c["id"] for c in candidates])
        return JudgeResult(judged=[{"job_id": c["id"], "tier": c["tier"]} for c in candidates])

    monkeypatch.setattr(judging, "judge_and_draft", fake_judge)
    monkeypatch.setattr(M.auto_send_flow, "start_countdowns", lambda *a, **k: [])
    app.state.judged_calls = judged_calls
    return app, client


def post(client, path, **data):
    token = csrf_from(client.get("/jobs/add").text)
    return client.post(path, data={"csrf_token": token, **data}, follow_redirects=False)


def last_action(app):
    return app.state.worker.get(
        app.state.db.execute("SELECT id FROM actions ORDER BY created DESC LIMIT 1").fetchone()[0]
    )


def test_parse_cases():
    assert M.parse_input("see ~021234567890abc here", 300) == M.Parsed("id", "~021234567890abc")
    assert M.parse_input(f"https://www.upwork.com/jobs/x_{JOB}", 300).value == JOB
    assert M.parse_input("job 2118524057353559796", 300).kind == "id"
    url = "https://www.upwork.com/nx/find-work/best-matches"
    assert M.parse_input(url, 300) == M.Parsed("url", url)
    assert M.parse_input("  n8n   crm  ", 300) == M.Parsed("title", "n8n crm")


@pytest.mark.parametrize("bad", ["", "   ", "x" * 301])
def test_parse_bad(bad):
    with pytest.raises(M.ManualError):
        M.parse_input(bad, 300)


def test_form_and_bad_input(web):
    app, client = web
    assert client.get("/jobs/add").status_code == 200
    r = post(client, "/jobs/add", text="x" * 400)
    assert r.status_code == 422


def test_add_by_id_saves_candidate_and_judges(web):
    app, client = web
    r = post(client, "/jobs/add", text=JOB)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/add?action=")
    row = app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()
    assert row["status"] == "candidate"
    assert app.state.judged_calls == [[JOB]]
    action = last_action(app)
    assert action["status"] == "done" and action["result_url"] == f"/jobs/{JOB}"
    assert "judged" in action["message"]
    assert client.get(r.headers["location"]).status_code == 200


def test_filter_reasons_reported_and_job_kept(web):
    app, client = web
    app.state.settings.set("pay.good_spending", 10_000_000)
    post(client, "/jobs/add", text=f"https://www.upwork.com/jobs/~02abcdefghij{JOB}")
    row = app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()
    assert row["status"] == "candidate"
    assert "would have dropped" in last_action(app)["message"]
    assert app.state.judged_calls  # judged anyway


def test_existing_job_not_re_added(web):
    app, client = web
    post(client, "/jobs/add", text=JOB)
    app.state.judged_calls.clear()
    app.state.bridge.calls.clear()
    post(client, "/jobs/add", text=JOB)
    assert app.state.judged_calls == []
    assert app.state.bridge.calls == []
    assert "already" in last_action(app)["message"]


def test_dropped_job_is_added_again(web):
    app, client = web
    app.state.db.execute(
        "INSERT INTO jobs(job_id, first_seen, title, status) VALUES (?, 'x', 't', 'dropped')",
        (JOB,),
    )
    post(client, "/jobs/add", text=JOB)
    row = app.state.db.execute("SELECT status FROM jobs WHERE job_id = ?", (JOB,)).fetchone()
    assert row["status"] == "candidate"


def test_job_not_found_fails_with_text(web):
    app, client = web
    app.state.bridge.detail_error = {"status": "error", "message": "nope"}
    post(client, "/jobs/add", text=JOB)
    action = last_action(app)
    assert action["status"] == "failed" and "did not return" in action["error"]


def test_title_search_writes_file_and_lists(web, config):
    app, client = web
    r = post(client, "/jobs/add", text="automation developer")
    action = last_action(app)
    assert action["status"] == "done"
    found_id = action["result_url"].split("found=")[1]
    saved = json.loads((config.data_dir / "manual" / f"{found_id}.json").read_text())
    assert saved and r.status_code == 303
    page = client.get(action["result_url"])
    assert page.status_code == 200 and saved[0]["id"] in page.text
    call = app.state.bridge.calls[0][1]
    assert call["action"] == "search" and call["params"]["title"] == "automation developer"
    assert call["params"]["limit"] == 10


def test_add_from_list_and_known_mark(web):
    app, client = web
    post(client, "/jobs/add", text="automation developer")
    found_id = last_action(app)["result_url"].split("found=")[1]
    rows = M.load_found(app.state.config, found_id)
    app.state.bridge.calls.clear()
    post(client, "/jobs/add", job_id=str(rows[0]["id"]), found=found_id)
    gets = [c for c in app.state.bridge.calls if c[1]["action"] == "get"]
    assert len(gets) == 1 and gets[0][1]["params"]["job_id"] == str(rows[0]["id"])
    assert last_action(app)["status"] == "done"
    r = post(client, "/jobs/add", job_id="999", found="not-hex")
    assert r.status_code == 422


def test_found_id_must_be_hex(config):
    assert M.load_found(config, "../../etc/passwd") == []


def test_low_tier_job_in_fresh_ids_is_judged(config):
    from app.db import init_db

    conn = init_db(config.db_path)
    for jid, tier in (("1", "LOW"), ("2", "LOW"), ("3", "MID")):
        conn.execute(
            "INSERT INTO jobs(job_id, first_seen, title, status, tier) VALUES (?, 'z', 't', "
            "'candidate', ?)",
            (jid, tier),
        )
    got = {r["job_id"] for r in judging._pending_judge(conn, {"1"}, "a")}
    assert got == {"1", "3"}


def test_jobs_page_has_add_button(web):
    app, client = web
    assert "/jobs/add" in client.get("/jobs").text
