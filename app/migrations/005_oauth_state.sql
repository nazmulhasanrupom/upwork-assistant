-- OAuth login state (plan D1). Kept in the DB, keyed by `state`, not in the session cookie:
-- SameSite=Strict drops the cookie on the redirect back from Upwork. One use, 10 minutes max.

CREATE TABLE IF NOT EXISTS oauth_state (
    state      TEXT PRIMARY KEY,
    verifier   TEXT NOT NULL,       -- PKCE code_verifier
    created_at TEXT NOT NULL        -- ISO-8601 UTC
);
