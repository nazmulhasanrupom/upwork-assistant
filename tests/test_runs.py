"""Runs and costs page /runs and the dashboard cost card (plan G4). No network."""

import os
import re
from datetime import UTC, datetime, timedelta

import pytest

from app import costs
from app.pages import zone
from app.settings import Settings
from app.tasks import daily
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin
from tests.test_daily import make_client as make_daily_client


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def add_run(conn, run_id, task="daily", started=None, status="ok", calls=0, error=None):
    started = started or datetime.now(UTC)
    conn.execute(
        "INSERT INTO runs(run_id, task, started, ended, status, mcp_calls, error) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, task, iso(started), iso(started + timedelta(seconds=75)), status, calls, error),
    )


def add_cost(conn, cost, when=None, run_id=None, task="judge"):
    conn.execute(
        "INSERT INTO claude_calls(run_id, model, input_tokens, output_tokens, cost_usd, "
        "created_at, task) VALUES (?,?,?,?,?,?,?)",
        (run_id, "m", 1, 1, cost, iso(when or datetime.now(UTC)), task),
    )


def stat_value(html, stat_id):
    return re.search(rf'id="{stat_id}".*?<strong>([^<]*)</strong>', html, re.S).group(1)


# ---------- access ----------


@pytest.mark.parametrize("path", ["/runs", "/runs/log/daily-20261106T010000.log"])
def test_needs_login(config, path):
    _, client = make_web_client(config)
    setup_admin(client)
    client.cookies.clear()
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_empty_page(web):
    _, client = web
    r = client.get("/runs")
    assert r.status_code == 200 and "No runs yet" in r.text
    assert stat_value(r.text, "cost-today") == "$0.00" and "0% used" in r.text
    assert 'aria-current="page">Runs' in r.text


# ---------- cost totals ----------


def test_cost_summary_periods(web, config):
    app, _ = web
    conn, settings = app.state.db, app.state.settings
    tz = zone("Asia/Dhaka")  # UTC+6
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # a Wednesday
    add_cost(conn, 1.00, now - timedelta(hours=1))  # today
    add_cost(conn, 2.00, datetime(2026, 11, 11, 17, 59, tzinfo=UTC) - timedelta(hours=7))  # today
    add_cost(conn, 4.00, datetime(2026, 11, 9, 1, 0, tzinfo=UTC))  # Monday: this week
    add_cost(conn, 8.00, datetime(2026, 11, 8, 12, 0, tzinfo=UTC))  # Sunday: last week, this month
    add_cost(conn, 16.00, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))  # last month
    c = costs.cost_summary(conn, config, settings, tz, now)
    assert c["today"] == pytest.approx(3.00)
    assert c["week"] == pytest.approx(7.00)
    assert c["month"] == pytest.approx(15.00)
    assert c["cap"] == 50.0 and c["percent"] == 30 and not c["warn"] and not c["reached"]


def test_week_starts_on_monday_in_the_user_zone():
    tz = zone("Asia/Dhaka")
    # Sunday 20:00 UTC is already Monday 02:00 in Dhaka, so a new week.
    start = costs.week_start(datetime(2026, 11, 8, 20, 0, tzinfo=UTC), tz)
    assert start.date().isoformat() == "2026-11-09"


def test_cap_bar_warning_and_reached(web, config):
    app, client = web
    conn, settings = app.state.db, app.state.settings
    add_cost(conn, 41.0)
    r = client.get("/runs")
    assert "82% used" in r.text and 'class="meter warn"' in r.text
    assert "82% of the monthly cap" in r.text
    add_cost(conn, 10.0)
    r = client.get("/runs")
    assert 'class="meter over"' in r.text and "Monthly cap reached" in r.text
    assert "100% used" in r.text
    settings.set("claude.monthly_cap_usd", 500)
    r = client.get("/runs")
    assert 'class="meter"' in r.text and "Monthly cap" in r.text and "10% used" in r.text


def test_cap_alert_helper():
    base = {"month": 1.0, "cap": 50.0, "percent": 2, "warn": False, "reached": False}
    assert costs.cap_alert(base) is None
    assert costs.cap_alert({**base, "warn": True})["kind"] == "warn"
    assert costs.cap_alert({**base, "reached": True})["kind"] == "error"


# ---------- table ----------


def test_table_rows_cost_and_error(web):
    app, client = web
    conn = app.state.db
    add_run(conn, "daily-a", calls=7)
    add_cost(conn, 0.25, run_id="daily-a")
    add_cost(conn, 0.0125, run_id="daily-a", task="fetch")
    add_run(
        conn,
        "daily-b",
        status="error",
        error="ClaudeFailed: boom",
        started=datetime.now(UTC) - timedelta(hours=2),
    )
    r = client.get("/runs")
    assert r.status_code == 200
    assert "$0.2625" in r.text and "$0.0000" in r.text
    assert "ClaudeFailed: boom" in r.text
    assert 'class="status error"' in r.text and "1m 15s" in r.text
    row = re.search(r"<tr>\s*<th scope=\"row\">daily</th>.*?</tr>", r.text, re.S).group(0)
    assert '<td class="num">7</td>' in row


