"""The scheduler (plan I4). supercronic runs `python -m app.tasks.scheduler` every 5 minutes.

Each tick, for every task whose module exists:
- find the latest scheduled time <= now (cron string from `schedules.<task>`, in the time zone
  from `general.timezone`, else the TZ env var);
- if that slot is newer than `scheduler.last_due.<task>`, it is due:
  - slot older than `schedules.grace_minutes` -> skipped and marked (a missed slot is not replayed);
  - else start the task detached (same helper and lock as the Run now buttons) and mark it.
A busy lock is not marked, so the next tick tries again. The kill switch stops the whole tick.
The process only starts tasks and exits; the tasks keep running on their own.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from app.config import Config, load_config
from app.db import init_db
from app.settings import Settings
from app.tasks import trigger
from app.tasks.lock import TaskBusy

log = logging.getLogger("scheduler")


@dataclass
class TickResult:
    started: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # missed slots, marked and not replayed
    busy: list[str] = field(default_factory=list)  # lock taken, tried again next tick
    errors: list[str] = field(default_factory=list)
    kill_switch: bool = False

    def line(self) -> str:
        if self.kill_switch:
            return "tick: kill switch is on, nothing started"
        parts = []
        if self.started:
            parts.append("started " + ", ".join(self.started))
        if self.skipped:
            parts.append("skipped (too late) " + ", ".join(self.skipped))
        if self.busy:
            parts.append("busy " + ", ".join(self.busy))
        if self.errors:
            parts.append("error " + ", ".join(self.errors))
        return "tick: " + ("; ".join(parts) if parts else "nothing due")


def _zone(config: Config, settings: Settings) -> tzinfo:
    for name in (settings.get("general.timezone"), config.tz, "UTC"):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError, KeyError):
            continue
    return UTC


def latest_slot(expr: str, now: datetime, zone: tzinfo) -> datetime | None:
    """Latest time <= now that matches the cron string, as an aware UTC datetime.

    None if the string is not a valid cron expression. The cron fields are read in `zone`.
    """
    if not isinstance(expr, str) or not croniter.is_valid(expr):
        return None
    minute = now.astimezone(UTC).replace(second=0, microsecond=0)
    # croniter's get_prev is strict ("before"), so start one minute after the current minute
    # to let a slot that is exactly now count.
    start = (minute + timedelta(minutes=1)).astimezone(zone)
    slot = croniter(expr, start).get_prev(datetime)
    return slot.astimezone(UTC)


def _parse(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _interval_passed(settings: Settings, name: str, now: datetime) -> bool:
    """`profile_test` is checked every day but runs only when `profile_loop.every_days` have passed
    since `profile_loop.last_run` (written by the task)."""
    last = _parse(settings.get("profile_loop.last_run"))
    if last is None:
        return True
    days = float(settings.get("profile_loop.every_days"))
    return now - last >= timedelta(days=days)


def _cron(settings: Settings, name: str) -> str:
    """The cron string of a task. The auto-send check has no schedule setting: it runs every
    `auto_send.check_minutes` (the tick itself only happens every 5 minutes)."""
    if name == "auto_send":
        return f"*/{max(1, int(settings.get('auto_send.check_minutes')))} * * * *"
    if name == "job_alerts":
        return f"*/{max(1, int(settings.get('job_alerts.interval_minutes')))} * * * *"
    return settings.get(f"schedules.{name}")


def tick(
    settings: Settings,
    config: Config,
    now: datetime | None = None,
) -> TickResult:
    """One scheduler pass. `now` is for tests."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    result = TickResult()
    if settings.get("general.kill_switch"):
        result.kill_switch = True
        return result

    zone = _zone(config, settings)
    grace = timedelta(minutes=float(settings.get("schedules.grace_minutes")))
    for name in trigger.TASKS + trigger.SCHEDULED_MANUAL + trigger.SCHEDULED_ONLY:
        if not trigger.is_built(name):
            continue
        if name == "job_alerts" and not settings.get("job_alerts.enabled"):
            continue  # off: nothing to start (the task checks this too)
        expr = _cron(settings, name)
        slot = latest_slot(expr, now, zone)
        if slot is None:
            log.warning("Schedule for %s is not a valid cron string: %r", name, expr)
            result.errors.append(f"{name} (bad cron)")
            continue
        key = f"scheduler.last_due.{name}"
        last = _parse(settings.get(key))
        if last is not None and last >= slot:
            continue  # this slot was already handled
        stamp = slot.isoformat(timespec="seconds")
        if now - slot > grace:
            log.warning("Missed slot %s for %s is older than the grace time: skipped", stamp, name)
            settings.set(key, stamp)
            result.skipped.append(name)
            continue
        if name == "profile_test" and not _interval_passed(settings, name, now):
            settings.set(key, stamp)  # not due yet (plan O3): this slot is handled
            continue
        try:
            trigger.start(config, name)
        except TaskBusy:
            log.info("%s is still running: will try again next tick", name)
            result.busy.append(name)
            continue
        except trigger.NotBuilt:  # the module vanished between the check and the start
            continue
        except Exception as exc:  # one broken task must not stop the others
            log.exception("Could not start %s", name)
            result.errors.append(f"{name} ({type(exc).__name__})")
            continue
        settings.set(key, stamp)
        result.started.append(name)
    return result


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper(), stream=sys.stdout)
    conn: sqlite3.Connection = init_db(config.db_path)
    result = tick(Settings(conn), config)
    print(result.line(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
