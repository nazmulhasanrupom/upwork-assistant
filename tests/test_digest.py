"""Daily digest task (plan G3): numbers, email, skip rules, dedupe, run row, lock. No network."""

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app import mail
from app.db import init_db
from app.settings import Settings
from app.tasks import daily, digest, trigger
from app.tasks.lock import TaskBusy, acquire
from tests.test_daily import make_client
from tests.test_mail import set_smtp, smtp  # noqa: F401  (smtp is a fixture)

ORG = "1000000000000000001"


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


def fill(conn, settings, config, balance=50):
    """A real daily run on the fixtures: 5 kept jobs (2 HIGH, 3 MID), plus dropped ones."""
    res = daily.run_daily(conn, settings, config, make_client(settings, balance=balance))
    assert res.status == "ok" and res.kept == 5
    return res


def judge(conn, job_id, fit, why=("Good fit", "Fair pay"), plan=None):
    conn.execute(
        "INSERT INTO judgements(job_id, fit_score, why_apply, risks, plan) VALUES (?,?,?,?,?)",
        (job_id, fit, "\n".join(why), "", json.dumps(plan or {})),
    )


def sent_mail(smtp):  # noqa: F811
    assert len(smtp.instances) == 1 and len(smtp.instances[0].sent) == 1
    msg = smtp.instances[0].sent[0]
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    return msg["Subject"], text, html


def last_run(conn):
    return conn.execute("SELECT * FROM runs WHERE task='digest' ORDER BY rowid DESC").fetchone()


def kept_ids(conn):
    rows = conn.execute("SELECT job_id, tier FROM jobs WHERE status='candidate' ORDER BY job_id")
    return [(r["job_id"], r["tier"]) for r in rows]


# ---------- skip rules ----------


