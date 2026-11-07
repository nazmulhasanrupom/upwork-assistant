"""Keyword discovery task (plan KW1). Run: python -m app.tasks.keywords

Started by hand from the "Find keywords" button on /approvals (a detached process, like the
Run buttons of plan T1). It is NOT in the scheduler. The work is in `app/keywords.py`.
"""

from __future__ import annotations

import logging
import sys

from app import keywords
from app.config import load_config
from app.db import init_db
from app.llm.live import ClaudeCancelled
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("keywords")

TASK = keywords.TASK


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the button and a second click share this lock
            conn = init_db(config.db_path)
            result = keywords.run_keywords(conn, Settings(conn), config)
    except ClaudeCancelled:  # the user pressed Cancel: stop cleanly, the lock is released
        log.warning("%s was cancelled by the user", TASK)
        return 0
    except TaskBusy:
        log.warning("Keywords task is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
