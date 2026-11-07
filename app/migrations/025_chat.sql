-- Plan C1: the Chat page (a read-only helper). One row per message.
CREATE TABLE IF NOT EXISTS chat_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    role      TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text      TEXT NOT NULL,
    created   TEXT NOT NULL,   -- UTC ISO
    action_id TEXT,            -- the worker action that made the reply
    cost_usd  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_id ON chat_messages(id);