def test_nothing_new_sends_nothing_by_default(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    res = digest.run_digest(conn, settings, config)
    assert res.status == "skipped" and res.sent is None and not smtp.instances
    row = last_run(conn)
    assert row["status"] == "skipped" and "Nothing new today" in row["error"] and row["ended"]
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_nothing_new_still_sends_when_skip_is_off(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    settings.set("email.digest_skip_empty", False)
    res = digest.run_digest(conn, settings, config)
    assert res.status == "ok" and res.sent == mail.SENT
    subject, text, _ = sent_mail(smtp)
    assert "0 new job(s)" in subject and "No new MID or HIGH job today." in text


def test_dropped_jobs_alone_count_as_nothing_new(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    conn.execute("UPDATE jobs SET status='dropped'")
    res = digest.run_digest(conn, settings, config)
    assert res.status == "skipped" and res.kept == 0 and res.seen > 0


def test_old_jobs_are_not_new(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET first_seen = ?", (old,))
    assert digest.run_digest(conn, settings, config).status == "skipped"


def test_kill_switch_skips(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    settings.set("general.kill_switch", True)
    res = digest.run_digest(conn, settings, config)
    assert res.status == "skipped" and not smtp.instances
    assert last_run(conn)["error"] == "kill switch is on"


# ---------- the email ----------


def test_email_content(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    jobs = kept_ids(conn)
    (low_id, _), (top_id, _) = jobs[0], jobs[1]
    judge(conn, low_id, 70, ("Reason <b>one</b>", "Reason two"))
    judge(
        conn,
        top_id,
        91,
        ("Top reason",),
        {"can_afford": False, "connects_cost": 17, "connects_balance": 3},
    )
    conn.execute(
        "INSERT INTO drafts(job_id, cover_letter, created_at) VALUES (?,?,?)",
        (top_id, "Hi", datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.execute(
        "INSERT INTO claude_calls(run_id, model, cost_usd, created_at) VALUES ('x','m',1.25,?)",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    )
    res = digest.run_digest(conn, settings, config)
    assert res.status == "ok" and res.sent == mail.SENT
    assert (res.seen, res.kept, res.drafted) == (res.seen, 5, 1) and res.seen >= 5
    subject, text, html = sent_mail(smtp)
    assert subject == f"Daily digest {res.day}: 5 new job(s), 1 draft(s)"
    assert (
        f"New jobs seen: {res.seen}. Kept (MID/HIGH): 5 (HIGH 2, MID 3). Drafts written: 1." in text
    )
    assert "fit 91" in text and "fit 70" in text and "Why apply: Top reason" in text
    assert text.index("fit 91") < text.index("fit 70")  # best fit first
    assert "Not enough Connects yet: it costs 17, you have 3." in text
    assert f"Open in panel: https://test.example/jobs/{top_id}" in text
    assert "Connects: 50" in text and "Claude cost today: $1.25" in text
    assert "of $50.00" in text
    assert "Reason <b>one</b>" not in html and "Reason &lt;b&gt;one&lt;/b&gt;" in html
    assert f'href="https://test.example/jobs/{top_id}"' in html
    assert res.jobs[0]["id"] == top_id
    assert last_run(conn)["status"] == "ok"


def test_upwork_link_only_for_upwork_urls(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    a, b = [i for i, _ in kept_ids(conn)][:2]
    conn.execute("UPDATE jobs SET url='https://www.upwork.com/jobs/~02abc' WHERE job_id=?", (a,))
    conn.execute("UPDATE jobs SET url='https://evil.example/x' WHERE job_id=?", (b,))
    digest.run_digest(conn, settings, config)
    _, text, html = sent_mail(smtp)
    assert "Open on Upwork: https://www.upwork.com/jobs/~02abc" in text
    assert "evil.example" not in text and "evil.example" not in html


def test_max_jobs_limit(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    settings.set("email.digest_max_jobs", 2)
    digest.run_digest(conn, settings, config)
    _, text, _ = sent_mail(smtp)
    assert text.count("\n* ") == 2 and "Showing the best 2 of 5 by fit score." in text


def test_connects_low_note(conn, settings, config, smtp):  # noqa: F811
    fill(conn, settings, config, balance=3)
    set_smtp(settings)  # after the daily run, so only the digest is mailed
    digest.run_digest(conn, settings, config)
    _, text, _ = sent_mail(smtp)
    assert "Connects: 3" in text and "Connects are low for the top job." in text


def test_no_panel_link_without_base_url(conn, settings, config, smtp):  # noqa: F811
    from dataclasses import replace

    set_smtp(settings)
    fill(conn, settings, config)
    digest.run_digest(conn, settings, replace(config, public_base_url=""))
    _, text, _ = sent_mail(smtp)
    assert "Open in panel" not in text and "http://" not in text.replace("https://", "")


def test_uses_the_panel_time_zone_for_today(conn, settings, config):
    from dataclasses import replace

    fill(conn, settings, config)
    # A job seen at 10:00 UTC. At 20:00 UTC it is still the same day in UTC, but in Dhaka
    # (UTC+6) it is already the next day (02:00), so the job is from "yesterday" there.
    now = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
    conn.execute("UPDATE jobs SET first_seen = '2026-11-10T10:00:00+00:00'")
    in_utc = digest.collect(conn, config, settings, now)
    in_dhaka = digest.collect(conn, replace(config, tz="Asia/Dhaka"), settings, now)
    assert in_utc["kept"] == 5 and in_dhaka["kept"] == 0
    assert in_utc["day"] == "2026-11-10" and in_dhaka["day"] == "2026-11-11"


# ---------- sending rules ----------


def test_sent_once_per_day(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    first = digest.run_digest(conn, settings, config)
    again = digest.run_digest(conn, settings, config)
    assert first.status == "ok" and again.status == "skipped" and again.sent == mail.DUPLICATE
    assert "already sent" in last_run(conn)["error"]
    assert len(smtp.instances) == 1
    row = conn.execute("SELECT event, ref_id FROM emails").fetchone()
    assert (row["event"], row["ref_id"]) == ("digest", first.day)


def test_next_day_sends_again(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    now = datetime.now(UTC)
    # Pin every job to `now` and to `now + 1 day` so a clock near local midnight cannot move
    # a job out of the digest day (no extra minutes, no real-time gap between the runs).
    conn.execute("UPDATE jobs SET first_seen = ?", (now.isoformat(timespec="seconds"),))
    digest.run_digest(conn, settings, config, now=now)
    later = now + timedelta(days=1)
    conn.execute("UPDATE jobs SET first_seen = ?", (later.isoformat(timespec="seconds"),))
    res = digest.run_digest(conn, settings, config, now=later)
    assert res.status == "ok" and len(smtp.instances) == 2


def test_event_switched_off(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    settings.set("email.events.digest", False)
    res = digest.run_digest(conn, settings, config)
    assert res.status == "skipped" and res.sent == mail.OFF and not smtp.instances
    assert "switched off" in last_run(conn)["error"]


def test_no_mail_transport(conn, settings, config):
    fill(conn, settings, config)
    res = digest.run_digest(conn, settings, config)
    assert res.status == "skipped" and res.sent == mail.NO_TRANSPORT
    assert "connect Gmail" in last_run(conn)["error"]
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_send_failure_is_an_error_run(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    smtp.fail = True
    res = digest.run_digest(conn, settings, config)
    assert res.status == "error" and res.sent == mail.FAILED
    assert last_run(conn)["status"] == "error"
    assert conn.execute("SELECT 1 FROM alerts WHERE key='email_failed'").fetchone()
    smtp.fail = False
    assert digest.run_digest(conn, settings, config).status == "ok"  # a retry still goes out


def test_dry_run_still_sends(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    assert settings.get("general.dry_run") is True
    assert digest.run_digest(conn, settings, config).status == "ok"


def test_crash_is_recorded(conn, settings, config, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(digest, "collect", boom)
    res = digest.run_digest(conn, settings, config)
    assert res.status == "error"
    row = last_run(conn)
    assert row["status"] == "error" and "RuntimeError: db gone" in row["error"] and row["ended"]


def test_no_claude_or_upwork_call(conn, settings, config, smtp):  # noqa: F811
    set_smtp(settings)
    fill(conn, settings, config)
    digest.run_digest(conn, settings, config)  # the autouse stub makes a real claude call exit 99
    assert last_run(conn)["mcp_calls"] == 0


# ---------- main(), lock, Run button ----------


def test_main_runs_with_the_task_lock(config, monkeypatch, smtp):  # noqa: F811
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    conn = init_db(config.db_path)
    conn.close()
    assert digest.main() == 0  # nothing new: skipped, exit 0
    conn = init_db(config.db_path)
    assert conn.execute("SELECT status FROM runs WHERE task='digest'").fetchone()[0] == "skipped"


def test_main_when_busy_starts_nothing(config, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    fd = acquire(config.data_dir, "digest")
    try:
        assert digest.main() == 0
    finally:
        import os

        os.close(fd)
    conn = init_db(config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_busy_error_type():
    assert issubclass(TaskBusy, Exception)


def test_digest_is_a_real_task_for_the_run_button(config, monkeypatch):
    assert "digest" in trigger.TASKS and trigger.is_built("digest")


def test_run_button_starts_the_digest_process(config, monkeypatch, tmp_path):
    """POST-free check of trigger.start: it launches `python -m app.tasks.digest`."""
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    calls = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd

        def wait(self):
            return 0

    monkeypatch.setattr(trigger.subprocess, "Popen", FakePopen)
    log = trigger.start(config, "digest")
    assert calls["cmd"] == [sys.executable, "-m", "app.tasks.digest"]
    assert log.parent == config.data_dir / "logs" and log.name.startswith("digest-")
