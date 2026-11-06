-- L1: full text search over daily notes and runs (SPEC2 5.1). kind = 'note' | 'run'.
CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5(
    kind UNINDEXED, ref UNINDEXED, body, tokenize = 'unicode61'
);

-- Runs are written by several tasks with plain SQL, so triggers keep the index in step.
CREATE TRIGGER IF NOT EXISTS search_runs_ai AFTER INSERT ON runs BEGIN
    INSERT INTO search_idx(kind, ref, body)
    VALUES ('run', new.run_id, new.task || ' ' || new.status || ' ' || COALESCE(new.error, ''));
END;

CREATE TRIGGER IF NOT EXISTS search_runs_au AFTER UPDATE ON runs BEGIN
    DELETE FROM search_idx WHERE kind = 'run' AND ref = old.run_id;
    INSERT INTO search_idx(kind, ref, body)
    VALUES ('run', new.run_id, new.task || ' ' || new.status || ' ' || COALESCE(new.error, ''));
END;

CREATE TRIGGER IF NOT EXISTS search_runs_ad AFTER DELETE ON runs BEGIN
    DELETE FROM search_idx WHERE kind = 'run' AND ref = old.run_id;
END;

INSERT INTO search_idx(kind, ref, body)
SELECT 'run', run_id, task || ' ' || status || ' ' || COALESCE(error, '') FROM runs;
