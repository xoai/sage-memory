-- Migration 003: Chunks (M1 / ADR-001)
-- Chunked storage for long memories. Each chunk is a row with its own
-- FTS5 + vec0 index. Chunking writes happen in M2 (chunker.py); this
-- migration just creates the empty tables and triggers.

CREATE TABLE IF NOT EXISTS chunks (
    id           TEXT PRIMARY KEY,
    memory_id    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    content      TEXT NOT NULL,
    byte_start   INTEGER NOT NULL,
    byte_end     INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE (memory_id, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id   TEXT PRIMARY KEY,
    embedding  float[384]
);

-- FTS sync triggers (same pattern as memories_fts in 001_initial.sql)
CREATE TRIGGER IF NOT EXISTS trg_chunks_fts_ins AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content)
    VALUES (NEW.rowid, NEW.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_chunks_fts_del AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content)
    VALUES ('delete', OLD.rowid, OLD.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_chunks_fts_upd AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content)
    VALUES ('delete', OLD.rowid, OLD.content);
    INSERT INTO chunks_fts(rowid, content)
    VALUES (NEW.rowid, NEW.content);
END;

CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(memory_id);
