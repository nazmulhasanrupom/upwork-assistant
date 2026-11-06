"""Weekly research task (plan L5). Run: python -m app.tasks.research

Started by the scheduler (weekly, schedules.research) and by the Run button on /learning.
The work is in `app/research.py`.
"""

from __future__ import annotations

import logging
import sys

from app import research
from app.config import load_config
from app.db import init_db
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("research")

TASK = research.TASK


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # the button and the scheduler share this lock
            conn = init_db(config.db_path)
            result = research.run_research(conn, Settings(conn), config)
    except TaskBusy:
        log.warning("research is already running: nothing started")
        return 0
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
