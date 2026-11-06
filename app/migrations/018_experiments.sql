-- Phase O2: profile experiments (one change at a time, SPEC2 4.C phase 2).
CREATE TABLE IF NOT EXISTS experiments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tactic        TEXT NOT NULL,                -- the playbook tactic text
    tactic_no     INTEGER,                      -- its number in the playbook table (NULL = free text)
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished')),
    started       TEXT NOT NULL,                -- local date, YYYY-MM-DD
    ends          TEXT NOT NULL,                -- local date the test is judged
    baseline_json TEXT NOT NULL DEFAULT '{}',   -- 7-day averages when the test started
    outcome       TEXT,                         -- worked | no effect | hurt (set when finished)
    result        TEXT,                         -- short text written when finished
    finished_at   TEXT                          -- UTC ISO
);
-- At most one active test, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS idx_experiments_one_active ON experiments(status) WHERE status = 'active';
