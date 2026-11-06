-- Phase X3: invitations seen on the Upwork dashboard, for the instant and the reminder email.
-- The table exists since 001 and nothing used it yet, so it is extended, not replaced.
-- invite_id = the invitation id (invitation_uid) from Upwork.
ALTER TABLE invites ADD COLUMN job_title TEXT;
ALTER TABLE invites ADD COLUMN first_seen TEXT;       -- when we first saw it pending (UTC ISO)
ALTER TABLE invites ADD COLUMN answered_at TEXT;      -- NULL = still waiting for my answer
ALTER TABLE invites ADD COLUMN alerted_at TEXT;       -- instant email sent
ALTER TABLE invites ADD COLUMN second_alert_at TEXT;  -- reminder email sent
CREATE INDEX IF NOT EXISTS idx_invites_open ON invites(answered_at, first_seen);
