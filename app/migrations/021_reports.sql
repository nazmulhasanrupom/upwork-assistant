-- Plan I1/I2: weekly and monthly reports (one row per kind and period; a re-run replaces it).
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- weekly | monthly
    period      TEXT NOT NULL,            -- 2026-W45 or 2026-10
    run_id      TEXT,
    created_at  TEXT NOT NULL,            -- UTC ISO
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,            -- plain text written by Claude (shown as text, never as HTML)
    stats_json  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (kind, period)
);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);
