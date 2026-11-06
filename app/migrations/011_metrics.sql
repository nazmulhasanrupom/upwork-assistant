-- Phase X1: daily metrics snapshot and the weekly profile views the owner types in.
CREATE TABLE IF NOT EXISTS metrics_daily (
    date                 TEXT PRIMARY KEY,        -- local day, YYYY-MM-DD (one row per day, upserted)
    connects             INTEGER,                 -- Connects balance
    jss                  REAL,                    -- Job Success Score, NULL when Upwork gave none
    invites_open         INTEGER,                 -- invitations waiting for an answer
    invites_new          INTEGER,                 -- invitations first seen today
    offers_open          INTEGER,
    messages_awaiting_me INTEGER,                 -- message rooms waiting for my reply
    active_contracts     INTEGER,
    proposals_sent       INTEGER,                 -- sent in the last metrics.funnel_days days
    proposals_viewed     INTEGER,                 -- of those, seen by the client
    interviews           INTEGER,
    hires                INTEGER,
    raw_json             TEXT NOT NULL DEFAULT '{}',  -- small summary, no client text
    created_at           TEXT NOT NULL            -- first snapshot of the day (UTC ISO)
);

CREATE TABLE IF NOT EXISTS metrics_weekly (
    week_start    TEXT PRIMARY KEY,               -- Monday of the week, local, YYYY-MM-DD
    profile_views INTEGER NOT NULL,
    entered_at    TEXT NOT NULL                   -- UTC ISO
);
