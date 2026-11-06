"""Daily task (plan E3) on fixtures with a fake MCP client. No network."""

import copy
import json

import pytest

from app.db import init_db
from app.settings import Settings
from app.tasks import daily
from app.upwork.client import CallBudgetExceeded, UpworkAuthError, UpworkToolError
from tests.upwork_fakes import FakeMCP, load

ORG = "1000000000000000001"
GET_JOB_ID = "2118524057353559796"


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


def detail_for(job_id, spend, hours, cost=17, balance=50, fixed=False):
    d = load("get_job.json")
    posting = d["data"]["marketplaceJobPosting"]
    posting["id"] = job_id
    posting["url"] = f"https://www.upwork.com/jobs/~02{job_id}"
    if fixed:
        posting["contractTerms"]["contractType"] = "FIXED_PRICE"
    d["client_record"]["spend_total"] = f"${spend:,.2f}"
    d["client_record"]["hours_total"] = hours
    d["client_feedback"]["reviews"] = []
    d["connects_cost"], d["connects_balance"] = cost, balance
    d["can_apply"] = True
    return d


def make_details(balance=50):
    return {
        GET_JOB_ID: load("get_job.json"),  # 13692.95 / 1542 = 8.88 -> LOW
        "2118457549206214329": detail_for("2118457549206214329", 13543.74, 0, 6, balance, True),
        "2118527230669974661": detail_for("2118527230669974661", 43281.04, 1000, 12, balance),
        "2118442174097105410": detail_for("2118442174097105410", 10457.80, 700, 9, balance),
        "2118387252193629626": detail_for("2118387252193629626", 180499.82, 8000, 15, balance),
        "2118356080680739330": detail_for("2118356080680739330", 34893.81, 4000, 8, balance),
        "2118413555956864697": detail_for("2118413555956864697", 32547.76, 1918, 11, balance),
    }


def make_client(settings, balance=50, **kw):
    return FakeMCP(
        settings,
        search={
            "most_recent": load("smart_search_most_recent.json"),
            "best_match": load("smart_search_best_match.json"),
        },
        details=make_details(balance),
        **kw,
    )


def rows(conn):
    return {r["job_id"]: r for r in conn.execute("SELECT * FROM jobs")}


def test_fixtures_fill_the_db(conn, settings, config):
    client = make_client(settings, balance=5)
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "ok"
    assert (res.seen, res.kept, res.dropped) == (8, 5, 3)
    assert res.tiers == {"HIGH": 2, "MID": 3}
    jobs = rows(conn)
    assert len(jobs) == 8

    # no spend data: dropped on search data, never fetched
    fra = jobs["2118494694720801027"]
    assert fra["status"] == "dropped" and fra["drop_reason"] == "no spend data"
    assert ("find_jobs", "2118494694720801027") not in [
        (n, a["params"].get("job_id")) for n, a in client.calls
    ]

    # SPEC 3.2 example: 32547.76 / 1918 = 16.97 -> MID
    slack = jobs["2118413555956864697"]
    assert slack["status"] == "candidate" and slack["tier"] == "MID"
    assert slack["rate_paid"] == pytest.approx(16.97, abs=0.01)
    assert slack["connects_cost"] == 11

    # get_job fixture: 8.88/hr -> LOW, dropped with a reason, flags kept
    low = jobs[GET_JOB_ID]
    assert low["status"] == "dropped" and low["tier"] == "LOW"
    assert low["drop_reason"].startswith("tier LOW")
    assert "1-star review" in json.loads(low["red_flags"])

    # E2b: Upwork's default range (8-25) is not the client's rate
    plat = jobs["2118442174097105410"]
    assert plat["status"] == "candidate" and plat["tier"] == "MID"
    assert (plat["budget_min"], plat["budget_max"]) == (8.0, 25.0)
    assert plat["hourly_budget_type"] == "platform default range"
    assert "platform default rate" in json.loads(plat["red_flags"])

    # fixed job with no hours: estimated tier
    fixed = jobs["2118457549206214329"]
    assert fixed["status"] == "candidate" and fixed["tier"] == "MID"
    assert fixed["tier_estimated"] == 1 and fixed["rate_paid"] is None

    assert json.loads(slack["details"])["description"]
    assert conn.execute("SELECT COUNT(*) FROM skills_seen").fetchone()[0] > 0


def test_run_row_and_call_count(conn, settings, config):
    res = daily.run_daily(conn, settings, config, make_client(settings))
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["task"] == "daily" and row["status"] == "ok"
    assert row["ended"] and row["error"] is None


def test_second_run_sees_nothing_new(conn, settings, config, monkeypatch):
    seen_hooks = []
    monkeypatch.setattr(daily, "judge_and_draft", lambda c, cands: seen_hooks.append(len(cands)))
    daily.run_daily(conn, settings, config, make_client(settings))
    res = daily.run_daily(conn, settings, config, make_client(settings))
    assert (res.status, res.seen, res.kept) == ("ok", 0, 0)
    assert seen_hooks == [5]  # hook ran once, with the five kept jobs; not on the empty run
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE task='daily'").fetchone()[0] == 2


def test_hook_does_nothing(conn):
    assert daily.judge_and_draft(conn, [{"id": "1"}]) is None


