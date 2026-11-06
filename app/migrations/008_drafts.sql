-- Phase G: what a draft needs besides the text (suggested rate, flags, when, which run).
ALTER TABLE drafts ADD COLUMN suggested_rate REAL;
ALTER TABLE drafts ADD COLUMN flags TEXT NOT NULL DEFAULT '[]';   -- JSON list of short notes
ALTER TABLE drafts ADD COLUMN created_at TEXT;
ALTER TABLE drafts ADD COLUMN run_id TEXT;
