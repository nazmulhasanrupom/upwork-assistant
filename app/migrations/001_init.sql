-- Initial schema (SPEC 12). Dates are ISO-8601 text in UTC.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    first_seen      TEXT NOT NULL,
    title           TEXT,
    url             TEXT,
    job_type        TEXT,          -- hourly | fixed
    budget_min      REAL,
    budget_max      REAL,
    client_spend    REAL,
    client_hours    REAL,
    client_hires    INTEGER,
    client_rating   REAL,
    client_reviews  INTEGER,
    rate_paid       REAL,
    tier            TEXT,          -- LOW | MID | HIGH
    tier_estimated  INTEGER NOT NULL DEFAULT 0,
    proposals_tier  TEXT,
    connects_cost   INTEGER,
    red_flags       TEXT NOT NULL DEFAULT '[]',   -- JSON
    status          TEXT NOT NULL DEFAULT 'seen'
        -- seen|dropped|candidate|judged|drafted|prepared|sent|skipped
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_tier ON jobs(tier);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);

CREATE TABLE IF NOT EXISTS skills_seen (
    job_id TEXT NOT NULL,
    skill  TEXT NOT NULL,
    week   TEXT NOT NULL,          -- ISO week, for example 2026-W45
    PRIMARY KEY (job_id, skill)
);
CREATE INDEX IF NOT EXISTS idx_skills_week ON skills_seen(week, skill);

CREATE TABLE IF NOT EXISTS judgements (
    job_id         TEXT PRIMARY KEY,   -- a job is judged only once
    run_id         TEXT,
    fit_score      INTEGER,
    why_apply      TEXT,
    risks          TEXT,
    plan           TEXT,               -- JSON
    suggested_rate REAL
);

CREATE TABLE IF NOT EXISTS drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL,
    cover_letter   TEXT,
    answers        TEXT NOT NULL DEFAULT '[]',   -- JSON
    materials      TEXT NOT NULL DEFAULT '[]',   -- JSON
    edited_by_user INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_drafts_job ON drafts(job_id);

CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,     -- proposal | profile | invite_reply
    ref_id          TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',   -- JSON
    preview_id      TEXT,
    preview_created TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS invites (
    invite_id   TEXT PRIMARY KEY,
    job_id      TEXT,
    received    TEXT,
    status      TEXT,
    draft_reply TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    room_id         TEXT PRIMARY KEY,
    last_seen_story TEXT,
    unread          INTEGER NOT NULL DEFAULT 0,
    notified        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    run_id    TEXT PRIMARY KEY,
    task      TEXT NOT NULL,
    started   TEXT NOT NULL,
    ended     TEXT,
    status    TEXT NOT NULL DEFAULT 'running',
    mcp_calls INTEGER NOT NULL DEFAULT 0,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task, started);

CREATE TABLE IF NOT EXISTS claude_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claude_calls_run ON claude_calls(run_id);

CREATE TABLE IF NOT EXISTS emails (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    event   TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    ref_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_event ON emails(event, ref_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT
);