def test_connects_low_alert_and_clear(conn, settings, config):
    # top kept job = the $43k one (rate 43/hr) which costs 12 Connects; balance is 5
    daily.run_daily(conn, settings, config, make_client(settings, balance=5))
    bal = conn.execute("SELECT * FROM connects ORDER BY id DESC LIMIT 1").fetchone()
    assert (bal["balance"], bal["needed"]) == (5, 12)
    alert = conn.execute("SELECT * FROM alerts WHERE key = 'connects_low'").fetchone()
    assert alert and "5" in alert["message"] and "12" in alert["message"]

    # a later run with enough Connects clears it
    conn.execute("DELETE FROM jobs")
    daily.run_daily(conn, settings, config, make_client(settings, balance=50))
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_note_is_written_and_appended(conn, settings, config):
    daily.run_daily(conn, settings, config, make_client(settings, balance=5))
    daily.run_daily(conn, settings, config, make_client(settings, balance=5))
    files = list((config.data_dir / "notes" / "daily").glob("*.md"))
    assert len(files) == 1 and len(files[0].stem) == 10  # YYYY-MM-DD
    text = files[0].read_text()
    assert text.count("daily run") == 2
    assert "New jobs seen: 8" in text and "New jobs seen: 0" in text
    assert "HIGH 2, MID 3" in text
    assert "Top skills seen:" in text and "CRM Automation (2)" in text
    assert "Connects low" in text  # an oddity
    assert "Connects balance: 5" in text


def test_top_skills_limited_by_setting(conn, settings, config):
    settings.set("daily.top_skills", 1)
    daily.run_daily(conn, settings, config, make_client(settings))
    text = next((config.data_dir / "notes" / "daily").glob("*.md")).read_text()
    line = next(x for x in text.splitlines() if x.startswith("- Top skills seen:"))
    assert line.count("(") == 1


def test_kill_switch(conn, settings, config):
    settings.set("general.kill_switch", True)
    client = make_client(settings)
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "skipped" and client.calls == []
    row = conn.execute("SELECT status, ended FROM runs").fetchone()
    assert row["status"] == "skipped" and row["ended"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_auth_expired(conn, settings, config):
    client = make_client(settings)
    client.search["most_recent"] = UpworkAuthError("Upwork login expired or missing")
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "auth_expired"
    row = conn.execute("SELECT status, error, ended FROM runs").fetchone()
    assert row["status"] == "auth_expired" and "login" in row["error"] and row["ended"]
    assert "auth_expired" in next((config.data_dir / "notes" / "daily").glob("*.md")).read_text()


def test_not_logged_in_is_auth_expired(conn, settings, config, fake_claude_stream):
    # the real bridge with a fake claude that says the Upwork server needs a login
    fake_claude_stream.env("FAKE_SERVER_STATUS", "needs-auth")
    res = daily.run_daily(conn, settings, config)
    assert res.status == "auth_expired"
    assert "log in" in res.error.lower()


def test_no_claude_binary_is_an_error(conn, settings, config, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    res = daily.run_daily(conn, settings, config)
    assert res.status == "error" and "claude" in res.error


def test_other_error(conn, config):
    s = Settings(conn)  # talent_org_uid is empty
    # F0a: the empty setting triggers list_accounts; no TALENT account is a clear error
    empty = {"accounts": [{"name": "Co", "org_uid": "9", "role": "CLIENT"}]}
    res = daily.run_daily(conn, s, config, FakeMCP(s, accounts=empty))
    assert res.status == "error" and "No freelancer account found" in res.error
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "error"


def test_tool_error_on_one_detail_skips_that_job(conn, settings, config):
    client = make_client(settings)
    client.details["2118413555956864697"] = UpworkToolError("job not found")
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "ok" and res.kept == 4
    assert "2118413555956864697" not in rows(conn)  # retried next run
    text = next((config.data_dir / "notes" / "daily").glob("*.md")).read_text()
    assert "tool error on job 2118413555956864697" in text


def test_call_limit_leaves_jobs_for_next_run(conn, settings, config):
    client = make_client(settings)
    names = list(client.details)
    client.details["2118442174097105410"] = CallBudgetExceeded("limit")
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "ok"
    saved = rows(conn)
    assert "2118442174097105410" not in saved  # and everything after it
    assert "2118387252193629626" not in saved
    assert len(names) == 7
    assert "MCP call limit reached" in next(
        (config.data_dir / "notes" / "daily").glob("*.md")
    ).read_text()


def test_candidates_passed_to_hook(conn, settings, config, monkeypatch):
    got = []
    monkeypatch.setattr(daily, "judge_and_draft", lambda c, cands: got.extend(cands))
    daily.run_daily(conn, settings, config, make_client(settings))
    assert [c["tier"] for c in got] == ["HIGH", "HIGH", "MID", "MID", "MID"]
    assert got[0]["id"] == "2118527230669974661"
    assert all(c["status"] == "candidate" and "flags" in c for c in got)


def test_fixtures_not_changed_by_run(conn, settings, config):
    before = copy.deepcopy(load("smart_search_best_match.json"))
    daily.run_daily(conn, settings, config, make_client(settings))
    assert load("smart_search_best_match.json") == before
