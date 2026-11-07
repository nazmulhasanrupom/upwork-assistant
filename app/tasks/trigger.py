"""Start a task from the panel as a detached process (plan T1).

`python -m app.tasks.<name>` runs in its own session, so a page reload or a panel restart does
not kill it. Output goes to `<DATA_DIR>/logs/<task>-<time>.log`. The task lock (see lock.py) is
taken here and handed to the new process, so a second click right away sees "running".
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.config import Config
from app.tasks import lock

log = logging.getLogger("trigger")

TASKS = ("daily", "digest", "weekly", "monthly")  # scheduled tasks (and Run now on the dashboard)
MANUAL_TASKS = (
    "keywords",
    "profile_test",
    "consolidate",
    "research",
)  # started by a button (KW1, O3, L4, L5)
# also started by the scheduler: profile_test when due (O3); consolidate, research weekly (L4, L5)
SCHEDULED_MANUAL = ("profile_test", "consolidate", "research")
# only the scheduler starts these (no button): the auto-send countdown check (round 3, A2) and
# the instant job alerts check
SCHEDULED_ONLY = ("auto_send", "job_alerts")
PACKAGE = "app.tasks"  # tests point this at a fake package
ROOT = Path(__file__).resolve().parent.parent.parent  # /app in the image


class NotBuilt(Exception):
    pass


def is_built(name: str) -> bool:
    """The module exists and has a main()."""
    if name not in TASKS + MANUAL_TASKS + SCHEDULED_ONLY:
        return False
    module_name = f"{PACKAGE}.{name}"
    try:
        if importlib.util.find_spec(module_name) is None:
            return False
        module = importlib.import_module(module_name)
    except Exception:  # a broken module is "not usable", not a crashed page
        log.exception("Cannot load task module %s", module_name)
        return False
    return callable(getattr(module, "main", None))


def start(config: Config, name: str) -> Path:
    """Start the task. Raises NotBuilt, or lock.TaskBusy if it is already running.

    Returns the log file path. Does not wait for the task.
    """
    if not is_built(name):
        raise NotBuilt(name)
    fd = lock.acquire(config.data_dir, name)  # TaskBusy goes to the caller
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        log_dir = config.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}-{stamp}.log"
        env = dict(os.environ)
        env["DATA_DIR"] = str(config.data_dir)
        env[lock.ENV_FD] = str(fd)
        with open(log_path, "ab") as out:
            proc = subprocess.Popen(
                [sys.executable, "-m", f"{PACKAGE}.{name}"],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(fd,),
            )
    finally:
        os.close(fd)  # the new process keeps its own copy, and with it the lock
    # Reap the child when it ends, so the panel does not collect zombies.
    threading.Thread(target=proc.wait, name=f"reap-{name}", daemon=True).start()
    return log_path


def running(config: Config) -> dict[str, str]:
    """Tasks whose lock is held now: name -> start time (ISO, or '' if not written yet)."""
    out: dict[str, str] = {}
    for name in TASKS + MANUAL_TASKS + SCHEDULED_ONLY:
        since = lock.running_since(config.data_dir, name)
        if since is not None:
            out[name] = since
    return out
