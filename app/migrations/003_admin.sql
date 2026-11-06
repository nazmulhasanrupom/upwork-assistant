-- Panel login (SPEC 10.4). One admin only. The TOTP secret is encrypted with a key
-- derived from SECRET_KEY. Lockout state lives here so it survives a restart.

CREATE TABLE IF NOT EXISTS admin (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    username        TEXT NOT NULL,
    password_hash   TEXT NOT NULL,      -- argon2
    totp_secret_enc TEXT NOT NULL,      -- Fernet token
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT                    -- ISO-8601 UTC, NULL when not locked
);
INSERT OR IGNORE INTO auth_state(id, failed_count, locked_until) VALUES (1, 0, NULL);
