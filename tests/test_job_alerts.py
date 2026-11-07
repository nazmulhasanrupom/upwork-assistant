"""Instant job alerts task (app/tasks/job_alerts.py) with a fake MCP client and a fake judge.
No network, no Claude."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from app import mail
from app.db import init_db
from app.llm.judging import JudgeResult
from app.settings import Settings
from app.tasks import daily, job_alerts
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.test_daily import ORG, detail_for
from tests.upwork_fakes import FakeMCP, load

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)
HIGH_SPEND = (180499.82, 8000)  # 22.5 / hr
MID_SPEND = (32547.76, 1918)  # 16.97 / hr
LOW_SPEND = (13692.95, 1542)  # 8.88 / hr


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    s.set("job_alerts.enabled", True)
    return s


def search_job(jid, minutes_old=10):
    base = next(
        j for j in load("smart_search_most_recent.json")["jobs"] if j["job_type"] == "hourly"
    )
    job = copy.deepcopy(base)
    job["id"] = jid
    job["title"] = f"Job {jid}"
    job["url"] = f"https://www.upwork.com/jobs/~02{jid}"
    stamp = (NOW - timedelta(minutes=minutes_old)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    job["created_date"] = job["published_date"] = stamp
    return job


def make_client(settings, spec):
    """spec: {job id: (spend, hours) or Exception}, listed newest first."""
    jobs = [search_job(jid, 10 + i) for i, jid in enumerate(spec)]
    details = {
        jid: v if isinstance(v, Exception) else detail_for(jid, v[0], v[1])
        for jid, v in spec.items()
    }
    return FakeMCP(
        settings, search={"most_recent": {"status": "ok", "jobs": jobs}}, details=details
    )


@pytest.fixture
def judge(monkeypatch):
    """Fake judge: fit score per job id (default `default_fit`); tier comes from the saved row."""
    state = {"fit": {}, "default_fit": 90, "seen": []}

    def fake(conn, candidates, **kw):
        state["seen"] = [c["id"] for c in candidates]
        items = [
            {
                "job_id": c["id"],
                "tier": c["tier"],
                "fit_score": state["fit"].get(c["id"], state["default_fit"]),
                "why_apply": ["good match"],
            }
            for c in candidates
        ]
        return JudgeResult(judged=items, drafted=len(items))

    monkeypatch.setattr(daily, "judge_and_draft", fake)
    return state


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        mail, "notify_job_alert", lambda conn, config, job, judgement=None, **kw: sent.append(job)
    )
    return sent


def run(conn, settings, config, client, now=NOW):
    return job_alerts.run_job_alerts(conn, settings, config, client, now=now)


def statuses(conn):
    return {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM jobs")}


# ---------------------------------------------------------------- gates


def gated(conn, settings, config, reason_part):
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    client = make_client(settings, {"1": HIGH_SPEND})
    res = run(conn, settings, config, client)
    assert res.status == "skipped" and reason_part in res.error
    assert client.calls == []  # no Upwork call
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before  # no new run row
    return res


def test_gate_disabled(conn, settings, config):
    settings.set("job_alerts.enabled", False)
    gated(conn, settings, config, "off")


def test_gate_kill_switch(conn, settings, config):
    settings.set("general.kill_switch", True)
    gated(conn, settings, config, "kill switch")


def test_gate_quiet_hours(conn, settings, config):
    settings.set("job_alerts.quiet_hours", "22:00-07:00")  # NOW is 03:00 UTC
    gated(conn, settings, config, "quiet hours")


def test_quiet_hours_use_user_time_zone(conn, settings, config):
    settings.set("general.timezone", "Asia/Dhaka")  # 09:00 local: outside the window
    settings.set("job_alerts.quiet_hours", "22:00-07:00")
    client = make_client(settings, {"1": HIGH_SPEND})
    assert run(conn, settings, config, client).status == "ok"


def test_in_quiet_hours_windows():
    zone = UTC
    at = lambda h, m=0: datetime(2026, 11, 6, h, m, tzinfo=UTC)  # noqa: E731
    assert job_alerts.in_quiet_hours("22:00-07:00", at(23), zone)
    assert job_alerts.in_quiet_hours("22:00-07:00", at(6, 59), zone)
    assert not job_alerts.in_quiet_hours("22:00-07:00", at(7), zone)
    assert job_alerts.in_quiet_hours("09:00-17:00", at(9), zone)
    assert not job_alerts.in_quiet_hours("09:00-17:00", at(17), zone)
    assert not job_alerts.in_quiet_hours("", at(3), zone)
    assert not job_alerts.in_quiet_hours("nonsense", at(3), zone)


def test_gate_daily_call_limit(conn, settings, config):
    settings.set("job_alerts.max_calls_per_day", 10)
    for n, started in ((6, NOW - timedelta(hours=1)), (4, NOW - timedelta(minutes=30))):
        conn.execute(
            "INSERT INTO runs(run_id, task, started, status, mcp_calls) "
            "VALUES (?, 'job_alerts', ?, 'ok', ?)",
            (f"r{n}", started.isoformat(), n),
        )
    gated(conn, settings, config, "daily call limit")


def test_daily_call_limit_counts_only_today_and_this_task(conn, settings, config):
    settings.set("job_alerts.max_calls_per_day", 10)
    rows = (
        ("old", "job_alerts", NOW - timedelta(days=1), 50),  # yesterday
        ("other", "daily", NOW - timedelta(hours=1), 50),  # other task
        ("mine", "job_alerts", NOW - timedelta(hours=2), 9),
    )
    for run_id, task, started, n in rows:
        conn.execute(
            "INSERT INTO runs(run_id, task, started, status, mcp_calls) VALUES (?,?,?,'ok',?)",
            (run_id, task, started.isoformat(), n),
        )
    assert job_alerts.calls_today(conn, config, settings, NOW) == 9


def test_gate_backoff_counts_down(conn, settings, config):
    settings.set("job_alerts.backoff_left", 2)
    gated(conn, settings, config, "backing off")
    assert settings.get("job_alerts.backoff_left") == 1
    gated(conn, settings, config, "backing off")
    assert settings.get("job_alerts.backoff_left") == 0
    client = make_client(settings, {"1": HIGH_SPEND})
    assert run(conn, settings, config, client).status == "ok"


# ---------------------------------------------------------------- the flow


def test_new_jobs_saved_and_runs_row(conn, settings, config, judge, emails):
    client = make_client(settings, {"1": HIGH_SPEND, "2": MID_SPEND, "3": LOW_SPEND})
    res = run(conn, settings, config, client)
    assert res.status == "ok" and (res.seen, res.kept, res.dropped) == (3, 2, 1)
    assert statuses(conn) == {"1": "candidate", "2": "candidate", "3": "dropped"}
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["task"] == "job_alerts" and row["status"] == "ok" and row["ended"]
    assert row["mcp_calls"] == 4 == res.calls  # 1 search + 3 details
    assert row["run_id"].startswith("job_alerts-")
    assert set(judge["seen"]) == {"1", "2"}


def test_dedup_known_jobs_are_not_fetched_again(conn, settings, config, judge, emails):
    client = make_client(settings, {"1": HIGH_SPEND})
    run(conn, settings, config, client)
    again = make_client(settings, {"1": HIGH_SPEND})
    res = run(conn, settings, config, again)
    assert res.seen == 0
    assert [c[1]["action"] for c in again.calls] == ["smart_search"]  # no detail read
    assert len(emails) == 1  # no second email


def test_high_job_with_fit_75_is_emailed(conn, settings, config, judge, emails):
    judge["fit"] = {"1": 75}
    res = run(conn, settings, config, make_client(settings, {"1": HIGH_SPEND}))
    assert res.emailed == 1 and [j["id"] for j in emails] == ["1"]
    job = emails[0]
    assert job["title"] and job["posted_at"] and "proposals_tier" in job


def test_high_job_with_fit_74_is_not_emailed(conn, settings, config, judge, emails):
    judge["fit"] = {"1": 74}
    res = run(conn, settings, config, make_client(settings, {"1": HIGH_SPEND}))
    assert res.emailed == 0 and emails == []
    assert statuses(conn) == {"1": "candidate"}  # still saved


def test_mid_job_is_saved_and_judged_but_not_emailed(conn, settings, config, judge, emails):
    judge["default_fit"] = 99
    res = run(conn, settings, config, make_client(settings, {"2": MID_SPEND}))
    assert judge["seen"] == ["2"] and res.emailed == 0 and emails == []


def test_email_tiers_setting_is_used(conn, settings, config, judge, emails):
    settings.set("job_alerts.email_tiers", ["HIGH", "MID"])
    run(conn, settings, config, make_client(settings, {"2": MID_SPEND}))
    assert [j["id"] for j in emails] == ["2"]


def test_fit_min_setting_is_used(conn, settings, config, judge, emails):
    settings.set("job_alerts.fit_min", 95)
    judge["default_fit"] = 90
    run(conn, settings, config, make_client(settings, {"1": HIGH_SPEND}))
    assert emails == []


def test_no_spend_data_dropped_without_detail_call(conn, settings, config, judge, emails):
    client = make_client(settings, {"1": HIGH_SPEND})
    nospend = search_job("9")
    nospend["client"] = {"country": "FRA", "verification_status": "VERIFIED"}
    client.search["most_recent"]["jobs"].append(nospend)
    res = run(conn, settings, config, client)
    assert statuses(conn)["9"] == "dropped" and res.dropped == 1
    assert "9" not in [c[1]["params"].get("job_id") for c in client.calls]


# ---------------------------------------------------------------- limits and errors


def test_call_cap_limits_detail_reads_newest_first(conn, settings, config, judge, emails):
    settings.set("job_alerts.max_details_per_run", 2)
    spec = {str(n): HIGH_SPEND for n in range(1, 6)}  # "1" is the newest
    client = make_client(settings, spec)
    res = run(conn, settings, config, client)
    assert res.calls == len(client.calls) == 3  # 1 + max_details_per_run
    assert set(statuses(conn)) == {"1", "2"}  # the rest wait for the next run
    row = conn.execute("SELECT mcp_calls FROM runs").fetchone()
    assert row["mcp_calls"] == 3


def test_capped_client_refuses_extra_calls(settings):
    inner = make_client(settings, {"1": HIGH_SPEND})
    capped = job_alerts._CappedClient(inner, 1)
    call = ("find_jobs", {"action": "get", "params": {"job_id": "1"}, "org_uid": ORG})
    first, second = capped.call_many([call, call])
    assert not first.is_error and second.is_error and second.error_kind == "budget"
    assert len(inner.calls) == 1


def test_rate_limited_search_sets_backoff(conn, settings, config, judge, emails):
    client = FakeMCP(settings, search={"most_recent": UpworkToolError("429 Too Many Requests")})
    res = run(conn, settings, config, client)
    assert res.status == "rate_limited"
    assert settings.get("job_alerts.backoff_left") == settings.get("job_alerts.backoff_runs")
    row = conn.execute("SELECT status, ended FROM runs").fetchone()
    assert row["status"] == "rate_limited" and row["ended"]
    assert run(conn, settings, config, client).status == "skipped"  # backing off


def test_rate_limited_detail_sets_backoff_and_keeps_earlier_jobs(
    conn, settings, config, judge, emails
):
    spec = {"1": HIGH_SPEND, "2": UpworkToolError("Too many requests"), "3": HIGH_SPEND}
    client = make_client(settings, spec)
    res = run(conn, settings, config, client)
    assert res.status == "rate_limited"
    assert statuses(conn) == {"1": "candidate"}  # "3" never fetched
    assert settings.get("job_alerts.backoff_left") == settings.get("job_alerts.backoff_runs")


def test_cooldown_stops_detail_calls_but_run_is_ok(conn, settings, config, judge, emails):
    spec = {"1": HIGH_SPEND, "2": UpworkToolError("JOB_BROWSE_COOLDOWN"), "3": HIGH_SPEND}
    client = make_client(settings, spec)
    res = run(conn, settings, config, client)
    assert res.status == "ok"
    assert statuses(conn) == {"1": "candidate"}
    assert settings.get("job_alerts.backoff_left") == 0
    assert len([c for c in client.calls if c[1]["action"] == "get"]) == 2


def test_cooldown_on_search_ends_run_without_details(conn, settings, config):
    client = FakeMCP(settings, search={"most_recent": UpworkToolError("job_browse_cooldown")})
    res = run(conn, settings, config, client)
    assert res.status == "ok" and len(client.calls) == 1


def test_auth_error_sets_status_and_does_not_crash(conn, settings, config, monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "notify_auth_expired", lambda *a, **k: sent.append(a))
    client = FakeMCP(settings, search={"most_recent": UpworkAuthError("login expired")})
    res = run(conn, settings, config, client)
    assert res.status == "auth_expired" and len(sent) == 1
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "auth_expired"


def test_other_search_error_is_an_error_run(conn, settings, config):
    client = FakeMCP(settings, search={"most_recent": UpworkToolError("boom")})
    res = run(conn, settings, config, client)
    assert res.status == "error" and settings.get("job_alerts.backoff_left") == 0


def test_claude_limit_is_a_skipped_run(conn, settings, config, monkeypatch):
    from app.llm.runner import ClaudeRateLimited

    def boom(*a, **k):
        raise ClaudeRateLimited("limit")

    monkeypatch.setattr(daily, "judge_and_draft", boom)
    res = run(conn, settings, config, make_client(settings, {"1": HIGH_SPEND}))
    assert res.status == "skipped" and "Claude limit" in res.error
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "skipped"
