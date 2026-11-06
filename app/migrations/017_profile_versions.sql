-- Phase O1: saved copies of my Upwork profile (`get_profile get`), and the preview rows that a
-- roll back creates. The raw answer is also kept as a file (profile_versions/<ts>.json).
CREATE TABLE IF NOT EXISTS profile_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,                 -- UTC ISO
    reason       TEXT NOT NULL,                 -- daily | manual | before_change | rollback ...
    content_hash TEXT NOT NULL,                 -- hash of the fields below (equal = not saved again)
    file         TEXT,                          -- file name inside the profile_versions folder
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    skills_json  TEXT NOT NULL DEFAULT '[]',
    rate         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_profile_versions_created ON profile_versions(created_at);

-- One row per `update_profile` PREVIEW (never confirmed by the roll back itself).
CREATE TABLE IF NOT EXISTS profile_previews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id   INTEGER,                      -- the approvals item that asked for it
    version_id    INTEGER,                      -- the version being restored
    field         TEXT NOT NULL,                -- title | description | skills
    action        TEXT NOT NULL,                -- update_title | update_overview | set_skills
    preview_id    TEXT,                         -- from Upwork; NULL when the preview failed
    request_json  TEXT NOT NULL,
    response_json TEXT,
    status        TEXT NOT NULL DEFAULT 'previewed' CHECK (status IN ('previewed', 'failed')),
    message       TEXT,
    created_at    TEXT NOT NULL,
    run_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_profile_previews_approval ON profile_previews(approval_id);
