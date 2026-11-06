-- One-time setup code (plan D0). Only the keyed hash is stored. A new code is made on
-- every start while no admin exists, and the row is deleted when the admin is created.

CREATE TABLE IF NOT EXISTS setup_code (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
