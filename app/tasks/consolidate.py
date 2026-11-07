"""Weekly consolidate task (plan L4). Run: python -m app.tasks.consolidate

Started by the scheduler (weekly, schedules.consolidate) and by the Run button on /learning.
The work is in `app/consolidation.py`.
"""

from __future__ import annotations

import logging
import sys

from app import consolidation
from app.config import load_config
from app.db import init_db
from app.llm.live import ClaudeCancelled
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("consolidate")

TASK = consolidation.TASK


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the button and the scheduler share this lock
            conn = init_db(config.db_path)
            result = consolidation.run_consolidation(conn, Settings(conn), config)
    except ClaudeCancelled:  # the user pressed Cancel: stop cleanly, the lock is released
        log.warning("%s was cancelled by the user", TASK)
        return 0
    except TaskBusy:
        log.warning("consolidate is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
