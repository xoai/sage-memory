-- 012 — Code-graph query indexes (P2-1, SM-CAP-01).
--
-- `path`/`affected`/`hubs` resolve symbols by name and render
-- file:line. Traversal rides the existing code_relations indexes
-- (source, target-partial, target_name-partial from 009/010); these
-- cover the symbol-side lookups.
--
-- Append-only per the migrations contract: never edit 001–011.

CREATE INDEX IF NOT EXISTS idx_code_symbols_name
    ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_code_symbols_file
    ON code_symbols(file_memory_id);
