-- Migration 007: worker_state singleton + extraction_queue NOT NULL relaxation
-- M5 T0 (per plan rev 2.1).
--
-- Part A: worker_state singleton table. Tracks daily-prune cadence
-- per ADR-001+003 (extraction_queue 30-day retention). One row only,
-- enforced by CHECK (id = 1). last_prune_at = NULL means "never
-- pruned" (first-run state).
--
-- Part B: extraction_queue.memory_id NOT NULL → nullable. Required
-- so dedup tasks (which have no memory_id) can be INSERTed by the
-- T3 dedup CLI + worker dedup task type. Uses SQLite's canonical
-- rebuild pattern (CREATE new → INSERT SELECT → DROP → RENAME)
-- because SQLite has no DROP NOT NULL.

CREATE TABLE IF NOT EXISTS worker_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_prune_at REAL
);

INSERT OR IGNORE INTO worker_state (id, last_prune_at) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS extraction_queue_new (
    id           TEXT PRIMARY KEY,
    memory_id    TEXT REFERENCES memories(id) ON DELETE CASCADE,
    task_type    TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    started_at   REAL,
    processed_at REAL
);

INSERT INTO extraction_queue_new
    SELECT id, memory_id, task_type, status, attempts,
           last_error, created_at, started_at, processed_at
    FROM extraction_queue;

DROP TABLE extraction_queue;

ALTER TABLE extraction_queue_new RENAME TO extraction_queue;

CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON extraction_queue(status, created_at)
    WHERE status IN ('pending', 'running');
