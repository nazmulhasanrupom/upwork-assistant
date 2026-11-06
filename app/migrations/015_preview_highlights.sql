-- Phase H2: the highlights (portfolio, certificates, past jobs) read for a preview, so the job
-- page can ask the user which to include. JSON list; NULL = not read (or the read failed).
ALTER TABLE previews ADD COLUMN highlights_json TEXT;
