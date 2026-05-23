-- Migration 009 — codebase scan (0.11.0).
-- Adds: code_symbols, code_relations, codebase_scans, scan_locks.
--
-- All four tables support the tree-sitter-backed `sage-memory
-- scan-codebase` capability (opt-in via the [codebase] pip extra).
-- See .sage/work/20260521-codebase-scan/spec.md for the full design.
--
-- Self-referential CASCADE on code_symbols.parent_id REQUIRES
-- `PRAGMA foreign_keys = ON`, which db._open() sets on every
-- connection. Future schema work must preserve that PRAGMA.

CREATE TABLE IF NOT EXISTS code_symbols (
    id             TEXT PRIMARY KEY,
    file_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind           TEXT NOT NULL,
    language       TEXT NOT NULL,
    signature      TEXT,
    line_start     INTEGER NOT NULL,
    line_end       INTEGER NOT NULL,
    parent_id      TEXT REFERENCES code_symbols(id) ON DELETE CASCADE,
    created_at     REAL NOT NULL,
    UNIQUE (file_memory_id, qualified_name, kind, line_start)
);

CREATE INDEX IF NOT EXISTS idx_code_symbols_name      ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_code_symbols_qualified ON code_symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_code_symbols_file      ON code_symbols(file_memory_id);
CREATE INDEX IF NOT EXISTS idx_code_symbols_lang_kind ON code_symbols(language, kind);

CREATE TABLE IF NOT EXISTS code_relations (
    id               TEXT PRIMARY KEY,
    source_symbol_id TEXT NOT NULL REFERENCES code_symbols(id) ON DELETE CASCADE,
    target_symbol_id TEXT REFERENCES code_symbols(id) ON DELETE CASCADE,
    target_name      TEXT NOT NULL,
    kind             TEXT NOT NULL,
    confidence       TEXT NOT NULL,
    line             INTEGER NOT NULL,
    column_start     INTEGER NOT NULL,
    created_at       REAL NOT NULL,
    UNIQUE (source_symbol_id, target_name, kind, line, column_start)
);

CREATE INDEX IF NOT EXISTS idx_code_relations_src  ON code_relations(source_symbol_id);
CREATE INDEX IF NOT EXISTS idx_code_relations_tgt  ON code_relations(target_symbol_id)
    WHERE target_symbol_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_code_relations_kind ON code_relations(kind);

CREATE TABLE IF NOT EXISTS codebase_scans (
    file_path      TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    file_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    language       TEXT NOT NULL,
    last_scanned   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codebase_scans_lang ON codebase_scans(language);

CREATE TABLE IF NOT EXISTS scan_locks (
    lock_name  TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL,
    started_at REAL NOT NULL
);