def test_error_text_is_escaped(web):
    app, client = web
    add_run(app.state.db, "daily-x", status="error", error="<script>alert(1)</script>")
    r = client.get("/runs")
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_filter_by_task_and_paging(web):
    app, client = web
    conn, settings = app.state.db, app.state.settings
    settings.set("ui.runs_page_size", 2)
    base = datetime.now(UTC) - timedelta(days=1)
    for i in range(5):
        add_run(conn, f"daily-{i}", started=base + timedelta(minutes=i))
    add_run(conn, "digest-1", task="digest", started=base)
    r = client.get("/runs")
    assert "6 run(s). Page 1 of 3." in r.text and 'href="/runs?page=2"' in r.text
    assert "Newer" not in r.text
    r = client.get("/runs?page=3")
    assert "Page 3 of 3" in r.text and "Older" not in r.text and 'href="/runs?page=2"' in r.text
    r = client.get("/runs?page=99")  # clamped
    assert "Page 3 of 3" in r.text
    r = client.get("/runs?task=digest")
    assert "1 run(s)." in r.text and "digest" in r.text and "daily-" not in r.text
    r = client.get("/runs?task=daily&page=2")
    assert "5 run(s). Page 2 of 3." in r.text and 'href="/runs?task=daily&amp;page=3"' in r.text
    r = client.get("/runs?task=evil")  # not a known task: ignored
    assert "6 run(s)." in r.text
    assert '<option value="weekly">' in r.text  # known task names are always offered


# ---------- log files ----------


def make_log(config, name, text="hello log\n"):
    d = config.data_dir / "logs"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text)
    return d / name


def test_run_row_links_to_its_log(web, config):
    app, client = web
    started = datetime(2026, 11, 6, 8, 0, 5, tzinfo=UTC)
    add_run(app.state.db, "daily-1", started=started)
    add_run(app.state.db, "digest-1", task="digest", started=started)
    make_log(config, "daily-20261106T080001.log", "line one <b>x</b>\n")
    make_log(config, "daily-20261106T060000.log")  # an older run's log
    r = client.get("/runs")
    assert 'href="/runs/log/daily-20261106T080001.log"' in r.text
    assert "daily-20261106T060000" not in r.text
    assert r.text.count("View log") == 1  # the digest run has no log
    page = client.get("/runs/log/daily-20261106T080001.log")
    assert page.status_code == 200 and "line one &lt;b&gt;x&lt;/b&gt;" in page.text
    assert "<b>x</b>" not in page.text


def test_log_too_old_for_the_run_is_not_linked(web, config):
    app, client = web
    add_run(app.state.db, "daily-1", started=datetime(2026, 11, 6, 8, 0, 5, tzinfo=UTC))
    make_log(config, "daily-20261106T075000.log")  # 10 min 5 s before: outside the window
    assert "View log" not in client.get("/runs").text
    app.state.settings.set("ui.log_match_minutes", 30)
    assert "View log" in client.get("/runs").text


@pytest.mark.parametrize(
    "name",
    [
        "..%2Fapp.db",
        "%2e%2e%2fapp.db",
        "daily-20261106T080001.log%00",
        "daily-20261106T080001.txt",
        "secrets.log",
        "..%2F..%2Fetc%2Fpasswd",
        "daily-20261106T080001.log%2F..%2F..%2Fapp.db",
    ],
)
def test_log_path_tricks_give_404(web, config, name):
    _, client = web
    make_log(config, "daily-20261106T080001.log")
    (config.data_dir / "app.db").write_text("db")
    assert client.get(f"/runs/log/{name}").status_code == 404


def test_log_symlink_and_missing_are_404(web, config):
    _, client = web
    secret = config.data_dir / "secret.txt"
    secret.write_text("TOP SECRET")
    logs = config.data_dir / "logs"
    logs.mkdir()
    os.symlink(secret, logs / "daily-20261106T090000.log")
    r = client.get("/runs/log/daily-20261106T090000.log")
    assert r.status_code == 404 and "TOP SECRET" not in r.text
    assert client.get("/runs/log/daily-20261106T090001.log").status_code == 404
    assert client.get("/runs/log/").status_code == 404


def test_big_log_shows_only_the_tail(web, config):
    app, client = web
    app.state.settings.set("ui.log_view_max_kb", 1)
    make_log(config, "daily-20261106T080001.log", "OLDSTART\n" + "x" * 5000 + "\nNEWEND\n")
    r = client.get("/runs/log/daily-20261106T080001.log")
    assert "NEWEND" in r.text and "OLDSTART" not in r.text and "Only the last 1 KB" in r.text


# ---------- dashboard card ----------


def test_dashboard_cost_card(web):
    app, client = web
    add_cost(app.state.db, 1.5)
    r = client.get("/")
    assert stat_value(r.text, "cost") == "$1.50"
    assert "month $1.50 of $50 cap (3%)" in r.text and "stat warn" not in r.text
    assert 'href="/runs"' in r.text


def test_dashboard_cost_warning_and_alert(web):
    app, client = web
    add_cost(app.state.db, 45.0)
    r = client.get("/")
    assert 'class="stat warn" id="cost"' in r.text
    assert "of the $50.00 monthly cap (90%)" in r.text
    add_cost(app.state.db, 10.0)
    assert "Monthly Claude cap reached" in client.get("/").text


# ---------- judge/write calls belong to the daily run ----------


@pytest.mark.judging
def test_daily_claude_calls_are_counted_in_the_run(web, config, fake_claude_gen):
    app, client = web
    conn, settings = app.state.db, app.state.settings
    settings.set("upwork.talent_org_uid", "1000000000000000001")
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("FACT")
    res = daily.run_daily(conn, settings, config, make_daily_client(settings, balance=50))
    assert res.judged > 0
    rows = conn.execute(
        "SELECT run_id, task FROM claude_calls WHERE task IN ('judge','write')"
    ).fetchall()
    assert rows and all(r["run_id"] == res.run_id for r in rows)
    cost = conn.execute(
        "SELECT SUM(cost_usd) FROM claude_calls WHERE run_id = ?", (res.run_id,)
    ).fetchone()[0]
    assert cost > 0
    assert f"${cost:.4f}" in client.get("/runs").text
    assert Settings(conn).get("claude.monthly_cap_usd") == 50.0
