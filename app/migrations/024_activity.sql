-- Plan F1: job activity filter. Label (HIDE, SUSPICIOUS, POTENTIAL, GOOD, NEUTRAL, UNKNOWN) and posted time.
ALTER TABLE jobs ADD COLUMN activity_label TEXT;
ALTER TABLE jobs ADD COLUMN posted_at TEXT;   -- UTC ISO from published_date or created_date
