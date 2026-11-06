-- Phase R3: proposals I sent, synced from Upwork (list, then get for new ones).
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id       TEXT PRIMARY KEY,
    job_id            TEXT,                          -- marketplaceJobPosting.id (matches jobs.job_id)
    job_title         TEXT,
    created_at        TEXT,                          -- when it was sent (UTC ISO)
    rate              REAL,                          -- charge rate or fixed bid
    cover_letter      TEXT,                          -- NULL until the `get` call has worked
    answers_json      TEXT NOT NULL DEFAULT '[]',    -- JSON list of {question, answer}
    status            TEXT,                          -- Upwork status, e.g. Accepted (= submitted)
    viewed_by_client  INTEGER NOT NULL DEFAULT 0,
    boosted           INTEGER NOT NULL DEFAULT 0,
    connects_bid      INTEGER,
    fetched_at        TEXT,                          -- last sync that saw this proposal
    review_id         INTEGER,                       -- reviews.id of the after-send review
    draft_id          INTEGER,                       -- drafts.id when we wrote a draft for this job
    draft_diff        TEXT                           -- JSON: draft text vs sent text
);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_job ON proposals(job_id);
