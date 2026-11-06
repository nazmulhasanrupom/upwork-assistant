-- Phase H1: every write preview (manage_proposals create / accept_invitation) and what happened
-- to it. One row per preview. `preview_id` is UNIQUE and the confirm step claims the row with
-- one UPDATE (confirmed_at IS NULL), so one preview is confirmed at most once.
CREATE TABLE IF NOT EXISTS previews (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT NOT NULL CHECK (kind IN ('create', 'accept_invitation')),
    job_id              TEXT,                 -- numeric job id (job_reference)
    invitation_uid      TEXT,                 -- set for accept_invitation
    preview_id          TEXT UNIQUE,          -- from Upwork; NULL when the preview failed
    request_json        TEXT NOT NULL,        -- the exact tool arguments sent
    response_json       TEXT,                 -- the tool's answer (raw)
    connects_cost       INTEGER,
    connects_balance    INTEGER,
    boost_json          TEXT,                 -- the `boost` block of the answer
    created_at          TEXT NOT NULL,        -- UTC ISO; previews expire (writes.preview_max_age_minutes)
    confirmed_at        TEXT,                 -- set by the single atomic claim before confirm runs
    confirm_result_json TEXT,                 -- confirm answer, dry-run record, or error
    status              TEXT NOT NULL DEFAULT 'previewed'
        CHECK (status IN ('previewed', 'confirmed', 'dry_run_blocked', 'expired', 'failed')),
    click_id            TEXT,                 -- the user click that made the preview
    attachments_asked   INTEGER NOT NULL DEFAULT 0,   -- user was asked about file attachments
    highlights_asked    INTEGER NOT NULL DEFAULT 0,   -- user was asked about highlights
    run_id              TEXT                  -- claude write run of the preview
);
CREATE INDEX IF NOT EXISTS idx_previews_job ON previews(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_previews_status ON previews(status);
