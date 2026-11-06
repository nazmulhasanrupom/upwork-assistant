-- Daily task (plan E3): more job columns, Connects history, dashboard alerts.
ALTER TABLE jobs ADD COLUMN drop_reason TEXT;
ALTER TABLE jobs ADD COLUMN hourly_budget_type TEXT;
ALTER TABLE jobs ADD COLUMN details TEXT;          -- JSON: the cut-down job (skills, text, reviews)

CREATE TABLE IF NOT EXISTS connects (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    balance   INTEGER NOT NULL,
    needed    INTEGER,                              -- Connects cost of the top kept job, if any
    seen_at   TEXT NOT NULL,
    run_id    TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    key        TEXT PRIMARY KEY,                    -- for example connects_low
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
