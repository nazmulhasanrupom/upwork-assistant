-- Round 3 A1: proposal rules result on a draft, and the judge's eligibility answers.
-- rules_ok: 1 follows the rules, 0 breaks them, NULL not checked (old drafts).
ALTER TABLE drafts ADD COLUMN rules_ok INTEGER;
ALTER TABLE drafts ADD COLUMN rules_problems TEXT NOT NULL DEFAULT '[]';   -- JSON list of lines
-- NULL means the judge did not say (an old or partial judgement): never eligible.
ALTER TABLE judgements ADD COLUMN skills_match_all INTEGER;
ALTER TABLE judgements ADD COLUMN win_chance TEXT;        -- low | medium | high
ALTER TABLE judgements ADD COLUMN client_quality TEXT;    -- poor | ok | good
ALTER TABLE judgements ADD COLUMN eligibility_reasons TEXT NOT NULL DEFAULT '{}';   -- JSON
