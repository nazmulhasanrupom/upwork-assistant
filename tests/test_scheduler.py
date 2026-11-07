"""Scheduler (plan I4) with a frozen clock. Tasks are not really started, except in the last two
tests, which use the fake task package (tests/fake_tasks). No network, no Claude."""

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.db import init_db
from app.settings import Settings
from app.tasks import lock, scheduler, trigger


def at(hour, minute=0, day=6, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


@pytest.fixture
def settings(config):
    return Settings(init_db(config.db_path))


class Started(list):
    built = None

    def __init__(self):
        super().__init__()
        self.built = {"daily"}


@pytest.fixture
def started(monkeypatch):
    """Replace the real start with a recorder; only 'daily' counts as built."""
    calls = Started()
    monkeypatch.setattr(trigger, "is_built", lambda name: name in calls.built)
    monkeypatch.setattr(trigger, "start", lambda config, name: calls.append(name))
    return calls


def test_due_task_starts_once_per_slot(settings, config, started):
    r = scheduler.tick(settings, config, at(10, 0))  # exactly on the slot
    assert started == ["daily"] and r.started == ["daily"]
    assert r.line() == "tick: started daily"
    assert settings.get("scheduler.last_due.daily") == "2026-11-06T10:00:00+00:00"
    r = scheduler.tick(settings, config, at(10, 5))  # same slot, next tick
    assert started == ["daily"] and r.line() == "tick: nothing due"
    r = scheduler.tick(settings, config, at(11, 59, second=59))
    assert started == ["daily"]
    r = scheduler.tick(settings, config, at(12, 0, second=1))  # next slot
    assert started == ["daily", "daily"]


def test_job_alerts_cron_uses_interval_setting(settings):
    assert scheduler._cron(settings, "job_alerts") == "*/15 * * * *"
    settings.set("job_alerts.interval_minutes", 10)
    assert scheduler._cron(settings, "job_alerts") == "*/10 * * * *"
    settings.set("job_alerts.interval_minutes", 0)  # never a zero step
    assert scheduler._cron(settings, "job_alerts") == "*/1 * * * *"


def test_job_alerts_starts_only_when_enabled(settings, config, started):
    started.built = {"job_alerts"}
    r = scheduler.tick(settings, config, at(10, 15))
    assert started == [] and r.line() == "tick: nothing due"  # off by default
    settings.set("job_alerts.enabled", True)
    r = scheduler.tick(settings, config, at(10, 15))
    assert started == ["job_alerts"] and r.started == ["job_alerts"]
    assert settings.get("scheduler.last_due.job_alerts") == "2026-11-06T10:15:00+00:00"
    scheduler.tick(settings, config, at(10, 20))  # same slot
    assert started == ["job_alerts"]
    scheduler.tick(settings, config, at(10, 30))  # next slot
    assert started == ["job_alerts", "job_alerts"]


def test_first_tick_after_grace_skips_and_marks(settings, config, started):
    # 10:40 is 40 minutes after the 10:00 slot: past the 30 minute grace, so skipped and marked.
    r = scheduler.tick(settings, config, at(10, 40))
    assert started == [] and r.skipped == ["daily"]
    assert r.line() == "tick: skipped (too late) daily"
    assert settings.get("scheduler.last_due.daily") == "2026-11-06T10:00:00+00:00"
    assert scheduler.tick(settings, config, at(10, 45)).line() == "tick: nothing due"


def test_late_tick_within_grace_still_runs(settings, config, started):
    r = scheduler.tick(settings, config, at(10, 25))
    assert started == ["daily"] and r.started == ["daily"]
    scheduler.tick(settings, config, at(10, 30))
    assert started == ["daily"]  # once only


def test_grace_edge_and_setting(settings, config, started):
    scheduler.tick(settings, config, at(10, 30))  # exactly 30 minutes late: still runs
    assert started == ["daily"]
    settings.set("schedules.grace_minutes", 5)
    scheduler.tick(settings, config, at(12, 6))  # 6 minutes late with a 5 minute grace
    assert started == ["daily"]
    assert settings.get("scheduler.last_due.daily") == "2026-11-06T12:00:00+00:00"


def test_older_than_grace_is_skipped_not_replayed(settings, config, started):
    settings.set("scheduler.last_due.daily", "2026-11-06T04:00:00+00:00")
    # The scheduler was down for hours. Only the newest slot counts, and it is too old.
    r = scheduler.tick(settings, config, at(9, 50))
    assert started == [] and r.skipped == ["daily"]
    assert settings.get("scheduler.last_due.daily") == "2026-11-06T08:00:00+00:00"
    scheduler.tick(settings, config, at(10, 1))
    assert started == ["daily"]  # the next slot runs normally


def test_kill_switch_logs_and_starts_nothing(settings, config, started):
    settings.set("general.kill_switch", True)
    r = scheduler.tick(settings, config, at(10, 0))
    assert started == [] and r.kill_switch
    assert r.line() == "tick: kill switch is on, nothing started"
    assert settings.get("scheduler.last_due.daily") == ""  # not marked
    settings.set("general.kill_switch", False)
    scheduler.tick(settings, config, at(10, 10))  # still inside grace: runs now
    assert started == ["daily"]


def test_busy_lock_is_not_marked_and_retried(settings, config, monkeypatch):
    monkeypatch.setattr(trigger, "PACKAGE", "tests.fake_tasks")
    fd = lock.acquire(config.data_dir, "daily")
    try:
        r = scheduler.tick(settings, config, at(10, 0))  # real trigger.start sees the lock
        assert r.busy == ["daily"] and r.started == []
        assert r.line() == "tick: busy daily"
        assert settings.get("scheduler.last_due.daily") == ""
        assert not (config.data_dir / "logs").exists()  # no process was started
    finally:
        os.close(fd)
    monkeypatch.setenv("FAKE_TASK_SLEEP", "0")
    r = scheduler.tick(settings, config, at(10, 5))  # lock free, still inside grace
    assert r.started == ["daily"]
    assert settings.get("scheduler.last_due.daily") == "2026-11-06T10:00:00+00:00"
    end = time.time() + 15
    while lock.running_since(config.data_dir, "daily") is not None and time.time() < end:
        time.sleep(0.05)


def test_busy_task_does_not_stop_the_others(settings, config, monkeypatch):
    calls = []

    def fake_start(cfg, name):
        if name == "daily":
            raise lock.TaskBusy(name)
        calls.append(name)

    monkeypatch.setattr(trigger, "is_built", lambda name: True)
    monkeypatch.setattr(trigger, "start", fake_start)
    settings.set("schedules.digest", "0 10 * * *")
    r = scheduler.tick(settings, config, at(10, 0))
    assert r.busy == ["daily"] and "digest" in calls
    assert r.line().startswith("tick: started digest")


def test_time_zone_dhaka(settings, config, started):
    started.built.update({"digest"})
    settings.set("general.timezone", "Asia/Dhaka")
    settings.set("schedules.daily", "0 1 1 1 *")  # never inside this test
    # digest is 21:00 in Dhaka (UTC+6) = 15:00 UTC.
    scheduler.tick(settings, config, at(14, 55))  # 20:55 in Dhaka
    assert started == []
    r = scheduler.tick(settings, config, at(15, 3))  # 21:03 in Dhaka
    assert started == ["digest"] and r.started == ["digest"]
    assert settings.get("scheduler.last_due.digest") == "2026-11-06T15:00:00+00:00"


def test_time_zone_falls_back_to_env_tz_then_utc(settings, config, started):
    started.built.update({"digest"})
    settings.set("schedules.daily", "0 1 1 1 *")
    settings.set("general.timezone", "Nowhere/Land")  # bad name: use config.tz
    cfg = type(config)(**{**config.__dict__, "tz": "Asia/Dhaka"})
    scheduler.tick(settings, cfg, at(15, 1))
    assert started == ["digest"]


def test_zone_and_midnight_crossing(settings, config, started):
    settings.set("general.timezone", "Asia/Dhaka")
    settings.set("schedules.daily", "30 0 * * *")  # 00:30 Dhaka = 18:30 UTC the day before
    scheduler.tick(settings, config, at(18, 31, day=5))
    assert started == ["daily"]
    assert settings.get("scheduler.last_due.daily") == "2026-11-05T18:30:00+00:00"


def test_dst_zone_uses_local_wall_clock(settings, config, started):
    settings.set("general.timezone", "America/New_York")
    settings.set("schedules.daily", "0 9 * * *")
    scheduler.tick(settings, config, datetime(2026, 7, 1, 13, 5, tzinfo=UTC))  # EDT: 9:05 local
    assert started == ["daily"]
    scheduler.tick(settings, config, datetime(2026, 12, 1, 13, 5, tzinfo=UTC))  # EST: 8:05 local
    assert started == ["daily"]  # nothing due; 14:00 UTC is the slot in winter
    scheduler.tick(settings, config, datetime(2026, 12, 1, 14, 5, tzinfo=UTC))
    assert started == ["daily", "daily"]


def test_missing_module_is_ignored(settings, config, monkeypatch):
    monkeypatch.setattr(trigger, "PACKAGE", "tests.fake_tasks")
    calls = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: calls.append(name))
    # fake package: daily has main(), weekly has no main(), digest and monthly do not exist.
    for name in ("daily", "digest", "weekly", "monthly"):
        settings.set(f"schedules.{name}", "* * * * *")
    r = scheduler.tick(settings, config, at(10, 0))
    assert calls == ["daily"] and r.started == ["daily"]
    assert settings.get("scheduler.last_due.weekly") == ""  # never marked
    assert settings.get("scheduler.last_due.digest") == ""


