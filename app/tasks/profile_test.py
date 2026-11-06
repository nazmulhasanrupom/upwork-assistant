"""Profile test loop task (plan O3). Run: python -m app.tasks.profile_test

Started by the scheduler (at most every `profile_loop.every_days` days) and by the "Start a
profile test" button on /approvals. The work is in `app/profile_loop.py`. It only makes an
approval item: every profile write is a user click.
"""

from __future__ import annotations

import logging
import sys

from app import profile_loop
from app.config import load_config
from app.db import init_db
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("profile_test")

TASK = profile_loop.TASK


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the button and the scheduler share this lock
            conn = init_db(config.db_path)
            result = profile_loop.run_profile_test(conn, Settings(conn), config)
    except TaskBusy:
        log.warning("Profile test is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
