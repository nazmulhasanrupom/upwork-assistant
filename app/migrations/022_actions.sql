-- Plan W1: background actions run by the in-app worker (one row per click that takes long).
CREATE TABLE IF NOT EXISTS actions (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,            -- what it does, e.g. job_prepare
    target      TEXT NOT NULL DEFAULT '', -- what it works on, e.g. a job id
    label       TEXT NOT NULL,            -- short plain words shown to the user
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | stopped
    created     TEXT NOT NULL,            -- UTC ISO
    started     TEXT,
    ended       TEXT,
    result_url  TEXT,                     -- local path to open when done
    message     TEXT,                     -- short plain result text
    error       TEXT,                     -- short safe reason (never a stack trace or secret)
    dedup_key   TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_dedup ON actions(dedup_key);
CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(created);