def test_bad_cron_is_reported_and_others_run(settings, config, started):
    started.built.add("digest")
    settings.set("schedules.digest", "not a cron")
    r = scheduler.tick(settings, config, at(10, 0))
    assert started == ["daily"]
    assert r.errors == ["digest (bad cron)"]
    assert "error digest (bad cron)" in r.line()


def test_start_error_is_not_marked(settings, config, monkeypatch):
    def boom(cfg, name):
        raise OSError("no fork")

    monkeypatch.setattr(trigger, "is_built", lambda name: name == "daily")
    monkeypatch.setattr(trigger, "start", boom)
    r = scheduler.tick(settings, config, at(10, 0))
    assert r.errors == ["daily (OSError)"]
    assert settings.get("scheduler.last_due.daily") == ""


def test_latest_slot_is_inclusive_and_utc():
    z = scheduler.ZoneInfo("UTC")
    assert scheduler.latest_slot("0 */2 * * *", at(10, 0), z) == at(10, 0)
    assert scheduler.latest_slot("0 */2 * * *", at(9, 59, second=59), z) == at(8, 0)
    assert scheduler.latest_slot("0 6 * * 1", at(12, 0), z) == at(6, 0, day=2)  # Monday
    assert scheduler.latest_slot("nope", at(10), z) is None
    assert at(10, 0) - at(8, 0) == timedelta(hours=2)


def test_main_prints_one_line_and_returns_zero(config, monkeypatch, capsys):
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setattr(trigger, "is_built", lambda name: False)
    assert scheduler.main() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "tick: nothing due"
