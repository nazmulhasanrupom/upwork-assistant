-- Phase X2: keyword rank, one row per local day and keyword (upserted).
CREATE TABLE IF NOT EXISTS rank_daily (
    date             TEXT NOT NULL,           -- local day, YYYY-MM-DD
    keyword          TEXT NOT NULL,
    position         INTEGER,                 -- 1-based place in the search, boosted (paid) rows counted; NULL = not found in the pages checked
    organic_position INTEGER,                 -- same, boosted rows skipped; NULL = not found
    pages_checked    INTEGER NOT NULL,
    total_seen       INTEGER NOT NULL,        -- freelancers looked at (the size of "the pages checked")
    created_at       TEXT NOT NULL,           -- UTC ISO
    PRIMARY KEY (date, keyword)
);
