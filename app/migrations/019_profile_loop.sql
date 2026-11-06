-- Phase O3: the profile test loop.
-- profile_previews: the confirm step of a profile preview. `status` keeps its two values; the
-- confirm result has its own columns (confirm_status: dry_run_blocked | confirmed | failed).
ALTER TABLE profile_previews ADD COLUMN before_version_id INTEGER;  -- the copy saved right before the preview
ALTER TABLE profile_previews ADD COLUMN confirmed_at TEXT;          -- set by the single atomic claim
ALTER TABLE profile_previews ADD COLUMN confirm_status TEXT;
ALTER TABLE profile_previews ADD COLUMN confirm_result_json TEXT;
-- experiments: what was changed, so a bad result can be rolled back.
ALTER TABLE experiments ADD COLUMN approval_id INTEGER;
ALTER TABLE experiments ADD COLUMN version_id INTEGER;              -- profile copy from before the change
ALTER TABLE experiments ADD COLUMN field TEXT;
