-- Phase H3: a generic approvals queue, and the inbox (rooms read from Upwork + reply drafts).
-- `approvals`: later phases (keywords, profile changes, playbook) add items; the user decides
-- on /approvals. Open proposal previews are shown there too but live in `previews`, not here.
-- The approvals table from 001 (kind, payload, preview_id ...) was never written by any code, so it
-- is replaced by this shape instead of extended (SQLite cannot add a status CHECK with ALTER).
DROP INDEX IF EXISTS idx_approvals_status;
DROP TABLE IF EXISTS approvals;
CREATE TABLE approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    ref_id      TEXT,
    title       TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);

-- messages = one row per Upwork chat room (the table existed since 001 and was unused).
ALTER TABLE messages ADD COLUMN topic TEXT;
ALTER TABLE messages ADD COLUMN awaiting TEXT;        -- 'you' | 'them' | NULL
ALTER TABLE messages ADD COLUMN last_from TEXT;
ALTER TABLE messages ADD COLUMN last_text TEXT;
ALTER TABLE messages ADD COLUMN last_at TEXT;         -- time of the last message (as Upwork gave it)
ALTER TABLE messages ADD COLUMN url TEXT;             -- http(s) link, or NULL
ALTER TABLE messages ADD COLUMN fetched_at TEXT;      -- UTC ISO, last time it was read
ALTER TABLE messages ADD COLUMN draft_reply TEXT;
ALTER TABLE messages ADD COLUMN draft_at TEXT;
ALTER TABLE invites ADD COLUMN draft_at TEXT;         -- invites.draft_reply exists since 001
