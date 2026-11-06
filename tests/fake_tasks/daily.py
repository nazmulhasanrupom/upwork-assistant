"""Fake task for the Run now tests: holds the task lock, sleeps, writes a runs row. No network."""

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from app.tasks.lock import TaskBusy, task_lock


def main() -> int:
    data_dir = Path(os.environ["DATA_DIR"])
    print("fake daily start", flush=True)
    try:
        with task_lock(data_dir, "daily"):
            conn = sqlite3.connect(data_dir / "app.db")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO runs(run_id, task, started, status) VALUES (?,?,?, 'running')",
                (f"fake-{time.time_ns()}", "daily", now),
            )
            conn.commit()
            time.sleep(float(os.environ.get("FAKE_TASK_SLEEP", "1")))
            conn.execute("UPDATE runs SET status = 'ok', ended = ? WHERE task = 'daily'", (now,))
            conn.commit()
            conn.close()
    except TaskBusy:
        print("busy", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
