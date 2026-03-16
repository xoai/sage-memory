-- sage-memory schema v1
-- Each database file IS the scope (project-local or global).
-- No scope column needed.

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL UNIQUE,
    embedded     INTEGER NOT NULL DEFAULT 0,

    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    accessed_at  REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0
);

-- FTS5: title=10, content=3, tags=1
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title, content, tags,
    content='memories',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- FTS sync triggers
CREATE TRIGGER IF NOT EXISTS trg_fts_ins AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, content, tags)
    VALUES (NEW.rowid, NEW.title, NEW.content, NEW.tags);
END;

CREATE TRIGGER IF NOT EXISTS trg_fts_del AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
    VALUES ('delete', OLD.rowid, OLD.title, OLD.content, OLD.tags);
END;

CREATE TRIGGER IF NOT EXISTS trg_fts_upd AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
    VALUES ('delete', OLD.rowid, OLD.title, OLD.content, OLD.tags);
    INSERT INTO memories_fts(rowid, title, content, tags)
    VALUES (NEW.rowid, NEW.title, NEW.content, NEW.tags);
END;

-- Vector index (384-dim)
CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    memory_id TEXT PRIMARY KEY,
    embedding float[384]
);

-- Fast lookups
CREATE INDEX IF NOT EXISTS idx_embedded ON memories(embedded) WHERE embedded = 0;
CREATE INDEX IF NOT EXISTS idx_updated ON memories(updated_at DESC);
