-- Migration 005: Embedding metadata + corpus dim lock (M1 / ADR-001, ADR-005)
-- Per-vector provenance: (model_name, model_version, dim, created_at).
-- Split into memory_embedding_meta + chunk_embedding_meta with real
-- FKs (no polymorphic id antipattern). corpus_meta locks the vec
-- dim of the corpus to whatever the first writer chose (default 384).

CREATE TABLE IF NOT EXISTS memory_embedding_meta (
    memory_id      TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model_name     TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_embedding_meta (
    chunk_id       TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model_name     TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mem_emb_model
    ON memory_embedding_meta(model_name, model_version);
CREATE INDEX IF NOT EXISTS idx_chunk_emb_model
    ON chunk_embedding_meta(model_name, model_version);

-- Default corpus vec_dim is 384 (matches LocalEmbedder + FastEmbedder).
INSERT OR IGNORE INTO corpus_meta (key, value) VALUES ('vec_dim', '384');

-- Backfill: every existing `memories.embedded=1` row gets a legacy meta
-- row. memories.created_at is NOT NULL (per 001_initial.sql:13) so no
-- COALESCE needed. embedded=0 rows intentionally get NO meta row —
-- they remain stale per ADR-005 §Staleness handling.
INSERT OR IGNORE INTO memory_embedding_meta
    (memory_id, model_name, model_version, dim, created_at)
    SELECT id, 'legacy', '0', 384, created_at
    FROM memories
    WHERE embedded = 1;
