-- Which job a Claude call belongs to (judge, write, fetch, ...). Old rows stay NULL.
ALTER TABLE claude_calls ADD COLUMN task TEXT;
