-- Phase R: proposal reviews (plan R2). One row per review; JSON columns hold the details.
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT,                        -- a stored job, or NULL for pasted job text
    job_text_hash TEXT,                        -- sha256 of the job text the review used
    original      TEXT NOT NULL DEFAULT '{}',  -- JSON: cover_letter, answers, rate, job title/text
    prechecks     TEXT NOT NULL DEFAULT '[]',  -- JSON: findings of the Python pre-checks
    result        TEXT NOT NULL DEFAULT '{}',  -- JSON: the model's review, or {"error": "..."}
    score         INTEGER,                     -- 0-100, NULL when the model did not run
    model         TEXT,
    cost          REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at);
