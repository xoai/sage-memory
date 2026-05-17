-- Migration 006: Extraction queue (M1 / ADR-001, ADR-003)
-- Background work queue consumed by the worker in M3a. Empty until
-- then. status='pending'|'running'|'done'|'failed'; task_type='extract'
-- |'dedup'|'reembed'. started_at set when status→'running'; the
-- worker's startup-recovery query resets stale 'running' rows back to
-- 'pending' based on started_at < unixepoch()-300.

CREATE TABLE IF NOT EXISTS extraction_queue (
    id           TEXT PRIMARY KEY,
    memory_id    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    task_type    TEXT NOT NULL,    -- 'extract' | 'dedup' | 'reembed'
    status       TEXT NOT NULL,    -- 'pending' | 'running' | 'done' | 'failed'
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    started_at   REAL,              -- set when status -> 'running'
    processed_at REAL                -- set when status -> 'done' | 'failed'
);

CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON extraction_queue(status, created_at)
    WHERE status IN ('pending', 'running');
