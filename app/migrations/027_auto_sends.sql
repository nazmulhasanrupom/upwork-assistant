-- Round 3 A2: one row per auto-send countdown. Status changes only through atomic
-- UPDATE ... WHERE status = '<expected>' (see app/auto_send.py), so Stop wins if it lands first.
CREATE TABLE IF NOT EXISTS auto_sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    preview_id  INTEGER,                  -- previews.id of the proposal the countdown will send
    created     TEXT NOT NULL,            -- UTC ISO
    deadline    TEXT NOT NULL,            -- created + auto_send.wait_minutes (UTC ISO, the truth)
    status      TEXT NOT NULL DEFAULT 'waiting'
        CHECK (status IN ('waiting', 'approved', 'stopped', 'sending', 'sent', 'failed', 'cancelled')),
    decided_by  TEXT CHECK (decided_by IS NULL OR decided_by IN ('user', 'system')),
    reason      TEXT NOT NULL DEFAULT '',
    decided_at  TEXT,
    sent_at     TEXT,
    error       TEXT,
    click_id    TEXT                      -- the click (user or system consent) of the confirm step
);
-- One live countdown per job; a stopped, sent, failed or cancelled row frees the slot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_sends_active
    ON auto_sends(job_id) WHERE status IN ('waiting', 'approved', 'sending');
CREATE INDEX IF NOT EXISTS idx_auto_sends_status ON auto_sends(status, deadline);
