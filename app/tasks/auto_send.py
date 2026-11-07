"""Auto-send countdown task (round 3, A2). Run: python -m app.tasks.auto_send

Started by the scheduler every `auto_send.check_minutes`. It sends the proposals whose countdown
ended with no Stop, with the recorded system consent. The work is in `app/auto_send_flow.py`.
All Claude runs go through `app/llm/live.py`, so they show in the Claude dock and can be cancelled.
"""

from __future__ import annotations

import logging
import sys

from app import auto_send_flow
from app.config import load_config
from app.db import init_db
from app.llm.live import ClaudeCancelled
from app.settings import Settings
from app.tasks.lock import TaskBusy, task_lock

log = logging.getLogger("auto_send")

TASK = "auto_send"


def main() -> int:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper())
    try:
        with task_lock(config.data_dir, TASK):  # a second tick finds the lock taken
            conn = init_db(config.db_path)
            lines = auto_send_flow.run_due(conn, Settings(conn), config)
    except ClaudeCancelled:  # the user pressed Cancel in the dock; the row is already closed
        log.warning("%s was cancelled by the user", TASK)
        return 0
    except TaskBusy:
        log.warning("auto_send is already running: nothing started")
        return 0
    for line in lines:
        log.warning("auto-send %s", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
