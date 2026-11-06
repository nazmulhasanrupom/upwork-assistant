"""Playbook and experiments (plan O2). No Claude, no Upwork."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from app import playbook
from app.db import init_db
from app.settings import Settings
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("keywords.baseline_started", (TODAY - timedelta(days=14)).isoformat())
    s.set("rank.keywords", ["n8n automation", "Zapier"])
    return s


def add_metrics(conn, day, **cols):
    cols = {"date": day.isoformat(), "created_at": NOW.isoformat(), **cols}
    names = list(cols)
    conn.execute(
        f"INSERT INTO metrics_daily({','.join(names)}) VALUES ({','.join('?' * len(names))})",
        list(cols.values()),
    )


def add_rank(conn, day, keyword, position):
    conn.execute(
        "INSERT INTO rank_daily(date, keyword, position, organic_position, pages_checked, "
        "total_seen, created_at) VALUES (?,?,?,?,?,?,?)",
        (day.isoformat(), keyword, position, position, 2, 40, NOW.isoformat()),
    )


# ---------------------------------------------------------------- seed


def test_seed_file_has_the_spec_table_and_nothing_personal():
    text = playbook.SEED_PATH.read_text(encoding="utf-8")
    assert "Tactic playbook" in text and "## Results log" in text
    assert "@" not in text


def test_ensure_seeded_writes_once_and_keeps_edits(config):
    assert playbook.ensure_seeded(config) is True
    target = config.memory_dir / "PLAYBOOK.md"
    assert target.read_text(encoding="utf-8") == playbook.SEED_PATH.read_text(encoding="utf-8")
    target.write_text("my edit\n", encoding="utf-8")
    assert playbook.ensure_seeded(config) is False
    assert target.read_text(encoding="utf-8") == "my edit\n"


def test_read_seeds_when_missing(config):
    assert "Tactic playbook" in playbook.read(config)
    assert (config.memory_dir / "PLAYBOOK.md").exists()


def test_tactics_parsed_with_escaped_pipes(config):
    rows = playbook.tactics(config)
    assert [r["no"] for r in rows] == list(range(1, 12))
    two = rows[1]
    assert two["tactic"] == "Title formula: Primary Skill | Secondary Skill | Niche or Tool"
    assert two["type"] == "Ask first" and two["status"] == "untested"
    assert rows[7]["status"] == "always on"


# ---------------------------------------------------------------- can_start_test


def test_can_start_when_all_clear(conn, settings):
    assert playbook.can_start_test(conn, settings, NOW) is None


def test_blocked_without_baseline(conn, settings):
    settings.set("keywords.baseline_started", "")
    assert "approve your keywords" in playbook.can_start_test(conn, settings, NOW)


def test_blocked_while_baseline_runs(conn, settings):
    settings.set("keywords.baseline_started", (TODAY - timedelta(days=10)).isoformat())
    reason = playbook.can_start_test(conn, settings, NOW)
    assert "baseline" in reason and "4 more" in reason


def test_baseline_days_come_from_settings(conn, settings):
    settings.set("keywords.baseline_started", (TODAY - timedelta(days=10)).isoformat())
    settings.set("keywords.baseline_days", 10)
    assert playbook.can_start_test(conn, settings, NOW) is None


def test_blocked_by_jss_guard(conn, settings):
    settings.set("safety.jss_guard", True)
    assert "JSS guard" in playbook.can_start_test(conn, settings, NOW)


def test_blocked_while_a_test_is_active(conn, settings, config):
    playbook.start_experiment(conn, settings, config, "Title", 2, NOW)
    reason = playbook.can_start_test(conn, settings, NOW)
    assert "already running" in reason


# ---------------------------------------------------------------- start / finish


def test_start_saves_baseline_dates_and_marks_the_tactic(conn, settings, config):
    for i in range(7):
        day = TODAY - timedelta(days=i)
        add_metrics(conn, day, jss=90 + i, invites_new=i % 2, proposals_sent=2)
        add_rank(conn, day, "n8n automation", 10 + i)
    exp_id = playbook.start_experiment(conn, settings, config, "Use all 20 skill slots", 3, NOW)
    row = playbook.active_experiment(conn)
    assert row["id"] == exp_id and row["status"] == "active"
    assert row["started"] == TODAY.isoformat()
    assert row["ends"] == (TODAY + timedelta(days=14)).isoformat()
    base = json.loads(row["baseline_json"])
    assert base["date"] == TODAY.isoformat()
    assert base["metrics"]["jss"] == 93.0 and base["metrics"]["proposals_sent"] == 2.0
    assert base["metrics"]["hires"] is None
    assert base["rank"] == {"n8n automation": 13.0, "Zapier": None}
    assert playbook.tactics(config)[2]["status"] == "testing"
    assert "| 3 | Use all 20 skill slots" in playbook.read(config)


def test_test_days_come_from_settings(conn, settings, config):
    settings.set("playbook.test_days", 7)
    playbook.start_experiment(conn, settings, config, "x", None, NOW)
    assert playbook.active_experiment(conn)["ends"] == (TODAY + timedelta(days=7)).isoformat()


def test_start_refused_with_reason(conn, settings, config):
    settings.set("safety.jss_guard", True)
    with pytest.raises(playbook.PlaybookError, match="JSS guard"):
        playbook.start_experiment(conn, settings, config, "Title", 2, NOW)
    assert playbook.active_experiment(conn) is None


def test_empty_tactic_refused(conn, settings, config):
    with pytest.raises(playbook.PlaybookError):
        playbook.start_experiment(conn, settings, config, "  ", None, NOW)


def test_database_allows_only_one_active(conn, settings, config):
    playbook.start_experiment(conn, settings, config, "a", None, NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO experiments(tactic, status, started, ends) VALUES ('b','active','x','y')"
        )
    # finished ones are not limited
    conn.execute(
        "INSERT INTO experiments(tactic, status, started, ends) VALUES ('c','finished','x','y')"
    )
    conn.execute(
        "INSERT INTO experiments(tactic, status, started, ends) VALUES ('d','finished','x','y')"
    )


def test_finish_updates_row_status_and_log(conn, settings, config):
    playbook.start_experiment(conn, settings, config, "Use all 20 skill slots", 3, NOW)
    later = NOW + timedelta(days=14)
    exp_id = playbook.finish_experiment(
        conn, settings, config, "worked", "Invites went  from 1 to\n3 a week.", later
    )
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    assert row["status"] == "finished" and row["outcome"] == "worked"
    assert row["result"] == "Invites went from 1 to 3 a week."
    assert row["finished_at"].startswith("2026-12-04")
    assert playbook.active_experiment(conn) is None
    assert playbook.tactics(config)[2]["status"] == "worked"
    log = playbook.read(config).splitlines()[-1]
    assert (
        log == "- 2026-12-04: #3 Use all 20 skill slots: worked. Invites went from 1 to 3 a week."
    )
    assert playbook.can_start_test(conn, settings, later) is None


def test_finish_result_is_cut(conn, settings, config):
    settings.set("playbook.result_max_chars", 10)
    playbook.start_experiment(conn, settings, config, "x", None, NOW)
    playbook.finish_experiment(conn, settings, config, "hurt", "a" * 50, NOW)
    assert conn.execute("SELECT result FROM experiments").fetchone()[0] == "a" * 10


def test_finish_errors(conn, settings, config):
    with pytest.raises(playbook.PlaybookError, match="No test"):
        playbook.finish_experiment(conn, settings, config, "worked", "", NOW)
    playbook.start_experiment(conn, settings, config, "x", None, NOW)
    with pytest.raises(playbook.PlaybookError, match="outcome"):
        playbook.finish_experiment(conn, settings, config, "great", "", NOW)
    assert playbook.active_experiment(conn) is not None


def test_no_claude_or_upwork_in_module():
    import inspect

    src = inspect.getsource(playbook)
    assert "bridge" not in src and "writes" not in src and "runner" not in src


# ---------------------------------------------------------------- page


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def test_page_needs_login(config):
    _, client = make_web_client(config)
    assert client.get("/playbook").status_code in (303, 307)


def test_page_shows_tactics_block_reason_and_tests(web, config):
    app, client = web
    r = client.get("/playbook")
    assert r.status_code == 200
    assert "Title formula: Primary Skill | Secondary Skill" in r.text
    assert "approve your keywords" in r.text
    s = app.state.settings
    s.set("keywords.baseline_started", (date.today() - timedelta(days=30)).isoformat())
    r = client.get("/playbook")
    assert "A new test can start." in r.text and "No test yet." in r.text
    playbook.start_experiment(app.state.db, s, config, "Use all 20 skill slots", 3)
    r = client.get("/playbook")
    assert "A test is running" in r.text and "testing" in r.text
    assert "already running" in r.text
    assert "<form" not in r.text.split('id="pb-tactics"')[1]  # read-only
