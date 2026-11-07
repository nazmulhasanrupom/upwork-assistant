-- Plan C4: many chats. Each message belongs to one conversation; a reply keeps its thinking steps.
CREATE TABLE IF NOT EXISTS chat_conversations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title   TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,   -- UTC ISO
    updated TEXT NOT NULL    -- UTC ISO, the last message
);
ALTER TABLE chat_messages ADD COLUMN conversation_id INTEGER REFERENCES chat_conversations(id) ON DELETE CASCADE;
ALTER TABLE chat_messages ADD COLUMN thinking TEXT NOT NULL DEFAULT '';   -- steps, one per line
ALTER TABLE chat_messages ADD COLUMN think_seconds INTEGER;               -- NULL: no thinking row
-- Messages from before this change become one chat.
INSERT INTO chat_conversations(title, created, updated)
    SELECT 'Earlier chat', MIN(created), MAX(created) FROM chat_messages HAVING COUNT(*) > 0;
UPDATE chat_messages SET conversation_id = (SELECT MIN(id) FROM chat_conversations)
    WHERE conversation_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_conversations_updated ON chat_conversations(updated);
