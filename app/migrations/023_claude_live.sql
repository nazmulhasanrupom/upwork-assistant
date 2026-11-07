-- Plan L1: every non-interactive claude process, so the dock can list it and Cancel can stop it.
-- Rows are written by the process that starts claude (the panel or a task process).
CREATE TABLE IF NOT EXISTS claude_live (
    id               TEXT PRIMARY KEY,
    pid              INTEGER NOT NULL,
    pgid             INTEGER NOT NULL,
    pid_start        TEXT NOT NULL DEFAULT '',  -- start time of the pid from /proc, to spot a reused pid
    host             TEXT NOT NULL DEFAULT '',  -- machine/container that owns the pid
    kind             TEXT NOT NULL,             -- claude | read | write | check
    label            TEXT NOT NULL,
    action_id        TEXT,                      -- linked worker action
    run_id           TEXT,                      -- linked task run (runs.run_id)
    model            TEXT,
    started          TEXT NOT NULL,             -- UTC ISO
    ended            TEXT,
    status           TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | cancelled
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    beat             TEXT                       -- last sign of life, UTC ISO
);
CREATE INDEX IF NOT EXISTS idx_claude_live_status ON claude_live(status);
CREATE INDEX IF NOT EXISTS idx_claude_live_ended ON claude_live(ended);
