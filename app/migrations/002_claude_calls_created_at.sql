-- The monthly cap (SPEC 7.3 rule 10) needs to know when each call happened.
ALTER TABLE claude_calls ADD COLUMN created_at TEXT;
CREATE INDEX IF NOT EXISTS idx_claude_calls_created ON claude_calls(created_at);
