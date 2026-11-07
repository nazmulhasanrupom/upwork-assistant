-- 2-step login is optional (round 4, E2): totp_secret_enc may be NULL (= off).
-- SQLite cannot drop NOT NULL, so the table is rebuilt and the row is kept.

CREATE TABLE admin_new (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    username        TEXT NOT NULL,
    password_hash   TEXT NOT NULL,      -- argon2
    totp_secret_enc TEXT,               -- Fernet token; NULL when 2-step login is off
    created_at      TEXT NOT NULL
);
INSERT INTO admin_new(id, username, password_hash, totp_secret_enc, created_at)
    SELECT id, username, password_hash, totp_secret_enc, created_at FROM admin;
DROP TABLE admin;
ALTER TABLE admin_new RENAME TO admin;
