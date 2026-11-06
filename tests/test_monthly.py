"""I2: monthly task. Python numbers, optional READ-only financials, Opus report, saved and emailed.
Fake claude and fake bridge only."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app import mail, reports
from app import profile_versions as pv
from app.db import init_db
from app.settings import Settings
from app.tasks import lock, monthly, scheduler, trigger
from tests.test_profile_versions import payload
from tests.test_weekly import ProfileBridge, add_job
from tests.upwork_fakes import to_result

NOW = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
EM = "—"


class FinBridge:
    def __init__(self, data=None, fail=False):
        self.data, self.fail, self.calls, self.closed = data, fail, [], 0

    def close(self):
        self.closed += 1

    def call_many(self, calls):
        self.calls.extend(calls)
        if self.fail:
            raise RuntimeError("boom")
        return [to_result(t, self.data or {"earnings": 1234}) for t, _ in calls]


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", "1000000000000000001")
    s.set("monthly.email", False)
    return s


@pytest.fixture
def data(conn, settings, config):
    pv.snapshot(ProfileBridge(), conn, settings, config, NOW - timedelta(days=3))
    for i in range(4):
        add_job(conn, f"j{i}", date(2026, 10, 5 + i), ["n8n"], budget=100 * (i + 1))
    for i, status in enumerate(["Accepted", "Accepted", "Hired", "Accepted"]):
        conn.execute(
            "INSERT INTO proposals(proposal_id, created_at, status, viewed_by_client, connects_bid)"
            " VALUES (?,?,?,?,?)",
            (f"p{i}", f"2026-10-1{i}T09:00:00+00:00", status, 1, 8),
        )
    conn.execute(
        "INSERT INTO reports(kind, period, created_at, title, body) "
        "VALUES ('weekly','2026-W42','2026-10-19T06:00:00+00:00','Weekly report 2026-W42','x')"
    )


def run(conn, settings, config, bridge=None):
    return monthly.run_monthly(conn, settings, config, bridge, now=NOW)


def test_month_range():
    assert monthly.month_range(date(2026, 11, 1)) == (date(2026, 10, 1), date(2026, 11, 1))
    assert monthly.month_range(date(2026, 1, 15)) == (date(2025, 12, 1), date(2026, 1, 1))


def test_stats_are_python_counts(conn, settings, data):
    st = monthly.build_stats(conn, settings, date(2026, 10, 1), date(2026, 11, 1))
    assert st["month"] == "2026-10" and st["jobs_seen"] == 4 and st["median_fixed"] == 250
    assert st["proposals"]["sent"] == 4 and st["connects_spent"] == 32
    hires = st["proposals"]["hires"]
    assert st["win_rate_percent"] == (round(100 * hires / 4, 1))
    assert st["skills"][0]["skill"] == "n8n"
    assert (
        monthly.build_stats(conn, settings, date(2020, 1, 1), date(2020, 2, 1))["win_rate_percent"]
        is None
    )


def test_run_saves_report_with_think_model_and_settings(
    conn, settings, config, data, fake_claude_gen
):
    settings.set("monthly.max_turns", 9)
    settings.set("monthly.max_budget_usd", 1.23)
    res = run(conn, settings, config)
    assert res.status == "ok", res.error
    row = reports.get(conn, res.report_id)
    assert (
        row["kind"] == "monthly" and row["period"] == "2026-10" and "Next month plan" in row["body"]
    )
    log = fake_claude_gen.log()
    argv = log["argv"]
    assert log["model"] == settings.get("models.think")
    assert (
        argv[argv.index("--max-turns") + 1] == "9"
        and argv[argv.index("--max-budget-usd") + 1] == "1.23"
    )
    inp = log["input"]
    assert inp["stats"].startswith("<untrusted_job_data>") and inp["month"] == "2026-10"
    assert inp["financials"] is None and res.financials == "off"
    assert "Weekly report 2026-W42" in inp["weekly_reports"]
    assert "Task: monthly plan" in log["prompt"]
    assert conn.execute("SELECT task FROM claude_calls").fetchone()[0] == "monthly"
    assert (
        conn.execute("SELECT status FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()[0]
        == "ok"
    )
    # it proposes no profile write
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_financials_are_read_through_the_read_bridge_and_marked_untrusted(
    conn, settings, config, data, fake_claude_gen
):
    settings.set("monthly.financials", True)
    bridge = FinBridge({"earnings": 1234})
    res = run(conn, settings, config, bridge)
    assert res.status == "ok" and res.financials == "read"
    assert [c[0] for c in bridge.calls] == ["get_freelancer_financials"]
    fin = fake_claude_gen.log()["input"]["financials"]
    assert fin.startswith("<untrusted_upwork_data>") and "1234" in fin
    assert reports.stats_of(reports.get(conn, res.report_id))["financials"] == "read"


def test_financials_that_fail_do_not_stop_the_report(conn, settings, config, data, fake_claude_gen):
    settings.set("monthly.financials", True)
    res = run(conn, settings, config, FinBridge(fail=True))
    assert res.status == "ok" and res.financials.startswith("not read")
    assert fake_claude_gen.log()["input"]["financials"] is None


def test_financials_are_cut(conn, settings, config, data, fake_claude_gen):
    settings.set("monthly.financials", True)
    settings.set("monthly.financials_max_chars", 50)
    run(conn, settings, config, FinBridge({"x": "y" * 500}))
    assert "y" * 100 not in fake_claude_gen.log()["input"]["financials"]


def test_em_dash_cleaned_bad_answer_and_switches(conn, settings, config, data, fake_claude_gen):
    fake_claude_gen.env("FAKE_M_EM", 1)
    assert EM not in reports.get(conn, run(conn, settings, config).report_id)["body"]
    fake_claude_gen.env("FAKE_M_BAD", 1)
    res = run(conn, settings, config)
    assert res.status == "error" and "no report" in res.error
    settings.set("monthly.enabled", False)
    assert run(conn, settings, config).status == "skipped"
    settings.set("monthly.enabled", True)
    settings.set("general.kill_switch", True)
    assert run(conn, settings, config).status == "skipped"


def test_report_is_emailed(conn, settings, config, data, fake_claude_gen, monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send_event", lambda *a, **k: sent.append(a) or "sent")
    settings.set("monthly.email", True)
    run(conn, settings, config)
    assert sent and sent[0][1] == "monthly" and sent[0][2] == "2026-10"


def test_scheduler_starts_monthly_on_the_first(conn, settings, config, monkeypatch):
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    scheduler.tick(settings, config, now=datetime(2026, 11, 1, 7, 2, tzinfo=UTC))
    assert started.count("monthly") == 1 and "weekly" not in started


def test_main_returns_zero_when_busy(config, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    with lock.task_lock(config.data_dir, "monthly"):
        assert monthly.main() == 0


def test_payload_helper_used():
    assert payload()


def test_task_main_runs_end_to_end(config, monkeypatch, fake_claude_gen):
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    assert monthly.main() == 0
    conn = init_db(config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM reports WHERE kind = 'monthly'").fetchone()[0] == 1
